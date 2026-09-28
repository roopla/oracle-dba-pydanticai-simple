"""Regression tests for bugs found while rehearsing a fresh deployment."""

import os
import unittest
from unittest import mock

from oracle_core import queries_advanced, remediation


class AshWindowTests(unittest.TestCase):
    """sample_time has no time zone; the window must not add one."""

    def test_window_compares_on_the_server_clock_without_time_zone(self):
        with mock.patch.object(queries_advanced, "validate_pdb_name", side_effect=str.upper), \
             mock.patch.object(queries_advanced, "query", return_value=[]) as query:
            for group_by in queries_advanced.ASH_GROUP_BY_CHOICES:
                queries_advanced.get_ash_activity(10, "PDB1", group_by, 5)

        for call in query.call_args_list:
            sql = " ".join(call.args[0].split())
            self.assertIn("sample_time >= CAST(SYSTIMESTAMP AS TIMESTAMP)", sql)
            # Bare SYSTIMESTAMP would convert sample_time with the session
            # (client) time zone and shift the window by the offset.
            self.assertNotIn("sample_time >= SYSTIMESTAMP", sql)


class SequenceGapTests(unittest.TestCase):
    def test_applied_counts_in_memory_logs_and_mrp_position(self):
        sql = " ".join(queries_advanced.SQL_SEQUENCE_GAP.split())
        self.assertIn("applied IN ('YES', 'IN-MEMORY')", sql)
        self.assertIn("FROM v$managed_standby m", sql)
        self.assertIn("GREATEST(last_received_seq - last_applied_seq, 0)", sql)
        self.assertIn("= 'PHYSICAL STANDBY'", sql)


class ActionAvailabilityTests(unittest.TestCase):
    STANDBY = {"ORACLE_STANDBY_DSN": "stby:1521/CDB_STBY", "ORACLE_STANDBY_PASSWORD": "x"}
    SSH = {
        "STANDBY_SSH_HOST": "stbyhost",
        "STANDBY_SSH_USER": "ops",
        "STANDBY_SSH_PASSWORD": "pw",
        "STANDBY_CONTAINER": "stby",
    }

    def availability(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return {a["action"]: a for a in remediation.list_actions()}

    def test_standby_restart_is_unavailable_without_ssh_settings(self):
        actions = self.availability(self.STANDBY)
        self.assertTrue(actions["restart_redo_apply"]["available"])
        self.assertFalse(actions["restart_standby_instance"]["available"])
        self.assertIn("STANDBY_SSH_HOST", actions["restart_standby_instance"]["unavailable_reason"])

    def test_standby_actions_are_unavailable_without_a_standby(self):
        actions = self.availability({})
        self.assertFalse(actions["restart_redo_apply"]["available"])
        self.assertIn("ORACLE_STANDBY_DSN", actions["restart_redo_apply"]["unavailable_reason"])

    def test_everything_is_available_when_configured(self):
        actions = self.availability({**self.STANDBY, **self.SSH})
        self.assertTrue(all(a["available"] for a in actions.values()))
        self.assertNotIn("unavailable_reason", actions["restart_standby_instance"])


if __name__ == "__main__":
    unittest.main()
