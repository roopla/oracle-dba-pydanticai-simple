"""FastMCP server exposing approved Oracle DBA tools.

Every tool here is read-only. The one apparent exception,
propose_remediation, executes nothing: it returns a plan that a human
must approve in the chat interface. oracle_core.remediation's
execute_remediation is deliberately NOT exposed - nothing reachable
through this server can change the database.
"""

from typing import Any

from fastmcp import FastMCP

from monitor.storage import get_monitor_summary as query_monitor_summary
from oracle_core.queries import (
    get_active_sessions as query_active_sessions,
    get_blocking_sessions as query_blocking_sessions,
    get_database_identity,
    get_database_role,
    get_database_startup_time,
    get_database_version,
    get_instance_status,
    get_top_sql as query_top_sql,
    get_wait_events as query_wait_events,
    get_tablespace_usage as query_tablespace_usage,
    get_pdb_status,
    list_pdbs as query_list_pdbs,
    list_users as query_list_users,
)
from oracle_core.queries_advanced import (
    get_ash_activity as query_ash_activity,
    get_dataguard_status as query_dataguard_status,
)
from oracle_core.remediation import (
    list_actions as query_remediation_actions,
    plan_remediation as query_plan_remediation,
)


mcp = FastMCP("Simple Oracle DBA MCP")


def _require_first_row(
    rows: list[dict[str, Any]],
    result_name: str,
) -> dict[str, Any]:
    """Return the first query row or raise a clear error."""
    if not rows:
        raise RuntimeError(
            f"Oracle returned no rows for {result_name}"
        )

    return rows[0]


