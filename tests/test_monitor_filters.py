"""Block 14 REST API filter, search, sorting, and limit tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from monitor.api import monitor_app
from monitor.config import get_monitor_settings
from monitor.models import DetectedIssue, IssueType, Recommendation, Severity
from monitor.storage import (
    acknowledge_issue,
    init_db,
    record_issue_observation,
    resolve_missing_issues,
)


def recommendation(label: str) -> Recommendation:
    return Recommendation(
        root_cause=f"{label} root cause",
        recommended_action=f"{label} action",
        risk_if_ignored=f"{label} risk",
        confidence=0.8,
    )


class MonitorFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "monitor.db"
        os.environ["MONITOR_SQLITE_PATH"] = str(self.database_path)
        get_monitor_settings.cache_clear()
        asyncio.run(self._seed())
        self.client = TestClient(monitor_app)

    def tearDown(self) -> None:
        self.client.close()
        get_monitor_settings.cache_clear()
        os.environ.pop("MONITOR_SQLITE_PATH", None)
        self.temp_dir.cleanup()

    async def _seed(self) -> None:
        await init_db()
        base = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

        critical = await record_issue_observation(
            DetectedIssue(
                issue_type=IssueType.BLOCKING_SESSION,
                severity=Severity.CRITICAL,
                summary="Payroll blocker requires immediate review",
                details={"blocking_sid": 11, "waiting_sid": 22},
                detected_at=base + timedelta(minutes=1),
            ),
            recommendation("payroll"),
        )

        acknowledged = await record_issue_observation(
            DetectedIssue(
                issue_type=IssueType.TABLESPACE_USAGE,
                severity=Severity.WARNING,
                summary="USERS tablespace is 89 percent full",
                details={
                    "con_name": "ORCLPDB1",
                    "tablespace_name": "USERS",
                    "pct_used": 89,
                },
                detected_at=base + timedelta(minutes=2),
            ),
            recommendation("storage"),
        )
        await acknowledge_issue(acknowledged.id or 0, "filter-test-dba")

        resolved = await record_issue_observation(
            DetectedIssue(
                issue_type=IssueType.WAIT_EVENT,
                severity=Severity.INFO,
                summary="Top wait event: db file sequential read",
                details={"event": "db file sequential read"},
                detected_at=base,
            ),
            recommendation("wait"),
        )
        await resolve_missing_issues(
            active_fingerprints={critical.fingerprint, acknowledged.fingerprint},
            successful_issue_types={IssueType.WAIT_EVENT},
            resolved_at=base + timedelta(minutes=3),
        )
        self.ids = {
            "critical": critical.id,
            "acknowledged": acknowledged.id,
            "resolved": resolved.id,
        }

    def get_issues(self, **params):
        response = self.client.get("/api/issues", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_status_filters(self) -> None:
        active = self.get_issues(status="ACTIVE")
        self.assertEqual({row["status"] for row in active}, {"ACTIVE"})
        self.assertEqual(len(active), 2)

        resolved = self.get_issues(status="RESOLVED")
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["status"], "RESOLVED")

        all_rows = self.get_issues(status="ALL")
        self.assertEqual(len(all_rows), 3)

    def test_acknowledgment_filters(self) -> None:
        acknowledged = self.get_issues(
            status="ALL",
            acknowledgment="ACKNOWLEDGED",
        )
        self.assertEqual(len(acknowledged), 1)
        self.assertTrue(acknowledged[0]["acknowledged"])

        unacknowledged = self.get_issues(
            status="ALL",
            acknowledgment="UNACKNOWLEDGED",
        )
        self.assertEqual(len(unacknowledged), 2)
        self.assertTrue(all(not row["acknowledged"] for row in unacknowledged))

    def test_severity_and_issue_type_filters(self) -> None:
        critical = self.get_issues(status="ALL", severity="CRITICAL")
        self.assertEqual(len(critical), 1)
        self.assertEqual(critical[0]["issue_type"], "BLOCKING_SESSION")

        tablespace = self.get_issues(
            status="ALL",
            issue_type="TABLESPACE_USAGE",
        )
        self.assertEqual(len(tablespace), 1)
        self.assertEqual(tablespace[0]["severity"], "WARNING")

    def test_text_search_is_case_insensitive_and_searches_json(self) -> None:
        summary_match = self.get_issues(status="ALL", search="users")
        self.assertEqual(len(summary_match), 1)
        self.assertEqual(summary_match[0]["issue_type"], "TABLESPACE_USAGE")

        details_match = self.get_issues(status="ALL", search="sequential read")
        self.assertEqual(len(details_match), 1)
        self.assertEqual(details_match[0]["status"], "RESOLVED")

        recommendation_match = self.get_issues(status="ALL", search="payroll action")
        self.assertEqual(len(recommendation_match), 1)
        self.assertEqual(recommendation_match[0]["severity"], "CRITICAL")

    def test_newest_oldest_sorting_and_limit(self) -> None:
        newest = self.get_issues(status="ALL", sort="newest")
        oldest = self.get_issues(status="ALL", sort="oldest")

        self.assertEqual(newest[0]["issue_type"], "TABLESPACE_USAGE")
        self.assertEqual(oldest[0]["issue_type"], "WAIT_EVENT")
        self.assertEqual(
            [row["id"] for row in newest],
            list(reversed([row["id"] for row in oldest])),
        )

        limited = self.get_issues(status="ALL", sort="newest", limit=1)
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0]["issue_type"], "TABLESPACE_USAGE")

    def test_invalid_limit_is_rejected(self) -> None:
        response = self.client.get(
            "/api/issues",
            params={"status": "ALL", "limit": 501},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
