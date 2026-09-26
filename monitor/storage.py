"""SQLite persistence, lifecycle transitions, and incident filtering.

The migration is additive: Block 14 columns and indexes are added in place,
legacy rows are backfilled, and existing issue/acknowledgment data is retained.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from monitor.config import get_monitor_settings
from monitor.actions import get_approved_actions
from monitor.models import (
    DetectedIssue,
    IssueRecord,
    IssueStatus,
    IssueType,
    Recommendation,
    Severity,
    SortOrder,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    summary TEXT NOT NULL,
    details TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    recommendation_updated_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    episode_count INTEGER NOT NULL DEFAULT 1,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_by TEXT,
    acknowledged_at TEXT
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_issues_status_last_seen ON issues(status, last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_issues_acknowledged ON issues(acknowledged)",
    "CREATE INDEX IF NOT EXISTS idx_issues_severity ON issues(severity)",
    "CREATE INDEX IF NOT EXISTS idx_issues_issue_type ON issues(issue_type)",
)


def _connect() -> sqlite3.Connection:
    database_path = Path(get_monitor_settings().monitor_sqlite_path).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(database_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _column_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(issues)").fetchall()
    }


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the schema without dropping or replacing issue rows."""
    conn.execute(_SCHEMA)
    columns = _column_names(conn)

    additions = {
        "status": "TEXT NOT NULL DEFAULT 'ACTIVE'",
        "resolved_at": "TEXT",
        "recommendation_updated_at": "TEXT",
        "observation_count": "INTEGER NOT NULL DEFAULT 1",
        "episode_count": "INTEGER NOT NULL DEFAULT 1",
        "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
    }

    for column_name, definition in additions.items():
        if column_name not in columns:
            conn.execute(
                f"ALTER TABLE issues ADD COLUMN {column_name} {definition}"
            )

    columns = _column_names(conn)
    if "occurrence_count" in columns:
        conn.execute(
            """UPDATE issues
               SET observation_count = MAX(
                       COALESCE(observation_count, 1),
                       COALESCE(occurrence_count, 1)
                   ),
                   occurrence_count = MAX(
                       COALESCE(observation_count, 1),
                       COALESCE(occurrence_count, 1)
                   )"""
        )

    conn.execute(
        """UPDATE issues
           SET status = CASE
                   WHEN status = 'RESOLVED' THEN 'RESOLVED'
                   ELSE 'ACTIVE'
               END,
               recommendation_updated_at = COALESCE(
                   recommendation_updated_at,
                   last_seen_at,
                   detected_at
               ),
               observation_count = COALESCE(observation_count, 1),
               episode_count = COALESCE(episode_count, 1),
               occurrence_count = COALESCE(occurrence_count, observation_count, 1)"""
    )

    for statement in _INDEXES:
        conn.execute(statement)


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_record(row: sqlite3.Row) -> IssueRecord:
    keys = set(row.keys())
    observation_count = int(
        row["observation_count"]
        if "observation_count" in keys
        else row["occurrence_count"]
    )
    occurrence_count = int(
        row["occurrence_count"]
        if "occurrence_count" in keys
        else observation_count
    )
    recommendation_updated_at = (
        row["recommendation_updated_at"]
        if "recommendation_updated_at" in keys
        else None
    ) or row["last_seen_at"] or row["detected_at"]

    return IssueRecord(
        id=row["id"],
        fingerprint=row["fingerprint"],
        issue_type=row["issue_type"],
        severity=row["severity"],
        status=row["status"] if "status" in keys else IssueStatus.ACTIVE.value,
        summary=row["summary"],
        details=json.loads(row["details"]),
        recommendation=Recommendation(**json.loads(row["recommendation"])),
        approved_actions=get_approved_actions(
            row["issue_type"],
            json.loads(row["details"]),
        ),
        detected_at=datetime.fromisoformat(row["detected_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        resolved_at=(
            _parse_datetime(row["resolved_at"])
            if "resolved_at" in keys
            else None
        ),
        recommendation_updated_at=datetime.fromisoformat(
            recommendation_updated_at
        ),
        observation_count=observation_count,
        episode_count=int(row["episode_count"] if "episode_count" in keys else 1),
        occurrence_count=occurrence_count,
        acknowledged=bool(row["acknowledged"]),
        acknowledged_by=row["acknowledged_by"],
        acknowledged_at=_parse_datetime(row["acknowledged_at"]),
    )


# --- synchronous core (runs in worker threads) ------------------------------


def _init_db_sync() -> None:
    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)


