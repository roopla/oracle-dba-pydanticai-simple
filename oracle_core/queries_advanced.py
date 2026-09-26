"""Data Guard and ASH queries.

Save as oracle_core/queries_advanced.py.

* get_dataguard_status()  - role, protection mode, transport/apply lag,
                            destination errors, and MRP health.
* get_ash_activity()      - Active Session History rollup. REQUIRES the
                            Oracle Diagnostics Pack licence.

Reading the standby
-------------------
Apply lag is only meaningful when read from the standby, and the standby
usually sits on a different host or port from the primary, so build_dsn()
cannot reach it. Set these in .env.mcp:

    ORACLE_STANDBY_DSN=oracle19c-stby:1521/ORCLCDB_STBY.localdomain
    ORACLE_STANDBY_USER=sys
    ORACLE_STANDBY_PASSWORD=your_sys_password

A MOUNTED standby is not open, so ordinary sessions are rejected with
ORA-01033. The connection is therefore made as SYSDBA, which needs the
password file that was copied during the standby build. If the SYS
password changes on the primary, copy the password file again.

Without ORACLE_STANDBY_DSN the tool still works, but reports primary-side
data only and says so through standby_note - deliberately, so the agent
never reports zero apply lag it did not actually measure.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from oracle_core.config import get_settings
from oracle_core.db import query, query_dsn
from oracle_core.queries import validate_pdb_name


# Hard caps. Everything returned here lands in the agent's conversation
# history and is resent on every subsequent turn.
MAX_ASH_ROWS = 25
MAX_ASH_MINUTES = 1440
MAX_DEST_ROWS = 20

ASH_GROUP_BY_CHOICES = ("WAIT_CLASS", "EVENT", "SQL_ID", "SESSION")


# An executor takes (sql, binds) and returns rows. This lets the same
# Data Guard queries run against the primary (via build_dsn) or the
# standby (via an explicit SYSDBA DSN).
Executor = Callable[[str, dict[str, Any] | None], list[dict[str, Any]]]


# --------------------------------------------------------------------------
# Data Guard
# --------------------------------------------------------------------------

SQL_DB_ROLE = """
    SELECT
        name,
        db_unique_name,
        database_role,
        open_mode,
        protection_mode,
        protection_level,
        switchover_status,
        log_mode,
        force_logging,
        flashback_on
    FROM v$database
"""

SQL_DG_LAG = """
    SELECT
        name,
        value,
        unit,
        time_computed,
        datum_time
    FROM v$dataguard_stats
    WHERE name IN ('transport lag', 'apply lag', 'apply finish time')
"""

SQL_DEST_STATUS = """
    SELECT
        dest_id,
        dest_name,
        status,
        type,
        database_mode,
        recovery_mode,
        protection_mode,
        destination,
        gap_status,
        archived_seq#  AS archived_seq,
        applied_seq#   AS applied_seq,
        error
    FROM v$archive_dest_status
    WHERE status <> 'INACTIVE'
    ORDER BY dest_id
    FETCH FIRST :row_limit ROWS ONLY
"""

SQL_APPLY_PROCESSES = """
    SELECT
        process,
        status,
        thread#   AS thread_num,
        sequence# AS sequence_num,
        block#    AS block_num
    FROM v$managed_standby
    WHERE process IN ('MRP0', 'RFS', 'ARCH')
    ORDER BY process
"""

SQL_STANDBY_REDO = """
    SELECT
        group#          AS group_num,
        thread#         AS thread_num,
        bytes/1024/1024 AS size_mb,
        status,
        archived
    FROM v$standby_log
    ORDER BY group#
