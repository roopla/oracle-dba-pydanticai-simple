"""Unit tests for the read-only DBA query helpers and MCP wrappers."""

import unittest
from unittest.mock import patch

from mcp_server import server
from oracle_core import queries


class DbaQueryTests(unittest.TestCase):
    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_active_sessions_uses_cdb_and_limit(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"sid": 10}]

        result = queries.get_active_sessions(limit=5)

        self.assertEqual(result, [{"sid": 10}])
        _, kwargs = mock_query.call_args
        self.assertEqual(kwargs["database_name"], "ORCL")
        self.assertEqual(kwargs["binds"], {"limit": 5})
        self.assertIn("v$session", mock_query.call_args.args[0].lower())

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_blocking_sessions_uses_blocking_session_relationship(
        self, mock_settings, mock_query
    ):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"blocking_sid": 24, "waiting_sid": 462}]

        result = queries.get_blocking_sessions(limit=7)

        self.assertEqual(result[0]["blocking_sid"], 24)
        sql = mock_query.call_args.args[0].lower()
        self.assertIn("waiter.blocking_session", sql)
        self.assertEqual(mock_query.call_args.kwargs["binds"], {"limit": 7})

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_blocking_sessions_ignore_background_processes(
        self, mock_settings, mock_query
    ):
        # A 'log file sync' waiter names LGWR as its blocker; that is not
        # blocking, and killing the "blocker" would crash the instance.
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = []

        queries.get_blocking_sessions()

        sql = mock_query.call_args.args[0].lower()
        self.assertIn("waiter.type = 'user'", sql)
        self.assertIn("blocker.type = 'user'", sql)

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_top_sql_passes_time_window_and_limit(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"sql_id": "abc123"}]

        result = queries.get_top_sql(limit=8, active_within_minutes=30)

        self.assertEqual(result, [{"sql_id": "abc123"}])
        binds = mock_query.call_args.kwargs["binds"]
        self.assertEqual(binds, {"active_minutes": 30, "limit": 8})
        self.assertIn("s.elapsed_time", mock_query.call_args.args[0].lower())

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_wait_events_excludes_idle_waits(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"event": "log file sync"}]

        result = queries.get_wait_events(limit=6)

        self.assertEqual(result, [{"event": "log file sync"}])
        sql = mock_query.call_args.args[0].lower()
        self.assertIn("wait_class != 'idle'", sql)
        self.assertEqual(mock_query.call_args.kwargs["binds"], {"limit": 6})


    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_tablespace_usage_is_shared_query(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"tablespace_name": "SYSTEM", "pct_used": 91.0}]

        result = queries.get_tablespace_usage()

        self.assertEqual(result[0]["pct_used"], 91.0)
        sql = mock_query.call_args.args[0].lower()
        self.assertIn("cdb_data_files", sql)
        self.assertIn("cdb_free_space", sql)


    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.validate_pdb_name")
    @patch("oracle_core.queries.get_settings")
    def test_tablespace_usage_can_filter_one_pdb(
        self, mock_settings, mock_validate, mock_query
    ):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_validate.return_value = "ORCLPDB2"
        mock_query.return_value = [
            {"con_name": "ORCLPDB2", "tablespace_name": "USERS"}
        ]

        result = queries.get_tablespace_usage("orclpdb2")

        self.assertEqual(result[0]["con_name"], "ORCLPDB2")
        mock_validate.assert_called_once_with("orclpdb2")
        self.assertEqual(
            mock_query.call_args.kwargs["database_name"],
            "ORCL",
        )
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
    def test_list_users_queries_validated_pdb(
        self, mock_validate, mock_query
    ):
        mock_validate.return_value = "ORCLPDB2"
        mock_query.return_value = [
            {"username": "APPUSER", "account_status": "OPEN"}
        ]

        result = queries.list_users("orclpdb2")

        self.assertEqual(result[0]["username"], "APPUSER")
        mock_validate.assert_called_once_with("orclpdb2")
        self.assertEqual(
            mock_query.call_args.kwargs["database_name"],
            "ORCLPDB2",
        )
        self.assertIn("from dba_users", mock_query.call_args.args[0].lower())

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_long_running_sessions_uses_threshold(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"sid": 42, "seconds_running": 600}]

        result = queries.get_long_running_sessions(300)

        self.assertEqual(result[0]["sid"], 42)
        self.assertEqual(mock_query.call_args.kwargs["binds"], {"threshold": 300})

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_alert_log_errors_is_shared_query(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"message_text": "ORA-00600 test"}]

        result = queries.get_alert_log_errors(limit=12)

        self.assertEqual(result[0]["message_text"], "ORA-00600 test")
        self.assertEqual(mock_query.call_args.kwargs["binds"], {"limit": 12})
        self.assertIn("v$diag_alert_ext", mock_query.call_args.args[0].lower())

    @patch("oracle_core.queries.query")
    @patch("oracle_core.queries.get_settings")
    def test_write_workload_counters_is_shared_query(self, mock_settings, mock_query):
        mock_settings.return_value.oracle_cdb_name = "ORCL"
        mock_query.return_value = [{"name": "user commits", "value": 100}]

        result = queries.get_write_workload_counters()

        self.assertEqual(result, [{"name": "user commits", "value": 100}])
        self.assertIn("v$sysstat", mock_query.call_args.args[0].lower())

    def test_limits_are_validated_before_querying(self):
        with self.assertRaises(ValueError):
            queries.get_active_sessions(limit=0)
        with self.assertRaises(ValueError):
            queries.get_blocking_sessions(limit=101)
        with self.assertRaises(ValueError):
            queries.get_top_sql(active_within_minutes=0)
        with self.assertRaises(ValueError):
            queries.get_wait_events(limit=51)


