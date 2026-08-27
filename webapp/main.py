from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from webapp.qr_login import (
    QRLoginBusyError,
    QRLoginStateError,
    qr_login_manager_for,
    shutdown_qr_login_managers,
)
from webapp.tenant_store import (
    StorageConfigurationError,
    TenantStoreError,
    admin_accounts,
    admin_overview,
    change_password,
    create_customer,
    create_session,
    delete_customer,
    get_session_user,
    get_user_by_id,
    get_workspace_runtime_status,
    get_workspace_state,
    initialize_store,
    list_audit_events,
    list_users,
    read_workspace_log,
    reset_customer_password,
    revoke_session,
    save_workspace_config,
    update_customer,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_PATH = STATIC_DIR / "index.html"
LOGIN_PATH = STATIC_DIR / "login.html"
ADMIN_PATH = STATIC_DIR / "admin.html"
AUTH_ENABLED = str(os.getenv("AUTH_ENABLED", "true")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SESSION_COOKIE_NAME = "prometheus_relay_session"
SESSION_COOKIE_SECURE = str(
    os.getenv("PROMETHEUS_RELAY_SESSION_COOKIE_SECURE", "true")
).strip().lower() in {"1", "true", "yes", "on"}
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
_RUN_LAUNCH_LOCK = threading.Lock()
_LOGIN_LOCK = threading.RLock()
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_FAILURE_LIMIT = 8


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_store()
    yield
    shutdown_qr_login_managers()


app = FastAPI(
    title="Prometheus Relay",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self'; script-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    if request.url.path in {"/", "/login", "/admin"} or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _development_principal() -> dict:
    return {
        "id": 0,
        "username": "development",
        "displayName": "开发模式",
        "role": "platform_admin",
        "enabled": True,
        "workspaceId": None,
        "lastLoginAt": None,
    }


def _current_principal(request: Request) -> dict | None:
    if not AUTH_ENABLED:
        return _development_principal()
    return get_session_user(request.cookies.get(SESSION_COOKIE_NAME))


def require_auth(request: Request) -> dict:
    principal = _current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="需要登录")
    return principal


def require_customer(principal: dict = Depends(require_auth)) -> dict:
    if principal.get("role") == "platform_admin":
        raise HTTPException(status_code=403, detail="管理员请使用管理后台")
    if not principal.get("workspaceId"):
        raise HTTPException(status_code=403, detail="当前用户没有可用工作区")
    return principal


def require_admin(principal: dict = Depends(require_auth)) -> dict:
    if principal.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return principal


def _check_same_origin(request: Request) -> None:
    fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    if fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="拒绝跨站请求")
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return
    origin_parts = urlsplit(origin)
    request_host = request.headers.get("Host", "").strip().lower()
    if (
        origin_parts.scheme not in {"http", "https"}
        or not request_host
        or origin_parts.netloc.lower() != request_host
    ):
        raise HTTPException(status_code=403, detail="请求来源不匹配")


def require_mutation_auth(
    request: Request,
    principal: dict = Depends(require_auth),
) -> dict:
    _check_same_origin(request)
    return principal


async def read_json_body(request: Request) -> object:
    content_length = request.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_JSON_BODY_BYTES:
                raise HTTPException(status_code=413, detail="请求内容过大")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 不合法") from exc
    body = await request.body()
    if len(body) > MAX_JSON_BODY_BYTES:
        raise HTTPException(status_code=413, detail="请求内容过大")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="请求不是有效 JSON") from exc


def _html_response(path: Path) -> HTMLResponse:
    return HTMLResponse(path.read_text(encoding="utf-8"))


def _customer_workspace(principal: dict) -> int:
    workspace_id = principal.get("workspaceId")
    if not workspace_id:
        raise HTTPException(status_code=403, detail="当前用户没有可用工作区")
    return int(workspace_id)


def _workspace_for_admin(user_id: int) -> tuple[dict, int]:
    user = get_user_by_id(user_id)
    if not user or user.get("role") == "platform_admin" or not user.get("workspaceId"):
        raise HTTPException(status_code=404, detail="客户不存在")
    return user, int(user["workspaceId"])


def _start_workspace_run(workspace_id: int, trigger: str) -> dict:
    with _RUN_LAUNCH_LOCK:
        status = get_workspace_runtime_status(workspace_id)
        if status.get("running"):
            raise HTTPException(status_code=409, detail="已有任务正在运行")
        environment = os.environ.copy()
        environment["PROMETHEUS_RELAY_WORKSPACE_ID"] = str(workspace_id)
        environment["PROMETHEUS_RELAY_TRIGGER"] = trigger
        process = subprocess.Popen(
            [sys.executable, "-m", "webapp.task_runner"],
            cwd=PROJECT_ROOT,
            env=environment,
            start_new_session=True,
        )
        return {"accepted": True, "pid": process.pid}


