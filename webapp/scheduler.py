from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from tempfile import NamedTemporaryFile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from webapp.config_store import (
    DATA_DIR,
    DEFAULT_CONFIG,
    _normalise_schedule,
    ensure_data_dir,
    load_config,
    read_status,
)
from webapp.task_runner import run_once


LOGGER = logging.getLogger("douyin-fire-scheduler")
MARKER_PATH = DATA_DIR / "schedule-status.json"


def _poll_interval() -> float:
    try:
        value = float(os.getenv("DOUYIN_SCHEDULER_POLL_SECONDS", "15"))
    except ValueError:
        value = 15.0
    return max(5.0, min(value, 60.0))


def _read_last_run_key() -> str | None:
    ensure_data_dir()
    try:
        with MARKER_PATH.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        key = value.get("lastRunKey") if isinstance(value, dict) else None
        return str(key) if key else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _write_last_run_key(key: str) -> None:
    ensure_data_dir()
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=DATA_DIR,
        prefix=".schedule-status.",
        delete=False,
    ) as handle:
        json.dump({"lastRunKey": key}, handle, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary_path = Path(handle.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, MARKER_PATH)


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
    last_run_key = _read_last_run_key()
    LOGGER.info("定时调度器已启动，检查间隔 %.0f 秒", interval)
    while True:
        try:
            config = load_config()
            schedule = _enabled_schedule(config)
            if schedule:
                schedule_time, timezone_name, timezone = schedule
                now = datetime.now(timezone)
                run_key = f"{now.date().isoformat()}|{timezone_name}|{schedule_time}"
                if now.strftime("%H:%M") == schedule_time and run_key != last_run_key:
                    if read_status().get("running"):
                        LOGGER.warning("到达定时运行时间，但已有任务运行，跳过本次任务")
                    else:
                        LOGGER.info("开始定时任务（%s %s）", now.strftime("%Y-%m-%d %H:%M"), timezone_name)
                        exit_code = run_once()
                        LOGGER.info("定时任务结束，退出码 %s", exit_code)
                    last_run_key = run_key
                    _write_last_run_key(run_key)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.error("读取定时配置失败: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "Info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_scheduler()
