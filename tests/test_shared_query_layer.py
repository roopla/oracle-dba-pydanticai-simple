"""Tests for Oracle queries shared by MCP and monitor checks."""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("oracledb", SimpleNamespace())

from oracle_core import queries


class SharedQueryLayerTests(unittest.TestCase):
    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_tablespace_query_lives_in_oracle_core(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"tablespace_name": "SYSTEM", "pct_used": 91.2}]

        rows = queries.get_tablespace_usage()

        self.assertEqual(rows[0]["pct_used"], 91.2)
        sql = mock_query.call_args.args[0].lower()
        self.assertIn("cdb_data_files", sql)
        self.assertIn("cdb_free_space", sql)
        self.assertEqual(mock_query.call_args.kwargs["database_name"], "ORCL")


    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.validate_pdb_name")
    @patch("oracle_core.queries.get_settings")
    def test_tablespace_query_can_filter_validated_pdb(
        self, mock_settings, mock_validate, mock_query
    ):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_validate.return_value = "ORCLPDB2"
        mock_query.return_value = [
            {"con_name": "ORCLPDB2", "tablespace_name": "USERS"}
        ]

        rows = queries.get_tablespace_usage("orclpdb2")

        self.assertEqual(rows[0]["con_name"], "ORCLPDB2")
        mock_validate.assert_called_once_with("orclpdb2")
        self.assertEqual(
            mock_query.call_args.kwargs["binds"],
            {"pdb_name": "ORCLPDB2"},
        )
        self.assertIn(
            "upper(c.name) = :pdb_name",
            mock_query.call_args.args[0].lower(),
        )

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.validate_pdb_name")
    def test_list_users_queries_the_validated_pdb(
        self, mock_validate, mock_query
    ):
        mock_validate.return_value = "ORCLPDB2"
        mock_query.return_value = [
            {"username": "APPUSER", "account_status": "OPEN"}
        ]

        rows = queries.list_users("orclpdb2")

        self.assertEqual(rows[0]["username"], "APPUSER")
        mock_validate.assert_called_once_with("orclpdb2")
        self.assertEqual(
            mock_query.call_args.kwargs["database_name"],
            "ORCLPDB2",
        )
        self.assertIn(
            "from dba_users",
            mock_query.call_args.args[0].lower(),
        )

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_blocking_query_is_reusable(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"blocking_sid": 24, "waiting_sid": 462}]

        rows = queries.get_blocking_sessions(limit=25)

        self.assertEqual(rows[0]["waiting_sid"], 462)
        self.assertEqual(mock_query.call_args.kwargs["binds"], {"limit": 25})

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_long_running_query_accepts_monitor_threshold(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"sid": 42, "seconds_running": 600}]

        rows = queries.get_long_running_sessions(300)

        self.assertEqual(rows[0]["sid"], 42)
        self.assertEqual(mock_query.call_args.kwargs["binds"], {"threshold": 300})

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_alert_log_query_is_reusable(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"message_text": "ORA-00600 test"}]

        rows = queries.get_alert_log_errors(limit=20)

        self.assertEqual(rows[0]["message_text"], "ORA-00600 test")
        self.assertIn("v$diag_alert_ext", mock_query.call_args.args[0].lower())

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_wait_event_query_is_reusable(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"event": "log file sync"}]

        rows = queries.get_wait_events(limit=6)

        self.assertEqual(rows[0]["event"], "log file sync")
        self.assertEqual(mock_query.call_args.kwargs["binds"], {"limit": 6})

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_write_counters_are_reusable(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"name": "user commits", "value": 100}]

        rows = queries.get_write_workload_counters()

        self.assertEqual(rows[0]["value"], 100)
        sql = mock_query.call_args.args[0].lower()
        self.assertIn("v$sysstat", sql)
        self.assertIn("user commits", sql)


if __name__ == "__main__":
    unittest.main()