def _get_issue_by_fingerprint_sync(fingerprint: str) -> Optional[IssueRecord]:
    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM issues WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        return _row_to_record(row) if row else None


def _record_issue_observation_sync(
    issue: DetectedIssue,
    recommendation: Recommendation | None,
) -> IssueRecord:
    fingerprint = issue.fingerprint()
    observed_at = issue.detected_at.isoformat()
    details_json = json.dumps(issue.details, default=str, sort_keys=True)

    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM issues WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()

        if row is None:
            if recommendation is None:
                raise ValueError("A new issue requires a recommendation")

            conn.execute(
                """INSERT INTO issues (
                       fingerprint, issue_type, severity, status, summary,
                       details, recommendation, detected_at, last_seen_at,
                       resolved_at, recommendation_updated_at,
                       observation_count, episode_count, occurrence_count,
                       acknowledged, acknowledged_by, acknowledged_at
                   ) VALUES (
                       ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, NULL, ?,
                       1, 1, 1, 0, NULL, NULL
                   )""",
                (
                    fingerprint,
                    issue.issue_type.value,
                    issue.severity.value,
                    issue.summary,
                    details_json,
                    recommendation.model_dump_json(),
                    observed_at,
                    observed_at,
                    observed_at,
                ),
            )
        else:
            existing = _row_to_record(row)
            reopening = existing.status == IssueStatus.RESOLVED
            severity_changed = existing.severity != issue.severity
            needs_recommendation = reopening or severity_changed

            if needs_recommendation and recommendation is None:
                reason = "reopened" if reopening else "severity-changed"
                raise ValueError(f"A {reason} issue requires a recommendation")

            if needs_recommendation:
                recommendation_json = recommendation.model_dump_json()
                recommendation_updated_at = observed_at
            else:
                # Continuous issues always reuse their stored recommendation.
                recommendation_json = existing.recommendation.model_dump_json()
                recommendation_updated_at = (
                    existing.recommendation_updated_at.isoformat()
                )

            conn.execute(
                """UPDATE issues
                   SET issue_type = ?,
                       severity = ?,
                       status = 'ACTIVE',
                       summary = ?,
                       details = ?,
                       recommendation = ?,
                       last_seen_at = ?,
                       resolved_at = NULL,
                       recommendation_updated_at = ?,
                       observation_count = observation_count + 1,
                       occurrence_count = occurrence_count + 1,
                       episode_count = episode_count + ?,
                       acknowledged = CASE WHEN ? = 1 THEN 0 ELSE acknowledged END,
                       acknowledged_by = CASE WHEN ? = 1 THEN NULL ELSE acknowledged_by END,
                       acknowledged_at = CASE WHEN ? = 1 THEN NULL ELSE acknowledged_at END
                   WHERE fingerprint = ?""",
                (
                    issue.issue_type.value,
                    issue.severity.value,
                    issue.summary,
                    details_json,
                    recommendation_json,
                    observed_at,
                    recommendation_updated_at,
                    1 if reopening else 0,
                    1 if reopening else 0,
                    1 if reopening else 0,
                    1 if reopening else 0,
                    fingerprint,
                ),
            )

        updated = conn.execute(
            "SELECT * FROM issues WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if updated is None:
            raise RuntimeError("Issue observation was not persisted")
        return _row_to_record(updated)


def _resolve_missing_issues_sync(
    active_fingerprints: set[str],
    successful_issue_types: set[IssueType],
    resolved_at: datetime,
) -> int:
    if not successful_issue_types:
        return 0

    type_values = sorted(issue_type.value for issue_type in successful_issue_types)
    type_placeholders = ",".join("?" for _ in type_values)

    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"""SELECT id, fingerprint
                FROM issues
                WHERE status = 'ACTIVE'
                  AND issue_type IN ({type_placeholders})""",
            type_values,
        ).fetchall()

        ids_to_resolve = [
            int(row["id"])
            for row in rows
            if row["fingerprint"] not in active_fingerprints
        ]
        if not ids_to_resolve:
            return 0

        id_placeholders = ",".join("?" for _ in ids_to_resolve)
        conn.execute(
            f"""UPDATE issues
                SET status = 'RESOLVED', resolved_at = ?
                WHERE id IN ({id_placeholders})""",
            (resolved_at.isoformat(), *ids_to_resolve),
        )
        return len(ids_to_resolve)


