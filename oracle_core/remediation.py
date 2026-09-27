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
"""

from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import oracledb
import paramiko


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
}


def list_actions() -> list[dict[str, Any]]:
    return [
        {
            "action": a.name,
            "description": a.description,
            "reversible": a.reversible,
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


def execute_remediation(
    action: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run an allowlisted action. Callers must obtain human approval first.

    Deliberately not exposed over MCP: nothing the model can call reaches
    this function.
    """
    return _get_action(action).executor(dict(params or {}))