class DbaMcpWrapperTests(unittest.TestCase):
    @patch("mcp_server.server.query_active_sessions")
    def test_active_sessions_wrapper(self, mock_query):
        mock_query.return_value = [{"sid": 11}]
        self.assertEqual(server.get_active_sessions(4), [{"sid": 11}])
        mock_query.assert_called_once_with(limit=4)

    @patch("mcp_server.server.query_blocking_sessions")
    def test_blocking_sessions_wrapper(self, mock_query):
        mock_query.return_value = [{"blocking_sid": 24}]
        self.assertEqual(server.get_blocking_sessions(3), [{"blocking_sid": 24}])
        mock_query.assert_called_once_with(limit=3)

    @patch("mcp_server.server.query_top_sql")
    def test_top_sql_wrapper(self, mock_query):
        mock_query.return_value = [{"sql_id": "abc"}]
        self.assertEqual(server.get_top_sql(5, 15), [{"sql_id": "abc"}])
        mock_query.assert_called_once_with(limit=5, active_within_minutes=15)


    @patch("mcp_server.server.query_tablespace_usage")
    def test_tablespace_usage_wrapper(self, mock_query):
        mock_query.return_value = [{"tablespace_name": "USERS"}]
        self.assertEqual(
            server.get_tablespace_usage("ORCLPDB2"),
            [{"tablespace_name": "USERS"}],
        )
        mock_query.assert_called_once_with(pdb_name="ORCLPDB2")

    @patch("mcp_server.server.query_list_users")
    def test_list_users_wrapper(self, mock_query):
        mock_query.return_value = [{"username": "APPUSER"}]
        self.assertEqual(
            server.list_users("ORCLPDB2"),
            [{"username": "APPUSER"}],
        )
        mock_query.assert_called_once_with(pdb_name="ORCLPDB2")

    @patch("mcp_server.server.query_wait_events")
    def test_wait_events_wrapper(self, mock_query):
        mock_query.return_value = [{"event": "log file sync"}]
        self.assertEqual(server.get_wait_events(2), [{"event": "log file sync"}])
        mock_query.assert_called_once_with(limit=2)


if __name__ == "__main__":
    unittest.main()