def _list_issues_sync(
    status: IssueStatus | None,
    acknowledged: bool | None,
    severity: Severity | None,
    issue_type: IssueType | None,
    search: str | None,
    sort_order: SortOrder,
    limit: int,
) -> List[IssueRecord]:
    predicates: list[str] = []
    params: list[object] = []

    if status is not None:
        predicates.append("status = ?")
        params.append(status.value)
    if acknowledged is not None:
        predicates.append("acknowledged = ?")
        params.append(1 if acknowledged else 0)
    if severity is not None:
        predicates.append("severity = ?")
        params.append(severity.value)
    if issue_type is not None:
        predicates.append("issue_type = ?")
        params.append(issue_type.value)

    normalized_search = (search or "").strip().lower()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        predicates.append(
            """(
                LOWER(summary) LIKE ? OR
                LOWER(details) LIKE ? OR
                LOWER(recommendation) LIKE ? OR
                LOWER(fingerprint) LIKE ?
            )"""
        )
        params.extend([pattern, pattern, pattern, pattern])

    direction = "DESC" if sort_order == SortOrder.NEWEST else "ASC"
    sql = "SELECT * FROM issues"
    if predicates:
        sql += " WHERE " + " AND ".join(predicates)
    sql += f" ORDER BY last_seen_at {direction}, id {direction} LIMIT ?"
    params.append(limit)

    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_record(row) for row in rows]


def _get_issue_sync(issue_id: int) -> Optional[IssueRecord]:
    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM issues WHERE id = ?",
            (issue_id,),
        ).fetchone()
        return _row_to_record(row) if row else None


