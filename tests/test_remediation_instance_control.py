import os
import unittest
from unittest import mock

from oracle_core import remediation
from oracle_core.remediation import StandbyHost


FULL_ENV = {
    "ORACLE_STANDBY_DSN": "db:1522/ORCL_STBY",
    "ORACLE_STANDBY_PASSWORD": "x",
    "STANDBY_SSH_HOST": "db",
    "STANDBY_SSH_USER": "ops",
    "STANDBY_SSH_PASSWORD": "pw",
    "STANDBY_CONTAINER": "oracle19c-stby",
}

TARGET = StandbyHost(
    host="db",
    port=22,
    user="ops",
    password="pw",
    key_file=None,
    container="oracle19c-stby",
)


class StandbyHostSettingsTests(unittest.TestCase):
    def test_complete_settings_are_accepted(self):
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            target = remediation._standby_host()
        self.assertEqual(target.port, 22)
        self.assertEqual(target.container, "oracle19c-stby")

    def test_missing_settings_are_named(self):
        env = {k: v for k, v in FULL_ENV.items() if k != "STANDBY_SSH_HOST"}
        env.pop("STANDBY_SSH_PASSWORD")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                remediation._standby_host()
        self.assertIn("STANDBY_SSH_HOST", str(ctx.exception))
        self.assertIn("STANDBY_SSH_PASSWORD or STANDBY_SSH_KEY_FILE", str(ctx.exception))

    def test_container_name_cannot_carry_shell_syntax(self):
        env = {**FULL_ENV, "STANDBY_CONTAINER": "stby; rm -rf /"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                remediation._standby_host()


class InstanceStepTests(unittest.TestCase):
    def test_success_marker_without_errors_is_ok(self):
        with mock.patch.object(
            remediation, "_container_sqlplus", return_value="ORACLE instance shut down.\n"
        ):
            result = remediation._instance_step(
                TARGET, "SHUTDOWN ABORT", "ORACLE instance shut down."
            )
        self.assertEqual(result["status"], "OK")

    def test_ora_error_fails_even_with_marker(self):
        output = "Database mounted.\nORA-01102: cannot mount database in EXCLUSIVE mode\n"
        with mock.patch.object(remediation, "_container_sqlplus", return_value=output):
            result = remediation._instance_step(TARGET, "STARTUP MOUNT", "Database mounted.")
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("ORA-01102", result["error"])

    def test_ssh_failure_is_reported_not_raised(self):
        with mock.patch.object(
            remediation, "_container_sqlplus", side_effect=OSError("no route to host")
        ):
            result = remediation._instance_step(TARGET, "STARTUP MOUNT", "Database mounted.")
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("no route to host", result["error"])


class RestartStandbyInstanceTests(unittest.TestCase):
    def test_plan_refused_when_instance_control_not_configured(self):
        env = {
            "ORACLE_STANDBY_DSN": "db:1522/ORCL_STBY",
            "ORACLE_STANDBY_PASSWORD": "x",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError) as ctx:
                remediation.plan_remediation("restart_standby_instance")
        self.assertIn("unavailable", str(ctx.exception))

    def test_failed_shutdown_stops_before_startup(self):
        standby = {"database_role": "PHYSICAL STANDBY", "open_mode": "MOUNTED"}
        with mock.patch.dict(os.environ, FULL_ENV, clear=True), mock.patch.object(
            remediation, "_standby_apply_state", return_value=standby
        ), mock.patch.object(
            remediation, "_container_sqlplus", return_value="ORA-01031: insufficient privileges"
        ) as sqlplus:
            result = remediation.execute_remediation("restart_standby_instance")

        self.assertFalse(result["succeeded"])
        self.assertEqual([s["statement"] for s in result["statements"]], ["SHUTDOWN ABORT"])
        sqlplus.assert_called_once()

    def test_refuses_to_touch_a_primary(self):
        primary = {"database_role": "PRIMARY", "open_mode": "READ WRITE"}
        with mock.patch.dict(os.environ, FULL_ENV, clear=True), mock.patch.object(
            remediation, "_standby_apply_state", return_value=primary
        ), mock.patch.object(remediation, "_container_sqlplus") as sqlplus:
            with self.assertRaises(RuntimeError):
                remediation.execute_remediation("restart_standby_instance")
        sqlplus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
