import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scenarios.py"
spec = importlib.util.spec_from_file_location("scenarios", SCRIPT)
scenarios = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenarios)


class ScenarioSafetyLockTests(unittest.TestCase):
    """The runner degrades databases on purpose: it must refuse by default."""

    def run_main(self, argv, env):
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("sys.argv", ["scenarios.py", *argv]):
            return scenarios.main()

    def test_refuses_without_the_lab_flag(self):
        for env in ({}, {"LAB_SCENARIOS_ENABLED": "false"}, {"LAB_SCENARIOS_ENABLED": "yes"}):
            with self.subTest(env=env), self.assertRaises(SystemExit) as ctx:
                self.run_main(["tablespace-full"], env)
            self.assertIn("scenarios are disabled", str(ctx.exception))

    def test_list_needs_no_flag_or_database(self):
        self.assertEqual(self.run_main(["list"], {}), 0)


if __name__ == "__main__":
    unittest.main()
