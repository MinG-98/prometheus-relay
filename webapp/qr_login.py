from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from webapp.config_store import upsert_scanned_account

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
QR_SESSION_SECONDS = 300
DETAILS_SESSION_SECONDS = 300
MAX_QR_IMAGE_BYTES = 512 * 1024
MAX_COOKIE_TOTAL_BYTES = 1024 * 1024


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
    thread: threading.Thread | None = None
    qr_png: bytes | None = None
    qr_digest: str = ""
    qr_revision: int = 0
    detected_unique_id: str = ""
    detected_username: str = ""
    pending_cookies: list[dict] | None = None
    account: dict | None = None
    created: bool | None = None


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
    if response_path.endswith("/aweme/v1/creator/user/info/"):
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
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._session: _QRSession | None = None

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
            session.qr_png = None
            session.qr_digest = ""
            if session.status != "complete":
                session.status = "cancelled"
                session.message = "扫码登录已取消"
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
            account, created = upsert_scanned_account(
                str(unique_id or ""), str(username or ""), cookies
            )
        except (OSError, ValueError) as exc:
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
                session.qr_png = None
                session.status = "complete"
                session.message = "账号已添加" if created else "账号登录状态已更新"
            return self._public_status_locked(session, time.time())

    def shutdown(self) -> None:
        with self._lock:
            session = self._session
            if session is None:
                return
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
        }

    def _expire_locked(self, now: float) -> None:
        session = self._session
        if session is None or session.status in TERMINAL_STATES:
            return
        if now < session.expires_at:
            return
        session.cancel_event.set()
        session.pending_cookies = None
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
                session.qr_png = None
                session.qr_digest = ""

    def _capture_profile_response(self, session: _QRSession, response: Response) -> None:
        path = urlsplit(response.url).path
        if not (
            path.endswith("/aweme/v1/creator/user/info/")
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

    def _is_logged_in(self, page, context) -> bool:
        path = urlsplit(page.url).path
        cookie_names = {cookie.get("name", "") for cookie in context.cookies()}
        if not AUTH_COOKIE_NAMES.intersection(cookie_names):
            return False
        if path.startswith("/creator-micro/"):
            return True
        try:
            page.goto(CREATOR_HOME_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            return False
        return urlsplit(page.url).path.startswith("/creator-micro/")

    def _capture_qr(self, session: _QRSession, page) -> None:
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
        navigated_home = urlsplit(page.url).path == "/creator-micro/home"
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
        account, created = upsert_scanned_account(unique_id, username, cookies)
        with self._lock:
            if self._session is not session or session.status in TERMINAL_STATES:
                return
            session.account = account
            session.created = created
            session.pending_cookies = None
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
                page.goto(CREATOR_URL, wait_until="domcontentloaded", timeout=120_000)

                last_qr_capture = 0.0
                while not session.cancel_event.is_set():
                    with self._lock:
                        if self._session is not session or session.status in TERMINAL_STATES:
                            return
                        if time.time() >= session.expires_at:
                            self._expire_locked(time.time())
                            return

                    if self._is_logged_in(page, context):
                        self._complete_login(session, page, context)
                        return

                    now = time.monotonic()
                    if now - last_qr_capture >= 2:
                        self._capture_qr(session, page)
                        self._detect_scan_state(session, page)
                        last_qr_capture = now
                    session.cancel_event.wait(0.5)
        except Exception:
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


qr_login_manager = QRLoginManager()
