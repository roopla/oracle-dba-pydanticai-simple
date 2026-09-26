"""Tests for approved monitor diagnostics and no-SQL recommendation schema."""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.modules.setdefault("oracledb", SimpleNamespace())

from monitor.diagnostics import collect_diagnostics
from monitor.models import DetectedIssue, IssueType, Recommendation, Severity


class MonitorDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_tablespace_incident_uses_baked_diagnostic_function(self) -> None:
        issue = DetectedIssue(
            issue_type=IssueType.TABLESPACE_USAGE,
            severity=Severity.CRITICAL,
            summary="[ORCLPDB1] tablespace SYSTEM is 99.31% full",
            details={
                "con_name": "ORCLPDB1",
                "tablespace_name": "SYSTEM",
                "pct_used": 99.31,
            },
        )

        rows = [
            {
                "file_id": 1,
                "autoextensible": "YES",
                "current_gb": 1.0,
                "effective_max_gb": 32.0,
            }
        ]
        with patch(
            "monitor.diagnostics._run_shared",
            new=AsyncMock(return_value=rows),
        ) as run_shared:
            evidence = await collect_diagnostics(issue)

        self.assertEqual(evidence["tablespace_files"], rows)
        args = run_shared.await_args
        self.assertEqual(args.kwargs["pdb_name"], "ORCLPDB1")
        self.assertEqual(args.kwargs["tablespace_name"], "SYSTEM")

    def test_recommendation_schema_has_no_sql_or_command_field(self) -> None:
        recommendation = Recommendation(
            root_cause="Tablespace is critically full.",
            recommended_action="Review capacity and autoextend settings.",
            risk_if_ignored="Extent allocation can fail.",
            confidence=0.95,
        )
        self.assertNotIn("sql_or_command", Recommendation.model_fields)
        self.assertEqual(recommendation.confidence, 0.95)


if __name__ == "__main__":
    unittest.main()
