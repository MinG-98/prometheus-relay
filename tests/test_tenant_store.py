import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from webapp import tenant_store
from webapp.tenant_store import (
    AccountOwnershipError,
    TenantStoreError,
    authenticate_user,
    change_password,
    create_customer,
    create_session,
    get_public_workspace_config,
    get_session_user,
    get_user_by_id,
    get_workspace_config,
    get_workspace_limits,
    get_workspace_state,
    initialize_store,
    platform_admin_workspace_id,
    save_workspace_config,
    delete_customer,
    update_customer,
)


class TenantStoreTests(unittest.TestCase):
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
            patch.dict(
                os.environ,
                {
                    "AUTH_ENABLED": "true",
                    "ADMIN_USERNAME": "admin-test",
                    "ADMIN_PASSWORD": "admin-password-123",
                    "PROMETHEUS_RELAY_COOKIE_KEY": Fernet.generate_key().decode("ascii"),
                },
                clear=False,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()
        initialize_store()
        self.admin = authenticate_user("admin-test", "admin-password-123")
        self.assertIsNotNone(self.admin)

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    @staticmethod
    def _payload(unique_id, cookie_value, target, message="续火花"):
        return {
            "settings": {
                "messageTemplate": message,
                "hitokotoTypes": ["文学"],
                "matchMode": "short_id",
                "schedule": {
                    "enabled": False,
                    "time": "09:00",
                    "timezone": "Asia/Shanghai",
                },
            },
            "accounts": [
                {
                    "unique_id": unique_id,
                    "username": f"账号-{unique_id}",
                    "targets": [target],
                    "cookies": [{
                        "name": "sessionid",
                        "value": cookie_value,
                        "domain": ".douyin.com",
                    }],
                }
            ],
        }

    def test_customer_data_and_cookies_are_scoped(self):
        customer_a = create_customer("customer-a", "customer-a-password", "客户 A")
        customer_b = create_customer("customer-b", "customer-b-password", "客户 B")
        workspace_a = customer_a["workspaceId"]
        workspace_b = customer_b["workspaceId"]

        save_workspace_config(
            workspace_a,
            self._payload("account-a", "cookie-a-secret", "target-a", "给 A 的消息"),
        )
        save_workspace_config(
            workspace_b,
            self._payload("account-b", "cookie-b-secret", "target-b", "给 B 的消息"),
        )

        public_a = get_public_workspace_config(workspace_a)
        public_b = get_public_workspace_config(workspace_b)
        self.assertEqual(public_a["accounts"][0]["unique_id"], "account-a")
        self.assertEqual(public_b["accounts"][0]["unique_id"], "account-b")
        self.assertNotIn("cookie-a-secret", json.dumps(public_a, ensure_ascii=False))
        self.assertNotIn("cookies", public_a["accounts"][0])
        self.assertEqual(public_a["accounts"][0]["cookieCount"], 1)

        stored_a = get_workspace_config(workspace_a, include_cookies=True)
        self.assertEqual(stored_a["accounts"][0]["cookies"][0]["value"], "cookie-a-secret")
        self.assertNotEqual(stored_a["settings"]["messageTemplate"], public_b["settings"]["messageTemplate"])

        raw_database = tenant_store.DATABASE_PATH.read_bytes()
        self.assertNotIn(b"cookie-a-secret", raw_database)
        self.assertNotIn(b"cookie-b-secret", raw_database)

    def test_account_cannot_be_claimed_by_another_workspace(self):
        customer_a = create_customer("owner-a", "owner-a-password", "所有者 A")
        customer_b = create_customer("owner-b", "owner-b-password", "所有者 B")
        save_workspace_config(
            customer_a["workspaceId"],
            self._payload("shared-account", "cookie-a", "target-a"),
        )

        with self.assertRaises(AccountOwnershipError):
            save_workspace_config(
                customer_b["workspaceId"],
                self._payload("shared-account", "cookie-b", "target-b"),
            )

        self.assertEqual(
            get_public_workspace_config(customer_a["workspaceId"])["accounts"][0]["targets"],
            ["target-a"],
        )
        self.assertEqual(get_public_workspace_config(customer_b["workspaceId"])["accounts"], [])

    def test_customer_save_preserves_system_settings_and_exposes_quota(self):
        customer = create_customer("limited-user", "limited-password", "受限客户", 2, 4)
        workspace_id = customer["workspaceId"]

        admin_payload = self._payload("system-account", "system-cookie", "system-target")
        admin_payload["settings"].update({
            "proxyAddress": "http://private-proxy.invalid:8080",
            "browserTimeout": 180000,
            "friendListTimeout": 3500,
            "taskRetryTimes": 5,
            "logLevel": "Debug",
        })
        save_workspace_config(workspace_id, admin_payload, role="platform_admin")

        customer_payload = self._payload("customer-account", "customer-cookie", "customer-target")
        save_workspace_config(workspace_id, customer_payload, role="workspace_owner")
        settings = get_public_workspace_config(workspace_id)["settings"]
        self.assertEqual(settings["proxyAddress"], "http://private-proxy.invalid:8080")
        self.assertEqual(settings["browserTimeout"], 180000)
        self.assertEqual(settings["friendListTimeout"], 3500)
        self.assertEqual(settings["taskRetryTimes"], 5)
        self.assertEqual(settings["logLevel"], "Debug")
        self.assertEqual(get_workspace_limits(workspace_id), {
            "maxAccounts": 2,
            "maxTargets": 4,
            "enabled": True,
        })
        self.assertEqual(get_workspace_state(workspace_id)["limits"]["maxAccounts"], 2)

    def test_sessions_are_revoked_when_password_changes_or_customer_is_disabled(self):
        customer = create_customer("session-user", "session-password", "会话客户")
        token = create_session(customer["id"])
        self.assertEqual(get_session_user(token)["username"], "session-user")

        change_password(customer["id"], "new-session-password")
        self.assertIsNone(get_session_user(token))
        self.assertIsNotNone(authenticate_user("session-user", "new-session-password"))

        update_customer(customer["id"], enabled=False)
        self.assertIsNone(authenticate_user("session-user", "new-session-password"))
        self.assertTrue(get_user_by_id(customer["id"])["enabled"] is False)

    def test_disabling_and_deleting_customer_also_stops_workspace_activity(self):
        customer = create_customer("lifecycle-user", "lifecycle-password", "生命周期客户")
        workspace_id = customer["workspaceId"]
        save_workspace_config(
            workspace_id,
            self._payload("lifecycle-account", "lifecycle-cookie", "lifecycle-target"),
        )

        update_customer(customer["id"], enabled=False)
        self.assertFalse(get_workspace_limits(workspace_id)["enabled"])
        delete_customer(customer["id"])
        self.assertEqual(get_public_workspace_config(workspace_id)["accounts"], [])
        with self.assertRaisesRegex(TenantStoreError, "工作区不存在"):
            get_workspace_limits(workspace_id)

    def test_legacy_json_is_migrated_to_the_private_admin_workspace(self):
        legacy_config = {
            "settings": {
                "messageTemplate": "旧配置",
                "hitokotoTypes": ["文学"],
                "matchMode": "short_id",
                "browserTimeout": 120000,
                "friendListTimeout": 2000,
                "taskRetryTimes": 3,
                "logLevel": "Info",
                "schedule": {
                    "enabled": False,
                    "time": "09:00",
                    "timezone": "Asia/Shanghai",
                },
            },
            "accounts": [{
                "unique_id": "legacy-account",
                "username": "旧账号",
                "targets": ["legacy-target"],
                "cookies": [{"name": "sessionid", "value": "legacy-secret"}],
            }],
        }
        tenant_store.LEGACY_CONFIG_PATH.write_text(
            json.dumps(legacy_config, ensure_ascii=False), encoding="utf-8"
        )
        initialize_store()

        migrated = get_workspace_config(platform_admin_workspace_id(), include_cookies=True)
        self.assertEqual(migrated["settings"]["messageTemplate"], "旧配置")
        self.assertEqual(migrated["accounts"][0]["unique_id"], "legacy-account")
        self.assertEqual(migrated["accounts"][0]["cookies"][0]["value"], "legacy-secret")


if __name__ == "__main__":
    unittest.main()