@mcp.tool
def check_db_version(
    pdb_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return the Oracle database version for a validated PDB.

    Args:
        pdb_name: Optional PDB name, such as ORCLPDB1 or ORCLPDB2.
            The name is validated against the live database.
            When omitted, the configured default PDB is used.
            PDB$SEED cannot be used for normal queries.
    """
    return get_database_version(pdb_name)


@mcp.tool
def list_pdbs() -> list[dict[str, Any]]:
    """Return the pluggable databases and their current open states."""
    return query_list_pdbs()


@mcp.tool
def get_database_status(
    pdb_name: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return consolidated CDB, instance, role, startup, and PDB status.

    Describes the present moment only.

    Args:
        pdb_name: Optional PDB name, such as ORCLPDB1 or ORCLPDB2.
            When omitted, the configured default PDB is used.
            The name is validated against the live database.
    """
    return {
        "database_identity": _require_first_row(
            get_database_identity(),
            "database identity",
        ),
        "instance_status": _require_first_row(
            get_instance_status(),
            "instance status",
        ),
        "database_role": _require_first_row(
            get_database_role(),
            "database role",
        ),
        "startup": _require_first_row(
            get_database_startup_time(),
            "database startup time",
        ),
        "pdb_status": _require_first_row(
            get_pdb_status(pdb_name),
            "PDB status",
        ),
    }


@mcp.tool
def get_active_sessions(
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return currently active Oracle user sessions across all containers.

    Describes the present moment only. For activity during a past window,
    use get_ash_activity instead.

    Args:
        limit: Maximum number of sessions to return, from 1 to 100.
    """
    return query_active_sessions(limit=limit)


@mcp.tool
def get_blocking_sessions(
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return current Oracle blocking and waiting session pairs.

    Describes the present moment only. Blocking that has already cleared
    will not appear here; use get_ash_activity for a past window.

    Args:
        limit: Maximum number of blocking relationships to return, 1 to 100.
    """
    return query_blocking_sessions(limit=limit)


@mcp.tool
def get_top_sql(
    limit: int = 10,
    active_within_minutes: int = 60,
) -> list[dict[str, Any]]:
    """Return recently active SQL ranked by cumulative elapsed time.

    Reads the shared pool, so it only sees cursors that are still cached.
    For SQL that was active during a specific past window, use
    get_ash_activity with group_by=SQL_ID.

    sql_text is truncated to 300 characters to keep result sets small.
    The chat interface offers a button to fetch the full text.

    Args:
        limit: Maximum number of SQL statements to return, from 1 to 50.
        active_within_minutes: Only consider SQL active within this many
            minutes, from 1 to 1440.
    """
    return query_top_sql(
        limit=limit,
        active_within_minutes=active_within_minutes,
    )


@mcp.tool
def get_wait_events(
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return top non-idle Oracle wait events by cumulative wait time.

    These are counters accumulated since instance startup, NOT a recent
    window. Do not use this to explain a slowdown that happened at a
    particular time; use get_ash_activity for that.

    Args:
        limit: Maximum number of wait events to return, from 1 to 50.
    """
    return query_wait_events(limit=limit)


@mcp.tool
def get_tablespace_usage(
    pdb_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return Oracle tablespace usage for all containers or one PDB.

    Args:
        pdb_name: Optional PDB name, such as ORCLPDB1 or ORCLPDB2.
            When supplied, the PDB is validated against the live database
            and only that PDB's tablespaces are returned. When omitted,
            usage for all containers is returned.
    """
    return query_tablespace_usage(pdb_name=pdb_name)


@mcp.tool
def list_users(
    pdb_name: str,
) -> list[dict[str, Any]]:
    """Return all database users visible in one validated PDB.

    Args:
        pdb_name: Required PDB name, such as ORCLPDB1 or ORCLPDB2.
            The name is validated against the live PDB list.
    """
    return query_list_users(pdb_name=pdb_name)


@mcp.tool
def get_dataguard_status() -> dict[str, Any]:
    """Return Data Guard health: role, protection mode, lag, and apply status.

    Use this for any question about the standby, replication, failover
    readiness, redo transport, or archive destinations. Also use it when
    asked whether this database is a primary or a standby.

    Reports the current point-in-time state, not history.

    The response includes:
      - database: role, open mode, protection mode and level, and
        switchover status
      - lag: transport lag and apply lag. Only populated when read from
        the standby side.
      - destinations: archive destinations with any error text or gap
      - apply_processes: MRP0 and RFS processes. On a standby, a missing
        MRP0 means redo apply is stopped.
      - standby_redo_logs: empty means real-time apply is not possible
      - sequence_gap: highest received versus highest applied sequence
        per thread

    If the response contains a standby_note, apply lag could not be read
    from the standby. Say so rather than reporting zero lag.
    """
    return query_dataguard_status()


@mcp.tool
def get_ash_activity(
    minutes: int = 30,
    pdb_name: str | None = None,
    group_by: str = "EVENT",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return where database time was spent over a recent window, from ASH.

    Use this to answer "why was the database slow" for a period that has
    already passed, for example the last hour or around a specific time.
    Prefer this over get_wait_events whenever the question refers to a
    window of time rather than the present moment: get_wait_events shows
    cumulative counters since instance startup, while this shows what was
    actually consuming time during the window.

    Each sample is roughly one second of active session time, so
    sample_count approximates seconds of database time, and pct_db_time
    is that group's share of total activity in the window.

    Args:
        minutes: Size of the lookback window, from 1 to 1440. ASH data is
            retained in memory for a limited period; for anything older
            than roughly an hour the data may be incomplete.
        pdb_name: Optional PDB name, such as ORCLPDB1. Validated against
            the live database. Defaults to the configured PDB.
        group_by: One of WAIT_CLASS, EVENT, SQL_ID, or SESSION.
            Start with WAIT_CLASS to see the shape of the problem, then
            EVENT for the specific wait, then SQL_ID to find the
            statement responsible.
        limit: Maximum rows to return, from 1 to 25.

    Any sql_id returned here can be passed to other SQL tools.
    """
    return query_ash_activity(
        minutes=minutes,
        pdb_name=pdb_name,
        group_by=group_by,
        limit=limit,
    )


@mcp.tool
def list_remediation_actions() -> list[dict[str, Any]]:
    """Return the remediation actions that a human can approve.

    These are the ONLY changes that can be made to the database through
    this system. There is no facility to run arbitrary SQL.

    Call this when the user asks what can be fixed automatically, or
    before proposing a remediation, to confirm the action exists.
    """
    return query_remediation_actions()


@mcp.tool
def propose_remediation(
    action: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare a remediation for human approval. Executes NOTHING.

    Returns the exact statements that would run, the target, and a plain
    description of the impact. The chat interface renders this as an
    approve/reject card. The action runs only if a human clicks approve.

    Use this when you have diagnosed a problem that one of the allowlisted
    actions addresses. Do not use it speculatively, and do not propose an
    action the user did not ask about unless the diagnosis clearly calls
    for it.

    Args:
        action: Action name from list_remediation_actions.
        params: Action parameters. Validated against the action's schema;
            an invalid value raises rather than being silently corrected.

    After calling this, tell the user what you found and what the action
    would do. Do not claim it has been done, and do not describe the
    outcome - nothing has run yet.
    """
    return query_plan_remediation(action, params)


@mcp.tool
async def get_monitor_summary(
    include_acknowledged: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    """Return a concise summary of persisted ACTIVE monitor incidents.

    Block 14 uses the stored ACTIVE/RESOLVED lifecycle rather than a recency
    window. Resolved incidents remain in SQLite for history but are not
    included in current incident counts.

    Args:
        include_acknowledged: Include acknowledged issues in the returned
            issue list. Counts always describe all current issues because
            acknowledgment means reviewed, not resolved.
        limit: Maximum number of current issues to return, from 1 to 50.
    """
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")

    return await query_monitor_summary(
        include_acknowledged=include_acknowledged,
        limit=limit,
    )


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="127.0.0.1",
        port=9000,
    )
