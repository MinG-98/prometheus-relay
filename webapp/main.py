from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from webapp.config_store import (
    load_config,
    normalise_config,
    public_config,
    read_history,
    read_log,
    read_scheduler_status,
    read_status,
    save_config,
)
from webapp.qr_login import (
    QRLoginBusyError,
    QRLoginStateError,
    qr_login_manager,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = Path(__file__).resolve().parent / "static" / "index.html"
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
AUTH_ENABLED = str(os.getenv("AUTH_ENABLED", "true")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    qr_login_manager.shutdown()

app = FastAPI(
    title="Prometheus Relay",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=INDEX_PATH.parent), name="static")


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
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def require_auth(request: Request) -> None:
    if not AUTH_ENABLED:
        return
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD 尚未配置")
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="需要登录",
            headers={"WWW-Authenticate": 'Basic realm="Prometheus Relay"'},
        )
    try:
        encoded = authorization[6:].strip()
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        username, password = "", ""
    if not (
        secrets.compare_digest(username, ADMIN_USERNAME)
        and secrets.compare_digest(password, ADMIN_PASSWORD)
    ):
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": 'Basic realm="Prometheus Relay"'},
        )


def require_mutation_auth(request: Request, _: None = Depends(require_auth)) -> None:
    """Reject browser cross-site requests before any state-changing action."""
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


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(require_auth)):
    return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))


@app.get("/api/state")
def state(_: None = Depends(require_auth)):
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"读取配置失败: {exc}") from exc
    return {
        "config": public_config(config),
        "status": read_status(),
        "history": read_history(),
        "scheduler": read_scheduler_status(),
    }


@app.get("/api/log")
def log(_: None = Depends(require_auth)):
    return {"log": read_log()}


@app.post("/api/config")
async def update_config(request: Request, _: None = Depends(require_mutation_auth)):
    if read_status().get("running"):
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改配置")
    try:
        payload = await read_json_body(request)
        current = load_config()
        config = normalise_config(payload, current=current)
        save_config(config)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config": public_config(config)}


@app.post("/api/run")
def run(_: None = Depends(require_mutation_auth)):
    if read_status().get("running"):
        raise HTTPException(status_code=409, detail="已有任务正在运行")
    environment = os.environ.copy()
    environment["PROMETHEUS_RELAY_TRIGGER"] = "manual"
    process = subprocess.Popen(
        [sys.executable, "-m", "webapp.task_runner"],
        cwd=PROJECT_ROOT,
        env=environment,
        start_new_session=True,
    )
    return JSONResponse({"accepted": True, "pid": process.pid})


@app.post("/api/qr-login/start", status_code=202)
def start_qr_login(_: None = Depends(require_mutation_auth)):
    if read_status().get("running"):
        raise HTTPException(status_code=409, detail="任务运行中，暂不能扫码添加账号")
    try:
        return qr_login_manager.start()
    except QRLoginBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/qr-login/status")
def qr_login_status(_: None = Depends(require_auth)):
    try:
        return qr_login_manager.status()
    except QRLoginStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/qr-login/image")
def qr_login_image(_: None = Depends(require_auth)):
    try:
        png = qr_login_manager.image()
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
def probe_qr_login(_: None = Depends(require_mutation_auth)):
    try:
        return qr_login_manager.probe()
    except QRLoginStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/qr-login/verify", status_code=202)
async def verify_qr_login(
    request: Request, _: None = Depends(require_mutation_auth)
):
    payload = await read_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="短信验证码必须是 JSON 对象")
    try:
        return qr_login_manager.submit_verification_code(payload.get("code"))
    except QRLoginStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/qr-login/confirm")
async def confirm_qr_login(
    request: Request, _: None = Depends(require_mutation_auth)
):
    payload = await read_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="账号资料必须是 JSON 对象")
    try:
        return qr_login_manager.confirm(
            payload.get("unique_id"), payload.get("username")
        )
    except QRLoginStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/qr-login/cancel")
def cancel_qr_login(_: None = Depends(require_mutation_auth)):
    try:
        return qr_login_manager.cancel()
    except QRLoginStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
