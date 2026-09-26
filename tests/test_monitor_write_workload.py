"""Write-workload rate calculation and severity tests."""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.modules.setdefault("oracledb", SimpleNamespace())

from monitor.config import get_monitor_settings
from monitor.models import IssueType, Severity


ROWS_BASELINE = [
    {"name": "user commits", "value": 1000},
    {"name": "redo size", "value": 100_000_000},
    {"name": "execute count", "value": 10_000},
    {"name": "user calls", "value": 20_000},
]


class WriteWorkloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from monitor.checks import reset_write_workload_baseline

        reset_write_workload_baseline()
        os.environ.update(
            {
                "MONITOR_WRITE_WARN_COMMITS_PER_SEC": "40",
                "MONITOR_WRITE_CRIT_COMMITS_PER_SEC": "150",
                "MONITOR_WRITE_WARN_REDO_MB_PER_SEC": "5",
                "MONITOR_WRITE_CRIT_REDO_MB_PER_SEC": "25",
                "MONITOR_WRITE_WARN_EXECUTES_PER_SEC": "2000",
                "MONITOR_WRITE_CRIT_EXECUTES_PER_SEC": "10000",
            }
        )
        get_monitor_settings.cache_clear()

    async def asyncTearDown(self) -> None:
        for name in list(os.environ):
            if name.startswith("MONITOR_WRITE_"):
                os.environ.pop(name, None)
        get_monitor_settings.cache_clear()

    async def test_first_sample_only_initializes_baseline(self) -> None:
        from monitor.checks import check_write_workload

        with patch("monitor.checks._run_shared", new=AsyncMock(return_value=ROWS_BASELINE)), patch(
            "monitor.checks.time.monotonic", return_value=100.0
        ):
            self.assertEqual(await check_write_workload(), [])

    async def test_warning_from_commit_rate(self) -> None:
        from monitor.checks import check_write_workload

        warning_rows = [dict(row) for row in ROWS_BASELINE]
        for row in warning_rows:
            if row["name"] == "user commits":
                row["value"] += 600  # 60/sec over 10 seconds

        query_mock = AsyncMock(side_effect=[ROWS_BASELINE, warning_rows])
        with patch("monitor.checks._run_shared", new=query_mock), patch(
            "monitor.checks.time.monotonic", side_effect=[100.0, 110.0]
        ):
            self.assertEqual(await check_write_workload(), [])
            issues = await check_write_workload()

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, IssueType.WRITE_WORKLOAD)
        self.assertEqual(issues[0].severity, Severity.WARNING)
        self.assertEqual(issues[0].details["commits_per_sec"], 60.0)
        self.assertEqual(issues[0].fingerprint(), "WRITE_WORKLOAD")

    async def test_critical_from_redo_rate(self) -> None:
        from monitor.checks import check_write_workload

        critical_rows = [dict(row) for row in ROWS_BASELINE]
        for row in critical_rows:
            if row["name"] == "redo size":
                row["value"] += 300 * 1024 * 1024  # 30 MB/sec over 10 seconds

        query_mock = AsyncMock(side_effect=[ROWS_BASELINE, critical_rows])
        with patch("monitor.checks._run_shared", new=query_mock), patch(
            "monitor.checks.time.monotonic", side_effect=[100.0, 110.0]
        ):
            await check_write_workload()
            issues = await check_write_workload()

        self.assertEqual(issues[0].severity, Severity.CRITICAL)
        self.assertEqual(issues[0].details["redo_mb_per_sec"], 30.0)


    async def test_uses_actual_elapsed_time_when_poll_is_delayed(self) -> None:
        from monitor.checks import check_write_workload

        delayed_rows = [dict(row) for row in ROWS_BASELINE]
        for row in delayed_rows:
            if row["name"] == "user commits":
                row["value"] += 720  # 40/sec over an actual 18-second gap

        query_mock = AsyncMock(side_effect=[ROWS_BASELINE, delayed_rows])
        with patch("monitor.checks._run_shared", new=query_mock), patch(
            "monitor.checks.time.monotonic", side_effect=[100.0, 118.0]
        ):
            self.assertEqual(await check_write_workload(), [])
            issues = await check_write_workload()

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, Severity.WARNING)
        self.assertEqual(issues[0].details["sample_seconds"], 18.0)
        self.assertEqual(issues[0].details["commits_per_sec"], 40.0)

    async def test_counter_reset_refreshes_baseline_without_alert(self) -> None:
        from monitor.checks import check_write_workload

        reset_rows = [dict(row) for row in ROWS_BASELINE]
        reset_rows[0]["value"] = 1

        query_mock = AsyncMock(side_effect=[ROWS_BASELINE, reset_rows])
        with patch("monitor.checks._run_shared", new=query_mock), patch(
            "monitor.checks.time.monotonic", side_effect=[100.0, 110.0]
        ):
            await check_write_workload()
            self.assertEqual(await check_write_workload(), [])


if __name__ == "__main__":
    unittest.main()
