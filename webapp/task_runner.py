from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

from webapp.tenant_store import (
    DATA_DIR,
    LOCK_PATH,
    create_run,
    finish_run,
    get_workspace_config,
    initialize_store,
    platform_admin_workspace_id,
    update_run_pid,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _task_environment(config: dict) -> dict:
    """Build the legacy worker environment from one customer workspace."""
    settings = config["settings"]
    environment = os.environ.copy()
    # The browser worker never needs web login credentials or portal secrets.
    for key in (
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "PROMETHEUS_RELAY_COOKIE_KEY",
        "PROMETHEUS_RELAY_SESSION_SECRET",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "PROXY_ADDRESS": settings.get("proxyAddress", ""),
            "MESSAGE_TEMPLATE": settings.get("messageTemplate", "续火花"),
            "HITOKOTO_TYPES": json.dumps(
                settings.get("hitokotoTypes", []), ensure_ascii=False
            ),
            "MATCH_MODE": settings.get("matchMode", "short_id"),
            "BROWSER_TIMEOUT": str(settings.get("browserTimeout", 120000)),
            "FRIEND_LIST_WAIT_TIME": str(settings.get("friendListTimeout", 2000)),
            "TASK_RETRY_TIMES": str(settings.get("taskRetryTimes", 3)),
            "LOG_LEVEL": settings.get("logLevel", "Info"),
            "TASKS": json.dumps(
                [
                    {
                        "unique_id": account["unique_id"],
                        "username": account["username"],
                        "targets": account.get("targets", []),
                    }
                    for account in config.get("accounts", [])
                ],
                ensure_ascii=False,
            ),
            "HEADLESS": "true",
            "DEBUG": "false",
        }
    )
    for account in config.get("accounts", []):
        key = f"COOKIES_{account['unique_id']}".upper()
        environment[key] = json.dumps(account.get("cookies", []), ensure_ascii=False)
    return environment


def _workspace_id_from_environment(workspace_id: int | None) -> int:
    if workspace_id is not None:
        return int(workspace_id)
    raw = os.getenv("PROMETHEUS_RELAY_WORKSPACE_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError("PROMETHEUS_RELAY_WORKSPACE_ID 不合法") from exc
    # Keep the existing one-shot systemd helper usable for the migrated
    # administrator workspace. Customer tasks always pass an explicit scope
    # from the web app or scheduler.
    return platform_admin_workspace_id()


def run_once(workspace_id: int | None = None, trigger: str | None = None) -> int:
    """Run one isolated workspace task without exposing other customers."""
    initialize_store()
    workspace_id = _workspace_id_from_environment(workspace_id)
    trigger_value = (
        trigger
        or os.getenv("PROMETHEUS_RELAY_TRIGGER")
        or "system"
    ).strip().lower()
    if trigger_value not in {"manual", "schedule", "system"}:
        trigger_value = "system"

    lock_handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        os.chmod(LOCK_PATH, 0o600)
    except PermissionError:
        pass
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return 2

    run_id = ""
    process = None
    try:
        config = get_workspace_config(workspace_id, include_cookies=True)
        account_count = len(config.get("accounts", []))
        target_count = sum(
            len(account.get("targets", [])) for account in config.get("accounts", [])
        )
        run_id, log_path = create_run(
            workspace_id,
            trigger_value,
            account_count,
            target_count,
            pid=os.getpid(),
        )
        started_at = _now()
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(
                f"\n=== task started {started_at}, trigger={trigger_value} ===\n"
            )
            log_handle.flush()
            process = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=PROJECT_ROOT,
                env=_task_environment(config),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            update_run_pid(run_id, process.pid)
            exit_code = process.wait()
            finished_at = _now()
            log_handle.write(
                f"=== task finished {finished_at}, exit={exit_code} ===\n"
            )
        finish_run(run_id, exit_code)
        return exit_code
    except Exception as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if run_id:
            finish_run(run_id, 1, f"{type(exc).__name__}: {str(exc)[:300]}")
        return 1
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(run_once())