def _login_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_allowed(key: str) -> bool:
    now = time.monotonic()
    with _LOGIN_LOCK:
        recent = [stamp for stamp in _LOGIN_FAILURES.get(key, []) if now - stamp < _LOGIN_WINDOW_SECONDS]
        _LOGIN_FAILURES[key] = recent
        return len(recent) < _LOGIN_FAILURE_LIMIT


def _record_login_failure(key: str) -> None:
    now = time.monotonic()
    with _LOGIN_LOCK:
        recent = [stamp for stamp in _LOGIN_FAILURES.get(key, []) if now - stamp < _LOGIN_WINDOW_SECONDS]
        recent.append(now)
        _LOGIN_FAILURES[key] = recent[-_LOGIN_FAILURE_LIMIT:]


def _clear_login_failures(key: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop(key, None)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    principal = _current_principal(request)
    if principal:
        return RedirectResponse("/", status_code=303)
    return _html_response(LOGIN_PATH)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    principal = _current_principal(request)
    if principal is None:
        return RedirectResponse("/login", status_code=303)
    if principal.get("role") == "platform_admin":
        return RedirectResponse("/admin", status_code=303)
    return _html_response(INDEX_PATH)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    principal = _current_principal(request)
    if principal is None:
        return RedirectResponse("/login", status_code=303)
    if principal.get("role") != "platform_admin":
        return RedirectResponse("/", status_code=303)
    return _html_response(ADMIN_PATH)


@app.get("/api/auth/session")
def auth_session(request: Request):
    principal = _current_principal(request)
    return {"authenticated": principal is not None, "user": principal}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    _check_same_origin(request)
    payload = await read_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="登录内容不合法")
    key = _login_key(request)
    if not _login_allowed(key):
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
    username = payload.get("username")
    password = payload.get("password")
    principal = authenticate_user_safe(username, password)
    if principal is None:
        _record_login_failure(key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _clear_login_failures(key)
    response = JSONResponse({"authenticated": True, "user": principal})
    if AUTH_ENABLED:
        token = create_session(int(principal["id"]))
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=12 * 60 * 60,
            httponly=True,
            secure=SESSION_COOKIE_SECURE,
            samesite="lax",
            path="/",
        )
    return response


def authenticate_user_safe(username: object, password: object) -> dict | None:
    from webapp.tenant_store import authenticate_user

    return authenticate_user(username, password)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    _check_same_origin(request)
    revoke_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.post("/api/auth/password")
async def auth_password(request: Request, principal: dict = Depends(require_mutation_auth)):
    payload = await read_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="密码内容不合法")
    try:
        change_password(int(principal["id"]), payload.get("password"))
    except TenantStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/api/state")
def state(principal: dict = Depends(require_customer)):
    workspace_id = _customer_workspace(principal)
    result = get_workspace_state(workspace_id)
    result["viewer"] = principal
    return result


@app.get("/api/log")
def log(principal: dict = Depends(require_customer)):
    return {"log": read_workspace_log(_customer_workspace(principal))}


@app.post("/api/config")
async def update_config(
    request: Request,
    principal: dict = Depends(require_mutation_auth),
):
    if principal.get("role") == "platform_admin":
        raise HTTPException(status_code=403, detail="管理员请使用管理后台")
    workspace_id = _customer_workspace(principal)
    if get_workspace_runtime_status(workspace_id).get("running"):
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改配置")
    try:
        payload = await read_json_body(request)
        if not isinstance(payload, dict):
            raise ValueError("配置必须是 JSON 对象")
        config = save_workspace_config(workspace_id, payload, role=principal["role"])
    except HTTPException:
        raise
    except StorageConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OSError, ValueError, TenantStoreError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config": config}


@app.post("/api/run")
def run(principal: dict = Depends(require_mutation_auth)):
    if principal.get("role") == "platform_admin":
        raise HTTPException(status_code=403, detail="管理员请在管理后台选择客户")
    return _start_workspace_run(_customer_workspace(principal), "manual")


def _customer_qr_manager(principal: dict):
    return qr_login_manager_for(_customer_workspace(principal))


@app.post("/api/qr-login/start", status_code=202)
def start_qr_login(principal: dict = Depends(require_mutation_auth)):
    workspace_id = _customer_workspace(principal)
    if get_workspace_runtime_status(workspace_id).get("running"):
        raise HTTPException(status_code=409, detail="任务运行中，暂不能扫码添加账号")
    try:
        return _customer_qr_manager(principal).start()
    except QRLoginBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/qr-login/status")
