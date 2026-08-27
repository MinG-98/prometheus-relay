from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from utils.logger import setup_logger

logger = setup_logger(name="prometheus-relay.qr", level="Info")

from webapp.config_store import upsert_scanned_account as legacy_upsert_scanned_account
from webapp.tenant_store import (
    AccountOwnershipError,
    TenantStoreError,
    upsert_scanned_account as tenant_upsert_scanned_account,
)

if TYPE_CHECKING:
    from playwright.sync_api import Response


CREATOR_URL = "https://creator.douyin.com/"
CREATOR_HOME_URL = "https://creator.douyin.com/creator-micro/home"
AUTH_COOKIE_NAMES = frozenset(
    {
        "sessionid",
        "sessionid_ss",
        "sid_guard",
        "sid_tt",
        "uid_tt",
        "uid_tt_ss",
    }
)
TERMINAL_STATES = frozenset({"complete", "expired", "cancelled", "error"})
QR_SESSION_SECONDS = 600
DETAILS_SESSION_SECONDS = 600
SECOND_VERIFY_SESSION_SECONDS = 600
MAX_QR_IMAGE_BYTES = 512 * 1024
MAX_COOKIE_TOTAL_BYTES = 1024 * 1024
PROFILE_PATHS = (
    "/aweme/v1/creator/user/info/",
    "/aweme/v1/creator/pc/user/info/",
    "/web/api/media/user/info/",
)


class QRLoginBusyError(RuntimeError):
    pass


class QRLoginStateError(RuntimeError):
    pass


@dataclass
class _QRSession:
    nonce: str
    status: str
    message: str
    started_at: float
    expires_at: float
    cancel_event: threading.Event = field(default_factory=threading.Event)
    probe_event: threading.Event = field(default_factory=threading.Event)
    verification_code_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    qr_png: bytes | None = None
    qr_digest: str = ""
    qr_revision: int = 0
    detected_unique_id: str = ""
    detected_username: str = ""
    pending_cookies: list[dict] | None = None
    account: dict | None = None
    created: bool | None = None
    auth_seen_at: float = 0.0
    last_home_probe_at: float = 0.0
    qr_redirect_url: str = ""
    last_qr_status: str = ""
    verification_seen_at: float = 0.0
    verification_stage: str = ""
    verification_input_ready: bool = False
    sms_method_selected: bool = False
    sms_code_requested: bool = False
    verification_code: str = ""
    verification_config_seen: bool = False
    verification_panel_seen: bool = False
    verification_debug_digest: str = ""
    last_verification_debug_at: float = 0.0
    verification_submitted_at: float = 0.0


