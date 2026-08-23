import unittest
from unittest.mock import Mock, patch

from webapp.qr_login import (
    QRLoginManager,
    QRLoginStateError,
    _QRSession,
    extract_profile,
    extract_qr_connect_payload,
    extract_qr_connect_status,
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

    def test_extracts_profile_from_creator_pc_user_info(self):
        unique_id, username = extract_profile(
            "/aweme/v1/creator/pc/user/info/",
            {"user": {"unique_id": "pc_owner", "nickname": "PC账号"}},
        )

        self.assertEqual(unique_id, "pc_owner")
        self.assertEqual(username, "PC账号")

    def test_extracts_qr_connect_status(self):
        self.assertEqual(
            extract_qr_connect_status(
                "/passport/web/check_qrconnect/",
                {"data": {"status": "confirmed"}},
            ),
            "confirmed",
        )
        self.assertEqual(
            extract_qr_connect_status(
                "/passport/web/check_qrconnect/",
                {
                    "data": {
                        "status": "3",
                        "redirect_url": "https://creator.douyin.com/login/?ticket=x",
                    }
                },
            ),
            "confirmed",
        )
        self.assertEqual(
            extract_qr_connect_status(
                "/passport/web/check_qrconnect/",
                {"data": {"status": "2"}},
            ),
            "scanned",
        )
        self.assertEqual(
            extract_qr_connect_status(
                "/aweme/v1/creator/user/info/",
                {"data": {"status": "confirmed"}},
            ),
            "",
        )

    def test_extracts_secondary_verification_metadata_without_secrets(self):
        result = extract_qr_connect_payload(
            "/passport/web/check_qrconnect/",
            {
                "error_code": 2046,
                "data": {
                    "verify_center_secondary_decision_conf": "secret-config",
                    "sms_code_key": "secret-key",
                },
            },
        )

        self.assertTrue(result["verification_config_present"])
        self.assertTrue(result["sms_code_key_present"])
        self.assertNotIn("secret-config", str(result))
        self.assertNotIn("secret-key", str(result))

    def test_qr_connect_response_marks_session_as_scanned(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="waiting_scan",
            message="等待扫码",
            started_at=0,
            expires_at=9999999999,
        )
        manager._session = session
        response = Mock()
        response.url = "https://creator.douyin.com/passport/web/check_qrconnect/"
        response.json.return_value = {"data": {"status": "confirmed"}}

        manager._capture_qr_connect_response(session, response)

        self.assertEqual(session.status, "scanned")
        self.assertIn("手机确认", session.message)

    @patch("webapp.qr_login.time.time", return_value=1_000.0)
    def test_secondary_verification_keeps_browser_session_active(self, _time):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="scanned",
            message="等待确认",
            started_at=0,
            expires_at=1_100,
        )
        manager._session = session
        response = Mock()
        response.url = "https://creator.douyin.com/passport/web/check_qrconnect/"
        response.json.return_value = {
            "error_code": 2046,
            "description": "请完成验证后重试",
        }

        manager._capture_qr_connect_response(session, response)

        self.assertEqual(session.status, "verification_required")
        self.assertIn("短信", session.message)
        self.assertFalse(session.cancel_event.is_set())
        self.assertEqual(session.expires_at, 1_600)

        session.expires_at = 1_550
        manager._capture_qr_connect_response(session, response)
        self.assertEqual(session.expires_at, 1_550)

    def test_repeated_2046_does_not_overwrite_real_verification_stage(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verification_required",
            message="已打开安全验证，正在识别短信验证方式…",
            started_at=0,
            expires_at=9999999999,
            verification_seen_at=1,
            verification_stage="choosing_method",
            verification_panel_seen=True,
        )
        manager._session = session
        response = Mock()
        response.url = "https://creator.douyin.com/passport/web/check_qrconnect/"
        response.json.return_value = {
            "error_code": 2046,
            "description": "请完成验证后重试",
        }

        manager._capture_qr_connect_response(session, response)

        self.assertEqual(session.verification_stage, "choosing_method")
        self.assertIn("已打开", session.message)

    def test_confirmed_status_does_not_hide_secondary_verification(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verification_required",
            message="请完成短信验证",
            started_at=0,
            expires_at=9999999999,
            verification_seen_at=1,
            verification_stage="code",
            verification_input_ready=True,
        )
        manager._session = session
        response = Mock()
        response.url = "https://creator.douyin.com/passport/web/check_qrconnect/"
        response.json.return_value = {"data": {"status": "3"}}

        manager._capture_qr_connect_response(session, response)

        self.assertEqual(session.status, "verification_required")
        self.assertIn("短信", session.message)

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

    def test_probe_rechecks_secondary_verification_without_ending_session(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verification_required",
            message="请完成短信验证",
            started_at=0,
            expires_at=9999999999,
            verification_seen_at=1,
        )
        manager._session = session

        result = manager.probe()

        self.assertEqual(result["status"], "verification_required")
        self.assertTrue(session.probe_event.is_set())
        self.assertIn("检查短信", result["message"])

    def test_verification_code_is_queued_without_public_echo(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verification_required",
            message="请填写验证码",
            started_at=0,
            expires_at=9999999999,
            verification_seen_at=1,
            verification_stage="code",
            verification_input_ready=True,
        )
        manager._session = session

        result = manager.submit_verification_code("123456")

        self.assertEqual(result["status"], "verifying")
        self.assertNotIn("123456", str(result))
        self.assertEqual(session.verification_code, "123456")
        self.assertTrue(session.verification_code_event.is_set())
        self.assertEqual(session.verification_stage, "submitting")
        self.assertFalse(session.verification_input_ready)

    def test_verification_code_rejects_invalid_or_inactive_submission(self):
        manager = QRLoginManager()
        with self.assertRaisesRegex(QRLoginStateError, "4 到 8 位"):
            manager.submit_verification_code("12ab")

        session = _QRSession(
            nonce="test",
            status="waiting_scan",
            message="等待扫码",
            started_at=0,
            expires_at=9999999999,
        )
        manager._session = session
        with self.assertRaisesRegex(QRLoginStateError, "没有等待"):
            manager.submit_verification_code("123456")

    def test_verification_code_waits_for_official_input(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verification_required",
            message="正在选择方式",
            started_at=0,
            expires_at=9999999999,
            verification_seen_at=1,
            verification_stage="choosing_method",
        )
        manager._session = session

        with self.assertRaisesRegex(QRLoginStateError, "仍在准备"):
            manager.submit_verification_code("123456")

    def test_browser_selects_sms_method_before_requesting_code(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verification_required",
            message="正在选择方式",
            started_at=0,
            expires_at=9999999999,
            verification_seen_at=1,
            verification_stage="choosing_method",
        )
        manager._session = session
        method = Mock()

        with patch.object(manager, "_find_verification_input", return_value=None), patch.object(
            manager, "_find_sms_method", return_value=method
        ):
            manager._advance_verification_flow(session, Mock())

        method.click.assert_called_once_with(timeout=5_000)
        self.assertTrue(session.sms_method_selected)
        self.assertEqual(session.verification_stage, "requesting_code")

    def test_browser_requests_code_then_exposes_input_when_ready(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verification_required",
            message="已选择短信",
            started_at=0,
            expires_at=9999999999,
            verification_seen_at=1,
            verification_stage="requesting_code",
            sms_method_selected=True,
        )
        manager._session = session
        send_code = Mock()

        with patch.object(manager, "_find_verification_input", return_value=None), patch.object(
            manager, "_find_send_code_control", return_value=send_code
        ):
            manager._advance_verification_flow(session, Mock())

        send_code.click.assert_called_once_with(timeout=5_000)
        self.assertTrue(session.sms_code_requested)
        self.assertEqual(session.verification_stage, "waiting_code_input")

        with patch.object(
            manager, "_find_verification_input", return_value=(Mock(), Mock())
        ):
            manager._advance_verification_flow(session, Mock())

        self.assertEqual(session.verification_stage, "code")
        self.assertTrue(session.verification_input_ready)
        self.assertTrue(manager.status()["verificationInputReady"])

    def test_browser_submission_fills_official_verification_input(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verifying",
            message="正在提交",
            started_at=0,
            expires_at=9999999999,
            verification_seen_at=1,
        )
        manager._session = session
        page = Mock()
        frame = Mock()
        field = Mock()

        with patch.object(
            manager, "_find_verification_input", return_value=(frame, field)
        ), patch.object(
            manager, "_click_verification_submit", return_value=True
        ) as click:
            manager._submit_verification_code(session, page, "123456")

        field.fill.assert_called_once_with("123456", timeout=5_000)
        click.assert_called_once_with(frame, field)
        self.assertEqual(session.status, "verifying")
        self.assertIn("已提交", session.message)

    def test_missing_official_input_returns_to_verification_form(self):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verifying",
            message="正在提交",
            started_at=0,
            expires_at=9999999999,
            verification_seen_at=1,
        )
        manager._session = session

        with patch.object(manager, "_find_verification_input", return_value=None):
            manager._submit_verification_code(session, Mock(), "123456")

        self.assertEqual(session.status, "verification_required")
        self.assertIn("未找到", session.message)

    @patch("webapp.qr_login.time.monotonic", return_value=20.0)
    def test_non_advancing_submission_returns_to_code_input(self, _monotonic):
        manager = QRLoginManager()
        session = _QRSession(
            nonce="test",
            status="verifying",
            message="验证码已提交",
            started_at=0,
            expires_at=9999999999,
            verification_submitted_at=1.0,
        )
        manager._session = session
        frame = Mock()
        frame.locator.return_value.inner_text.return_value = "短信验证"
        page = Mock()
        page.frames = [frame]

        with patch.object(
            manager, "_find_verification_input", return_value=(frame, Mock())
        ):
            manager._check_verification_submission(session, page)

        self.assertEqual(session.status, "verification_required")
        self.assertEqual(session.verification_stage, "code")
        self.assertTrue(session.verification_input_ready)
        self.assertIn("未通过", session.message)

    def test_probe_requires_an_active_session(self):
        with self.assertRaisesRegex(QRLoginStateError, "没有扫码登录会话"):
            QRLoginManager().probe()


if __name__ == "__main__":
    unittest.main()