def qr_login_status(principal: dict = Depends(require_customer)):
    try:
        return _customer_qr_manager(principal).status()
    except QRLoginStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/qr-login/image")
def qr_login_image(principal: dict = Depends(require_customer)):
    try:
        png = _customer_qr_manager(principal).image()
    except QRLoginStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Content-Disposition": "inline; filename=login-qr.png",
        },
    )


@app.post("/api/qr-login/probe", status_code=202)
def probe_qr_login(principal: dict = Depends(require_mutation_auth)):
    try:
        return _customer_qr_manager(principal).probe()
    except QRLoginStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/qr-login/verify", status_code=202)
async def verify_qr_login(
    request: Request,
    principal: dict = Depends(require_mutation_auth),
):
    payload = await read_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="短信验证码必须是 JSON 对象")
    try:
        return _customer_qr_manager(principal).submit_verification_code(payload.get("code"))
    except QRLoginStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/qr-login/confirm")
async def confirm_qr_login(
    request: Request,
    principal: dict = Depends(require_mutation_auth),
):
    payload = await read_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="账号资料必须是 JSON 对象")
    try:
        return _customer_qr_manager(principal).confirm(
            payload.get("unique_id"), payload.get("username")
        )
    except QRLoginStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/qr-login/cancel")
def cancel_qr_login(principal: dict = Depends(require_mutation_auth)):
    try:
        return _customer_qr_manager(principal).cancel()
    except QRLoginStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/admin/overview")
def admin_overview_route(_: dict = Depends(require_admin)):
    return admin_overview()


@app.get("/api/admin/users")
def admin_users(_: dict = Depends(require_admin)):
    return {"users": list_users()}


@app.post("/api/admin/users")
async def admin_create_user(
    request: Request,
    principal: dict = Depends(require_mutation_auth),
):
    if principal.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    payload = await read_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="客户资料不合法")
    provided_password = str(payload.get("password") or "").strip()
    temporary_password = provided_password or secrets.token_urlsafe(12)
    try:
        user = create_customer(
            payload.get("username"),
            temporary_password,
            payload.get("displayName"),
            payload.get("maxAccounts", 3),
            payload.get("maxTargets", 50),
            actor_user_id=int(principal["id"]),
        )
    except (ValueError, TenantStoreError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "user": user,
        "temporaryPassword": None if provided_password else temporary_password,
    }


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(
    user_id: int,
    request: Request,
    principal: dict = Depends(require_mutation_auth),
):
    if principal.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    payload = await read_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="客户资料不合法")
    try:
        user = update_customer(
            user_id,
            enabled=payload.get("enabled"),
            display_name=payload.get("displayName"),
            max_accounts=payload.get("maxAccounts"),
            max_targets=payload.get("maxTargets"),
            actor_user_id=int(principal["id"]),
        )
    except (ValueError, TenantStoreError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": user}


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    principal: dict = Depends(require_mutation_auth),
):
    if principal.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    try:
        password = reset_customer_password(user_id, actor_user_id=int(principal["id"]))
    except TenantStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"temporaryPassword": password}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    user_id: int,
    principal: dict = Depends(require_mutation_auth),
):
    if principal.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    try:
        delete_customer(user_id, actor_user_id=int(principal["id"]))
    except TenantStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/admin/users/{user_id}/state")
def admin_user_state(user_id: int, _: dict = Depends(require_admin)):
    _, workspace_id = _workspace_for_admin(user_id)
    return get_workspace_state(workspace_id)


@app.get("/api/admin/users/{user_id}/log")
def admin_user_log(user_id: int, _: dict = Depends(require_admin)):
    _, workspace_id = _workspace_for_admin(user_id)
    return {"log": read_workspace_log(workspace_id)}


@app.post("/api/admin/users/{user_id}/config")
async def admin_user_config(
    user_id: int,
    request: Request,
    principal: dict = Depends(require_mutation_auth),
):
    if principal.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    _, workspace_id = _workspace_for_admin(user_id)
    if get_workspace_runtime_status(workspace_id).get("running"):
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改配置")
    payload = await read_json_body(request)
    try:
        config = save_workspace_config(workspace_id, payload, role="platform_admin")
    except StorageConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OSError, ValueError, TenantStoreError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config": config}


@app.post("/api/admin/users/{user_id}/run")
def admin_user_run(user_id: int, _: dict = Depends(require_admin)):
    _, workspace_id = _workspace_for_admin(user_id)
    return _start_workspace_run(workspace_id, "manual")


@app.get("/api/admin/accounts")
def admin_account_list(_: dict = Depends(require_admin)):
    return {"accounts": admin_accounts()}


@app.get("/api/admin/audit")
def admin_audit(_: dict = Depends(require_admin)):
    return {"events": list_audit_events()}
