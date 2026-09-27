#!/usr/bin/env python3
"""Simulate space pressure on a table range-partitioned by month.

Builds the situation the agent's drop_old_partitions action is for: a
monthly interval-partitioned table holding many months of history in a
small, fixed-size tablespace, filling up as the current month is written.

Commands (run from the project root)
    setup      DBA user: create a fixed-size tablespace and give LOADGEN a
               quota on it. LOADGEN: create the partitioned table.
    backfill   Load --months of past history, --mb-per-month each.
    fill       Write current-month rows until the tablespace reaches
               --target-pct, or with --until-error until Oracle refuses
               (ORA-1653 / ORA-1688), as a production insert would.
    status     Tablespace usage and every partition's month and size.
    cleanup    Drop the table and the tablespace.

Typical run
    sim_part() { uv run --env-file .env.mcp --env-file .env.loadgen \\
                     python scripts/partition_pressure_simulator.py "$@"; }
    sim_part setup
    sim_part backfill --months 12
    sim_part fill --target-pct 97
    sim_part status

Connections: DDL on the tablespace uses SCENARIO_ADMIN_USER / _PASSWORD if
set, else the account from .env.mcp (ORACLE_USER); the table and its rows belong to LOADGEN (.env.loadgen).
Everything runs in the foreground, so it works the same on Windows.
"""

from __future__ import annotations

import argparse
import calendar
import os
import posixpath
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import oracledb  # noqa: E402

from oracle_core.config import get_settings  # noqa: E402
from oracle_core.db import build_dsn  # noqa: E402
from oracle_core.partitions import add_months, database_current_month  # noqa: E402

_NAME = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
_MB = 1024 * 1024

# About 2,000 rows of this shape per MB of table data.
ROWS_PER_MB = 2000
CHUNK_ROWS = 2000

INSERT_MONTH_SQL = """
INSERT INTO {table} (sale_date, customer_id, region, amount, payload)
SELECT :month_start + DBMS_RANDOM.VALUE(0, :days),
       TRUNC(DBMS_RANDOM.VALUE(1, 50000)),
       DECODE(MOD(LEVEL, 4), 0, 'NORTH', 1, 'SOUTH', 2, 'EAST', 'WEST'),
       ROUND(DBMS_RANDOM.VALUE(5, 500), 2),
       RPAD('order-', 400, 'x')
FROM dual
CONNECT BY LEVEL <= :row_count
"""

SPACE_ERRORS = (1653, 1688)  # unable to extend table / table partition


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def identifier(value: str, what: str) -> str:
    name = value.strip().upper()
    if not _NAME.match(name):
        sys.exit(f"{what} {value!r} is not a valid Oracle name")
    return name


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def admin_connection(pdb: str) -> Any:
    """DBA login for tablespace DDL: SCENARIO_ADMIN_* if set, else ORACLE_*."""
    user = os.environ.get("SCENARIO_ADMIN_USER")
    password = os.environ.get("SCENARIO_ADMIN_PASSWORD")
    if not (user and password):
        settings = get_settings()
        user, password = settings.oracle_user, settings.oracle_password.get_secret_value()
    return oracledb.connect(user=user, password=password, dsn=build_dsn(pdb))


def loadgen_connection(pdb: str) -> Any:
    user = os.environ.get("LOADGEN_ORACLE_USER")
    password = os.environ.get("LOADGEN_ORACLE_PASSWORD")
    if not user or not password:
        sys.exit(
            "LOADGEN_ORACLE_USER / LOADGEN_ORACLE_PASSWORD are not set. "
            "Load .env.loadgen (see scripts/ingest_simulator_user.sql)."
        )
    connection = oracledb.connect(user=user, password=password, dsn=build_dsn(pdb))
    connection.module = "partition_pressure_sim"
    return connection


