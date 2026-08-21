import unittest

from webapp.config_store import normalise_config, public_config


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


if __name__ == "__main__":
    unittest.main()
