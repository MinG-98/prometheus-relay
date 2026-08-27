from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone as utc_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from webapp.task_runner import run_once
from webapp.tenant_store import (
    initialize_store,
    list_enabled_workspaces,
    read_scheduler_status,
    write_scheduler_status,
)


LOGGER = logging.getLogger("prometheus-relay-scheduler")


def _poll_interval() -> float:
    try:
        value = float(
            os.getenv(
                "PROMETHEUS_RELAY_SCHEDULER_POLL_SECONDS",
                os.getenv("DOUYIN_SCHEDULER_POLL_SECONDS", "15"),
            )
        )
    except ValueError:
        value = 15.0
    return max(5.0, min(value, 60.0))


def _utc_now() -> str:
    return datetime.now(utc_timezone.utc).isoformat()


def _scheduler_enabled() -> bool:
    return str(os.getenv("PROMETHEUS_RELAY_SCHEDULER_ENABLED", "true")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def run_scheduler() -> None:
    initialize_store()
    interval = _poll_interval()
    scheduler_status = read_scheduler_status()
    last_run_keys = scheduler_status.get("lastRunKeys", {})
    if not isinstance(last_run_keys, dict):
        last_run_keys = {}
    last_heartbeat = 0.0
    LOGGER.info("定时调度器已启动，检查间隔 %.0f 秒", interval)

    while True:
        try:
            enabled = _scheduler_enabled()
            due_count = 0
            if enabled:
                for workspace_id, schedule in list_enabled_workspaces():
                    if not schedule.get("enabled"):
                        continue
                    timezone_name = str(schedule.get("timezone") or "Asia/Shanghai")
                    try:
                        timezone = ZoneInfo(timezone_name)
                    except (ZoneInfoNotFoundError, ValueError):
                        LOGGER.error("工作区 %s 的定时配置时区无效", workspace_id)
                        continue
                    schedule_time = str(schedule.get("time") or "09:00")
                    now = datetime.now(timezone)
                    run_key = f"{workspace_id}|{now.date().isoformat()}|{timezone_name}|{schedule_time}"
                    if now.strftime("%H:%M") != schedule_time or last_run_keys.get(str(workspace_id)) == run_key:
                        continue
                    due_count += 1
                    LOGGER.info("开始工作区 %s 的定时任务（%s %s）", workspace_id, now.strftime("%Y-%m-%d %H:%M"), timezone_name)
                    exit_code = run_once(workspace_id=workspace_id, trigger="schedule")
                    LOGGER.info("工作区 %s 定时任务结束，退出码 %s", workspace_id, exit_code)
                    last_run_keys[str(workspace_id)] = run_key

            now_monotonic = time.monotonic()
            if now_monotonic - last_heartbeat >= 60 or due_count:
                scheduler_status.update(
                    {
                        "heartbeatAt": _utc_now(),
                        "enabled": enabled,
                        "state": "idle",
                        "lastError": None,
                        "lastRunKeys": last_run_keys,
                    }
                )
                if due_count:
                    scheduler_status["lastTriggeredAt"] = _utc_now()
                write_scheduler_status(scheduler_status)
                last_heartbeat = now_monotonic
        except Exception as exc:  # Keep the long-running scheduler alive after a bad poll.
            LOGGER.exception("定时调度器检查失败: %s", exc)
            try:
                scheduler_status.update(
                    {
                        "heartbeatAt": _utc_now(),
                        "state": "error",
                        "lastError": str(exc)[:300],
                        "lastRunKeys": last_run_keys,
                    }
                )
                write_scheduler_status(scheduler_status)
            except OSError:
                LOGGER.exception("无法写入调度器状态")
            last_heartbeat = time.monotonic()
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "Info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_scheduler()
