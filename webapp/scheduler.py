from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone as utc_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from webapp.config_store import (
    DEFAULT_CONFIG,
    _normalise_schedule,
    load_config,
    read_scheduler_status,
    read_status,
    write_scheduler_status,
)
from webapp.task_runner import run_once


LOGGER = logging.getLogger("douyin-fire-scheduler")


def _poll_interval() -> float:
    try:
        value = float(os.getenv("DOUYIN_SCHEDULER_POLL_SECONDS", "15"))
    except ValueError:
        value = 15.0
    return max(5.0, min(value, 60.0))


def _utc_now() -> str:
    return datetime.now(utc_timezone.utc).isoformat()


def _enabled_schedule(config: dict) -> tuple[str, str, ZoneInfo] | None:
    settings = config.get("settings", {}) if isinstance(config, dict) else {}
    raw_schedule = settings.get("schedule", DEFAULT_CONFIG["settings"]["schedule"])
    try:
        schedule = _normalise_schedule(raw_schedule, DEFAULT_CONFIG["settings"]["schedule"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        LOGGER.error("定时配置无效: %s", exc)
        return None
    if not schedule["enabled"]:
        return None
    try:
        timezone = ZoneInfo(schedule["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        LOGGER.error("定时配置时区无效: %s", exc)
        return None
    return schedule["time"], schedule["timezone"], timezone


def run_scheduler() -> None:
    interval = _poll_interval()
    scheduler_status = read_scheduler_status()
    last_run_key = scheduler_status.get("lastRunKey")
    last_heartbeat = 0.0
    LOGGER.info("定时调度器已启动，检查间隔 %.0f 秒", interval)
    while True:
        try:
            config = load_config()
            schedule = _enabled_schedule(config)
            now_monotonic = time.monotonic()
            should_write_status = now_monotonic - last_heartbeat >= 60

            if schedule:
                schedule_time, timezone_name, timezone = schedule
                now = datetime.now(timezone)
                run_key = f"{now.date().isoformat()}|{timezone_name}|{schedule_time}"
                if now.strftime("%H:%M") == schedule_time and run_key != last_run_key:
                    if read_status().get("running"):
                        LOGGER.warning("到达定时运行时间，但已有任务运行，跳过本次任务")
                        scheduler_status.update(
                            {
                                "lastSkippedAt": _utc_now(),
                                "lastOutcome": "skipped-running",
                            }
                        )
                    else:
                        LOGGER.info("开始定时任务（%s %s）", now.strftime("%Y-%m-%d %H:%M"), timezone_name)
                        scheduler_status.update(
                            {
                                "heartbeatAt": _utc_now(),
                                "enabled": True,
                                "scheduleTime": schedule_time,
                                "timezone": timezone_name,
                                "state": "running",
                                "lastTriggeredAt": _utc_now(),
                            }
                        )
                        write_scheduler_status(scheduler_status)
                        exit_code = run_once(trigger="schedule")
                        LOGGER.info("定时任务结束，退出码 %s", exit_code)
                        scheduler_status.update(
                            {
                                "lastFinishedAt": _utc_now(),
                                "lastExitCode": exit_code,
                                "lastOutcome": "success" if exit_code == 0 else "failed",
                            }
                        )
                    last_run_key = run_key
                    scheduler_status["lastRunKey"] = run_key
                    should_write_status = True

                if should_write_status:
                    scheduler_status.update(
                        {
                            "heartbeatAt": _utc_now(),
                            "enabled": True,
                            "scheduleTime": schedule_time,
                            "timezone": timezone_name,
                            "state": "idle",
                            "lastError": None,
                        }
                    )
            elif should_write_status:
                scheduler_status.update(
                    {
                        "heartbeatAt": _utc_now(),
                        "enabled": False,
                        "scheduleTime": None,
                        "timezone": None,
                        "state": "idle",
                        "lastError": None,
                    }
                )

            if should_write_status:
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