def tablespace_usage(admin: Any, tablespace: str) -> dict[str, float]:
    with admin.cursor() as cursor:
        cursor.execute(
            """
            SELECT (SELECT SUM(bytes) FROM dba_data_files
                    WHERE tablespace_name = :ts) AS total,
                   (SELECT NVL(SUM(bytes), 0) FROM dba_free_space
                    WHERE tablespace_name = :ts) AS free
            FROM dual
            """,
            ts=tablespace,
        )
        total, free = cursor.fetchone()
    if not total:
        sys.exit(f"Tablespace {tablespace} does not exist. Run setup first.")
    used = total - free
    return {
        "total_mb": round(total / _MB, 1),
        "used_mb": round(used / _MB, 1),
        "pct_used": round(used / total * 100, 1),
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    ts, table, owner = args.tablespace, args.table, args.owner

    with admin_connection(args.pdb) as admin, admin.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM dba_tablespaces WHERE tablespace_name = :ts", ts=ts
        )
        if cursor.fetchone()[0]:
            log(f"tablespace {ts} already exists")
        else:
            # Put the new file next to the PDB's SYSTEM datafile.
            cursor.execute(
                "SELECT file_name FROM dba_data_files "
                "WHERE tablespace_name = 'SYSTEM' AND ROWNUM = 1"
            )
            directory = posixpath.dirname(cursor.fetchone()[0])
            path = posixpath.join(directory, f"{ts.lower()}01.dbf")
            # Uniform 1 MB extents: every partition starts at 1 MB instead of
            # the 8 MB default for partition segments, so a small tablespace
            # can hold a year of monthly partitions.
            cursor.execute(
                f"CREATE TABLESPACE {ts} DATAFILE '{path}' SIZE {args.size_mb}M "
                "AUTOEXTEND OFF EXTENT MANAGEMENT LOCAL UNIFORM SIZE 1M"
            )
            log(f"created tablespace {ts}: {args.size_mb} MB, fixed size, {path}")
        cursor.execute(f"ALTER USER {owner} QUOTA UNLIMITED ON {ts}")
        log(f"{owner} has an unlimited quota on {ts}")

    first_month = add_months(database_current_month(args.pdb), -args.months)
    with loadgen_connection(args.pdb) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=table
        )
        if cursor.fetchone()[0]:
            log(f"table {owner}.{table} already exists")
            return 0
        # Interval partitioning creates a partition per month on first insert.
        # The range section holds one partition for everything before the
        # history window.
        cursor.execute(
            f"""
            CREATE TABLE {table} (
                sale_id      NUMBER GENERATED BY DEFAULT AS IDENTITY,
                sale_date    DATE           NOT NULL,
                customer_id  NUMBER(10)     NOT NULL,
                region       VARCHAR2(10)   NOT NULL,
                amount       NUMBER(12, 2)  NOT NULL,
                payload      VARCHAR2(400)  NOT NULL,
                CONSTRAINT pk_{table.lower()} PRIMARY KEY (sale_id)
                    USING INDEX TABLESPACE {ts}
            )
            TABLESPACE {ts}
            PARTITION BY RANGE (sale_date)
            INTERVAL (NUMTOYMINTERVAL(1, 'MONTH'))
            (PARTITION p_before_history VALUES LESS THAN
                (DATE '{first_month.isoformat()}'))
            """
        )
        cursor.execute(
            f"CREATE INDEX ix_{table.lower()}_customer ON {table} (customer_id) "
            f"LOCAL TABLESPACE {ts}"
        )
    log(
        f"created {owner}.{table}: range-partitioned by month on SALE_DATE "
        f"(interval), global primary key, local index, all in {ts}"
    )
    return 0


def _insert_month(conn: Any, table: str, month_start: date, rows: int) -> None:
    days = calendar.monthrange(month_start.year, month_start.month)[1]
    with conn.cursor() as cursor:
        remaining = rows
        while remaining > 0:
            chunk = min(CHUNK_ROWS, remaining)
            cursor.execute(
                INSERT_MONTH_SQL.format(table=table),
                month_start=datetime(month_start.year, month_start.month, 1),
                days=days,
                row_count=chunk,
            )
            conn.commit()
            remaining -= chunk


def cmd_backfill(args: argparse.Namespace) -> int:
    this_month = database_current_month(args.pdb)
    rows = args.mb_per_month * ROWS_PER_MB
    with loadgen_connection(args.pdb) as conn, admin_connection(args.pdb) as admin:
        for back in range(args.months, 0, -1):
            month = add_months(this_month, -back)
            try:
                _insert_month(conn, args.table, month, rows)
            except oracledb.DatabaseError as exc:
                (error,) = exc.args
                if error.code in SPACE_ERRORS:
                    log(f"{month:%Y-%m}: out of space ({error.message.strip()}); "
                        "use a larger --size-mb or fewer --months")
                    return 1
                raise
            usage = tablespace_usage(admin, args.tablespace)
            log(f"{month:%Y-%m}: {rows:,} rows, tablespace {usage['pct_used']}% "
                f"({usage['used_mb']} of {usage['total_mb']} MB)")
    return 0


