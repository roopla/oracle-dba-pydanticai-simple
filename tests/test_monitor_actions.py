import unittest

from monitor.actions import get_approved_actions
from monitor.models import ApprovedActionType, IssueType


class MonitorApprovedActionsTests(unittest.TestCase):
    def test_tablespace_incident_returns_baked_sql(self):
        actions = get_approved_actions(
            IssueType.TABLESPACE_USAGE,
            {
                "con_name": "ORCLPDB1",
                "tablespace_name": "SYSTEM",
                "pct_used": 99.31,
            },
        )
        self.assertEqual(len(actions), 3)
        self.assertIn("FROM dba_data_files", actions[0].sql)
        self.assertIn("tablespace_name = 'SYSTEM'", actions[0].sql)
        self.assertIn("ORCLPDB1", actions[0].sql)
        self.assertEqual(actions[2].action_type, ApprovedActionType.REMEDIATION_TEMPLATE)
        self.assertTrue(actions[2].requires_review)

    def test_blocking_session_actions_are_incident_specific(self):
        actions = get_approved_actions(
            IssueType.BLOCKING_SESSION,
            {
                "blocking_sid": 24,
                "blocking_serial": 101,
                "waiting_sid": 462,
            },
        )
        self.assertEqual(len(actions), 3)
        self.assertIn("blocker.sid = 24", actions[0].sql)
        self.assertIn("waiter.sid = 462", actions[0].sql)
        self.assertIn("WHERE s.sid = 24", actions[1].sql)
        self.assertIn("ALTER SYSTEM KILL SESSION '24,101' IMMEDIATE", actions[2].sql)
        self.assertTrue(actions[2].requires_review)

    def test_long_running_query_actions_use_sid_and_sql_id(self):
        actions = get_approved_actions(
            IssueType.LONG_RUNNING_QUERY,
            {"sid": 55, "serial_num": 999, "sql_id": "abc123"},
        )
        self.assertEqual(len(actions), 3)
        self.assertIn("WHERE s.sid = 55", actions[0].sql)
        self.assertIn("WHERE sql_id = 'abc123'", actions[1].sql)
        self.assertIn("ALTER SYSTEM KILL SESSION '55,999' IMMEDIATE", actions[2].sql)

    def test_alert_log_actions_filter_the_detected_ora_code(self):
        actions = get_approved_actions(
            IssueType.ALERT_LOG_ERROR,
            {"message_text": "ORA-00600: internal error code"},
        )
        self.assertEqual(len(actions), 2)
        self.assertIn("v$diag_alert_ext", actions[0].sql)
        self.assertIn("'ORA-00600'", actions[0].sql)
        self.assertIn("FROM v$instance", actions[1].sql)

    def test_wait_event_actions_quote_event_safely(self):
        actions = get_approved_actions(
            IssueType.WAIT_EVENT,
            {"event": "enq: TX - row lock contention"},
        )
        self.assertEqual(len(actions), 2)
        self.assertIn("FROM v$system_event", actions[0].sql)
        self.assertIn("event = 'enq: TX - row lock contention'", actions[0].sql)
        self.assertIn("FROM v$session", actions[1].sql)

    def test_write_workload_actions_cover_counters_dml_and_redo(self):
        actions = get_approved_actions(IssueType.WRITE_WORKLOAD, {})
        self.assertEqual(len(actions), 4)
        combined = "\n".join(action.sql for action in actions)
        self.assertIn("FROM v$sysstat", combined)
        self.assertIn("FROM v$sql", combined)
        self.assertIn("command_type IN (2, 3, 6)", combined)
        self.assertIn("event = 'log file sync'", combined)
        self.assertIn("FROM v$log_history", combined)
        self.assertTrue(all(not action.requires_review for action in actions))

    def test_quotes_tablespace_literal_safely(self):
        actions = get_approved_actions(
            IssueType.TABLESPACE_USAGE,
            {"con_name": "ORCLPDB1", "tablespace_name": "ODD'TS"},
        )
        self.assertIn("'ODD''TS'", actions[0].sql)

    def test_quotes_wait_event_literal_safely(self):
        actions = get_approved_actions(
            IssueType.WAIT_EVENT,
            {"event": "custom ' wait"},
        )
        self.assertIn("'custom '' wait'", actions[0].sql)


if __name__ == "__main__":
    unittest.main()
