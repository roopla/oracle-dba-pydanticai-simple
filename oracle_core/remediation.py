"""Allowlisted remediation actions.

Save as oracle_core/remediation.py.

Design
------
The model never writes SQL that executes. It selects an action by name and
supplies parameters; the SQL that runs comes from a template in this file,
reviewed once, offline. Parameters are validated here and bound, never
interpolated.

    plan_remediation(action, params)     - safe. Returns what WOULD run.
                                           Exposed to the agent over MCP.
    execute_remediation(action, params)  - runs it. NOT exposed over MCP;
                                           called only by the chat UI after
                                           an explicit human approval.

Actions
-------
restart_redo_apply
    Gentle. Restarts MRP0 in place. Works when no recovery session is
    wedged. Try this first.

restart_standby_instance
    SHUTDOWN ABORT, STARTUP MOUNT, then start apply. The only thing that
    clears a recovery session Oracle still considers active while
    v$managed_standby shows no MRP0 - the state behind ORA-16448. Bigger
    hammer, marked not reversible, and guarded so it can never run against
    anything but a mounted physical standby.

    python-oracledb in thin mode cannot shut down or start an instance
    (DPY-3001), so those two steps run as SQL*Plus inside the standby's
    Docker container over SSH (STANDBY_SSH_* settings). The scripts are
    fixed text; nothing from the model reaches the remote command.

Both poll for MRP0 after running rather than reading state once, because
MRP0 startup can take longer than the statement's own return.

enable_tablespace_autoextend
    Turns on AUTOEXTEND (or raises MAXSIZE) for a PDB tablespace's
    datafiles on the primary. Reversible. Try this first for a full
    tablespace.

add_tablespace_datafile
    Adds a datafile next to the tablespace's existing ones. Not reversible.

Both tablespace actions derive their SQL from the dictionary (file names,
paths), so they set pin_statements: execute_remediation re-plans and
refuses unless the statements are exactly the ones the human approved.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import oracledb
import paramiko

from oracle_core.db import execute_statements, query
from oracle_core.queries import validate_pdb_name


# Hard ceiling per database round trip, in milliseconds. Without this a
# wedged "MRP inactivation" wait blocks the caller indefinitely.
CALL_TIMEOUT_MS = 90_000

CONNECT_TIMEOUT_SECONDS = 10

# How long to wait for MRP0 to appear after starting apply.
MRP_POLL_SECONDS = 120
MRP_POLL_INTERVAL = 5

# Startup after SHUTDOWN ABORT needs its own budget.
STARTUP_POLL_SECONDS = 180

# SSH to the Docker host that runs the standby container.
SSH_CONNECT_TIMEOUT_SECONDS = 10
# SHUTDOWN ABORT is seconds; STARTUP MOUNT on a small lab SGA is well
# under a minute. Generous, but bounded.
CONTAINER_SQLPLUS_TIMEOUT_SECONDS = 180

_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class ActionPlan:
    """What an action would do, with nothing executed yet."""

    action: str
    description: str
    statements: list[str]
    impact: str
    target: str
    reversible: bool
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "description": self.description,
            "statements": self.statements,
            "impact": self.impact,
            "target": self.target,
            "reversible": self.reversible,
            "params": self.params,
        }


@dataclass(frozen=True)
class RemediationAction:
    name: str
    description: str
    reversible: bool
    planner: Callable[[dict[str, Any]], ActionPlan]
    executor: Callable[[dict[str, Any]], dict[str, Any]]
    # Parameter guide for the agent, returned by list_actions().
    params_help: str = "Takes no parameters."
    # When True, execution re-plans and refuses unless the statements are
    # exactly those the human approved. Right for actions whose SQL is
    # derived from database state (file names, paths); wrong for actions
    # that deliberately adapt to state at run time (restart_redo_apply
    # decides then whether a CANCEL is needed).
    pin_statements: bool = False


# --------------------------------------------------------------------------
# Standby connection helpers (SYSDBA, mounted database)
# --------------------------------------------------------------------------


def _standby_credentials() -> tuple[str, str, str]:
    dsn = (os.environ.get("ORACLE_STANDBY_DSN") or "").strip()
    user = (os.environ.get("ORACLE_STANDBY_USER") or "sys").strip()
    password = os.environ.get("ORACLE_STANDBY_PASSWORD") or ""

    if not dsn:
        raise RuntimeError(
            "ORACLE_STANDBY_DSN is not set, so the standby cannot be reached."
        )

    if not password:
        raise RuntimeError(
            "ORACLE_STANDBY_PASSWORD is not set. A mounted standby requires "
            "a SYSDBA connection."
        )

    return dsn, user, password


def _standby_connection():
    """Open a bounded SYSDBA connection to the standby."""
    dsn, user, password = _standby_credentials()

    connection = oracledb.connect(
        user=user,
        password=password,
        dsn=dsn,
        mode=oracledb.AUTH_MODE_SYSDBA,
        tcp_connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )
    connection.call_timeout = CALL_TIMEOUT_MS

    return connection


# --------------------------------------------------------------------------
# Standby instance control (SQL*Plus in the container, over SSH)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StandbyHost:
    """Where the standby container runs, and how to reach it."""

    host: str
    port: int
    user: str
    password: str | None
    key_file: str | None
    container: str

    def describe(self) -> str:
        return f"container {self.container} on {self.user}@{self.host}"


def _standby_host() -> StandbyHost:
    """Read STANDBY_SSH_* settings, refusing anything incomplete."""
    host = (os.environ.get("STANDBY_SSH_HOST") or "").strip()
    user = (os.environ.get("STANDBY_SSH_USER") or "").strip()
    password = os.environ.get("STANDBY_SSH_PASSWORD") or None
    key_file = (os.environ.get("STANDBY_SSH_KEY_FILE") or "").strip() or None
    container = (os.environ.get("STANDBY_CONTAINER") or "").strip()
    port_text = (os.environ.get("STANDBY_SSH_PORT") or "22").strip()

    missing = [
        name
        for name, value in (
            ("STANDBY_SSH_HOST", host),
            ("STANDBY_SSH_USER", user),
            ("STANDBY_CONTAINER", container),
        )
        if not value
    ]
    if not (password or key_file):
        missing.append("STANDBY_SSH_PASSWORD or STANDBY_SSH_KEY_FILE")

    if missing:
        raise RuntimeError(
            "Standby instance control is not configured. Missing: "
            + ", ".join(missing)
        )

    if not _CONTAINER_NAME.match(container):
        raise RuntimeError(f"STANDBY_CONTAINER {container!r} is not a valid name")

    if not port_text.isdigit():
        raise RuntimeError(f"STANDBY_SSH_PORT {port_text!r} is not a number")

    return StandbyHost(
        host=host,
        port=int(port_text),
        user=user,
        password=password,
        key_file=os.path.expanduser(key_file) if key_file else None,
        container=container,
    )


def _container_sqlplus(target: StandbyHost, script: str) -> str:
    """Run a fixed SQL*Plus script as SYSDBA inside the standby container.

    The host key must already be in known_hosts: an unknown or changed
    key is refused rather than trusted, so a spoofed host cannot collect
    the credentials. Returns combined stdout and stderr.
    """
    remote = (
        f"docker exec -i -u oracle {shlex.quote(target.container)} "
        + "bash -c "
        + shlex.quote('"$ORACLE_HOME/bin/sqlplus" -s / as sysdba')
    )

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    try:
        client.connect(
            hostname=target.host,
            port=target.port,
            username=target.user,
            password=target.password,
            key_filename=target.key_file,
            timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            allow_agent=False,
            look_for_keys=False,
        )
        stdin, stdout, stderr = client.exec_command(
            remote, timeout=CONTAINER_SQLPLUS_TIMEOUT_SECONDS
        )
        stdin.write(script.rstrip() + "\nEXIT;\n")
        stdin.channel.shutdown_write()

        output = stdout.read().decode(errors="replace")
        output += stderr.read().decode(errors="replace")
        exit_status = stdout.channel.recv_exit_status()
    finally:
        client.close()

    if exit_status != 0:
        raise RuntimeError(
            f"docker exec exited with status {exit_status}: {output.strip()[:500]}"
        )

    return output


def _instance_step(
    target: StandbyHost,
    statement: str,
    success_marker: str,
) -> dict[str, Any]:
    """Run one instance-level statement and judge it by SQL*Plus output."""
    try:
        output = _container_sqlplus(target, statement + ";")
    except Exception as exc:  # noqa: BLE001
        return {
            "statement": statement,
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }

    errors = [line.strip() for line in output.splitlines() if "ORA-" in line]

    if success_marker in output and not errors:
        return {"statement": statement, "status": "OK"}

    return {
        "statement": statement,
        "status": "ERROR",
        "error": "; ".join(errors) or f"Unexpected output: {output.strip()[:300]}",
    }


def _run_on_standby(statements: list[str]) -> list[dict[str, Any]]:
    """Execute statements on the standby, reporting each outcome.

    Errors are captured per statement rather than raised: several Data
    Guard statements fail harmlessly, and ORA-16448 in particular does
    not always mean the operation failed.
    """
    results: list[dict[str, Any]] = []

    with _standby_connection() as connection:
        with connection.cursor() as cursor:
            for statement in statements:
                try:
                    cursor.execute(statement)
                    results.append({"statement": statement, "status": "OK"})
                except Exception as exc:  # noqa: BLE001
                    results.append(
                        {
                            "statement": statement,
                            "status": "ERROR",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

    return results


def _standby_apply_state() -> dict[str, Any]:
    """Read whether redo apply is currently running."""
    with _standby_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT process, status, sequence# FROM v$managed_standby "
                "WHERE process = 'MRP0'"
            )
            mrp = cursor.fetchall()

            cursor.execute(
                "SELECT recovery_mode FROM v$archive_dest_status WHERE dest_id = 1"
            )
            mode_row = cursor.fetchone()

            cursor.execute(
                "SELECT database_role, open_mode FROM v$database"
            )
            db_row = cursor.fetchone()

    return {
        "mrp0_running": bool(mrp),
        "mrp0": (
            {"process": mrp[0][0], "status": mrp[0][1], "sequence": mrp[0][2]}
            if mrp
            else None
        ),
        "recovery_mode": mode_row[0] if mode_row else None,
        "database_role": db_row[0] if db_row else None,
        "open_mode": db_row[1] if db_row else None,
    }


def _wait_for_mrp0(timeout_seconds: int = MRP_POLL_SECONDS) -> dict[str, Any]:
    """Poll until MRP0 appears, or the budget runs out.

    A single post-statement snapshot reports failure for a slow but
    successful start, which is misleading in an audit trail.
    """
    deadline = time.monotonic() + timeout_seconds
    state: dict[str, Any] = {}

    while True:
        try:
            state = _standby_apply_state()

            if state.get("mrp0_running"):
                return state
        except Exception as exc:  # noqa: BLE001
            state = {"error": f"{type(exc).__name__}: {exc}"}

        if time.monotonic() >= deadline:
            return state

        time.sleep(MRP_POLL_INTERVAL)


def _wait_for_mount(timeout_seconds: int = STARTUP_POLL_SECONDS) -> dict[str, Any]:
    """Poll until the instance answers again after a restart."""
    deadline = time.monotonic() + timeout_seconds
    state: dict[str, Any] = {}

    while True:
        try:
            state = _standby_apply_state()

            if state.get("open_mode"):
                return state
        except Exception as exc:  # noqa: BLE001
            state = {"error": f"{type(exc).__name__}: {exc}"}

        if time.monotonic() >= deadline:
            return state

        time.sleep(MRP_POLL_INTERVAL)


def _require_physical_standby(state: dict[str, Any]) -> None:
    """Refuse to touch anything that is not a mounted physical standby."""
    role = state.get("database_role")
    open_mode = state.get("open_mode")

    if role != "PHYSICAL STANDBY":
        raise RuntimeError(
            f"Refusing to run: target role is {role!r}, not PHYSICAL STANDBY."
        )

    if open_mode not in ("MOUNTED", "READ ONLY WITH APPLY", "READ ONLY"):
        raise RuntimeError(
            f"Refusing to run: unexpected open_mode {open_mode!r}."
        )


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------

_CANCEL_APPLY = "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL"

# USING CURRENT LOGFILE is deprecated in 19c and implied when standby
# redo logs exist.
_START_APPLY = (
    "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT FROM SESSION"
)

_REGISTER = "ALTER SYSTEM REGISTER"


# --------------------------------------------------------------------------
# Action: restart_redo_apply
# --------------------------------------------------------------------------


def _restart_apply_statements(mrp0_running: bool) -> list[str]:
    """Cancel only when apply is genuinely running.

    Issuing CANCEL when MRP0 is absent either returns ORA-16136 or blocks
    on an "MRP inactivation" wait.
    """
    if mrp0_running:
        return [_CANCEL_APPLY, _START_APPLY]

    return [_START_APPLY]


def _plan_restart_redo_apply(params: dict[str, Any]) -> ActionPlan:
    if params:
        raise ValueError("restart_redo_apply takes no parameters")

    dsn, _, _ = _standby_credentials()

    try:
        state = _standby_apply_state()
        mrp0_running = bool(state.get("mrp0_running"))
        state_note = (
            "Redo apply is currently running, so it will be cancelled first."
            if mrp0_running
            else "Redo apply is not currently running, so no cancel is needed."
        )
    except Exception as exc:  # noqa: BLE001
        mrp0_running = True
        state_note = (
            "Could not read the standby's current state "
            f"({type(exc).__name__}), so both statements are shown."
        )

    return ActionPlan(
        action="restart_redo_apply",
        description="Restart managed redo apply (MRP0) on the physical standby.",
        statements=_restart_apply_statements(mrp0_running),
        impact=(
            f"{state_note} Starts redo apply in the background, then waits up "
            f"to {MRP_POLL_SECONDS} seconds for MRP0 to appear. No data is "
            "lost: the primary retains all redo and the standby catches up on "
            "any gap. The instance is not restarted and no primary-side change "
            "is made. If a previous recovery session is wedged this fails with "
            "ORA-16448, and restart_standby_instance is then the fix."
        ),
        target=f"standby {dsn}",
        reversible=True,
        params={},
    )


def _execute_restart_redo_apply(params: dict[str, Any]) -> dict[str, Any]:
    if params:
        raise ValueError("restart_redo_apply takes no parameters")

    # Read state fresh rather than trusting the plan, which passed through
    # the model on its way to the approval card.
    before = _standby_apply_state()
    _require_physical_standby(before)

    statements = _restart_apply_statements(bool(before.get("mrp0_running")))
    statement_results = _run_on_standby(statements)

    after = _wait_for_mrp0()
    succeeded = bool(after.get("mrp0_running"))

    result: dict[str, Any] = {
        "action": "restart_redo_apply",
        "before": before,
        "statements": statement_results,
        "after": after,
        "succeeded": succeeded,
    }

    if not succeeded:
        wedged = any(
            "ORA-16448" in str(r.get("error", "")) for r in statement_results
        )

        result["note"] = (
            (
                "ORA-16448 means Oracle still considers a recovery session "
                "active even though MRP0 is absent. Only an instance restart "
                "clears this. Propose restart_standby_instance."
            )
            if wedged
            else (
                f"MRP0 did not appear within {MRP_POLL_SECONDS} seconds. "
                "Check the standby alert log before escalating."
            )
        )
        result["suggested_next_action"] = "restart_standby_instance"

    return result


# --------------------------------------------------------------------------
# Action: restart_standby_instance
# --------------------------------------------------------------------------


def _plan_restart_standby_instance(params: dict[str, Any]) -> ActionPlan:
    if params:
        raise ValueError("restart_standby_instance takes no parameters")

    dsn, _, _ = _standby_credentials()

    # Refuse to plan what cannot be executed, so the agent is never able
    # to put an unrunnable action in front of a human.
    try:
        target = _standby_host()
    except RuntimeError as exc:
        raise ValueError(f"restart_standby_instance is unavailable: {exc}") from exc

    return ActionPlan(
        action="restart_standby_instance",
        description=(
            "Bounce the standby instance and restart redo apply. Use only "
            "when restart_redo_apply has failed with ORA-16448."
        ),
        statements=[
            "SHUTDOWN ABORT",
            "STARTUP MOUNT",
            _START_APPLY,
            _REGISTER,
        ],
        impact=(
            "Aborts and restarts the standby instance, then starts redo "
            "apply. THE STANDBY IS UNAVAILABLE FOR ROUGHLY ONE TO THREE "
            "MINUTES. No data is lost - the primary retains all redo and "
            "the standby catches up on any gap - but during the restart "
            "there is no standby to fail over to. Redo transport to this "
            "destination will error until it is back, and the primary's "
            "log_archive_dest_2 may need re-enabling afterwards. Nothing "
            "on the primary is changed. This action cannot be undone once "
            "started. SHUTDOWN ABORT and STARTUP MOUNT run through SQL*Plus "
            f"in {target.describe()}."
        ),
        target=f"standby {dsn}",
        reversible=False,
        params={},
    )


def _execute_restart_standby_instance(params: dict[str, Any]) -> dict[str, Any]:
    if params:
        raise ValueError("restart_standby_instance takes no parameters")

    target = _standby_host()
    before = _standby_apply_state()
    _require_physical_standby(before)

    statement_results: list[dict[str, Any]] = []

    # SHUTDOWN ABORT and STARTUP are instance operations that thin mode
    # cannot perform (DPY-3001), so they run as SQL*Plus in the container.
    shutdown = _instance_step(target, "SHUTDOWN ABORT", "ORACLE instance shut down.")
    statement_results.append(shutdown)

    if shutdown["status"] != "OK":
        return {
            "action": "restart_standby_instance",
            "before": before,
            "statements": statement_results,
            "after": before,
            "succeeded": False,
            "note": (
                "The instance was not shut down, so nothing else was "
                "attempted. The standby should be unaffected."
            ),
        }

    startup = _instance_step(target, "STARTUP MOUNT", "Database mounted.")
    statement_results.append(startup)

    if startup["status"] != "OK":
        return {
            "action": "restart_standby_instance",
            "before": before,
            "statements": statement_results,
            "after": {},
            "succeeded": False,
            "note": (
                "The instance was aborted but did not come back up. THE "
                "STANDBY IS DOWN. Start it manually inside the container: "
                "STARTUP MOUNT, then start redo apply."
            ),
        }

    mounted = _wait_for_mount()

    if not mounted.get("open_mode"):
        return {
            "action": "restart_standby_instance",
            "before": before,
            "statements": statement_results,
            "after": mounted,
            "succeeded": False,
            "note": (
                "The instance did not answer within "
                f"{STARTUP_POLL_SECONDS} seconds. Check it manually."
            ),
        }

    statement_results.extend(_run_on_standby([_START_APPLY, _REGISTER]))

    after = _wait_for_mrp0()
    succeeded = bool(after.get("mrp0_running"))

    result: dict[str, Any] = {
        "action": "restart_standby_instance",
        "before": before,
        "statements": statement_results,
        "after": after,
        "succeeded": succeeded,
    }

    if not succeeded:
        result["note"] = (
            "The instance restarted but MRP0 did not appear within "
            f"{MRP_POLL_SECONDS} seconds. The standby is mounted and "
            "receiving redo but not applying it. Check the alert log."
        )

    return result


# --------------------------------------------------------------------------
# Tablespace capacity (primary, one PDB)
# --------------------------------------------------------------------------
#
# DDL cannot take bind variables, so identifiers and file names are put into
# the SQL text. Neither comes from the model: the tablespace name must match
# a strict pattern AND exist in the dictionary, and every file name or path
# is read from cdb_data_files. Sizes are integers inside hard bounds.

_TABLESPACE_NAME = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
_MB = 1024 * 1024

# A smallfile datafile holds at most 4,194,303 blocks.
_SMALLFILE_MAX_BLOCKS = 4_194_303

_NEXT_MB_RANGE = (1, 1024)
_SIZE_MB_RANGE = (16, 32767)
_DEFAULT_NEXT_MB = 64
_DEFAULT_MAX_MB = 2048
_DEFAULT_SIZE_MB = 100

_TABLESPACE_PARAMS_HELP = (
    "pdb_name (required, e.g. ORCLPDB1; validated against the live PDB "
    "list), tablespace_name (required; must be an existing PERMANENT "
    "tablespace in that PDB), next_mb (optional autoextend increment, "
    f"{_NEXT_MB_RANGE[0]}-{_NEXT_MB_RANGE[1]}, default {_DEFAULT_NEXT_MB}), "
    f"max_mb (optional MAXSIZE per datafile, default {_DEFAULT_MAX_MB}, "
    "capped at the smallfile limit)"
)


def _sql_string(value: str) -> str:
    """Quote a dictionary-sourced value as an Oracle string literal."""
    return "'" + value.replace("'", "''") + "'"


def _int_param(
    params: dict[str, Any],
    key: str,
    default: int,
    low: int,
    high: int,
) -> int:
    """Read an integer parameter, refusing anything outside [low, high]."""
    raw = params.get(key, default)

    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError(f"{key} must be a whole number of MB")

    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a whole number of MB") from exc

    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high} MB")

    return value


def _tablespace_target(params: dict[str, Any]) -> tuple[str, str]:
    """Validate and normalise pdb_name and tablespace_name."""
    pdb_name = validate_pdb_name(str(params.get("pdb_name") or ""))
    tablespace = str(params.get("tablespace_name") or "").strip().upper()

    if not _TABLESPACE_NAME.match(tablespace):
        raise ValueError(f"{tablespace!r} is not a valid tablespace name")

    return pdb_name, tablespace


def _tablespace_files(pdb_name: str, tablespace: str) -> list[dict[str, Any]]:
    """Datafiles of one PERMANENT tablespace, read inside the PDB itself.

    Read through the PDB's own service and DBA_ views - the same place the
    DDL runs - not CDB_DATA_FILES from the root. Read from the root right
    after an AUTOEXTEND change, CDB_DATA_FILES still returned the old
    values, so the post-run check reported a successful change as failed.
    """
    rows = query(
        """
        SELECT df.file_id,
               df.file_name,
               df.autoextensible,
               df.bytes,
               df.maxbytes,
               ts.contents,
               ts.bigfile,
               ts.block_size
        FROM dba_data_files df
        JOIN dba_tablespaces ts
          ON ts.tablespace_name = df.tablespace_name
        WHERE df.tablespace_name = :tablespace
        ORDER BY df.file_id
        """,
        database_name=pdb_name,
        binds={"tablespace": tablespace},
    )

    if not rows:
        raise ValueError(f"Tablespace {tablespace} does not exist in {pdb_name}")

    if rows[0]["contents"] != "PERMANENT":
        raise ValueError(
            f"{tablespace} is a {rows[0]['contents']} tablespace; only "
            "PERMANENT tablespaces are handled by this action"
        )

    return rows


def _file_limit_mb(files: list[dict[str, Any]]) -> int:
    """Largest MAXSIZE a smallfile datafile of this tablespace can have."""
    block_size = int(files[0]["block_size"])
    return min(_SIZE_MB_RANGE[1], block_size * _SMALLFILE_MAX_BLOCKS // _MB)


def _mb(value: Any) -> int:
    return int(value or 0) // _MB


def _files_summary(files: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{posixpath.basename(f['file_name'])} {_mb(f['bytes'])} MB, "
        + (
            f"autoextend to {_mb(f['maxbytes'])} MB"
            if f["autoextensible"] == "YES"
            else "fixed size"
        )
        for f in files
    )


_DATAGUARD_NOTE = (
    "Runs on the primary; with standby_file_management=AUTO the change "
    "reaches the physical standby through redo. The filesystem is not "
    "checked: make sure it has room for the new maximum."
)


# --- enable_tablespace_autoextend ------------------------------------------


def _needs_autoextend(file: dict[str, Any], max_mb: int) -> bool:
    """Fixed-size files, and autoextensible ones capped below max_mb."""
    return file["autoextensible"] != "YES" or _mb(file["maxbytes"]) < max_mb


def _plan_enable_tablespace_autoextend(params: dict[str, Any]) -> ActionPlan:
    pdb_name, tablespace = _tablespace_target(params)
    files = _tablespace_files(pdb_name, tablespace)

    limit_mb = _file_limit_mb(files)
    next_mb = _int_param(params, "next_mb", _DEFAULT_NEXT_MB, *_NEXT_MB_RANGE)
    max_mb = _int_param(
        params, "max_mb", min(_DEFAULT_MAX_MB, limit_mb), _SIZE_MB_RANGE[0], limit_mb
    )

    targets = [f for f in files if _needs_autoextend(f, max_mb)]

    if not targets:
        raise ValueError(
            f"Nothing to do: every datafile of {tablespace} already "
            f"autoextends to at least {max_mb} MB ({_files_summary(files)})"
        )

    too_small = [f for f in targets if _mb(f["bytes"]) >= max_mb]
    if too_small:
        raise ValueError(
            f"max_mb {max_mb} is not above the current size of "
            + ", ".join(
                f"{posixpath.basename(f['file_name'])} ({_mb(f['bytes'])} MB)"
                for f in too_small
            )
        )

    statements = [
        f"ALTER DATABASE DATAFILE {_sql_string(f['file_name'])} "
        f"AUTOEXTEND ON NEXT {next_mb}M MAXSIZE {max_mb}M"
        for f in targets
    ]

    return ActionPlan(
        action="enable_tablespace_autoextend",
        description=(
            f"Let {tablespace} in {pdb_name} grow on demand: autoextend "
            f"{len(targets)} datafile(s) in steps of {next_mb} MB up to "
            f"{max_mb} MB each."
        ),
        statements=statements,
        impact=(
            f"Current files: {_files_summary(files)}. No new files are "
            "created and nothing is allocated up front; files grow only as "
            "space is used. Reversible with AUTOEXTEND OFF, though space a "
            f"file has already grown into stays allocated. {_DATAGUARD_NOTE}"
        ),
        target=f"primary {pdb_name} tablespace {tablespace}",
        reversible=True,
        params={
            "pdb_name": pdb_name,
            "tablespace_name": tablespace,
            "next_mb": next_mb,
            "max_mb": max_mb,
        },
    )


def _execute_enable_tablespace_autoextend(params: dict[str, Any]) -> dict[str, Any]:
    plan = _plan_enable_tablespace_autoextend(params)
    pdb_name = plan.params["pdb_name"]
    tablespace = plan.params["tablespace_name"]
    max_mb = plan.params["max_mb"]

    before = _tablespace_files(pdb_name, tablespace)
    touched = {f["file_name"] for f in before if _needs_autoextend(f, max_mb)}

    statement_results = execute_statements(plan.statements, database_name=pdb_name)
    after = _tablespace_files(pdb_name, tablespace)

    succeeded = all(r["status"] == "OK" for r in statement_results) and all(
        not _needs_autoextend(f, max_mb) for f in after if f["file_name"] in touched
    )

    return {
        "action": "enable_tablespace_autoextend",
        "before": _files_summary(before),
        "statements": statement_results,
        "after": _files_summary(after),
        "succeeded": succeeded,
    }


# --- add_tablespace_datafile -----------------------------------------------


def _next_datafile_path(files: list[dict[str, Any]], tablespace: str) -> str:
    """A new file next to the existing ones, with an unused name."""
    directory = posixpath.dirname(files[0]["file_name"])
    existing = {posixpath.basename(f["file_name"]).lower() for f in files}

    number = len(files) + 1
    while f"{tablespace.lower()}_{number:02d}.dbf" in existing:
        number += 1

    return posixpath.join(directory, f"{tablespace.lower()}_{number:02d}.dbf")


def _plan_add_tablespace_datafile(params: dict[str, Any]) -> ActionPlan:
    pdb_name, tablespace = _tablespace_target(params)
    files = _tablespace_files(pdb_name, tablespace)

    if files[0]["bigfile"] == "YES":
        raise ValueError(
            f"{tablespace} is a bigfile tablespace, which holds exactly one "
            "datafile; use enable_tablespace_autoextend instead"
        )

    limit_mb = _file_limit_mb(files)
    size_mb = _int_param(
        params, "size_mb", _DEFAULT_SIZE_MB, _SIZE_MB_RANGE[0], limit_mb
    )
    next_mb = _int_param(params, "next_mb", _DEFAULT_NEXT_MB, *_NEXT_MB_RANGE)
    max_mb = _int_param(
        params,
        "max_mb",
        max(size_mb, min(_DEFAULT_MAX_MB, limit_mb)),
        size_mb,
        limit_mb,
    )

    path = _next_datafile_path(files, tablespace)
    statement = (
        f'ALTER TABLESPACE "{tablespace}" ADD DATAFILE {_sql_string(path)} '
        f"SIZE {size_mb}M AUTOEXTEND ON NEXT {next_mb}M MAXSIZE {max_mb}M"
    )

    return ActionPlan(
        action="add_tablespace_datafile",
        description=(
            f"Add a {size_mb} MB datafile to {tablespace} in {pdb_name}, "
            f"autoextending in {next_mb} MB steps up to {max_mb} MB."
        ),
        statements=[statement],
        impact=(
            f"Current files: {_files_summary(files)}. Creates {path} and "
            f"allocates {size_mb} MB on disk immediately. Not reversible in "
            "practice: a datafile can only be dropped while it holds no "
            f"data. {_DATAGUARD_NOTE}"
        ),
        target=f"primary {pdb_name} tablespace {tablespace}",
        reversible=False,
        params={
            "pdb_name": pdb_name,
            "tablespace_name": tablespace,
            "size_mb": size_mb,
            "next_mb": next_mb,
            "max_mb": max_mb,
        },
    )


def _execute_add_tablespace_datafile(params: dict[str, Any]) -> dict[str, Any]:
    plan = _plan_add_tablespace_datafile(params)
    pdb_name = plan.params["pdb_name"]
    tablespace = plan.params["tablespace_name"]

    before = _tablespace_files(pdb_name, tablespace)
    statement_results = execute_statements(plan.statements, database_name=pdb_name)
    after = _tablespace_files(pdb_name, tablespace)

    succeeded = (
        all(r["status"] == "OK" for r in statement_results)
        and len(after) == len(before) + 1
    )

    return {
        "action": "add_tablespace_datafile",
        "before": _files_summary(before),
        "statements": statement_results,
        "after": _files_summary(after),
        "succeeded": succeeded,
    }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

ACTIONS: dict[str, RemediationAction] = {
    "restart_redo_apply": RemediationAction(
        name="restart_redo_apply",
        description=(
            "Restart managed redo apply on the physical standby. Try this "
            "FIRST when get_dataguard_status shows MRP0 missing from "
            "apply_processes, recovery_mode IDLE, or a non-zero "
            "sequence_gap. Does not restart the instance."
        ),
        reversible=True,
        planner=_plan_restart_redo_apply,
        executor=_execute_restart_redo_apply,
    ),
    "restart_standby_instance": RemediationAction(
        name="restart_standby_instance",
        description=(
            "Bounce the standby instance, then restart redo apply. Use ONLY "
            "after restart_redo_apply has failed with ORA-16448, which means "
            "a recovery session is wedged and cannot be cleared any other "
            "way. The standby is unavailable for one to three minutes. Not "
            "reversible."
        ),
        reversible=False,
        planner=_plan_restart_standby_instance,
        executor=_execute_restart_standby_instance,
    ),
    "enable_tablespace_autoextend": RemediationAction(
        name="enable_tablespace_autoextend",
        description=(
            "Let a tablespace grow on demand by turning on AUTOEXTEND (or "
            "raising MAXSIZE) for its datafiles. Try this FIRST for a "
            "tablespace that is full or near its maximum size, or for "
            "ORA-1653/ORA-1654 'unable to extend' errors. Creates no files "
            "and allocates nothing up front. Refuses when every datafile "
            "already autoextends far enough."
        ),
        reversible=True,
        planner=_plan_enable_tablespace_autoextend,
        executor=_execute_enable_tablespace_autoextend,
        params_help=_TABLESPACE_PARAMS_HELP,
        pin_statements=True,
    ),
    "add_tablespace_datafile": RemediationAction(
        name="add_tablespace_datafile",
        description=(
            "Add a datafile to a tablespace, next to its existing files. Use "
            "when its datafiles already autoextend to the smallfile limit, "
            "or when the user asks for a new datafile. Allocates size_mb on "
            "disk immediately. Not reversible."
        ),
        reversible=False,
        planner=_plan_add_tablespace_datafile,
        executor=_execute_add_tablespace_datafile,
        params_help=(
            _TABLESPACE_PARAMS_HELP
            + f", size_mb (optional initial size, default {_DEFAULT_SIZE_MB}; "
            "max_mb must be at least size_mb)"
        ),
        pin_statements=True,
    ),
}


def list_actions() -> list[dict[str, Any]]:
    return [
        {
            "action": a.name,
            "description": a.description,
            "reversible": a.reversible,
            "params": a.params_help,
        }
        for a in ACTIONS.values()
    ]


def _get_action(action: str) -> RemediationAction:
    key = (action or "").strip().lower()

    if key not in ACTIONS:
        raise ValueError(
            f"Unknown remediation action {action!r}. "
            f"Allowed: {', '.join(sorted(ACTIONS))}"
        )

    return ACTIONS[key]


def plan_remediation(
    action: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return what an action would do. Executes nothing."""
    return _get_action(action).planner(dict(params or {})).as_dict()


