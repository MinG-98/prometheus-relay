from __future__ import annotations

import base64
import binascii
import os
import secrets
import subprocess
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
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

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=INDEX_PATH.parent), name="static")


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
            headers={"WWW-Authenticate": 'Basic realm="Douyin Fire"'},
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
            headers={"WWW-Authenticate": 'Basic realm="Douyin Fire"'},
        )


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
async def update_config(request: Request, _: None = Depends(require_auth)):
    if read_status().get("running"):
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改配置")
    try:
        payload = await request.json()
        current = load_config()
        config = normalise_config(payload, current=current)
        save_config(config)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config": public_config(config)}


@app.post("/api/run")
def run(_: None = Depends(require_auth)):
    if read_status().get("running"):
        raise HTTPException(status_code=409, detail="已有任务正在运行")
    environment = os.environ.copy()
    environment["DOUYIN_TRIGGER"] = "manual"
    process = subprocess.Popen(
        [sys.executable, "-m", "webapp.task_runner"],
        cwd=PROJECT_ROOT,
        env=environment,
        start_new_session=True,
    )
    return JSONResponse({"accepted": True, "pid": process.pid})