def _nested_value(payload: object, *path: str) -> object:
    value = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def extract_profile(response_path: str, payload: object) -> tuple[str, str]:
    """Extract only the public account id and nickname from known Creator APIs."""
    if not isinstance(payload, dict):
        return "", ""

    candidates: list[tuple[object, object]] = []
    if response_path.endswith(
        ("/aweme/v1/creator/user/info/", "/aweme/v1/creator/pc/user/info/")
    ):
        candidates.extend(
            [
                (
                    _nested_value(payload, "douyin_user_verify_info", "douyin_unique_id"),
                    _nested_value(payload, "douyin_user_verify_info", "nick_name"),
                ),
                (
                    _nested_value(payload, "user_profile", "unique_id"),
                    _nested_value(payload, "user_profile", "nick_name"),
                ),
                (
                    _nested_value(payload, "user_profile", "unique_id"),
                    _nested_value(payload, "user_profile", "nickname"),
                ),
                (
                    _nested_value(payload, "user", "unique_id"),
                    _nested_value(payload, "user", "nickname"),
                ),
                (
                    _nested_value(payload, "user_info", "unique_id"),
                    _nested_value(payload, "user_info", "nickname"),
                ),
            ]
        )
    elif response_path.endswith("/web/api/media/user/info/"):
        candidates.append(
            (
                _nested_value(payload, "user", "short_id"),
                _nested_value(payload, "user", "nickname"),
            )
        )

    for raw_unique_id, raw_username in candidates:
        unique_id = str(raw_unique_id or "").strip()
        username = str(raw_username or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", unique_id) and username:
            return unique_id, username[:120]
    return "", ""


QR_CONNECT_STATUS_ALIASES = {
    "1": "waiting",
    "new": "waiting",
    "waiting": "waiting",
    "wait": "waiting",
    "2": "scanned",
    "scanned": "scanned",
    "scaned": "scanned",
    "scan": "scanned",
    "3": "confirmed",
    "confirmed": "confirmed",
    "confirm": "confirmed",
    "success": "confirmed",
    "logged_in": "confirmed",
    "authorized": "confirmed",
    "5": "expired",
    "expired": "expired",
    "invalid": "expired",
    "timeout": "expired",
    "closed": "expired",
    "cancelled": "expired",
}


def extract_qr_connect_payload(response_path: str, payload: object) -> dict:
    """Return the normalised QR-connect status plus any follow-up URL."""
    if not response_path.endswith("/passport/web/check_qrconnect/"):
        return {}
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    raw_status = str(data.get("status") or "").strip().lower()
    status = QR_CONNECT_STATUS_ALIASES.get(raw_status, raw_status)
    redirect_url = str(data.get("redirect_url") or "").strip()
    error_code = payload.get("error_code")
    description = str(payload.get("description") or "").strip()
    decision_conf = (
        data.get("verify_center_decision_conf")
        or data.get("verify_center_secondary_decision_conf")
        or payload.get("verify_center_decision_conf")
        or payload.get("verify_center_secondary_decision_conf")
    )
    sms_code_key = data.get("sms_code_key") or payload.get("sms_code_key")
    return {
        "raw_status": raw_status,
        "status": status,
        "redirect_url": redirect_url,
        "error_code": error_code,
        "description": description,
        # These values can contain security material. Expose presence only and
        # let the official Douyin SDK consume the actual response in-browser.
        "verification_config_present": bool(decision_conf),
        "sms_code_key_present": bool(sms_code_key),
    }


def extract_qr_connect_status(response_path: str, payload: object) -> str:
    """Extract the state returned by Douyin's QR-login polling endpoint."""
    return str(extract_qr_connect_payload(response_path, payload).get("status") or "")


def sanitise_browser_cookies(cookies: object) -> list[dict]:
    """Keep only Douyin cookies and fields accepted by Playwright."""
    if not isinstance(cookies, list):
        raise ValueError("浏览器没有返回有效 Cookie")

    allowed_fields = {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    }
    clean: list[dict] = []
    total_size = 0
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain", "")).lower().lstrip(".")
        if domain != "douyin.com" and not domain.endswith(".douyin.com"):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            continue
        cleaned = {key: cookie[key] for key in allowed_fields if key in cookie}
        total_size += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total_size > MAX_COOKIE_TOTAL_BYTES:
            raise ValueError("登录 Cookie 超出安全大小限制")
        clean.append(cleaned)

    if not clean or not AUTH_COOKIE_NAMES.intersection(
        str(cookie.get("name", "")) for cookie in clean
    ):
        raise ValueError("没有取得有效的抖音登录凭证")
    if len(clean) > 200:
        raise ValueError("登录 Cookie 数量超出安全限制")
    return clean


class QRLoginManager:
    def __init__(self, workspace_id: int | None = None) -> None:
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._session: _QRSession | None = None
        self.workspace_id = workspace_id

    def start(self) -> dict:
        with self._start_lock:
            now = time.time()
            old_thread = None
            with self._lock:
                self._expire_locked(now)
                current = self._session
                if current and current.status not in TERMINAL_STATES:
                    raise QRLoginBusyError("已有扫码登录正在进行，请完成或取消后再试")
                if current and current.thread is not None and current.thread.is_alive():
                    current.cancel_event.set()
                    old_thread = current.thread

            if old_thread is not None:
                old_thread.join(timeout=8)
                if old_thread.is_alive():
                    raise QRLoginBusyError("上一个扫码会话正在关闭，请稍后再试")

            with self._lock:
                session = _QRSession(
                    nonce=secrets.token_urlsafe(24),
                    status="starting",
                    message="正在连接抖音创作者中心…",
                    started_at=now,
                    expires_at=now + QR_SESSION_SECONDS,
                )
                worker = threading.Thread(
                    target=self._run_browser,
                    args=(session,),
                    name="prometheus-qr-login",
                    daemon=True,
                )
                session.thread = worker
                self._session = session
                worker.start()
                return self._public_status_locked(session, now)

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            self._expire_locked(now)
            if self._session is None:
                raise QRLoginStateError("当前没有扫码登录会话")
            return self._public_status_locked(self._session, now)

    def image(self) -> bytes:
        with self._lock:
            if self._session is None or not self._session.qr_png:
                raise QRLoginStateError("二维码尚未生成")
            return bytes(self._session.qr_png)

    def cancel(self) -> dict:
        now = time.time()
        with self._lock:
            session = self._session
            if session is None:
                raise QRLoginStateError("当前没有扫码登录会话")
            session.cancel_event.set()
            session.pending_cookies = None
            session.verification_code = ""
            session.verification_code_event.clear()
            session.verification_stage = ""
            session.verification_input_ready = False
            session.verification_submitted_at = 0.0
            session.qr_png = None
            session.qr_digest = ""
            if session.status != "complete":
                session.status = "cancelled"
                session.message = "扫码登录已取消"
            return self._public_status_locked(session, now)

    def probe(self) -> dict:
        """Immediately ask the browser worker to verify phone-side confirmation."""
        now = time.time()
        with self._lock:
            session = self._session
            if session is None:
                raise QRLoginStateError("当前没有扫码登录会话")
            self._expire_locked(now)
            if session.status in TERMINAL_STATES:
                raise QRLoginStateError("扫码会话已结束，请重新生成二维码")
            if session.status == "needs_details":
                return self._public_status_locked(session, now)

            session.probe_event.set()
            if session.status in {"starting", "waiting_scan"}:
                session.status = "scanned"
                session.message = "正在检查手机确认登录，请稍候…"
            elif session.status == "verification_required":
                session.message = "正在检查短信安全验证结果，请稍候…"
            return self._public_status_locked(session, now)

    def submit_verification_code(self, code: object) -> dict:
        """Queue one SMS code for the active official Douyin browser session."""
        value = str(code or "").strip()
        if not re.fullmatch(r"[0-9]{4,8}", value):
            raise QRLoginStateError("短信验证码必须是 4 到 8 位数字")

        now = time.time()
        with self._lock:
            self._expire_locked(now)
            session = self._session
            if session is None or session.status not in {
                "verification_required",
            }:
                raise QRLoginStateError("当前扫码会话没有等待短信验证码")
            if not session.verification_input_ready:
                raise QRLoginStateError("VPS 仍在准备短信验证，请稍候")
            if session.verification_code_event.is_set():
                raise QRLoginStateError("短信验证码正在提交，请稍候")
            session.verification_code = value
            session.verification_code_event.set()
            session.status = "verifying"
            session.verification_stage = "submitting"
            session.verification_input_ready = False
            session.verification_submitted_at = time.monotonic()
            session.message = "正在 VPS 的抖音安全验证页面中提交验证码…"
            return self._public_status_locked(session, now)

    def confirm(self, unique_id: object, username: object) -> dict:
        now = time.time()
        with self._lock:
            self._expire_locked(now)
            session = self._session
            if session is None or session.status != "needs_details":
                raise QRLoginStateError("当前扫码会话不需要补充账号资料")
            if not session.pending_cookies:
                raise QRLoginStateError("登录凭证已失效，请重新扫码")
            cookies = list(session.pending_cookies)
            session.status = "saving"
            session.message = "正在保存账号…"

        try:
            if self.workspace_id is None:
                account, created = legacy_upsert_scanned_account(
                    str(unique_id or ""), str(username or ""), cookies
                )
            else:
                account, created = tenant_upsert_scanned_account(
                    self.workspace_id,
                    str(unique_id or ""),
                    str(username or ""),
                    cookies,
                )
        except (OSError, ValueError, TenantStoreError) as exc:
            with self._lock:
                if self._session is session:
                    session.status = "needs_details"
                    session.message = str(exc)
            raise QRLoginStateError(str(exc)) from exc

        with self._lock:
            if self._session is session:
                session.account = account
                session.created = created
                session.pending_cookies = None
                session.verification_code = ""
                session.verification_code_event.clear()
                session.verification_stage = ""
                session.verification_input_ready = False
                session.verification_submitted_at = 0.0
                session.qr_png = None
                session.status = "complete"
                session.message = "账号已添加" if created else "账号登录状态已更新"
            return self._public_status_locked(session, time.time())

    def shutdown(self) -> None:
        with self._lock:
            session = self._session
            if session is None:
                return
            session.verification_code = ""
            session.verification_code_event.clear()
            session.verification_stage = ""
            session.verification_input_ready = False
            session.verification_submitted_at = 0.0
            session.cancel_event.set()
            thread = session.thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def _public_status_locked(self, session: _QRSession, now: float) -> dict:
        detected = None
        if session.detected_unique_id or session.detected_username:
            detected = {
                "unique_id": session.detected_unique_id,
                "username": session.detected_username,
            }
        return {
            "status": session.status,
            "message": session.message,
            "expiresIn": max(0, int(session.expires_at - now)),
            "qrReady": bool(session.qr_png),
            "qrRevision": session.qr_revision,
            "detected": detected,
            "account": session.account,
            "created": session.created,
            "verificationStage": session.verification_stage or None,
            "verificationInputReady": session.verification_input_ready,
        }

    def _expire_locked(self, now: float) -> None:
        session = self._session
        if session is None or session.status in TERMINAL_STATES:
            return
        if now < session.expires_at:
            return
        session.cancel_event.set()
        session.pending_cookies = None
        session.verification_code = ""
        session.verification_code_event.clear()
        session.verification_stage = ""
        session.verification_input_ready = False
        session.verification_submitted_at = 0.0
        session.qr_png = None
        session.status = "expired"
        session.message = "二维码已过期，请重新生成"

    def _set_state(self, session: _QRSession, status: str, message: str) -> None:
        with self._lock:
            if self._session is not session or session.status in TERMINAL_STATES:
                return
            session.status = status
            session.message = message
            if status in {"expired", "cancelled", "error"}:
                session.pending_cookies = None
                session.verification_code = ""
                session.verification_code_event.clear()
                session.verification_stage = ""
                session.verification_input_ready = False
                session.verification_submitted_at = 0.0
                session.qr_png = None
                session.qr_digest = ""

    def _capture_profile_response(self, session: _QRSession, response: Response) -> None:
        path = urlsplit(response.url).path
        if not (
            path.endswith("/aweme/v1/creator/user/info/")
            or path.endswith("/aweme/v1/creator/pc/user/info/")
            or path.endswith("/web/api/media/user/info/")
        ):
            return
        try:
            unique_id, username = extract_profile(path, response.json())
        except Exception:
            return
        if not unique_id or not username:
            return
        with self._lock:
            if self._session is session:
                session.detected_unique_id = unique_id
                session.detected_username = username

    def _capture_qr_connect_response(self, session: _QRSession, response: Response) -> None:
        path = urlsplit(response.url).path
        if not path.endswith("/passport/web/check_qrconnect/"):
            return
        try:
            payload = extract_qr_connect_payload(path, response.json())
        except Exception:
            return
        status = str(payload.get("status") or "")
        if not status:
            error_code = payload.get("error_code")
            description = str(payload.get("description") or "")
            if error_code in {2046, "2046"} or "完成验证" in description:
                has_config = bool(payload.get("verification_config_present"))
                with self._lock:
                    first_config = has_config and not session.verification_config_seen
                    if self._session is session and has_config:
                        session.verification_config_seen = True
                if first_config:
                    logger.info(
                        "qr-login received official secondary-verification config "
                        "sms-key=%s",
                        "yes" if payload.get("sms_code_key_present") else "no",
                    )
                self._require_verification(session, has_config=has_config)
            return
        if status != session.last_qr_status:
            logger.info(
                "qr-connect status=%s raw=%s error_code=%s redirect=%s",
                status,
                payload.get("raw_status"),
                payload.get("error_code"),
                "yes" if payload.get("redirect_url") else "no",
            )
            session.last_qr_status = status
        if status == "waiting":
            return
        if status == "expired":
            self._set_state(session, "expired", "二维码已过期，请重新生成")
            session.cancel_event.set()
            return
        redirect_url = str(payload.get("redirect_url") or "")
        if redirect_url.startswith("https://") and "douyin.com" in urlsplit(
            redirect_url
        ).netloc:
            session.qr_redirect_url = redirect_url
        if status == "scanned":
            self._set_state(session, "scanned", "已检测到手机扫码，等待确认登录…")
            return
        if status == "confirmed":
            with self._lock:
                if (
                    self._session is session
                    and session.status in {"verification_required", "verifying"}
                ):
                    return
            self._set_state(session, "scanned", "已检测到手机确认，正在获取登录状态…")
            return
        self._set_state(session, "scanned", "已收到抖音扫码状态，正在获取登录状态…")

    def _require_verification(
        self, session: _QRSession, *, has_config: bool = False
    ) -> None:
        """Keep the browser alive while Douyin completes phone-side verification."""
        now = time.time()
        with self._lock:
            if self._session is not session or session.status in TERMINAL_STATES:
                return
            if not session.verification_seen_at:
                session.verification_seen_at = now
                session.verification_stage = "loading_panel"
                session.expires_at = max(
                    session.expires_at, now + SECOND_VERIFY_SESSION_SECONDS
                )
                logger.info("qr-login requires secondary verification")
            if has_config:
                session.verification_config_seen = True
            if session.status != "verifying":
                session.status = "verification_required"
                if session.verification_input_ready:
                    session.message = "短信验证已就绪，请输入收到的验证码"
                elif session.sms_method_selected:
                    session.message = "已选择短信验证，正在等待验证码输入框…"
                elif session.verification_panel_seen:
                    session.message = "已打开抖音安全验证，正在识别短信验证方式…"
                elif session.verification_stage == "loading_panel":
                    session.message = (
                        "抖音要求二次验证，正在打开官方短信安全验证面板…"
                    )
            session.qr_png = None
            session.qr_digest = ""

    def _detect_verification_state(self, session: _QRSession, page) -> None:
        """Detect a secondary-verification prompt rendered by the official page."""
        with self._lock:
            if self._session is not session or session.status not in {
                "scanned",
                "verification_required",
            }:
                return
        keywords = (
            "选择验证方式",
            "二次验证",
            "身份验证",
            "发送短信验证",
            "短信验证",
            "安全验证",
            "完成验证",
        )
        for frame in page.frames:
            try:
                text = frame.locator("body").inner_text(timeout=500)
            except Exception:
                continue
            if any(keyword in text for keyword in keywords):
                # Body text can come from a hidden Verify SDK template. The
                # visible-control diagnostic is the authority for whether the
                # panel is actually ready; this fallback only keeps the
                # session alive while that panel is loading.
                self._require_verification(session)
                return

    @staticmethod
    def _verification_control_rank(control) -> int:
        return int(
            control.evaluate(
                """
                (element) => {
                    const words = [
                        '选择验证方式', '请选择验证方式', '二次验证',
                        '安全验证', '身份验证', '验证身份', '短信验证',
                        '完成验证', '输入短信验证码', '已发送至'
                    ];
                    let node = element;
                    for (let depth = 0; node && depth < 9; depth += 1) {
                        const role = node.getAttribute?.('role') || '';
                        const modal = node.getAttribute?.('aria-modal') || '';
                        const className = String(node.className || '');
                        const text = String(node.innerText || '');
                        if (role === 'dialog' || modal === 'true') return 0;
                        if (words.some((word) => text.includes(word))) return 1;
                        if (/verify|verification|security|captcha|modal|dialog/i.test(className)) return 2;
                        node = node.parentElement;
                    }
                    return 9;
                }
                """
            )
        )

    def _find_verification_control(
        self,
        page,
        accepted_terms: tuple[str, ...],
        rejected_terms: tuple[str, ...] = (),
    ):
        # Verify SDK pages can contain thousands of divs and hidden templates.
        # Exact text lookup keeps this operation bounded and returns as soon as
        # the official leaf control is found.
        for frame in page.frames:
            for term in accepted_terms:
                try:
                    candidates = frame.get_by_text(term, exact=True)
                    count = min(candidates.count(), 8)
                except Exception:
                    continue
                for index in range(count):
                    control = candidates.nth(index)
                    try:
                        if not control.is_visible(timeout=700):
                            continue
                        label = " ".join(control.inner_text(timeout=700).split())
                        if any(rejected in label for rejected in rejected_terms):
                            continue
                        rank = self._verification_control_rank(control)
                        if frame is not page.main_frame and rank > 2:
                            rank = 3
                        if frame is page.main_frame and rank > 2:
                            continue
                        return control
                    except Exception:
                        continue

        return None

    def _find_sms_method(self, page):
        return self._find_verification_control(
            page,
            (
                "短信验证码",
                "短信验证",
                "接收短信验证码",
                "手机验证码",
                "手机号验证",
                "手机验证",
                "接收验证码",
                "收验证码",
                "手机接收验证码",
                "短信接收验证码",
                "使用短信验证",
                "通过短信验证",
            ),
            ("验证码登录", "获取验证码", "发送验证码", "重新发送", "密码登录"),
        )

    def _find_send_code_control(self, page):
        return self._find_verification_control(
            page,
            (
                "获取验证码",
                "发送验证码",
                "发送短信验证码",
                "发送短信验证",
                "重新发送",
            ),
        )

    @staticmethod
    def _click_verification_control(control) -> None:
        """Click one already-vetted official verification control."""
        try:
            control.scroll_into_view_if_needed(timeout=3_000)
            control.click(timeout=5_000)
        except Exception:
            # Some Verify SDK controls attach the handler to a custom div and
            # Playwright's actionability check is stricter than the SDK. The
            # native DOM click still bubbles through the already-vetted node.
            control.evaluate("element => element.click()")

    @staticmethod
    def _redact_verification_label(value: object) -> str:
        label = " ".join(str(value or "").split())
        label = re.sub(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+", "<email>", label)
        label = re.sub(r"\d", "#", label)
        return label[:80]

    def _log_verification_ui(self, session: _QRSession, page) -> None:
        """Log a rate-limited, redacted view of the official challenge UI."""
        now = time.monotonic()
        with self._lock:
            if self._session is not session:
                return
            if now - session.last_verification_debug_at < 3:
                return
            session.last_verification_debug_at = now

        frame_states: list[str] = []
        challenge_seen = False
        try:
            frames = list(page.frames)
        except Exception:
            # Keeps the manager's state-machine helpers usable with lightweight
            # test doubles; real Playwright pages always expose an iterable.
            return
        for frame in frames:
            try:
                state = frame.evaluate(
                    """
                    () => {
                        const terms = [
                            '选择验证方式', '二次验证', '安全验证', '身份验证',
                            '验证身份', '完成验证', '短信验证', '接收验证码',
                            '收验证码', '输入短信验证码', '使用短信验证',
                            '手机短信', '手机验证'
                        ];
                        const labels = [];
                        let visibleInputs = 0;
                        const visit = (root) => {
                            for (const element of root.querySelectorAll(
                                'button,[role="button"],label,li,a,span,div,[tabindex],input'
                            )) {
                                const style = getComputedStyle(element);
                                const rect = element.getBoundingClientRect();
                                if (style.visibility === 'hidden' || style.display === 'none' ||
                                    rect.width < 1 || rect.height < 1) continue;
                                if (element.matches('input')) visibleInputs += 1;
                                const text = String(element.innerText || '').replace(/\\s+/g, ' ').trim();
                                if (text && text.length <= 80 &&
                                    terms.some((term) => text.includes(term))) labels.push(text);
                                if (element.shadowRoot) visit(element.shadowRoot);
                            }
                        };
                        visit(document);
                        return {
                            challenge: labels.length > 0,
                            labels: [...new Set(labels)].slice(0, 8),
                            inputs: visibleInputs,
                        };
                    }
                    """
                )
                parsed = urlsplit(frame.url)
                location = f"{parsed.hostname or 'local'}{parsed.path[:80]}"
                labels = [
                    self._redact_verification_label(label)
                    for label in state.get("labels", [])
                ]
                challenge = bool(state.get("challenge"))
                challenge_seen = challenge_seen or challenge
                frame_states.append(
                    f"{location}:challenge={'yes' if challenge else 'no'}:"
                    f"inputs={int(state.get('inputs') or 0)}:labels={labels}"
                )
            except Exception:
                continue

        digest = hashlib.sha256("|".join(frame_states).encode()).hexdigest()
        with self._lock:
            if self._session is not session:
                return
            if challenge_seen:
                session.verification_panel_seen = True
                if session.verification_stage == "loading_panel":
                    session.verification_stage = "choosing_method"
            if digest == session.verification_debug_digest:
                return
            session.verification_debug_digest = digest
        logger.info("secondary-verification ui %s", " | ".join(frame_states))

    def _advance_verification_flow(self, session: _QRSession, page) -> None:
        with self._lock:
            if self._session is not session or session.status not in {
                "verification_required",
                "verifying",
            }:
                return
            status = session.status
            method_selected = session.sms_method_selected
            code_requested = session.sms_code_requested

        if status == "verifying":
            return
        self._log_verification_ui(session, page)
        code_input = self._find_verification_input(page)
        if code_input is not None:
            with self._lock:
                if self._session is session and session.status not in TERMINAL_STATES:
                    session.verification_stage = "code"
                    session.verification_input_ready = True
                    if session.status != "verifying":
                        session.status = "verification_required"
                        session.message = "短信验证码已就绪，请输入收到的验证码"
            return
        if not method_selected:
            method = self._find_sms_method(page)
            if method is None:
                with self._lock:
                    if self._session is session:
                        if session.verification_panel_seen:
                            session.verification_stage = "choosing_method"
                            session.message = "已打开安全验证，正在识别短信验证方式…"
                        else:
                            session.verification_stage = "loading_panel"
                            session.message = "正在等待抖音官方短信安全验证面板加载…"
                return
            try:
                self._click_verification_control(method)
            except Exception:
                logger.info("secondary-verification SMS method click failed")
                return
            logger.info("secondary-verification selected SMS method")
            with self._lock:
                if self._session is session:
                    session.sms_method_selected = True
                    session.verification_stage = "requesting_code"
                    session.message = "已选择短信验证码，正在请求验证码…"
            return

        if not code_requested:
            send_code = self._find_send_code_control(page)
            if send_code is not None:
                try:
                    self._click_verification_control(send_code)
                except Exception:
                    logger.info("secondary-verification send-code click failed")
                    return
                logger.info("secondary-verification requested SMS code")
                with self._lock:
                    if self._session is session:
                        session.sms_code_requested = True
                        session.verification_stage = "waiting_code_input"
                        session.message = "验证码已请求，正在等待官方输入框…"
                return
            with self._lock:
                if self._session is session:
                    session.verification_stage = "waiting_code_input"
                    session.message = "短信验证已选择，正在等待验证码输入框…"

    def _find_verification_input(self, page):
        selectors = ", ".join(
            (
                'input[autocomplete="one-time-code"]',
                'input[placeholder*="验证码"]',
                'input[aria-label*="验证码"]',
                'input[name*="verify" i]',
                'input[name*="sms" i][name*="code" i]',
            )
        )
        ranked: list[tuple[int, int, object, object]] = []
        sequence = 0
        for frame in page.frames:
            candidates = frame.locator(selectors)
            try:
                count = min(candidates.count(), 20)
            except Exception:
                continue
            for index in range(count):
                field = candidates.nth(index)
                try:
                    if not field.is_visible() or not field.is_enabled():
                        continue
                    challenge_rank = self._verification_control_rank(field)
                except Exception:
                    continue
                is_subframe = frame is not page.main_frame
                if not is_subframe and challenge_rank > 2:
                    continue
                if is_subframe and challenge_rank > 2:
                    challenge_rank = 3
                ranked.append((int(challenge_rank), sequence, frame, field))
                sequence += 1
        if not ranked:
            return None
        ranked.sort(key=lambda item: (item[0], item[1]))
        return ranked[0][2], ranked[0][3]

    @staticmethod
    def _click_verification_submit(frame, field) -> bool:
        accepted_labels = {
            "确认",
            "确定",
            "提交",
            "验证",
            "下一步",
            "完成",
            "登录",
            "确认登录",
            "立即验证",
            "确认验证",
            "提交验证",
        }
        try:
            field_box = field.bounding_box()
        except Exception:
            field_box = None
        candidates_groups = [
            frame.locator('button, [role="button"], input[type="submit"]')
        ]
        for label in accepted_labels:
            try:
                # The current Verify SDK renders “验证” as a clickable div or
                # span rather than a button. Exact text lookup reaches both.
                candidates_groups.append(frame.get_by_text(label, exact=True))
            except Exception:
                continue
        ranked: list[tuple[float, object]] = []
        sequence = 0
        for candidates in candidates_groups:
            try:
                count = min(candidates.count(), 40)
            except Exception:
                continue
            for index in range(count):
                button = candidates.nth(index)
                try:
                    label = (
                        button.get_attribute("value")
                        if button.evaluate("node => node.tagName === 'INPUT'")
                        else button.inner_text(timeout=700)
                    )
                    if str(label or "").strip() not in accepted_labels:
                        continue
                    if not button.is_visible(timeout=700) or not button.is_enabled(
                        timeout=700
                    ):
                        continue
                    box = button.bounding_box()
                except Exception:
                    continue
                score = float(sequence)
                sequence += 1
                if field_box and box:
                    horizontal = abs(
                        (box["x"] + box["width"] / 2)
                        - (field_box["x"] + field_box["width"] / 2)
                    )
                    vertical = abs(box["y"] - (field_box["y"] + field_box["height"]))
                    score = horizontal + vertical
                ranked.append((score, button))
        if ranked:
            ranked.sort(key=lambda item: item[0])
            try:
                ranked[0][1].click(timeout=5_000)
            except Exception:
                ranked[0][1].evaluate("element => element.click()")
            return True
        field.press("Enter", timeout=5_000)
        return False

    def _submit_verification_code(self, session: _QRSession, page, code: str) -> None:
        match = self._find_verification_input(page)
        if match is None:
            with self._lock:
                if self._session is session and session.status not in TERMINAL_STATES:
                    session.status = "verification_required"
                    session.verification_stage = "waiting_code_input"
                    session.verification_input_ready = False
                    session.message = (
                        "暂未找到抖音验证码输入框，请等待安全验证页面加载后重试"
                    )
            return
        frame, field = match
        try:
            field.fill(code, timeout=5_000)
            clicked_submit = self._click_verification_submit(frame, field)
            logger.info(
                "secondary-verification submitted code via=%s",
                "control" if clicked_submit else "enter",
            )
        except Exception:
            logger.exception("qr-login browser worker failed")
            with self._lock:
                if self._session is session and session.status not in TERMINAL_STATES:
                    session.status = "verification_required"
                    session.verification_stage = "code"
                    session.verification_input_ready = True
                    session.message = "验证码提交失败，请确认验证码仍有效后重试"
            return
        with self._lock:
            if self._session is session and session.status == "verifying":
                session.verification_stage = "submitting"
                session.verification_input_ready = False
                session.verification_submitted_at = time.monotonic()
                session.message = "验证码已提交，正在等待抖音确认登录…"

    def _check_verification_submission(self, session: _QRSession, page) -> None:
        """Turn a non-advancing official challenge into a retryable state."""
        with self._lock:
            if self._session is not session or session.status != "verifying":
                return
            submitted_at = session.verification_submitted_at
        if not submitted_at or time.monotonic() - submitted_at < 10:
            return

        failure_terms = (
            "验证码错误",
            "验证码不正确",
            "验证码无效",
            "验证码已失效",
            "验证失败",
            "校验失败",
            "请重新输入验证码",
        )
        failure_seen = False
        try:
            frames = list(page.frames)
        except Exception:
            return
        for frame in frames:
            try:
                text = frame.locator("body").inner_text(timeout=500)
            except Exception:
                continue
            if any(term in text for term in failure_terms):
                failure_seen = True
                break

        with self._lock:
            if self._session is not session or session.status != "verifying":
                return
            session.status = "verification_required"
            session.verification_stage = "code"
            session.verification_input_ready = True
            session.verification_submitted_at = 0.0
            session.message = "抖音未通过验证码，请检查后重新提交"
        logger.info("secondary-verification submission did not advance")

    def _probe_profile(self, session: _QRSession, page) -> None:
        """Ask the same browser session whether the QR login is now authenticated."""
        try:
            result = page.evaluate(
                """
                async (paths) => {
                    return Promise.all(paths.map(async (path) => {
                        try {
                            const response = await fetch(path, {
                                credentials: 'include',
                                cache: 'no-store',
                            });
                            let payload = null;
                            try {
                                payload = await response.json();
                            } catch (_error) {
                                // Some endpoints can return an empty body while logged out.
                            }
                            return {path, status: response.status, payload};
                        } catch (_error) {
                            return {path, status: 0, payload: null};
                        }
                    }));
                }
                """,
                list(PROFILE_PATHS),
            )
        except Exception:
            return
        if not isinstance(result, list):
            return
        unique_id = ""
        username = ""
        for item in result:
            if not isinstance(item, dict) or item.get("status") != 200:
                continue
            try:
                unique_id, username = extract_profile(
                    str(item.get("path") or ""), item.get("payload")
                )
            except Exception:
                continue
            if unique_id and username:
                break
        if not unique_id or not username:
            return
        with self._lock:
            if self._session is session:
                session.detected_unique_id = unique_id
                session.detected_username = username
                if session.status == "waiting_scan":
                    session.status = "scanned"
                    session.message = "已检测到登录确认，正在保存账号…"

    def _is_logged_in(self, session: _QRSession, page, context) -> bool:
        path = urlsplit(page.url).path
        cookie_names = {cookie.get("name", "") for cookie in context.cookies()}
        has_auth_cookie = bool(AUTH_COOKIE_NAMES.intersection(cookie_names))
        if not has_auth_cookie:
            with self._lock:
                session.auth_seen_at = 0.0
            return False

        now = time.monotonic()
        with self._lock:
            if self._session is not session:
                return False
            if not session.auth_seen_at:
                session.auth_seen_at = now
                if session.status == "waiting_scan":
                    session.status = "scanned"
                    session.message = "已检测到登录确认，正在打开创作者中心…"
            auth_seen_for = now - session.auth_seen_at

        if path.startswith("/creator-micro/"):
            return True

        # Douyin can set the session cookie shortly before the phone-side
        # confirmation finishes. Give that transition time to settle instead
        # of navigating away from the QR confirmation page immediately.
        if auth_seen_for < 5:
            return False
        with self._lock:
            if now - session.last_home_probe_at < 5:
                return False
            session.last_home_probe_at = now
        try:
            page.goto(CREATOR_HOME_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            return False
        return urlsplit(page.url).path.startswith("/creator-micro/")

    def _capture_qr(self, session: _QRSession, page) -> None:
        if session.status not in {"starting", "waiting_scan"}:
            return
        image = page.locator("#animate_qrcode_container img").first
        if image.count() == 0 or not image.is_visible(timeout=1_000):
            return
        try:
            loaded = image.evaluate(
                "(node) => Boolean(node.complete && node.naturalWidth > 100 && node.naturalHeight > 100)"
            )
            if not loaded:
                return
            qr = image.locator("xpath=..")
            png = qr.screenshot(type="png", timeout=7_000)
        except Exception:
            return
        if not png or len(png) > MAX_QR_IMAGE_BYTES:
            return
        digest = hashlib.sha256(png).hexdigest()
        with self._lock:
            if self._session is not session or session.status in TERMINAL_STATES:
                return
            if digest != session.qr_digest:
                session.qr_png = png
                session.qr_digest = digest
                session.qr_revision += 1
            if session.status in {"starting", "waiting_scan"}:
                session.status = "waiting_scan"
                session.message = "请使用抖音 App 扫码，并在手机上确认登录"

    def _detect_scan_state(self, session: _QRSession, page) -> None:
        if urlsplit(page.url).path.startswith("/creator-micro/"):
            self._set_state(session, "scanned", "已扫码，正在确认登录状态…")
            return
        try:
            text = page.locator("#animate_qrcode_container").inner_text(timeout=1_000)
        except Exception:
            return
        if any(keyword in text for keyword in ("已扫码", "扫码成功", "确认登录", "手机上确认")):
            self._set_state(session, "scanned", "扫码成功，请在手机上确认登录")
        elif any(keyword in text for keyword in ("已失效", "已过期", "重新扫码")):
            self._set_state(session, "expired", "二维码已过期，请重新生成")
            session.cancel_event.set()

    def _wait_for_profile(self, session: _QRSession, page) -> tuple[str, str]:
        deadline = time.monotonic() + 20
        navigated_home = urlsplit(page.url).path.startswith("/creator-micro/")
        while time.monotonic() < deadline and not session.cancel_event.is_set():
            with self._lock:
                unique_id = session.detected_unique_id
                username = session.detected_username
            if unique_id and username:
                return unique_id, username
            if not navigated_home:
                try:
                    page.goto(CREATOR_HOME_URL, wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    pass
                navigated_home = True
            page.wait_for_timeout(500)
        with self._lock:
            return session.detected_unique_id, session.detected_username

    def _complete_login(self, session: _QRSession, page, context) -> None:
        self._set_state(session, "saving", "登录成功，正在识别并保存账号…")
        unique_id, username = self._wait_for_profile(session, page)
        if session.cancel_event.is_set():
            return
        cookies = sanitise_browser_cookies(context.cookies())

        if not unique_id or not username:
            with self._lock:
                if self._session is session and session.status not in TERMINAL_STATES:
                    session.pending_cookies = cookies
                    session.expires_at = time.time() + DETAILS_SESSION_SECONDS
                    session.qr_png = None
                    session.status = "needs_details"
                    session.message = "登录成功，但未能自动识别完整资料，请补充后保存"
            return

        if session.cancel_event.is_set():
            return
        try:
            if self.workspace_id is None:
                account, created = legacy_upsert_scanned_account(
                    unique_id, username, cookies
                )
            else:
                account, created = tenant_upsert_scanned_account(
                    self.workspace_id, unique_id, username, cookies
                )
        except AccountOwnershipError as exc:
            with self._lock:
                if self._session is session and session.status not in TERMINAL_STATES:
                    session.status = "error"
                    session.message = str(exc)
                    session.pending_cookies = None
                    session.qr_png = None
            return
        except (OSError, ValueError, TenantStoreError) as exc:
            with self._lock:
                if self._session is session and session.status not in TERMINAL_STATES:
                    session.status = "error"
                    session.message = str(exc)
                    session.pending_cookies = None
                    session.qr_png = None
            return
        with self._lock:
            if self._session is not session or session.status in TERMINAL_STATES:
                return
            session.account = account
            session.created = created
            session.pending_cookies = None
            session.verification_code = ""
            session.verification_code_event.clear()
            session.verification_stage = ""
            session.verification_input_ready = False
            session.verification_submitted_at = 0.0
            session.qr_png = None
            session.status = "complete"
            session.message = "账号已添加" if created else "账号登录状态已更新"

    def _run_browser(self, session: _QRSession) -> None:
        from playwright.sync_api import sync_playwright

        browser = None
        context = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )
                context.set_default_timeout(10_000)
                page = context.new_page()
                page.on(
                    "response",
                    lambda response: self._capture_profile_response(session, response),
                )
                page.on(
                    "response",
                    lambda response: self._capture_qr_connect_response(session, response),
                )
                page.goto(CREATOR_URL, wait_until="domcontentloaded", timeout=120_000)

                last_qr_capture = 0.0
                last_profile_probe = 0.0
                last_verification_probe = 0.0
                while not session.cancel_event.is_set():
                    with self._lock:
                        if self._session is not session or session.status in TERMINAL_STATES:
                            return
                        if time.time() >= session.expires_at:
                            self._expire_locked(time.time())
                            return

                    now = time.monotonic()
                    manual_probe = session.probe_event.is_set()
                    if manual_probe:
                        session.probe_event.clear()
                    verification_code = ""
                    with self._lock:
                        if session.verification_code_event.is_set():
                            verification_code = session.verification_code
                            session.verification_code = ""
                            session.verification_code_event.clear()
                    if verification_code:
                        self._submit_verification_code(
                            session, page, verification_code
                        )
                        verification_code = ""
                    redirect_url = ""
                    with self._lock:
                        redirect_url = session.qr_redirect_url
                        if redirect_url:
                            session.qr_redirect_url = ""
                    if redirect_url:
                        try:
                            page.goto(
                                redirect_url,
                                wait_until="domcontentloaded",
                                timeout=30_000,
                            )
                        except Exception:
                            logger.info("qr-connect redirect follow failed")
                    if now - last_qr_capture >= 2:
                        self._capture_qr(session, page)
                        self._detect_scan_state(session, page)
                        self._detect_verification_state(session, page)
                        last_qr_capture = now
                    if now - last_verification_probe >= 1:
                        self._check_verification_submission(session, page)
                        self._advance_verification_flow(session, page)
                        last_verification_probe = now
                    if manual_probe or now - last_profile_probe >= 4:
                        self._probe_profile(session, page)
                        last_profile_probe = now
                    with self._lock:
                        profile_ready = bool(
                            session.detected_unique_id and session.detected_username
                        )
                        cookie_names = {
                            cookie.get("name", "") for cookie in context.cookies()
                        }
                        auth_cookie_ready = bool(
                            AUTH_COOKIE_NAMES.intersection(cookie_names)
                        )
                    if profile_ready and auth_cookie_ready:
                        self._complete_login(session, page, context)
                        return
                    if self._is_logged_in(session, page, context):
                        self._complete_login(session, page, context)
                        return
                    session.cancel_event.wait(0.5)
        except Exception:
            logger.exception("qr-login browser worker failed")
            with self._lock:
                if self._session is session and session.status not in TERMINAL_STATES:
                    session.pending_cookies = None
                    session.qr_png = None
                    session.status = "error"
                    session.message = "扫码登录暂时不可用，请稍后重试"
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

_MANAGERS_LOCK = threading.RLock()
_MANAGERS: dict[int, QRLoginManager] = {}


def qr_login_manager_for(workspace_id: int) -> QRLoginManager:
    """Return the isolated QR manager for one customer workspace."""
    workspace_id = int(workspace_id)
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(workspace_id)
        if manager is None:
            manager = QRLoginManager(workspace_id=workspace_id)
            _MANAGERS[workspace_id] = manager
        return manager


def shutdown_qr_login_managers() -> None:
    with _MANAGERS_LOCK:
        managers = list(_MANAGERS.values())
        _MANAGERS.clear()
    for manager in managers:
        manager.shutdown()


# Kept for backwards-compatible unit tests and local integrations. New web
# requests must use qr_login_manager_for() so sessions cannot cross users.
qr_login_manager = QRLoginManager()
