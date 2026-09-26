"""Approved Oracle SQL shown with monitor recommendations.

These statements are deterministic and owned by application code, never generated
by the LLM. Verification statements are read-only Oracle SQL. Remediation
statements are explicit templates and must be reviewed by a DBA before execution.

The baked SQL targets Oracle Database 12.2/19c-compatible dictionary and dynamic
performance views used by the monitor itself.
"""

from __future__ import annotations

import re
from typing import Any

from monitor.models import ApprovedAction, ApprovedActionType, IssueType


def _sql_literal(value: str) -> str:
    """Return a safely quoted Oracle string literal for display-only baked SQL."""
    return "'" + value.replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    """Return a safely quoted Oracle identifier for display-only baked SQL."""
    return '"' + value.replace('"', '""') + '"'


def _int_value(details: dict[str, Any], key: str) -> int | None:
    """Return a positive integer detail value when available."""
    value = details.get(key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _tablespace_actions(details: dict[str, Any]) -> list[ApprovedAction]:
    pdb_name = str(details.get("con_name") or "").strip()
    tablespace_name = str(details.get("tablespace_name") or "").strip()

    if not tablespace_name:
        return []

    ts_literal = _sql_literal(tablespace_name.upper())
    ts_identifier = _sql_identifier(tablespace_name.upper())
    location_note = (
        f"Run while connected to PDB {pdb_name}."
        if pdb_name
        else "Run in the affected PDB."
    )

    verify_files_sql = f"""-- {location_note}
SELECT
    file_id,
    file_name,
    ROUND(bytes / 1024 / 1024, 2) AS current_mb,
    autoextensible,
    ROUND(maxbytes / 1024 / 1024, 2) AS max_mb,
    increment_by
FROM dba_data_files
WHERE tablespace_name = {ts_literal}
ORDER BY file_id;"""

    verify_free_sql = f"""-- {location_note}
SELECT
    tablespace_name,
    ROUND(SUM(bytes) / 1024 / 1024, 2) AS free_mb
FROM dba_free_space
WHERE tablespace_name = {ts_literal}
GROUP BY tablespace_name;"""

    add_datafile_template = f"""-- TEMPLATE ONLY: review path, sizes, storage limits, and policy before execution.
-- {location_note}
ALTER TABLESPACE {ts_identifier}
  ADD DATAFILE '<datafile_path>' SIZE <initial_size>
  AUTOEXTEND ON NEXT <next_size> MAXSIZE <max_size>;"""

    return [
        ApprovedAction(
            title="Verify datafile capacity and autoextend",
            action_type=ApprovedActionType.VERIFY,
            sql=verify_files_sql,
            requires_review=False,
        ),
        ApprovedAction(
            title="Verify current free space",
            action_type=ApprovedActionType.VERIFY,
            sql=verify_free_sql,
            requires_review=False,
        ),
        ApprovedAction(
            title="Add capacity if required",
            action_type=ApprovedActionType.REMEDIATION_TEMPLATE,
            sql=add_datafile_template,
            requires_review=True,
        ),
    ]


def _blocking_session_actions(details: dict[str, Any]) -> list[ApprovedAction]:
    blocking_sid = _int_value(details, "blocking_sid")
    blocking_serial = _int_value(details, "blocking_serial")
    waiting_sid = _int_value(details, "waiting_sid")
    if blocking_sid is None:
        return []

    blocking_filter = f"AND blocker.sid = {blocking_sid}"
    waiting_filter = f"\n  AND waiter.sid = {waiting_sid}" if waiting_sid is not None else ""

    verify_chain_sql = f"""SELECT
    blocker.sid AS blocking_sid,
    blocker.serial# AS blocking_serial,
    blocker.username AS blocking_user,
    blocker.sql_id AS blocking_sql_id,
    blocker.module AS blocking_module,
    waiter.sid AS waiting_sid,
    waiter.serial# AS waiting_serial,
    waiter.username AS waiting_user,
    waiter.sql_id AS waiting_sql_id,
    waiter.event AS waiting_event,
    waiter.wait_class AS waiting_wait_class,
    waiter.seconds_in_wait AS waiting_seconds,
    c.name AS con_name
FROM v$session waiter
JOIN v$session blocker
  ON blocker.sid = waiter.blocking_session
LEFT JOIN v$containers c
  ON c.con_id = waiter.con_id
WHERE waiter.blocking_session IS NOT NULL
  {blocking_filter}{waiting_filter}
ORDER BY waiter.seconds_in_wait DESC;"""

    blocker_sql = f"""SELECT
    s.sid,
    s.serial#,
    s.username,
    s.status,
    s.module,
    s.machine,
    s.sql_id,
    q.sql_text
FROM v$session s
LEFT JOIN v$sql q
  ON q.sql_id = s.sql_id
WHERE s.sid = {blocking_sid};"""

    actions = [
        ApprovedAction(
            title="Verify the blocking chain",
            action_type=ApprovedActionType.VERIFY,
            sql=verify_chain_sql,
            requires_review=False,
        ),
        ApprovedAction(
            title="Inspect the blocker and its SQL",
            action_type=ApprovedActionType.VERIFY,
            sql=blocker_sql,
            requires_review=False,
        ),
    ]

    if blocking_serial is not None:
        kill_template = f"""-- TEMPLATE ONLY: verify business impact, transaction owner, and application policy first.
ALTER SYSTEM KILL SESSION '{blocking_sid},{blocking_serial}' IMMEDIATE;"""
        actions.append(
            ApprovedAction(
                title="Terminate the blocker only after DBA review",
                action_type=ApprovedActionType.REMEDIATION_TEMPLATE,
                sql=kill_template,
                requires_review=True,
            )
        )

    return actions


def _long_running_query_actions(details: dict[str, Any]) -> list[ApprovedAction]:
    sid = _int_value(details, "sid")
    serial_num = _int_value(details, "serial_num")
    sql_id = str(details.get("sql_id") or "").strip()
    if sid is None:
        return []

    session_sql = f"""SELECT
    s.sid,
    s.serial#,
    s.username,
    s.status,
    s.sql_id,
    s.last_call_et AS seconds_running,
    s.event,
    s.wait_class,
    s.module,
    s.machine,
    c.name AS con_name
FROM v$session s
LEFT JOIN v$containers c
  ON c.con_id = s.con_id
WHERE s.sid = {sid};"""

    actions = [
        ApprovedAction(
            title="Inspect the long-running session",
            action_type=ApprovedActionType.VERIFY,
            sql=session_sql,
            requires_review=False,
        )
    ]

    if sql_id:
        sql_literal = _sql_literal(sql_id)
        statement_sql = f"""SELECT
    sql_id,
    SUBSTR(sql_text, 1, 1000) AS sql_text,
    executions,
    parse_calls,
    buffer_gets,
    disk_reads,
    rows_processed,
    ROUND(elapsed_time / 1000000, 3) AS elapsed_seconds,
    ROUND(cpu_time / 1000000, 3) AS cpu_seconds,
    last_active_time
FROM v$sql
WHERE sql_id = {sql_literal}
ORDER BY last_active_time DESC;"""
        actions.append(
            ApprovedAction(
                title="Inspect the SQL statement",
                action_type=ApprovedActionType.VERIFY,
                sql=statement_sql,
                requires_review=False,
            )
        )

    if serial_num is not None:
        cancel_template = f"""-- TEMPLATE ONLY: review the session owner and application impact first.
ALTER SYSTEM KILL SESSION '{sid},{serial_num}' IMMEDIATE;"""
        actions.append(
            ApprovedAction(
                title="Terminate the session only after DBA review",
                action_type=ApprovedActionType.REMEDIATION_TEMPLATE,
                sql=cancel_template,
                requires_review=True,
            )
        )

    return actions


def _alert_log_error_actions(details: dict[str, Any]) -> list[ApprovedAction]:
    message = str(details.get("message_text") or "")
    match = re.search(r"ORA-\d{5}", message)
    ora_code = match.group(0) if match else None

    if ora_code:
        code_predicate = f"AND INSTR(message_text, {_sql_literal(ora_code)}) > 0"
    else:
        code_predicate = "AND REGEXP_LIKE(message_text, 'ORA-[0-9]+')"

    alert_sql = f"""SELECT *
FROM (
    SELECT
        TO_CHAR(originating_timestamp, 'YYYY-MM-DD HH24:MI:SS') AS logged_at,
        message_text
    FROM v$diag_alert_ext
    WHERE originating_timestamp > SYSTIMESTAMP - INTERVAL '1' HOUR
      {code_predicate}
    ORDER BY originating_timestamp DESC
)
WHERE ROWNUM <= 50;"""

    incident_sql = """SELECT
    instance_name,
    status,
    database_status,
    startup_time
FROM v$instance;"""

    return [
        ApprovedAction(
            title="Review recent matching alert-log errors",
            action_type=ApprovedActionType.VERIFY,
            sql=alert_sql,
            requires_review=False,
        ),
        ApprovedAction(
            title="Verify current instance status",
            action_type=ApprovedActionType.VERIFY,
            sql=incident_sql,
            requires_review=False,
        ),
    ]


def _wait_event_actions(details: dict[str, Any]) -> list[ApprovedAction]:
    event = str(details.get("event") or "").strip()
    if not event:
        return []

    event_literal = _sql_literal(event)
    event_sql = f"""SELECT
    event,
    wait_class,
    total_waits,
    ROUND(time_waited_micro / 1000000, 3) AS time_waited_seconds,
    ROUND(
        time_waited_micro / NULLIF(total_waits, 0) / 1000,
        3
    ) AS avg_wait_ms
FROM v$system_event
WHERE event = {event_literal};"""

    sessions_sql = f"""SELECT
    s.sid,
    s.serial#,
    s.username,
    s.status,
    s.sql_id,
    s.event,
    s.wait_class,
    s.seconds_in_wait,
    s.module,
    s.machine,
    c.name AS con_name
FROM v$session s
LEFT JOIN v$containers c
  ON c.con_id = s.con_id
WHERE s.type = 'USER'
  AND s.event = {event_literal}
ORDER BY s.seconds_in_wait DESC;"""

    return [
        ApprovedAction(
            title="Verify cumulative statistics for this wait event",
            action_type=ApprovedActionType.VERIFY,
            sql=event_sql,
            requires_review=False,
        ),
        ApprovedAction(
            title="Find user sessions currently waiting on this event",
            action_type=ApprovedActionType.VERIFY,
            sql=sessions_sql,
            requires_review=False,
        ),
    ]


def _write_workload_actions(details: dict[str, Any]) -> list[ApprovedAction]:
    counters_sql = """SELECT name, value
FROM v$sysstat
WHERE name IN (
    'user commits',
    'redo size',
    'execute count',
    'user calls'
)
ORDER BY name;"""

    top_dml_sql = """SELECT *
FROM (
    SELECT
        sql_id,
        SUBSTR(sql_text, 1, 300) AS sql_text,
        executions,
        parse_calls,
        buffer_gets,
        disk_reads,
        rows_processed,
        ROUND(elapsed_time / 1000000, 3) AS elapsed_seconds,
        ROUND(cpu_time / 1000000, 3) AS cpu_seconds,
        last_active_time
    FROM v$sql
    WHERE command_type IN (2, 3, 6)
      AND last_active_time >= SYSDATE - (60 / 1440)
    ORDER BY executions DESC
)
WHERE ROWNUM <= 20;"""

    commit_wait_sql = """SELECT
    event,
    total_waits,
    ROUND(time_waited_micro / 1000000, 3) AS time_waited_seconds,
    ROUND(
        time_waited_micro / NULLIF(total_waits, 0) / 1000,
        3
    ) AS avg_wait_ms
FROM v$system_event
WHERE event = 'log file sync';"""

    log_switch_sql = """SELECT
    COUNT(*) AS switches_last_hour
FROM v$log_history
WHERE first_time > SYSDATE - (1 / 24);"""

    return [
        ApprovedAction(
            title="Verify write-workload counters",
            action_type=ApprovedActionType.VERIFY,
            sql=counters_sql,
            requires_review=False,
        ),
        ApprovedAction(
            title="Identify recently active DML statements",
            action_type=ApprovedActionType.VERIFY,
            sql=top_dml_sql,
            requires_review=False,
        ),
        ApprovedAction(
            title="Check commit latency",
            action_type=ApprovedActionType.VERIFY,
            sql=commit_wait_sql,
            requires_review=False,
        ),
        ApprovedAction(
            title="Check redo log switch frequency",
            action_type=ApprovedActionType.VERIFY,
            sql=log_switch_sql,
            requires_review=False,
        ),
    ]


_ACTION_BUILDERS = {
    IssueType.TABLESPACE_USAGE: _tablespace_actions,
    IssueType.BLOCKING_SESSION: _blocking_session_actions,
    IssueType.LONG_RUNNING_QUERY: _long_running_query_actions,
    IssueType.ALERT_LOG_ERROR: _alert_log_error_actions,
    IssueType.WAIT_EVENT: _wait_event_actions,
    IssueType.WRITE_WORKLOAD: _write_workload_actions,
}


def get_approved_actions(
    issue_type: IssueType | str,
    details: dict[str, Any],
) -> list[ApprovedAction]:
    """Return deterministic Oracle-compatible approved SQL for an incident."""
    normalized = IssueType(issue_type)
    builder = _ACTION_BUILDERS.get(normalized)
    return builder(details) if builder is not None else []
