import json
import os
import unittest
from unittest.mock import patch

from utils import config as config_module


class ConfigTests(unittest.TestCase):
    def setUp(self):
        config_module.config = None
        config_module.userData = None

    def tearDown(self):
        config_module.config = None
        config_module.userData = None

    def test_invalid_json_uses_fallback(self):
        fallback = ["文学"]
        self.assertEqual(
            config_module._parse_json_list("not-json", fallback, "TEST"),
            fallback,
        )

    def test_log_level_is_normalised(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "debug"}, clear=False):
            self.assertEqual(config_module.get_config()["logLevel"], "Debug")

    def test_sanitize_cookies_does_not_mutate_input(self):
        cookies = [
            {"name": "session", "value": "中文", "sameSite": "Lax"},
        ]

        sanitized = config_module.sanitize_cookies(cookies)

        self.assertEqual(sanitized, [{"name": "session", "value": "中文"}])
        self.assertIn("sameSite", cookies[0])

    def test_invalid_account_is_skipped_without_corrupting_unicode(self):
        tasks = [
            {"username": "invalid", "unique_id": "bad", "targets": []},
            {"username": "valid", "unique_id": "good", "targets": ["好友"]},
        ]
        valid_cookies = json.dumps(
            [{"name": "session", "value": "中文", "sameSite": "Lax"}],
            ensure_ascii=False,
        )

        with patch.dict(
            os.environ,
            {
                "TASKS": json.dumps(tasks, ensure_ascii=False),
                "COOKIES_BAD": "not-json",
                "COOKIES_GOOD": valid_cookies,
            },
            clear=False,
        ):
            users = config_module.get_userData()

        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], "valid")
        self.assertEqual(users[0]["cookies"][0]["value"], "中文")
        self.assertNotIn("sameSite", users[0]["cookies"][0])


if __name__ == "__main__":
    unittest.main()