"""

SQL_SEQUENCE_GAP = """
    SELECT
        thread#                                           AS thread_num,
        MAX(sequence#)                                    AS last_received_seq,
        MAX(CASE WHEN applied = 'YES' THEN sequence# END) AS last_applied_seq,
        MAX(sequence#)
          - NVL(MAX(CASE WHEN applied = 'YES' THEN sequence# END), 0)
                                                          AS sequence_gap
    FROM v$archived_log
    WHERE resetlogs_change# = (SELECT resetlogs_change# FROM v$database)
    GROUP BY thread#
    ORDER BY thread#
"""


def _collect_dg(execute: Executor, label: str) -> dict[str, Any]:
    """Gather every Data Guard signal available from one connection.

    Each section is isolated: a view that is unavailable or unpopulated
    must not take down the whole report.
    """
    result: dict[str, Any] = {"connected_to": label}
    errors: dict[str, str] = {}

    sections: list[tuple[str, str, dict[str, Any] | None, bool]] = [
        ("database", SQL_DB_ROLE, None, True),
        ("lag", SQL_DG_LAG, None, False),
        ("destinations", SQL_DEST_STATUS, {"row_limit": MAX_DEST_ROWS}, False),
        ("apply_processes", SQL_APPLY_PROCESSES, None, False),
        ("standby_redo_logs", SQL_STANDBY_REDO, None, False),
        ("sequence_gap", SQL_SEQUENCE_GAP, None, False),
    ]

    for key, sql, binds, single_row in sections:
        try:
            rows = execute(sql, binds)
        except Exception as exc:  # noqa: BLE001 - report, do not abort
            errors[key] = f"{type(exc).__name__}: {exc}"
            continue

        result[key] = (rows[0] if rows else None) if single_row else rows

    if errors:
        result["errors"] = errors

    return result


def _primary_executor(database_name: str) -> Executor:
    def execute(sql: str, binds: dict[str, Any] | None) -> list[dict[str, Any]]:
        return query(sql, database_name=database_name, binds=binds)

    return execute


def _standby_executor(dsn: str, user: str, password: str) -> Executor:
    def execute(sql: str, binds: dict[str, Any] | None) -> list[dict[str, Any]]:
        return query_dsn(
            sql,
            dsn=dsn,
            user=user,
            password=password,
            binds=binds,
            sysdba=True,
        )

    return execute


def get_dataguard_status() -> dict[str, Any]:
    """Return Data Guard role, lag, destination health, and apply status."""
    settings = get_settings()

    report: dict[str, Any] = {
        "primary": _collect_dg(
            _primary_executor(settings.oracle_cdb_name),
            settings.oracle_cdb_name,
        ),
    }

    standby_dsn = (os.environ.get("ORACLE_STANDBY_DSN") or "").strip()
    standby_user = (os.environ.get("ORACLE_STANDBY_USER") or "sys").strip()
    standby_password = os.environ.get("ORACLE_STANDBY_PASSWORD") or ""

    if not standby_dsn:
        report["standby"] = None
        report["standby_note"] = (
            "ORACLE_STANDBY_DSN is not set, so apply lag was not read from "
            "the standby. The values above are primary-side only and do not "
            "confirm that redo is being applied."
        )
        return report

    if not standby_password:
        report["standby"] = None
        report["standby_note"] = (
            "ORACLE_STANDBY_DSN is set but ORACLE_STANDBY_PASSWORD is not. "
            "A mounted standby requires a SYSDBA connection, so apply lag "
            "could not be read."
        )
        return report

    report["standby"] = _collect_dg(
        _standby_executor(standby_dsn, standby_user, standby_password),
        standby_dsn,
    )

    return report


# --------------------------------------------------------------------------
# ASH (Active Session History) - requires Diagnostics Pack
# --------------------------------------------------------------------------


def _validated_pdb(pdb_name: str | None) -> str:
    settings = get_settings()
    requested = pdb_name or settings.oracle_default_pdb_name
    return validate_pdb_name(requested)


def get_ash_activity(
    minutes: int = 30,
    pdb_name: str | None = None,
    group_by: str = "EVENT",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return where database time was actually spent over a recent window.

    Requires the Oracle Diagnostics Pack licence.

    Each ASH sample represents roughly one second of active session time,
    so sample_count approximates seconds of DB time.
    """
    if minutes < 1 or minutes > MAX_ASH_MINUTES:
        raise ValueError(f"minutes must be between 1 and {MAX_ASH_MINUTES}")

    if limit < 1 or limit > MAX_ASH_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_ASH_ROWS}")

    grouping = group_by.strip().upper()

    if grouping not in ASH_GROUP_BY_CHOICES:
        raise ValueError(
            f"group_by must be one of {', '.join(ASH_GROUP_BY_CHOICES)}"
        )

    validated_pdb_name = _validated_pdb(pdb_name)

    window = "sample_time >= SYSTIMESTAMP - NUMTODSINTERVAL(:minutes, 'MINUTE')"

    if grouping == "WAIT_CLASS":
        select_sql = f"""
            SELECT
                NVL(wait_class, 'CPU')                  AS wait_class,
                COUNT(*)                                AS sample_count,
                ROUND(COUNT(*) * 100
                      / SUM(COUNT(*)) OVER (), 1)       AS pct_db_time,
                COUNT(DISTINCT session_id)              AS distinct_sessions
            FROM v$active_session_history
            WHERE {window}
            GROUP BY NVL(wait_class, 'CPU')
            ORDER BY sample_count DESC
            FETCH FIRST :row_limit ROWS ONLY
        """
    elif grouping == "EVENT":
        select_sql = f"""
            SELECT
                NVL(wait_class, 'CPU')                       AS wait_class,
                NVL(event, 'CPU + Wait for CPU')             AS event,
                COUNT(*)                                     AS sample_count,
                ROUND(COUNT(*) * 100
                      / SUM(COUNT(*)) OVER (), 1)            AS pct_db_time,
                COUNT(DISTINCT session_id)                   AS distinct_sessions
            FROM v$active_session_history
            WHERE {window}
            GROUP BY NVL(wait_class, 'CPU'),
                     NVL(event, 'CPU + Wait for CPU')
            ORDER BY sample_count DESC
            FETCH FIRST :row_limit ROWS ONLY
        """
    elif grouping == "SQL_ID":
        select_sql = f"""
            SELECT
                sql_id,
                COUNT(*)                                     AS sample_count,
                ROUND(COUNT(*) * 100
                      / SUM(COUNT(*)) OVER (), 1)            AS pct_db_time,
                COUNT(DISTINCT session_id)                   AS distinct_sessions,
                MAX(NVL(wait_class, 'CPU'))                  AS sample_wait_class,
                MAX(sql_plan_hash_value)                     AS plan_hash_value
            FROM v$active_session_history
            WHERE {window}
              AND sql_id IS NOT NULL
            GROUP BY sql_id
            ORDER BY sample_count DESC
            FETCH FIRST :row_limit ROWS ONLY
        """
    else:  # SESSION
        select_sql = f"""
            SELECT
                session_id,
                session_serial#                              AS session_serial,
                MAX(NVL(program, 'unknown'))                 AS program,
                MAX(NVL(machine, 'unknown'))                 AS machine,
                COUNT(*)                                     AS sample_count,
                ROUND(COUNT(*) * 100
                      / SUM(COUNT(*)) OVER (), 1)            AS pct_db_time,
                MAX(sql_id)                                  AS sample_sql_id
            FROM v$active_session_history
            WHERE {window}
            GROUP BY session_id, session_serial#
            ORDER BY sample_count DESC
            FETCH FIRST :row_limit ROWS ONLY
        """

    return query(
        select_sql,
        database_name=validated_pdb_name,
        binds={"minutes": minutes, "row_limit": limit},
    )
