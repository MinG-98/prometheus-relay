import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webapp import config_store
from webapp.config_store import normalise_config, public_config
from webapp.task_runner import _task_environment


def payload(schedule):
    return {
        "settings": {"schedule": schedule},
        "accounts": [
            {
                "username": "test",
                "unique_id": "test123",
                "targets": ["friend123"],
                "cookies": [{"name": "session", "value": "safe-test-value"}],
            }
        ],
    }


class WebConfigTests(unittest.TestCase):
    def test_schedule_is_normalised_and_exposed_without_cookies(self):
        config = normalise_config(
            payload({"enabled": True, "time": "21:30", "timezone": "America/New_York"})
        )
        public = public_config(config)

        self.assertEqual(public["settings"]["schedule"], {
            "enabled": True,
            "time": "21:30",
            "timezone": "America/New_York",
        })
        self.assertNotIn("cookies", public["accounts"][0])

    def test_invalid_schedule_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            normalise_config(payload({"enabled": True, "time": "9:30", "timezone": "UTC"}))

        with self.assertRaisesRegex(ValueError, "时区"):
            normalise_config(payload({"enabled": True, "time": "09:30", "timezone": "Not/AZone"}))

    def test_task_environment_keeps_runtime_settings_but_not_admin_credentials(self):
        config = normalise_config(
            {
                **payload({"enabled": False, "time": "09:00", "timezone": "Asia/Shanghai"}),
                "settings": {
                    "proxyAddress": "http://proxy.example:8080",
                    "schedule": {"enabled": False, "time": "09:00", "timezone": "Asia/Shanghai"},
                },
            }
        )
        with patch.dict(
            os.environ,
            {"ADMIN_USERNAME": "admin-test", "ADMIN_PASSWORD": "secret-test"},
            clear=False,
        ):
            environment = _task_environment(config)

        self.assertEqual(environment["PROXY_ADDRESS"], "http://proxy.example:8080")
        self.assertEqual(environment["HEADLESS"], "true")
        self.assertNotIn("ADMIN_USERNAME", environment)
        self.assertNotIn("ADMIN_PASSWORD", environment)


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        data_directory = Path(self.temporary_directory.name)
        replacements = {
            "DATA_DIR": data_directory,
            "CONFIG_PATH": data_directory / "config.json",
            "STATUS_PATH": data_directory / "status.json",
            "LOG_PATH": data_directory / "task.log",
            "LOCK_PATH": data_directory / "task.lock",
            "HISTORY_PATH": data_directory / "history.json",
            "SCHEDULER_STATUS_PATH": data_directory / "schedule-status.json",
        }
        self.patchers = [patch.object(config_store, name, value) for name, value in replacements.items()]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_run_history_is_bounded_and_returned_oldest_first(self):
        for index in range(5):
            config_store.append_history({"startedAt": str(index)}, max_items=3)

        history = config_store.read_history()

        self.assertEqual([item["startedAt"] for item in history], ["2", "3", "4"])

    def test_scheduler_status_round_trip(self):
        expected = {
            "heartbeatAt": "2026-08-21T12:00:00+00:00",
            "enabled": True,
            "state": "idle",
        }

        config_store.write_scheduler_status(expected)

        self.assertEqual(config_store.read_scheduler_status(), expected)


if __name__ == "__main__":
    unittest.main()
