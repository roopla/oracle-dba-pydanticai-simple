import unittest
from datetime import date
from unittest import mock

from oracle_core import partitions, remediation
from oracle_core.partitions import add_months, parse_high_value, retention_cutoff

TO_DATE = "TO_DATE(' {} 00:00:00', 'SYYYY-MM-DD HH24:MI:SS', 'NLS_CALENDAR=GREGORIAN')"


def _partition(name, bound, size=6.0, position=1, interval=True):
    return {
        "partition_name": name,
        "position": position,
        "high_value": bound,
        "month": "MAXVALUE" if bound == "MAXVALUE" else partitions.data_month(date.fromisoformat(bound)),
        "tablespace_name": "PART_TS",
        "size_mb": size,
        "interval_section": interval,
    }


def _table(parts, interval="NUMTOYMINTERVAL(1, 'MONTH')"):
    return {
        "owner": "APP",
        "table_name": "SALES",
        "partition_key": "SALE_DATE",
        "interval": interval,
        "partitions": parts,
    }


def _monthly(first: date, count: int):
    """count monthly partitions, the first holding `first`'s month."""
    return [
        _partition(f"P{i}", add_months(first, i + 1).isoformat(), position=i + 1)
        for i in range(count)
    ]


SEPT = date(2026, 9, 1)


