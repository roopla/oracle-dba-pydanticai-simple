"""Block 14 poll-cycle recommendation and failed-check behavior tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from monitor.config import get_monitor_settings
from monitor.models import (
    DetectedIssue,
    IssueStatus,
    IssueType,
    Recommendation,
    Severity,
)
from monitor.poller import run_poll_cycle
from monitor.storage import get_issue_by_fingerprint, init_db


def recommendation(label: str) -> Recommendation:
    return Recommendation(
        root_cause=f"root {label}",
        recommended_action=f"action {label}",
        risk_if_ignored=f"risk {label}",
        confidence=0.9,
    )


def issue(at: datetime, severity: Severity = Severity.WARNING) -> DetectedIssue:
    return DetectedIssue(
        issue_type=IssueType.BLOCKING_SESSION,
        severity=severity,
        summary="SID 30 blocks SID 40",
        details={"blocking_sid": 30, "waiting_sid": 40},
        detected_at=at,
    )


def result(
    issues: list[DetectedIssue],
    *,
    successful: set[IssueType],
    failed: set[IssueType] | None = None,
):
    return SimpleNamespace(
        issues=issues,
        successful_issue_types=successful,
        failed_issue_types=failed or set(),
    )


class MonitorPollerTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_recommendation_calls_and_failed_check_resolution_guard(self) -> None:
        base = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
        detections = [
            result([issue(base)], successful={IssueType.BLOCKING_SESSION}),
            result(
                [issue(base + timedelta(minutes=1))],
                successful={IssueType.BLOCKING_SESSION},
            ),
            result(
                [issue(base + timedelta(minutes=2), Severity.CRITICAL)],
                successful={IssueType.BLOCKING_SESSION},
            ),
            # The owning check failed, so the incident must remain ACTIVE.
            result(
                [],
                successful=set(),
                failed={IssueType.BLOCKING_SESSION},
            ),
            # The owning check succeeded with no detection, so it resolves.
            result([], successful={IssueType.BLOCKING_SESSION}),
            # It returns and begins a new episode.
            result(
                [issue(base + timedelta(minutes=5), Severity.CRITICAL)],
                successful={IssueType.BLOCKING_SESSION},
            ),
        ]
        generated = [
            recommendation("new"),
            recommendation("severity"),
            recommendation("reopened"),
        ]

        with (
            patch(
                "monitor.poller._run_all_checks",
                new=AsyncMock(side_effect=detections),
            ),
            patch(
                "monitor.poller._generate_recommendation",
                new=AsyncMock(side_effect=generated),
            ) as generate,
        ):
            await run_poll_cycle()
            await run_poll_cycle()
            await run_poll_cycle()

            current = await get_issue_by_fingerprint("BLOCKING_SESSION:30")
            self.assertEqual(current.observation_count, 3)
            self.assertEqual(current.episode_count, 1)
            self.assertEqual(current.status, IssueStatus.ACTIVE)
            self.assertEqual(current.recommendation.root_cause, "root severity")
            self.assertEqual(generate.await_count, 2)

            await run_poll_cycle()
            after_failure = await get_issue_by_fingerprint("BLOCKING_SESSION:30")
            self.assertEqual(after_failure.status, IssueStatus.ACTIVE)
            self.assertEqual(generate.await_count, 2)

            await run_poll_cycle()
            resolved = await get_issue_by_fingerprint("BLOCKING_SESSION:30")
            self.assertEqual(resolved.status, IssueStatus.RESOLVED)

            await run_poll_cycle()
            reopened = await get_issue_by_fingerprint("BLOCKING_SESSION:30")
            self.assertEqual(reopened.status, IssueStatus.ACTIVE)
            self.assertEqual(reopened.observation_count, 4)
            self.assertEqual(reopened.episode_count, 2)
            self.assertEqual(reopened.recommendation.root_cause, "root reopened")
            self.assertEqual(generate.await_count, 3)


if __name__ == "__main__":
    unittest.main()
