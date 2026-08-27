import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from webapp import main, tenant_store


class PortalApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        data_directory = Path(self.temporary_directory.name)
        self.patchers = [
            patch.object(tenant_store, "DATA_DIR", data_directory),
            patch.object(tenant_store, "DATABASE_PATH", data_directory / "relay.sqlite3"),
            patch.object(tenant_store, "LEGACY_CONFIG_PATH", data_directory / "config.json"),
            patch.object(tenant_store, "RUNS_DIR", data_directory / "runs"),
            patch.object(tenant_store, "LOCK_PATH", data_directory / "task.lock"),
            patch.object(tenant_store, "SCHEDULER_STATUS_PATH", data_directory / "schedule-status.json"),
            patch.object(main, "AUTH_ENABLED", True),
            patch.object(main, "SESSION_COOKIE_SECURE", False),
            patch.dict(
                os.environ,
                {
                    "AUTH_ENABLED": "true",
                    "ADMIN_USERNAME": "portal-admin",
                    "ADMIN_PASSWORD": "portal-admin-password",
                    "PROMETHEUS_RELAY_COOKIE_KEY": Fernet.generate_key().decode("ascii"),
                },
                clear=False,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def _login(self, username, password):
        response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_customer_api_is_scoped_and_admin_api_does_not_return_cookies(self):
        admin = self._login("portal-admin", "portal-admin-password")
        self.assertEqual(admin["role"], "platform_admin")
        created = self.client.post(
            "/api/admin/users",
            json={
                "username": "portal-customer",
                "displayName": "门户客户",
                "password": "portal-customer-password",
                "maxAccounts": 2,
                "maxTargets": 4,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        customer_id = created.json()["user"]["id"]

        customer = TestClient(main.app)
        customer.__enter__()
        try:
            login = customer.post(
                "/api/auth/login",
                json={"username": "portal-customer", "password": "portal-customer-password"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            self.assertEqual(login.json()["user"]["workspaceId"], created.json()["user"]["workspaceId"])

            self.assertEqual(customer.get("/api/admin/users").status_code, 403)
            self.assertEqual(customer.get("/api/log").status_code, 403)
            config_response = customer.post(
                "/api/config",
                json={
                    "settings": {
                        "messageTemplate": "客户消息",
                        "hitokotoTypes": ["文学"],
                        "matchMode": "short_id",
                        "schedule": {
                            "enabled": False,
                            "time": "09:00",
                            "timezone": "Asia/Shanghai",
                        },
                    },
                    "accounts": [{
                        "unique_id": "portal-account",
                        "username": "门户抖音号",
                        "targets": ["target-001"],
                        "cookies": [{"name": "sessionid", "value": "portal-secret"}],
                    }],
                },
            )
            self.assertEqual(config_response.status_code, 200, config_response.text)
            public_config = config_response.json()["config"]
            self.assertNotIn("cookies", public_config["accounts"][0])
            self.assertEqual(customer.get("/api/state").json()["config"]["accounts"][0]["unique_id"], "portal-account")
        finally:
            customer.__exit__(None, None, None)

        admin_accounts = self.client.get("/api/admin/accounts")
        self.assertEqual(admin_accounts.status_code, 200, admin_accounts.text)
        self.assertEqual(admin_accounts.json()["accounts"][0]["ownerUsername"], "portal-customer")
        self.assertNotIn("portal-secret", admin_accounts.text)
        customer_state = self.client.get(f"/api/admin/users/{customer_id}/state")
        self.assertEqual(customer_state.status_code, 200, customer_state.text)
        self.assertEqual(customer_state.json()["limits"]["maxAccounts"], 2)


if __name__ == "__main__":
    unittest.main()
