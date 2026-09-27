import asyncio
import unittest
from unittest import mock

from monitor import checks
from monitor.config import get_monitor_settings
from monitor.models import Severity


def _row(tablespace, pct_used, pct_used_of_max, autoextensible="YES"):
    row = {
        "con_name": "CDB$ROOT",
        "tablespace_name": tablespace,
        "total_gb": 0.89,
        "used_gb": 0.88,
        "free_gb": 0.01,
        "pct_used": pct_used,
        "max_gb": 32.0,
        "autoextensible": autoextensible,
    }
    if pct_used_of_max is not None:
        row["pct_used_of_max"] = pct_used_of_max
    return row


def _run(rows):
    with mock.patch.object(checks, "get_tablespace_usage", return_value=rows):
        return asyncio.run(checks.check_tablespace_usage())


class TablespaceCheckTests(unittest.TestCase):
    def setUp(self):
        settings = get_monitor_settings()
        self.warn = settings.monitor_tablespace_warn_pct
        self.crit = settings.monitor_tablespace_crit_pct

    def test_autoextensible_full_allocation_is_not_an_issue(self):
        # The live false alarm: SYSTEM at 99.37% of 910MB allocated,
        # 2.8% of its 32GB MAXSIZE.
        issues = _run([_row("SYSTEM", 99.37, 2.76)])
        self.assertEqual(issues, [])

    def test_near_maxsize_is_critical_and_names_both_figures(self):
        issues = _run([_row("USERS", 99.9, self.crit + 1)])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, Severity.CRITICAL)
        self.assertIn("of its maximum size", issues[0].summary)
        self.assertIn("99.9% of currently allocated", issues[0].summary)

    def test_fixed_size_tablespace_alerts_on_allocation(self):
        # Not autoextensible: max == allocated, so both figures agree.
        pct = self.warn + 1
        issues = _run([_row("DATA", pct, pct, autoextensible="NO")])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, Severity.WARNING)

    def test_falls_back_to_pct_used_without_the_new_column(self):
        issues = _run([_row("OLD", self.crit + 1, None)])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, Severity.CRITICAL)


if __name__ == "__main__":
    unittest.main()
