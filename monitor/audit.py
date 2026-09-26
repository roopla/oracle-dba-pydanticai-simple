"""Audit trail for remediation actions.

Save as monitor/audit.py.

Kept separate from monitor/storage.py so the existing issue lifecycle code
is untouched. It writes to the same SQLite file, so /monitor and the chat
UI share one database.

Every approved execution is recorded, whether it succeeded or not. A
remediation that ran and failed is exactly the thing you want in the log.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from monitor.config import get_monitor_settings


_SCHEMA = """
CREATE TABLE IF NOT EXISTS remediation_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    params TEXT NOT NULL,
    target TEXT NOT NULL,
    statements TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT NOT NULL,
    result TEXT,
    error TEXT
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_audit_requested_at "
    "ON remediation_audit(requested_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON remediation_audit(action)",
)


def _connect() -> sqlite3.Connection:
    database_path = Path(get_monitor_settings().monitor_sqlite_path).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(database_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)

    for index_sql in _INDEXES:
        conn.execute(index_sql)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_sync(
    action: str,
    params: dict[str, Any],
    target: str,
    statements: list[str],
    approved_by: str,
    requested_at: str,
    outcome: str,
    result: dict[str, Any] | None,
    error: str | None,
) -> int:
    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)
        cursor = conn.execute(
            """INSERT INTO remediation_audit (
                   action, params, target, statements, approved_by,
                   requested_at, completed_at, outcome, result, error
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                action,
                json.dumps(params, default=str, sort_keys=True),
                target,
                json.dumps(statements, default=str),
                approved_by,
                requested_at,
                _now(),
                outcome,
                json.dumps(result, default=str) if result is not None else None,
                error,
            ),
        )
        return int(cursor.lastrowid)


async def record_remediation(
    *,
    action: str,
    params: dict[str, Any],
    target: str,
    statements: list[str],
    approved_by: str,
    requested_at: str,
    outcome: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> int:
    """Write one audit row. outcome is APPROVED, REJECTED, or FAILED."""
    return await asyncio.to_thread(
        _record_sync,
        action,
        params,
        target,
        statements,
        approved_by,
        requested_at,
        outcome,
        result,
        error,
    )


def _recent_sync(limit: int) -> list[dict[str, Any]]:
    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM remediation_audit "
            "ORDER BY requested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


async def recent_remediations(limit: int = 20) -> list[dict[str, Any]]:
    """Most recent audit entries, newest first."""
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")

    return await asyncio.to_thread(_recent_sync, limit)


def now_iso() -> str:
    """Timestamp helper so callers do not import datetime themselves."""
    return _now()
