from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from webapp.config_store import (
    LOCK_PATH,
    LOG_PATH,
    read_status,
    load_config,
    ensure_data_dir,
    write_status,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_environment(config: dict) -> dict:
    settings = config["settings"]
    environment = os.environ.copy()
    # Do not pass the web admin credentials to the automation subprocess.
    environment.pop("ADMIN_USERNAME", None)
    environment.pop("ADMIN_PASSWORD", None)
    environment.update(
        {
            "PROXY_ADDRESS": settings.get("proxyAddress", ""),
            "MESSAGE_TEMPLATE": settings.get("messageTemplate", "续火花"),
            "HITOKOTO_TYPES": json.dumps(settings.get("hitokotoTypes", []), ensure_ascii=False),
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


def run_once() -> int:
    ensure_data_dir()
    lock_handle = LOCK_PATH.open("a+", encoding="utf-8")
    os.chmod(LOCK_PATH, 0o600)
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 2

    config = load_config()
    existing_status = read_status()
    if existing_status.get("running"):
        return 2

    started_at = _now()
    write_status(
        {
            "running": True,
            "startedAt": started_at,
            "finishedAt": None,
            "exitCode": None,
            "pid": os.getpid(),
        }
    )

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log_handle:
        log_handle.write(f"\n=== task started {started_at} ===\n")
        log_handle.flush()
        process = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=PROJECT_ROOT,
            env=_task_environment(config),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        write_status(
            {
                "running": True,
                "startedAt": started_at,
                "finishedAt": None,
                "exitCode": None,
                "pid": process.pid,
            }
        )
        exit_code = process.wait()
        finished_at = _now()
        log_handle.write(f"=== task finished {finished_at}, exit={exit_code} ===\n")

    write_status(
        {
            "running": False,
            "startedAt": started_at,
            "finishedAt": finished_at,
            "exitCode": exit_code,
            "pid": None,
        }
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_once())