def cmd_fill(args: argparse.Namespace) -> int:
    this_month = database_current_month(args.pdb)
    with loadgen_connection(args.pdb) as conn, admin_connection(args.pdb) as admin:
        written = 0
        while True:
            usage = tablespace_usage(admin, args.tablespace)
            if not args.until_error and usage["pct_used"] >= args.target_pct:
                log(f"reached {usage['pct_used']}% of {args.tablespace} "
                    f"({usage['used_mb']} of {usage['total_mb']} MB) after "
                    f"{written:,} current-month rows")
                return 0
            try:
                _insert_month(conn, args.table, this_month, CHUNK_ROWS)
            except oracledb.DatabaseError as exc:
                (error,) = exc.args
                if error.code in SPACE_ERRORS:
                    log(f"Oracle refused the insert: {error.message.strip()}")
                    log(f"tablespace {usage['pct_used']}% after {written:,} rows")
                    return 0
                raise
            written += CHUNK_ROWS
            if written % (CHUNK_ROWS * 10) == 0:
                log(f"{written:,} rows written, tablespace {usage['pct_used']}%")
            if args.pause:
                time.sleep(args.pause)


def cmd_status(args: argparse.Namespace) -> int:
    from oracle_core.partitions import table_partitions

    with admin_connection(args.pdb) as admin:
        usage = tablespace_usage(admin, args.tablespace)
    print(f"Tablespace {args.tablespace}: {usage['pct_used']}% used "
          f"({usage['used_mb']} of {usage['total_mb']} MB, fixed size)")
    info = table_partitions(args.pdb, args.owner, args.table)
    print(f"\n{args.owner}.{args.table}: {len(info['partitions'])} partitions")
    print(f"  {'month':<9} {'partition':<20} {'size MB':>8}")
    for p in info["partitions"]:
        print(f"  {p['month']:<9} {p['partition_name']:<20} {p['size_mb']:>8}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input(f"Drop {args.owner}.{args.table} and tablespace "
                       f"{args.tablespace}? Type YES: ")
        if answer.strip() != "YES":
            print("Cancelled.")
            return 1
    with loadgen_connection(args.pdb) as conn, conn.cursor() as cursor:
        try:
            cursor.execute(f"DROP TABLE {args.table} PURGE")
            log(f"dropped {args.owner}.{args.table}")
        except oracledb.DatabaseError as exc:
            if exc.args[0].code != 942:
                raise
    with admin_connection(args.pdb) as admin, admin.cursor() as cursor:
        try:
            cursor.execute(
                f"DROP TABLESPACE {args.tablespace} INCLUDING CONTENTS AND DATAFILES"
            )
            log(f"dropped tablespace {args.tablespace}")
        except oracledb.DatabaseError as exc:
            if exc.args[0].code != 959:
                raise
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate space pressure on a monthly-partitioned table.",
    )
    parser.add_argument("--pdb", help="PDB (default: ORACLE_DEFAULT_PDB_NAME).")
    parser.add_argument("--owner", default="LOADGEN")
    parser.add_argument("--table", default="SALES_HISTORY")
    parser.add_argument("--tablespace", default="PART_DEMO_TS")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="Create the tablespace and the table.")
    p.add_argument("--size-mb", type=int, default=128,
                   help="Fixed tablespace size (default 128).")
    p.add_argument("--months", type=int, default=12,
                   help="History months the table is laid out for (default 12).")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("backfill", help="Load past months of history.")
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--mb-per-month", type=int, default=6)
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("fill", help="Write the current month until space runs short.")
    p.add_argument("--target-pct", type=float, default=97.0)
    p.add_argument("--until-error", action="store_true",
                   help="Keep writing until Oracle refuses (ORA-1653/1688).")
    p.add_argument("--pause", type=float, default=0.0,
                   help="Seconds between chunks, to make the fill watchable.")
    p.set_defaults(func=cmd_fill)

    p = sub.add_parser("status", help="Tablespace usage and partitions.")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cleanup", help="Drop the table and the tablespace.")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_cleanup)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.pdb = identifier(args.pdb or get_settings().oracle_default_pdb_name, "PDB")
    args.owner = identifier(args.owner, "Owner")
    args.table = identifier(args.table, "Table")
    args.tablespace = identifier(args.tablespace, "Tablespace")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
