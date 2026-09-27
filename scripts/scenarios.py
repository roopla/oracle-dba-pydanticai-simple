#!/usr/bin/env python3
"""Recreate the demo scenarios in any environment, from the .env files alone.

Every scenario the agent was built and tested against, runnable against a
new database without Docker, SSH or SQL*Plus: all work goes through the
same Oracle connections the application uses, and object locations come
from the target database's own dictionary.

    uv run --env-file .env.mcp --env-file .env.agent --env-file .env.loadgen \\
        python scripts/scenarios.py <command>

Commands
    prepare          Create the LOADGEN schema and the demo tables.
    list             Show the scenarios and what each one demonstrates.
    status           What exists, tablespace fill, standby apply state.

    write-load       Write load: --level warning | critical | burst.
    blocking         A real row-lock blocking chain for --seconds.
    long-query       A session busy for --seconds (long-running query).
    tablespace-full  Fill a small fixed-size tablespace until ORA-1653.
    partition-pressure  Monthly-partitioned table filling its tablespace.
    dg-stop-apply    Stop redo apply on the standby, force log switches.
    dg-start-apply   Restart redo apply (if you do not use the chat card).
    history-burst    Bursts for --minutes, then ask about the past window.

    cleanup-all      Drop everything the scenarios created.

SAFETY: these scenarios degrade a database on purpose. The runner refuses
to start unless LAB_SCENARIOS_ENABLED=true (normally in .env.loadgen).
Never enable that for a production database.

Accounts
    Scenario admin  SCENARIO_ADMIN_USER / SCENARIO_ADMIN_PASSWORD, falling
                    back to ORACLE_USER / ORACLE_PASSWORD. Needs to create
                    users and tablespaces and run ALTER SYSTEM (a DBA).
    Load schema     LOADGEN_ORACLE_USER / LOADGEN_ORACLE_PASSWORD.
    Standby         ORACLE_STANDBY_DSN / _USER / _PASSWORD (SYSDBA), for
                    the Data Guard scenarios only.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import oracledb  # noqa: E402

from oracle_core.config import get_settings  # noqa: E402
from oracle_core.db import build_dsn  # noqa: E402

_NAME = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
_MB = 1024 * 1024

LOCK_TABLE = "SCENARIO_LOCKS"
FILL_TABLE = "SCENARIO_FILL"
FULL_TABLESPACE = "SCEN_FULL_TS"
PARTITION_TABLESPACE = "PART_DEMO_TS"
PARTITION_TABLE = "SALES_HISTORY"
INGEST_TABLE = "INGEST_SIM_ORDERS"

SPACE_ERRORS = (1536, 1653, 1654, 1688)  # quota, table, index, partition

SCENARIOS = [
    ("write-load --level warning", "WRITE_WORKLOAD warning (commits/s between the warn and crit thresholds)"),
    ("write-load --level critical", "WRITE_WORKLOAD critical; ask the agent what is generating load"),
    ("write-load --level burst", "incident opens, resolves and reopens with each burst"),
    ("blocking", "BLOCKING_SESSION on a real row lock; the agent names blocker and waiter"),
    ("long-query", "LONG_RUNNING_QUERY for a session busy past the threshold"),
    ("tablespace-full", "TABLESPACE_USAGE critical + ORA-1653 in the alert log; autoextend / add-datafile cards"),
    ("partition-pressure", "monthly-partitioned table fills its tablespace; three cards incl. drop old partitions"),
    ("dg-stop-apply", "MRP0 stopped, apply gap grows; restart_redo_apply card"),
    ("history-burst", "activity in the past; the agent must answer from ASH history"),
]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def fail(message: str) -> None:
    sys.exit(f"ERROR: {message}")


def identifier(value: str, what: str) -> str:
    name = value.strip().upper()
    if not _NAME.match(name):
        fail(f"{what} {value!r} is not a valid Oracle name")
    return name


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def admin_credentials() -> tuple[str, str]:
    user = os.environ.get("SCENARIO_ADMIN_USER")
    password = os.environ.get("SCENARIO_ADMIN_PASSWORD")
    if user and password:
        return user, password
    settings = get_settings()
    return settings.oracle_user, settings.oracle_password.get_secret_value()


def admin(database: str) -> Any:
    user, password = admin_credentials()
    return oracledb.connect(user=user, password=password, dsn=build_dsn(database))


def loadgen_user() -> str:
    return identifier(os.environ.get("LOADGEN_ORACLE_USER") or "LOADGEN", "LOADGEN user")


def loadgen(database: str, action: str = "scenario") -> Any:
    password = os.environ.get("LOADGEN_ORACLE_PASSWORD")
    if not password:
        fail("LOADGEN_ORACLE_PASSWORD is not set (load .env.loadgen)")
    connection = oracledb.connect(
        user=loadgen_user(), password=password, dsn=build_dsn(database)
    )
    connection.module = "scenarios"
    connection.action = action
    return connection


def standby() -> Any:
    dsn = os.environ.get("ORACLE_STANDBY_DSN")
    password = os.environ.get("ORACLE_STANDBY_PASSWORD")
    if not dsn or not password:
        fail("ORACLE_STANDBY_DSN / ORACLE_STANDBY_PASSWORD are not set: no standby to use")
    return oracledb.connect(
        user=os.environ.get("ORACLE_STANDBY_USER") or "sys",
        password=password,
        dsn=dsn,
        mode=oracledb.AUTH_MODE_SYSDBA,
        tcp_connect_timeout=10,
    )


def execute(connection: Any, sql: str, ignore: tuple[int, ...] = (), **binds: Any) -> bool:
    """Run one statement; return False if it raised an ignored error."""
    with connection.cursor() as cursor:
        try:
            cursor.execute(sql, binds)
            return True
        except oracledb.DatabaseError as exc:
            if exc.args[0].code in ignore:
                return False
            raise


def scalar(connection: Any, sql: str, **binds: Any) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(sql, binds)
        row = cursor.fetchone()
    return row[0] if row else None


def run_script(script: str, *args: str) -> int:
    """Run a sibling script with this process's environment."""
    command = [sys.executable, str(PROJECT_ROOT / "scripts" / script), *args]
    log("running: " + " ".join(command[1:]))
    return subprocess.call(command, cwd=str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def datafile_directory(connection: Any) -> str:
    """Directory of this container's SYSTEM datafile, from the dictionary."""
    path = scalar(
        connection,
        "SELECT file_name FROM dba_data_files WHERE tablespace_name = 'SYSTEM' AND ROWNUM = 1",
    )
    if not path or path.startswith("+"):
        fail(
            "the SYSTEM datafile is on ASM or could not be read; create the demo "
            "tablespaces by hand (see SCENARIOS.md) and rerun"
        )
    return posixpath.dirname(path)


def tablespace_usage(connection: Any, tablespace: str) -> str:
    total = scalar(connection, "SELECT SUM(bytes) FROM dba_data_files WHERE tablespace_name = :t", t=tablespace)
    if not total:
        return f"{tablespace}: not present"
    free = scalar(connection, "SELECT NVL(SUM(bytes), 0) FROM dba_free_space WHERE tablespace_name = :t", t=tablespace)
    used = total - free
    return f"{tablespace}: {used / total * 100:.1f}% used ({used // _MB} of {total // _MB} MB)"


def create_fixed_tablespace(connection: Any, name: str, size_mb: int) -> None:
    if scalar(connection, "SELECT COUNT(*) FROM dba_tablespaces WHERE tablespace_name = :t", t=name):
        log(f"tablespace {name} already exists")
        return
    path = posixpath.join(datafile_directory(connection), f"{name.lower()}01.dbf")
    execute(
        connection,
        f"CREATE TABLESPACE {name} DATAFILE '{path}' SIZE {size_mb}M AUTOEXTEND OFF "
        "EXTENT MANAGEMENT LOCAL UNIFORM SIZE 1M",
    )
    log(f"created tablespace {name}: {size_mb} MB, fixed size ({path})")


def monitor_threshold(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    print("Scenarios (run with: python scripts/scenarios.py <command>):\n")
    for command, what in SCENARIOS:
        print(f"  {command:<30} {what}")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    user = loadgen_user()
    password = os.environ.get("LOADGEN_ORACLE_PASSWORD")
    if not password:
        fail("LOADGEN_ORACLE_PASSWORD is not set (load .env.loadgen)")
    if '"' in password:
        fail("LOADGEN_ORACLE_PASSWORD must not contain a double quote")

    with admin(args.pdb) as conn:
        default_ts = scalar(
            conn,
            "SELECT property_value FROM database_properties "
            "WHERE property_name = 'DEFAULT_PERMANENT_TABLESPACE'",
        )
        if scalar(conn, "SELECT COUNT(*) FROM dba_users WHERE username = :u", u=user):
            log(f"user {user} already exists; making sure its grants are in place")
        else:
            execute(conn, f'CREATE USER {user} IDENTIFIED BY "{password}" '
                          f"DEFAULT TABLESPACE {default_ts} QUOTA 2G ON {default_ts}")
            log(f"created {user} in {args.pdb} (default tablespace {default_ts}, 2 GB quota)")
        execute(conn, f"GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO {user}")

    with loadgen(args.pdb) as conn:
        if execute(conn, f"CREATE TABLE {LOCK_TABLE} (id NUMBER PRIMARY KEY, val NUMBER)", ignore=(955,)):
            execute(conn, f"INSERT INTO {LOCK_TABLE} VALUES (1, 0)")
            conn.commit()
            log(f"created {user}.{LOCK_TABLE} (for the blocking scenario)")
        else:
            log(f"{user}.{LOCK_TABLE} already exists")

    status = run_script("ingest_simulator.py", "setup", "--database", args.pdb)
    if status:
        return status
    log("prepared. Next: python scripts/scenarios.py list")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    user = loadgen_user()
    with admin(args.pdb) as conn:
        exists = scalar(conn, "SELECT COUNT(*) FROM dba_users WHERE username = :u", u=user)
        print(f"{user} in {args.pdb}: {'present' if exists else 'missing - run prepare'}")
        for table in (LOCK_TABLE, INGEST_TABLE, FILL_TABLE, PARTITION_TABLE):
            n = scalar(conn, "SELECT COUNT(*) FROM dba_tables WHERE owner = :o AND table_name = :t", o=user, t=table)
            print(f"  table {table:<20} {'present' if n else '-'}")
        for ts in (FULL_TABLESPACE, PARTITION_TABLESPACE):
            print(f"  {tablespace_usage(conn, ts)}")
    if os.environ.get("ORACLE_STANDBY_DSN") and os.environ.get("ORACLE_STANDBY_PASSWORD"):
        try:
            with standby() as conn:
                mrp = scalar(conn, "SELECT status FROM v$managed_standby WHERE process = 'MRP0'")
                received = scalar(conn, "SELECT MAX(sequence#) FROM v$archived_log")
                applied = scalar(conn, "SELECT MAX(sequence#) FROM v$archived_log WHERE applied = 'YES'")
            print(f"standby: MRP0 {mrp or 'NOT RUNNING'}, received {received} / applied {applied}")
        except oracledb.DatabaseError as exc:
            print(f"standby: cannot connect ({exc.args[0].message.strip()})")
    else:
        print("standby: not configured")
    return 0


def cmd_write_load(args: argparse.Namespace) -> int:
    warn = monitor_threshold("MONITOR_WRITE_WARN_COMMITS_PER_SEC", 40)
    crit = monitor_threshold("MONITOR_WRITE_CRIT_COMMITS_PER_SEC", 150)
    common = ["start", "--foreground", "--database", args.pdb, "--duration", str(args.duration)]
    if args.level == "warning":
        rate = round((warn + crit) / 2)
        log(f"target {rate} commits/s: between the warning ({warn:g}) and critical ({crit:g}) thresholds")
        extra = ["--mode", "steady", "--workers", "4", "--rate", str(rate)]
    elif args.level == "critical":
        log(f"8 uncapped workers: aims well above the critical threshold ({crit:g} commits/s)")
        extra = ["--mode", "steady", "--workers", "8"]
    else:
        extra = ["--mode", "burst", "--workers", "8", "--burst-seconds", "45", "--pause-seconds", "60"]
    log("stops by itself after --duration; Ctrl+C stops it earlier")
    return run_script("ingest_simulator.py", *common, *extra)


def cmd_blocking(args: argparse.Namespace) -> int:
    holder = loadgen(args.pdb, "blocking holder")
    waiter = loadgen(args.pdb, "blocking waiter")
    waiter.call_timeout = (args.seconds + 60) * 1000
    execute(holder, f"UPDATE {LOCK_TABLE} SET val = val + 1 WHERE id = 1")
    log("holder session locked the row (not committed)")

    def wait() -> None:
        try:
            execute(waiter, f"UPDATE {LOCK_TABLE} SET val = val + 1 WHERE id = 1")
            waiter.rollback()
        except oracledb.DatabaseError as exc:
            log(f"waiter: {exc.args[0].message.strip()}")

    thread = threading.Thread(target=wait)
    thread.start()
    log(f"waiter session is now blocked on 'enq: TX - row lock contention' for {args.seconds}s")
    log("ask the agent: 'Is anything blocked right now?'")
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        log("interrupted")
    holder.rollback()
    thread.join(30)
    holder.close()
    waiter.close()
    log("lock released; the incident resolves on the next poll")
    return 0


def cmd_long_query(args: argparse.Namespace) -> int:
    threshold = monitor_threshold("MONITOR_LONG_RUNNING_QUERY_SECONDS", 60)
    with loadgen(args.pdb, "long running") as conn:
        conn.call_timeout = (args.seconds + 60) * 1000
        log(f"session busy for {args.seconds}s; the monitor flags it after {threshold:g}s")
        log("ask the agent: 'Are there any long-running sessions?'")
        execute(conn, "BEGIN DBMS_SESSION.SLEEP(:s); END;", s=args.seconds)
    log("session finished; the incident resolves on the next poll")
    return 0


def cmd_tablespace_full(args: argparse.Namespace) -> int:
    user = loadgen_user()
    with admin(args.pdb) as conn:
        create_fixed_tablespace(conn, FULL_TABLESPACE, args.size_mb)
        execute(conn, f"ALTER USER {user} QUOTA UNLIMITED ON {FULL_TABLESPACE}")
    with loadgen(args.pdb, "tablespace fill") as conn:
        if execute(conn, f"CREATE TABLE {FILL_TABLE} (id NUMBER, payload VARCHAR2(1000)) "
                         f"TABLESPACE {FULL_TABLESPACE}", ignore=(955,)):
            log(f"created {user}.{FILL_TABLE} in {FULL_TABLESPACE}")
        written = 0
        while True:
            try:
                execute(conn, f"INSERT INTO {FILL_TABLE} SELECT LEVEL, RPAD('x', 1000, 'x') "
                              "FROM dual CONNECT BY LEVEL <= 2000")
                conn.commit()
                written += 2000
            except oracledb.DatabaseError as exc:
                error = exc.args[0]
                if error.code in SPACE_ERRORS:
                    log(f"Oracle refused the insert: {error.message.strip()}")
                    break
                raise
    with admin(args.pdb) as conn:
        log(tablespace_usage(conn, FULL_TABLESPACE) + f" after {written:,} rows")
    log(f"ask the agent: '{FULL_TABLESPACE} in {args.pdb} is full, how can we fix it?'")
    return 0


def cmd_partition_pressure(args: argparse.Namespace) -> int:
    base = ["--pdb", args.pdb]
    with admin(args.pdb) as conn:
        partitions = scalar(
            conn,
            "SELECT COUNT(*) FROM dba_tab_partitions WHERE table_owner = :o AND table_name = :t",
            o=loadgen_user(), t=PARTITION_TABLE,
        )
    steps = []
    if partitions > 2:
        # Already built: backfilling again would double the history.
        log(f"{PARTITION_TABLE} already has {partitions} partitions; skipping setup and backfill")
    else:
        steps += [["setup", "--months", str(args.months)],
                  ["backfill", "--months", str(args.months)]]
    steps += [["fill", "--target-pct", str(args.target_pct)], ["status"]]
    for step in steps:
        status = run_script("partition_pressure_simulator.py", *base, *step)
        if status:
            return status
    log(f"ask the agent: '{PARTITION_TABLESPACE} in {args.pdb} is almost full, what can we do?'")
    return 0


def cmd_dg_stop_apply(args: argparse.Namespace) -> int:
    with standby() as conn:
        if scalar(conn, "SELECT COUNT(*) FROM v$managed_standby WHERE process = 'MRP0'"):
            execute(conn, "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL")
            log("redo apply stopped on the standby (MRP0 cancelled)")
        else:
            log("redo apply was already stopped")
    with admin(get_settings().oracle_cdb_name) as conn:
        for i in range(args.log_switches):
            execute(conn, "ALTER SYSTEM ARCHIVE LOG CURRENT")
        log(f"{args.log_switches} log switches on the primary: the apply gap grows")
    log("ask the agent: 'Is my Data Guard standby healthy?', then 'Can you fix it?'")
    return 0


def cmd_dg_start_apply(args: argparse.Namespace) -> int:
    with standby() as conn:
        if scalar(conn, "SELECT COUNT(*) FROM v$managed_standby WHERE process = 'MRP0'"):
            log("redo apply is already running")
            return 0
        execute(conn, "ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT FROM SESSION")
    log("redo apply restarted")
    return 0


def cmd_history_burst(args: argparse.Namespace) -> int:
    log("ASH history needs the Oracle Diagnostics Pack licence on this database")
    status = run_script(
        "ingest_simulator.py", "start", "--foreground", "--database", args.pdb,
        "--mode", "burst", "--workers", "8", "--burst-seconds", "45",
        "--pause-seconds", "60", "--duration", str(args.minutes * 60),
    )
    log(f"done. Ask the agent: 'What was the database waiting on in the last {args.minutes + 5} minutes?'")
    return status


def cmd_cleanup_all(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input("Drop every scenario table and tablespace? Type YES: ")
        if answer.strip() != "YES":
            print("Cancelled.")
            return 1
    user = loadgen_user()
    with admin(args.pdb) as conn:
        for table in (LOCK_TABLE, INGEST_TABLE, FILL_TABLE, PARTITION_TABLE):
            if execute(conn, f"DROP TABLE {user}.{table} PURGE", ignore=(942,)):
                log(f"dropped {user}.{table}")
        for ts in (FULL_TABLESPACE, PARTITION_TABLESPACE):
            if execute(conn, f"DROP TABLESPACE {ts} INCLUDING CONTENTS AND DATAFILES", ignore=(959,)):
                log(f"dropped tablespace {ts}")
        if args.drop_user and execute(conn, f"DROP USER {user} CASCADE", ignore=(1918,)):
            log(f"dropped user {user}")
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recreate the demo scenarios from the .env files alone.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pdb", help="PDB to use (default: ORACLE_DEFAULT_PDB_NAME).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("prepare", help="Create LOADGEN and the demo tables.").set_defaults(func=cmd_prepare)
    sub.add_parser("list", help="List the scenarios.").set_defaults(func=cmd_list)
    sub.add_parser("status", help="What exists and its state.").set_defaults(func=cmd_status)

    p = sub.add_parser("write-load", help="Write load via the ingest simulator.")
    p.add_argument("--level", choices=["warning", "critical", "burst"], default="warning")
    p.add_argument("--duration", type=int, default=300, help="Seconds (default 300).")
    p.set_defaults(func=cmd_write_load)

    p = sub.add_parser("blocking", help="Hold a row lock that blocks another session.")
    p.add_argument("--seconds", type=int, default=120)
    p.set_defaults(func=cmd_blocking)

    p = sub.add_parser("long-query", help="Keep one session busy.")
    p.add_argument("--seconds", type=int, default=150)
    p.set_defaults(func=cmd_long_query)

    p = sub.add_parser("tablespace-full", help="Fill a fixed-size tablespace until ORA-1653.")
    p.add_argument("--size-mb", type=int, default=32)
    p.set_defaults(func=cmd_tablespace_full)

    p = sub.add_parser("partition-pressure", help="Monthly-partitioned table filling its tablespace.")
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--target-pct", type=float, default=97.0)
    p.set_defaults(func=cmd_partition_pressure)

    p = sub.add_parser("dg-stop-apply", help="Stop standby redo apply and force log switches.")
    p.add_argument("--log-switches", type=int, default=5)
    p.set_defaults(func=cmd_dg_stop_apply)

    sub.add_parser("dg-start-apply", help="Restart standby redo apply.").set_defaults(func=cmd_dg_start_apply)

    p = sub.add_parser("history-burst", help="Bursts of load for later ASH questions.")
    p.add_argument("--minutes", type=int, default=10)
    p.set_defaults(func=cmd_history_burst)

    p = sub.add_parser("cleanup-all", help="Drop everything the scenarios created.")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--drop-user", action="store_true", help="Also drop the LOADGEN user.")
    p.set_defaults(func=cmd_cleanup_all)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "list":
        # Needs no settings, database or safety flag.
        return cmd_list(args)
    if os.environ.get("LAB_SCENARIOS_ENABLED", "").lower() != "true":
        fail(
            "scenarios are disabled. They degrade the database on purpose. For a "
            "lab or test database only, set LAB_SCENARIOS_ENABLED=true in .env.loadgen."
        )
    args.pdb = identifier(args.pdb or get_settings().oracle_default_pdb_name, "PDB")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
