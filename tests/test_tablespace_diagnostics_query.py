"""Shared-query tests for PDB tablespace diagnostics."""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("oracledb", SimpleNamespace())

from oracle_core.queries import get_tablespace_diagnostics


class TablespaceDiagnosticsQueryTests(unittest.TestCase):
    @patch("oracle_core.queries.query", return_value=[])
    @patch("oracle_core.queries.validate_pdb_name", return_value="ORCLPDB1")
    @patch("oracle_core.queries.get_settings")
    def test_uses_validated_pdb_and_bound_tablespace(
        self,
        get_settings,
        validate_pdb,
        query_mock,
    ) -> None:
        get_settings.return_value = SimpleNamespace(oracle_cdb_name="ORCLCDB")

        get_tablespace_diagnostics("orclpdb1", "system")

        validate_pdb.assert_called_once_with("orclpdb1")
        kwargs = query_mock.call_args.kwargs
        self.assertEqual(kwargs["database_name"], "ORCLCDB")
        self.assertEqual(
            kwargs["binds"],
            {"pdb_name": "ORCLPDB1", "tablespace_name": "SYSTEM"},
        )
        sql = query_mock.call_args.args[0]
        self.assertIn("autoextensible", sql.lower())
        self.assertIn("maxbytes", sql.lower())


if __name__ == "__main__":
    unittest.main()