class HighValueTests(unittest.TestCase):
    def test_parses_to_date_and_timestamp_bounds(self):
        self.assertEqual(parse_high_value(TO_DATE.format("2026-09-01")), date(2026, 9, 1))
        self.assertEqual(parse_high_value("TIMESTAMP' 2026-10-01 00:00:00'"), date(2026, 10, 1))

    def test_maxvalue_is_none(self):
        self.assertIsNone(parse_high_value("MAXVALUE"))

    def test_unexpected_bounds_raise_instead_of_guessing(self):
        for text in ("'2026'", TO_DATE.replace("00:00:00", "12:00:00").format("2026-09-01")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_high_value(text)

    def test_month_arithmetic_crosses_years(self):
        self.assertEqual(add_months(date(2026, 1, 1), -1), date(2025, 12, 1))
        self.assertEqual(retention_cutoff(SEPT, 2), date(2026, 8, 1))


class RetentionPlanTests(unittest.TestCase):
    def plan(self, parts, keep=2, interval="NUMTOYMINTERVAL(1, 'MONTH')"):
        with mock.patch.object(partitions, "table_partitions", return_value=_table(parts, interval)):
            return partitions.retention_plan("PDB1", "APP", "SALES", keep, current_month=SEPT)

    def test_keeps_current_and_previous_month(self):
        # Twelve months, Oct 2025 .. Sep 2026.
        plan = self.plan(_monthly(date(2025, 10, 1), 12))
        self.assertEqual([p["month"] for p in plan["keep"]], ["2026-08", "2026-09"])
        self.assertEqual(len(plan["drop"]), 10)
        self.assertEqual(plan["drop"][0]["month"], "2025-10")
        self.assertEqual(plan["drop_size_mb"], 60.0)

    def test_two_newest_partitions_survive_when_current_month_is_missing(self):
        # Newest partition is July: the date rule alone would keep only
        # nothing from July, but the two newest partitions always stay.
        plan = self.plan(_monthly(date(2026, 3, 1), 5))  # Mar .. Jul
        self.assertEqual([p["month"] for p in plan["keep"]], ["2026-06", "2026-07"])

    def test_maxvalue_partition_is_never_dropped(self):
        parts = _monthly(date(2026, 1, 1), 9) + [_partition("PMAX", "MAXVALUE", position=10)]
        plan = self.plan(parts, interval=None)
        self.assertIn("PMAX", [p["partition_name"] for p in plan["keep"]])
        self.assertNotIn("PMAX", [p["partition_name"] for p in plan["drop"]])

    def test_longer_retention_keeps_more(self):
        plan = self.plan(_monthly(date(2025, 10, 1), 12), keep=6)
        self.assertEqual(plan["oldest_kept_month"], "2026-04")
        self.assertEqual(len(plan["keep"]), 6)

    def test_keep_months_below_two_is_refused(self):
        for bad in (0, 1, "one", True, 121):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                partitions.validate_keep_months(bad)


class DropOldPartitionsActionTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(remediation, "validate_pdb_name", side_effect=str.upper),
            mock.patch.object(remediation, "retention_plan"),
            mock.patch.object(remediation, "execute_statements"),
        ]
        self.pdb, self.retention, self.execute = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def retention_for(self, parts, interval="NUMTOYMINTERVAL(1, 'MONTH')"):
        with mock.patch.object(partitions, "table_partitions", return_value=_table(parts, interval)):
            return partitions.retention_plan("PDB1", "APP", "SALES", 2, current_month=SEPT)

    def plan(self, **params):
        return remediation.plan_remediation(
            "drop_old_partitions",
            {"pdb_name": "pdb1", "owner": "app", "table_name": "sales", **params},
        )

    def test_interval_table_resets_the_interval_before_dropping(self):
        self.retention.return_value = self.retention_for(_monthly(date(2026, 4, 1), 6))
        plan = self.plan()
        self.assertEqual(
            plan["statements"][0],
            'ALTER TABLE "APP"."SALES" SET INTERVAL (NUMTOYMINTERVAL(1, \'MONTH\'))',
        )
        self.assertEqual(
            plan["statements"][1:],
            [f'ALTER TABLE "APP"."SALES" DROP PARTITION "P{i}" UPDATE INDEXES' for i in range(4)],
        )
        self.assertFalse(plan["reversible"])
        self.assertIn("PERMANENTLY DELETES", plan["impact"])

    def test_plain_range_table_has_no_interval_statement(self):
        self.retention.return_value = self.retention_for(_monthly(date(2026, 4, 1), 6), interval=None)
        self.assertTrue(all("DROP PARTITION" in s for s in self.plan()["statements"]))

    def test_nothing_to_drop_is_refused(self):
        self.retention.return_value = self.retention_for(_monthly(date(2026, 7, 1), 2))
        with self.assertRaisesRegex(ValueError, "Nothing to drop"):
            self.plan()

    def test_bad_names_are_refused_before_any_lookup(self):
        with self.assertRaisesRegex(ValueError, "not a valid name"):
            self.plan(table_name='SALES" CASCADE')
        self.retention.assert_not_called()

    def test_execution_refuses_when_partitions_changed_since_approval(self):
        self.retention.return_value = self.retention_for(_monthly(date(2026, 4, 1), 6))
        with self.assertRaisesRegex(RuntimeError, "changed after approval"):
            remediation.execute_remediation(
                "drop_old_partitions",
                {"pdb_name": "PDB1", "owner": "APP", "table_name": "SALES"},
                approved_statements=['ALTER TABLE "APP"."SALES" DROP PARTITION "OTHER" UPDATE INDEXES'],
            )
        self.execute.assert_not_called()


class TablespaceOptionsWithPartitionsTests(unittest.TestCase):
    def test_partitioned_table_adds_a_third_option_in_the_same_group(self):
        def fake_plan(action, params):
            return {"action": action, "target": f"{action} target", "params": params}

        with mock.patch.object(remediation, "plan_remediation", side_effect=fake_plan), \
             mock.patch.object(remediation, "validate_pdb_name", side_effect=str.upper), \
             mock.patch.object(remediation, "monthly_partitioned_tables",
                               return_value=[{"owner": "APP", "table_name": "SALES"}]):
            result = remediation.plan_tablespace_options("pdb1", "part_ts")

        self.assertEqual(
            [o["action"] for o in result["options"]],
            ["enable_tablespace_autoextend", "add_tablespace_datafile", "drop_old_partitions"],
        )
        self.assertEqual({o["alternative_group"] for o in result["options"]}, {"tablespace:PDB1:PART_TS"})
        self.assertEqual(result["options"][2]["params"]["keep_months"], 2)


if __name__ == "__main__":
    unittest.main()
