import unittest
from unittest import mock

from oracle_core import remediation

MB = 1024 * 1024
DIR = "/opt/oracle/oradata/ORCL/ORCLPDB1"


def _file(name, size_mb, autoextend="NO", max_mb=0, bigfile="NO", contents="PERMANENT"):
    return {
        "file_id": 1,
        "file_name": f"{DIR}/{name}",
        "autoextensible": autoextend,
        "bytes": size_mb * MB,
        "maxbytes": max_mb * MB,
        "contents": contents,
        "bigfile": bigfile,
        "block_size": 8192,
    }


class TablespaceRemediationTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(remediation, "validate_pdb_name", side_effect=str.upper),
            mock.patch.object(remediation, "_tablespace_files"),
            mock.patch.object(remediation, "execute_statements"),
        ]
        self.pdb, self.files, self.execute = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def plan(self, action, **params):
        return remediation.plan_remediation(
            action, {"pdb_name": "orclpdb1", "tablespace_name": "space_ts", **params}
        )

    # --- enable_tablespace_autoextend ------------------------------------

    def test_autoextend_targets_only_files_that_cannot_grow_far_enough(self):
        self.files.return_value = [
            _file("space_ts01.dbf", 10),
            _file("space_ts02.dbf", 10, autoextend="YES", max_mb=4096),
        ]
        plan = self.plan("enable_tablespace_autoextend", max_mb=2048)
        self.assertEqual(
            plan["statements"],
            [
                f"ALTER DATABASE DATAFILE '{DIR}/space_ts01.dbf' "
                "AUTOEXTEND ON NEXT 64M MAXSIZE 2048M"
            ],
        )
        self.assertTrue(plan["reversible"])
        self.assertEqual(plan["params"]["tablespace_name"], "SPACE_TS")

    def test_autoextend_refuses_when_nothing_to_do(self):
        self.files.return_value = [_file("a.dbf", 10, autoextend="YES", max_mb=32767)]
        with self.assertRaisesRegex(ValueError, "Nothing to do"):
            self.plan("enable_tablespace_autoextend")

    def test_autoextend_refuses_max_not_above_current_size(self):
        self.files.return_value = [_file("big.dbf", 3000)]
        with self.assertRaisesRegex(ValueError, "not above the current size"):
            self.plan("enable_tablespace_autoextend", max_mb=2048)

    def test_file_name_quotes_are_escaped(self):
        self.files.return_value = [_file("o'brien.dbf", 10)]
        statement = self.plan("enable_tablespace_autoextend")["statements"][0]
        self.assertIn("o''brien.dbf", statement)

    def test_bad_sizes_and_names_are_refused(self):
        self.files.return_value = [_file("a.dbf", 10)]
        for params in ({"next_mb": 0}, {"next_mb": "lots"}, {"max_mb": 10**6}, {"next_mb": True}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.plan("enable_tablespace_autoextend", **params)
        with self.assertRaisesRegex(ValueError, "not a valid tablespace name"):
            remediation.plan_remediation(
                "enable_tablespace_autoextend",
                {"pdb_name": "ORCLPDB1", "tablespace_name": 'X" DROP TABLESPACE Y'},
            )

    # --- add_tablespace_datafile -----------------------------------------

    def test_add_datafile_picks_an_unused_name_next_to_existing_files(self):
        self.files.return_value = [_file("space_ts01.dbf", 10), _file("space_ts_02.dbf", 10)]
        plan = self.plan("add_tablespace_datafile", size_mb=50)
        self.assertEqual(
            plan["statements"],
            [
                f"ALTER TABLESPACE \"SPACE_TS\" ADD DATAFILE '{DIR}/space_ts_03.dbf' "
                "SIZE 50M AUTOEXTEND ON NEXT 64M MAXSIZE 2048M"
            ],
        )
        self.assertFalse(plan["reversible"])

    def test_add_datafile_refuses_bigfile_and_max_below_size(self):
        self.files.return_value = [_file("big.dbf", 10, bigfile="YES")]
        with self.assertRaisesRegex(ValueError, "bigfile"):
            self.plan("add_tablespace_datafile")
        self.files.return_value = [_file("a.dbf", 10)]
        with self.assertRaises(ValueError):
            self.plan("add_tablespace_datafile", size_mb=500, max_mb=100)

    # --- execution --------------------------------------------------------

    def test_execution_refuses_when_statements_changed_since_approval(self):
        self.files.return_value = [_file("space_ts01.dbf", 10)]
        with self.assertRaisesRegex(RuntimeError, "changed after approval"):
            remediation.execute_remediation(
                "enable_tablespace_autoextend",
                {"pdb_name": "ORCLPDB1", "tablespace_name": "SPACE_TS"},
                approved_statements=["ALTER DATABASE DATAFILE 'other.dbf' AUTOEXTEND ON"],
            )
        self.execute.assert_not_called()

    def test_execution_requires_approved_statements(self):
        self.files.return_value = [_file("space_ts01.dbf", 10)]
        with self.assertRaisesRegex(RuntimeError, "requires the approved statements"):
            remediation.execute_remediation(
                "enable_tablespace_autoextend",
                {"pdb_name": "ORCLPDB1", "tablespace_name": "SPACE_TS"},
            )
        self.execute.assert_not_called()

    def test_execution_runs_in_the_pdb_and_verifies_the_result(self):
        before = [_file("space_ts01.dbf", 10)]
        after = [_file("space_ts01.dbf", 10, autoextend="YES", max_mb=2048)]
        # plan (pin check), plan (executor), before, after
        self.files.side_effect = [before, before, before, after]
        approved = self.plan("enable_tablespace_autoextend")["statements"]
        self.files.side_effect = [before, before, before, after]
        self.execute.return_value = [{"statement": approved[0], "status": "OK"}]

        result = remediation.execute_remediation(
            "enable_tablespace_autoextend",
            {"pdb_name": "ORCLPDB1", "tablespace_name": "SPACE_TS"},
            approved_statements=approved,
        )

        self.execute.assert_called_once_with(approved, database_name="ORCLPDB1")
        self.assertTrue(result["succeeded"])
        self.assertIn("autoextend to 2048 MB", result["after"])

    def test_execution_reports_failure_when_file_did_not_change(self):
        before = [_file("space_ts01.dbf", 10)]
        self.files.side_effect = [before, before, before, before]
        approved = remediation._plan_enable_tablespace_autoextend(
            {"pdb_name": "ORCLPDB1", "tablespace_name": "SPACE_TS"}
        ).statements
        self.files.side_effect = [before, before, before, before]
        self.execute.return_value = [{"statement": approved[0], "status": "ERROR", "error": "ORA-01031"}]

        result = remediation.execute_remediation(
            "enable_tablespace_autoextend",
            {"pdb_name": "ORCLPDB1", "tablespace_name": "SPACE_TS"},
            approved_statements=approved,
        )
        self.assertFalse(result["succeeded"])


if __name__ == "__main__":
    unittest.main()
