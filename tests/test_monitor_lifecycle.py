"""Block 14 lifecycle and legacy-schema migration tests."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitor.config import get_monitor_settings
from monitor.models import (
    DetectedIssue,
    IssueStatus,
    IssueType,
    Recommendation,
    Severity,
)
from monitor.storage import (
    acknowledge_issue,
    get_issue_by_fingerprint,
    init_db,
    record_issue_observation,
    resolve_missing_issues,
)


def recommendation(label: str) -> Recommendation:
    return Recommendation(
        root_cause=f"root cause {label}",
        recommended_action=f"action {label}",
        risk_if_ignored=f"risk {label}",
        confidence=0.9,
    )


def blocking_issue(
    at: datetime,
    severity: Severity = Severity.WARNING,
) -> DetectedIssue:
    return DetectedIssue(
        issue_type=IssueType.BLOCKING_SESSION,
        severity=severity,
        summary="SID 10 is blocking SID 20",
        details={
            "blocking_sid": 10,
            "waiting_sid": 20,
            "waiting_seconds": 120,
            "con_name": "ORCLPDB1",
        },
        detected_at=at,
    )


class MonitorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "monitor.db"
        os.environ["MONITOR_SQLITE_PATH"] = str(self.database_path)
        get_monitor_settings.cache_clear()
        await init_db()

    async def asyncTearDown(self) -> None:
        get_monitor_settings.cache_clear()
        os.environ.pop("MONITOR_SQLITE_PATH", None)
        self.temp_dir.cleanup()

    async def test_full_lifecycle_and_recommendation_policy(self) -> None:
        t1 = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        issue1 = blocking_issue(t1)
        fingerprint = issue1.fingerprint()

        first = await record_issue_observation(issue1, recommendation("new"))
        self.assertEqual(first.status, IssueStatus.ACTIVE)
        self.assertEqual(first.observation_count, 1)
        self.assertEqual(first.episode_count, 1)
        self.assertFalse(first.acknowledged)

        acknowledged = await acknowledge_issue(first.id or 0, "test-dba")
        self.assertIsNotNone(acknowledged)
        self.assertTrue(acknowledged.acknowledged)

        # Same active issue: count the poll, preserve ack, and reuse recommendation.
        continuous = await record_issue_observation(
            blocking_issue(t1 + timedelta(minutes=1)),
            recommendation=None,
        )
        self.assertEqual(continuous.observation_count, 2)
        self.assertEqual(continuous.episode_count, 1)
        self.assertTrue(continuous.acknowledged)
        self.assertEqual(continuous.recommendation.root_cause, "root cause new")

        # Severity change: regenerate recommendation but preserve ack because the
        # issue never resolved between observations.
        changed = await record_issue_observation(
            blocking_issue(
                t1 + timedelta(minutes=2),
                severity=Severity.CRITICAL,
            ),
            recommendation("severity-change"),
        )
        self.assertEqual(changed.observation_count, 3)
        self.assertEqual(changed.episode_count, 1)
        self.assertTrue(changed.acknowledged)
        self.assertEqual(changed.severity, Severity.CRITICAL)
        self.assertEqual(
            changed.recommendation.root_cause,
            "root cause severity-change",
        )

        # A different failed check cannot resolve this blocking-session issue.
        resolved_count = await resolve_missing_issues(
            active_fingerprints=set(),
            successful_issue_types={IssueType.TABLESPACE_USAGE},
            resolved_at=t1 + timedelta(minutes=3),
        )
        self.assertEqual(resolved_count, 0)
        still_active = await get_issue_by_fingerprint(fingerprint)
        self.assertEqual(still_active.status, IssueStatus.ACTIVE)

        # A successful blocking-session check with no detection resolves it.
        resolved_count = await resolve_missing_issues(
            active_fingerprints=set(),
            successful_issue_types={IssueType.BLOCKING_SESSION},
            resolved_at=t1 + timedelta(minutes=4),
        )
        self.assertEqual(resolved_count, 1)
        resolved = await get_issue_by_fingerprint(fingerprint)
        self.assertEqual(resolved.status, IssueStatus.RESOLVED)
        self.assertTrue(resolved.acknowledged)
        self.assertIsNotNone(resolved.resolved_at)

        # Reopen: new episode, cleared ack, and regenerated recommendation.
        reopened = await record_issue_observation(
            blocking_issue(t1 + timedelta(minutes=5), Severity.CRITICAL),
            recommendation("reopened"),
        )
        self.assertEqual(reopened.status, IssueStatus.ACTIVE)
        self.assertEqual(reopened.observation_count, 4)
        self.assertEqual(reopened.occurrence_count, 4)
        self.assertEqual(reopened.episode_count, 2)
        self.assertFalse(reopened.acknowledged)
        self.assertIsNone(reopened.acknowledged_by)
        self.assertIsNone(reopened.acknowledged_at)
        self.assertIsNone(reopened.resolved_at)
        self.assertEqual(
            reopened.recommendation.root_cause,
            "root cause reopened",
        )

    async def test_required_recommendation_transitions_are_enforced(self) -> None:
        at = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        issue = blocking_issue(at)

        with self.assertRaisesRegex(ValueError, "new issue"):
            await record_issue_observation(issue, recommendation=None)

        await record_issue_observation(issue, recommendation("new"))
        with self.assertRaisesRegex(ValueError, "severity-changed"):
            await record_issue_observation(
                blocking_issue(at + timedelta(minutes=1), Severity.CRITICAL),
                recommendation=None,
            )

    async def test_existing_block13_database_is_migrated_in_place(self) -> None:
        # Replace the test DB with the exact legacy column set.
        if self.database_path.exists():
            self.database_path.unlink()

        legacy_recommendation = recommendation("legacy")
        detected_at = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
        with closing(sqlite3.connect(self.database_path)) as conn, conn:
            conn.execute(
                """CREATE TABLE issues (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       fingerprint TEXT UNIQUE NOT NULL,
                       issue_type TEXT NOT NULL,
                       severity TEXT NOT NULL,
                       summary TEXT NOT NULL,
                       details TEXT NOT NULL,
                       recommendation TEXT NOT NULL,
                       detected_at TEXT NOT NULL,
                       last_seen_at TEXT NOT NULL,
                       occurrence_count INTEGER NOT NULL DEFAULT 1,
                       acknowledged INTEGER NOT NULL DEFAULT 0,
                       acknowledged_by TEXT,
                       acknowledged_at TEXT
                   )"""
            )
            conn.execute(
                """INSERT INTO issues (
                       id, fingerprint, issue_type, severity, summary, details,
                       recommendation, detected_at, last_seen_at,
                       occurrence_count, acknowledged, acknowledged_by,
                       acknowledged_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    41,
                    "BLOCKING_SESSION:10",
                    IssueType.BLOCKING_SESSION.value,
                    Severity.WARNING.value,
                    "legacy blocking incident",
                    json.dumps({"blocking_sid": 10}),
                    legacy_recommendation.model_dump_json(),
                    detected_at.isoformat(),
                    detected_at.isoformat(),
                    7,
                    1,
                    "legacy-dba",
                    detected_at.isoformat(),
                ),
            )

        await init_db()
        migrated = await get_issue_by_fingerprint("BLOCKING_SESSION:10")

        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.id, 41)
        self.assertEqual(migrated.summary, "legacy blocking incident")
        self.assertEqual(migrated.status, IssueStatus.ACTIVE)
        self.assertEqual(migrated.observation_count, 7)
        self.assertEqual(migrated.occurrence_count, 7)
        self.assertEqual(migrated.episode_count, 1)
        self.assertTrue(migrated.acknowledged)
        self.assertEqual(migrated.acknowledged_by, "legacy-dba")
        self.assertEqual(
            migrated.recommendation.root_cause,
            "root cause legacy",
        )

        with closing(sqlite3.connect(self.database_path)) as conn, conn:
            row_count = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(issues)").fetchall()
            }

        self.assertEqual(row_count, 1)
        self.assertTrue(
            {
                "status",
                "resolved_at",
                "recommendation_updated_at",
                "observation_count",
                "episode_count",
            }.issubset(columns)
        )


if __name__ == "__main__":
    unittest.main()
