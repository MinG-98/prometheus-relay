import unittest

from webapp.qr_login import (
    QRLoginManager,
    QRLoginStateError,
    _QRSession,
    extract_profile,
    sanitise_browser_cookies,
)


class QRLoginTests(unittest.TestCase):
    def test_extracts_profile_from_creator_user_info(self):
        unique_id, username = extract_profile(
            "/aweme/v1/creator/user/info/",
            {
                "douyin_user_verify_info": {
                    "douyin_unique_id": "owner_123",
                    "nick_name": "测试账号",
                }
            },
        )

        self.assertEqual(unique_id, "owner_123")
        self.assertEqual(username, "测试账号")

    def test_extracts_profile_from_media_user_info_fallback(self):
        unique_id, username = extract_profile(
            "/web/api/media/user/info/",
            {"user": {"short_id": 123456, "nickname": "备用账号"}},
        )

        self.assertEqual(unique_id, "123456")
        self.assertEqual(username, "备用账号")

    def test_browser_cookies_are_limited_to_douyin_without_export_metadata(self):
        cookies = sanitise_browser_cookies(
            [
                {
                    "name": "sessionid",
                    "value": "safe-test-session",
                    "domain": ".douyin.com",
                    "path": "/",
                    "secure": True,
                    "hostOnly": False,
                    "expirationDate": 9999999999,
                },
                {
                    "name": "third_party",
                    "value": "ignored",
                    "domain": ".example.com",
                    "path": "/",
                },
            ]
        )

        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "sessionid")
        self.assertNotIn("hostOnly", cookies[0])
        self.assertNotIn("expirationDate", cookies[0])

    def test_browser_cookies_require_an_authenticated_session(self):
        with self.assertRaisesRegex(ValueError, "登录凭证"):
            sanitise_browser_cookies(
                [
                    {
                        "name": "csrf_session_id",
                        "value": "not-a-login",
                        "domain": ".douyin.com",
                    }
                ]
            )

    def test_probe_marks_active_session_and_wakes_browser(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="waiting_scan",
            message="等待扫码",
            started_at=0,
            expires_at=9999999999,
        )
        manager._session = session

        result = manager.probe()

        self.assertEqual(result["status"], "scanned")
        self.assertTrue(session.probe_event.is_set())
        self.assertIn("检查手机确认", result["message"])

    def test_probe_requires_an_active_session(self):
        with self.assertRaisesRegex(QRLoginStateError, "没有扫码登录会话"):
            QRLoginManager().probe()


if __name__ == "__main__":
    unittest.main()