def _acknowledge_issue_sync(
    issue_id: int,
    acknowledged_by: str,
) -> Optional[IssueRecord]:
    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)
        conn.execute(
            """UPDATE issues
               SET acknowledged = 1,
                   acknowledged_by = ?,
                   acknowledged_at = ?
               WHERE id = ? AND status = 'ACTIVE'""",
            (
                acknowledged_by,
                datetime.now(timezone.utc).isoformat(),
                issue_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM issues WHERE id = ?",
            (issue_id,),
        ).fetchone()
        return _row_to_record(row) if row else None


# --- async facade ------------------------------------------------------------


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


async def get_issue_by_fingerprint(
    fingerprint: str,
) -> Optional[IssueRecord]:
    return await asyncio.to_thread(
        _get_issue_by_fingerprint_sync,
        fingerprint,
    )


async def record_issue_observation(
    issue: DetectedIssue,
    recommendation: Recommendation | None,
) -> IssueRecord:
    return await asyncio.to_thread(
        _record_issue_observation_sync,
        issue,
        recommendation,
    )


async def upsert_issue(
    issue: DetectedIssue,
    recommendation: Recommendation,
) -> None:
    """Backward-compatible Block 13 entry point using Block 14 semantics."""
    await record_issue_observation(issue, recommendation)


async def resolve_missing_issues(
    active_fingerprints: Iterable[str],
    successful_issue_types: Iterable[IssueType],
    resolved_at: datetime | None = None,
) -> int:
    return await asyncio.to_thread(
        _resolve_missing_issues_sync,
        set(active_fingerprints),
        set(successful_issue_types),
        resolved_at or datetime.now(timezone.utc),
    )


async def list_issues(
    *,
    status: IssueStatus | None = IssueStatus.ACTIVE,
    acknowledged: bool | None = None,
    severity: Severity | None = None,
    issue_type: IssueType | None = None,
    search: str | None = None,
    sort_order: SortOrder = SortOrder.NEWEST,
    limit: int = 100,
) -> List[IssueRecord]:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")

    return await asyncio.to_thread(
        _list_issues_sync,
        status,
        acknowledged,
        severity,
        issue_type,
        search,
        sort_order,
        limit,
    )


async def get_issue(issue_id: int) -> Optional[IssueRecord]:
    return await asyncio.to_thread(_get_issue_sync, issue_id)


async def acknowledge_issue(
    issue_id: int,
    acknowledged_by: str,
) -> Optional[IssueRecord]:
    return await asyncio.to_thread(
        _acknowledge_issue_sync,
        issue_id,
        acknowledged_by,
    )


# --- monitor summary ---------------------------------------------------------


def _severity_rank(record: IssueRecord) -> int:
    """Sort CRITICAL before WARNING before INFO."""
    return {
        Severity.CRITICAL: 3,
        Severity.WARNING: 2,
        Severity.INFO: 1,
    }.get(record.severity, 0)


def _record_to_summary(record: IssueRecord) -> dict:
    return {
        "id": record.id,
        "fingerprint": record.fingerprint,
        "issue_type": record.issue_type.value,
        "severity": record.severity.value,
        "status": record.status.value,
        "summary": record.summary,
        "details": record.details,
        "recommendation": record.recommendation.model_dump(mode="json"),
        "approved_actions": [
            action.model_dump(mode="json") for action in record.approved_actions
        ],
        "detected_at": record.detected_at.isoformat(),
        "last_seen_at": record.last_seen_at.isoformat(),
        "resolved_at": (
            record.resolved_at.isoformat() if record.resolved_at else None
        ),
        "recommendation_updated_at": (
            record.recommendation_updated_at.isoformat()
        ),
        "observation_count": record.observation_count,
        "episode_count": record.episode_count,
        "occurrence_count": record.occurrence_count,
        "acknowledged": record.acknowledged,
        "acknowledged_by": record.acknowledged_by,
        "acknowledged_at": (
            record.acknowledged_at.isoformat()
            if record.acknowledged_at
            else None
        ),
    }


def _get_monitor_summary_sync(
    include_acknowledged: bool,
    limit: int,
) -> dict:
    settings = get_monitor_settings()
    now = datetime.now(timezone.utc)

    with closing(_connect()) as conn, conn:
        _ensure_schema(conn)
        rows = conn.execute("SELECT * FROM issues").fetchall()

    records = [_row_to_record(row) for row in rows]
    active_records = [
        record for record in records if record.status == IssueStatus.ACTIVE
    ]
    active_records.sort(
        key=lambda record: (
            _severity_rank(record),
            record.last_seen_at,
        ),
        reverse=True,
    )

    severity_counts = {
        Severity.CRITICAL.value: 0,
        Severity.WARNING.value: 0,
        Severity.INFO.value: 0,
    }
    issue_type_counts: dict[str, int] = {}
    for record in active_records:
        severity_counts[record.severity.value] += 1
        issue_type_counts[record.issue_type.value] = (
            issue_type_counts.get(record.issue_type.value, 0) + 1
        )

    latest_observation_at = (
        max(record.last_seen_at for record in records).isoformat()
        if records
        else None
    )
    if not records:
        data_status = "EMPTY"
    elif active_records:
        data_status = "CURRENT"
    else:
        data_status = "CLEAR"

    visible_records = active_records
    if not include_acknowledged:
        visible_records = [
            record for record in visible_records if not record.acknowledged
        ]

    current_unacknowledged_count = sum(
        1 for record in active_records if not record.acknowledged
    )

    return {
        "generated_at": now.isoformat(),
        "data_status": data_status,
        "current_definition": "Persisted incident status is ACTIVE.",
        # Retained for Block 13 response compatibility; no longer used to
        # decide whether an incident is current.
        "current_window_seconds": max(
            settings.monitor_poll_interval_seconds * 3,
            180,
        ),
        "poll_interval_seconds": settings.monitor_poll_interval_seconds,
        "latest_observation_at": latest_observation_at,
        "total_stored_issue_count": len(records),
        "current_issue_count": len(active_records),
        "resolved_issue_count": len(records) - len(active_records),
        "current_unacknowledged_count": current_unacknowledged_count,
        "current_acknowledged_count": (
            len(active_records) - current_unacknowledged_count
        ),
        "severity_counts": severity_counts,
        "issue_type_counts": issue_type_counts,
        "most_serious_issue": (
            _record_to_summary(active_records[0])
            if active_records
            else None
        ),
        "issues": [
            _record_to_summary(record)
            for record in visible_records[:limit]
        ],
        "returned_issue_count": min(len(visible_records), limit),
        "include_acknowledged": include_acknowledged,
        "limit": limit,
    }


async def get_monitor_summary(
    include_acknowledged: bool = True,
    limit: int = 10,
) -> dict:
    """Return persisted ACTIVE monitor counts and issue details."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")

    return await asyncio.to_thread(
        _get_monitor_summary_sync,
        include_acknowledged,
        limit,
    )