# The tablespace actions offered side by side, gentlest first.
TABLESPACE_OPTIONS = ("enable_tablespace_autoextend", "add_tablespace_datafile")


def plan_tablespace_options(
    pdb_name: str,
    tablespace_name: str,
    size_mb: int | None = None,
    next_mb: int | None = None,
    max_mb: int | None = None,
) -> dict[str, Any]:
    """Plan every applicable tablespace fix, for a human to choose one.

    Executes nothing. Each returned plan carries the same
    alternative_group, which the chat uses to withdraw the other options
    once one is approved. An option that does not apply is listed under
    unavailable with the reason instead of being dropped silently.
    """
    base: dict[str, Any] = {"pdb_name": pdb_name, "tablespace_name": tablespace_name}
    for key, value in (("next_mb", next_mb), ("max_mb", max_mb)):
        if value is not None:
            base[key] = value

    options: list[dict[str, Any]] = []
    unavailable: dict[str, str] = {}

    for action in TABLESPACE_OPTIONS:
        params = dict(base)
        if action == "add_tablespace_datafile" and size_mb is not None:
            params["size_mb"] = size_mb
        try:
            options.append(plan_remediation(action, params))
        except ValueError as exc:
            unavailable[action] = str(exc)

    if not options:
        raise ValueError(
            "No tablespace fix applies: "
            + "; ".join(f"{name}: {reason}" for name, reason in unavailable.items())
        )

    first = options[0]["params"]
    group = f"tablespace:{first['pdb_name']}:{first['tablespace_name']}"
    for plan in options:
        plan["alternative_group"] = group

    return {
        "options": options,
        "unavailable": unavailable,
        "note": (
            "These are alternatives. The human approves at most one; "
            "approving one withdraws the others."
        ),
    }


def execute_remediation(
    action: str,
    params: dict[str, Any] | None = None,
    approved_statements: list[str] | None = None,
) -> dict[str, Any]:
    """Run an allowlisted action. Callers must obtain human approval first.

    Deliberately not exposed over MCP: nothing the model can call reaches
    this function.

    approved_statements are the statements the human saw on the approval
    card. For actions with pin_statements, execution re-plans and refuses
    if the fresh statements differ - the database changed after approval,
    so what would run is no longer what was approved.
    """
    definition = _get_action(action)
    clean_params = dict(params or {})

    if definition.pin_statements:
        if approved_statements is None:
            raise RuntimeError(
                f"{definition.name} requires the approved statements so it "
                "can confirm they are still what would run"
            )
        fresh = definition.planner(clean_params).statements
        if list(approved_statements) != fresh:
            raise RuntimeError(
                "The database changed after approval, so the statements that "
                "would run now differ from the approved ones. Nothing was "
                "run; ask for a fresh proposal."
            )

    return definition.executor(clean_params)
