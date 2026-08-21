from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DATA_DIR = Path(os.getenv("DOUYIN_DATA_DIR", "/app/data"))
CONFIG_PATH = DATA_DIR / "config.json"
STATUS_PATH = DATA_DIR / "status.json"
LOG_PATH = DATA_DIR / "task.log"
LOCK_PATH = DATA_DIR / "task.lock"

SUPPORTED_LOG_LEVELS = {"Debug", "Info", "Warning", "Error"}
DEFAULT_CONFIG = {
    "settings": {
        "proxyAddress": "",
        "messageTemplate": "续火花",
        "hitokotoTypes": ["文学", "影视", "诗词", "哲学"],
        "matchMode": "short_id",
        "browserTimeout": 120000,
        "friendListTimeout": 2000,
        "taskRetryTimes": 3,
        "logLevel": "Info",
        "schedule": {
            "enabled": False,
            "time": "09:00",
            "timezone": "Asia/Shanghai",
        },
    },
    "accounts": [],
}


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except PermissionError:
        pass


def _atomic_write_json(path: Path, value: dict) -> None:
    ensure_data_dir()
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=DATA_DIR,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary_path = Path(handle.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)


def load_config() -> dict:
    ensure_data_dir()
    if not CONFIG_PATH.exists():
        config = deepcopy(DEFAULT_CONFIG)
        _atomic_write_json(CONFIG_PATH, config)
        return config

    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    return config


def save_config(config: dict) -> None:
    _atomic_write_json(CONFIG_PATH, config)


def read_status() -> dict:
    ensure_data_dir()
    if not STATUS_PATH.exists():
        return {"running": False, "exitCode": None}
    try:
        with STATUS_PATH.open("r", encoding="utf-8") as handle:
            status = json.load(handle)
        return status if isinstance(status, dict) else {"running": False, "exitCode": None}
    except (OSError, json.JSONDecodeError):
        return {"running": False, "exitCode": None}


def write_status(status: dict) -> None:
    _atomic_write_json(STATUS_PATH, status)


def read_log(max_lines: int = 200) -> str:
    ensure_data_dir()
    if not LOG_PATH.exists():
        return ""
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


def _as_int(value, default: int, minimum: int, maximum: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return number


def _normalise_cookies(value, account_name: str):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{account_name} 的 Cookie 不是有效 JSON: {exc}") from exc
    if not isinstance(value, list) or not value:
        raise ValueError(f"{account_name} 必须提供非空 Cookie JSON 数组")
    cookies = []
    for index, cookie in enumerate(value):
        if not isinstance(cookie, dict):
            raise ValueError(f"{account_name} 的 Cookie 第 {index + 1} 项必须是对象")
        cookies.append(dict(cookie))
    return cookies


def _normalise_schedule(value: object, defaults: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("schedule 必须是 JSON 对象")
    enabled = value.get("enabled", defaults["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError("schedule.enabled 必须是布尔值")
    schedule_time = str(value.get("time", defaults["time"])).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time):
        raise ValueError("schedule.time 必须是 HH:MM 格式")
    timezone = str(value.get("timezone", defaults["timezone"])).strip()
    if not timezone or len(timezone) > 64:
        raise ValueError("schedule.timezone 不合法")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"定时运行时区不支持: {timezone}") from exc
    return {"enabled": enabled, "time": schedule_time, "timezone": timezone}


def normalise_config(payload: dict, current: dict | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("配置必须是 JSON 对象")
    current = current or {"settings": {}, "accounts": []}
    current_accounts = {
        str(account.get("unique_id")): account
        for account in current.get("accounts", [])
        if isinstance(account, dict) and account.get("unique_id")
    }
    raw_settings = payload.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise ValueError("settings 必须是 JSON 对象")

    defaults = DEFAULT_CONFIG["settings"]
    match_mode = str(raw_settings.get("matchMode", defaults["matchMode"])).strip()
    if match_mode not in {"nickname", "short_id"}:
        raise ValueError("matchMode 只能是 nickname 或 short_id")
    log_level = str(raw_settings.get("logLevel", defaults["logLevel"])).strip().capitalize()
    if log_level not in SUPPORTED_LOG_LEVELS:
        raise ValueError("logLevel 不合法")
    hitokoto_types = raw_settings.get("hitokotoTypes", defaults["hitokotoTypes"])
    if not isinstance(hitokoto_types, list) or not all(isinstance(item, str) for item in hitokoto_types):
        raise ValueError("hitokotoTypes 必须是字符串数组")
    schedule = _normalise_schedule(raw_settings.get("schedule", defaults["schedule"]), defaults["schedule"])

    settings = {
        "proxyAddress": str(raw_settings.get("proxyAddress", defaults["proxyAddress"])).strip(),
        "messageTemplate": str(
            raw_settings.get("messageTemplate", defaults["messageTemplate"])
        )[:2000],
        "hitokotoTypes": hitokoto_types,
        "matchMode": match_mode,
        "browserTimeout": _as_int(
            raw_settings.get("browserTimeout", defaults["browserTimeout"]),
            defaults["browserTimeout"],
            1000,
            600000,
            "browserTimeout",
        ),
        "friendListTimeout": _as_int(
            raw_settings.get("friendListTimeout", defaults["friendListTimeout"]),
            defaults["friendListTimeout"],
            0,
            60000,
            "friendListTimeout",
        ),
        "taskRetryTimes": _as_int(
            raw_settings.get("taskRetryTimes", defaults["taskRetryTimes"]),
            defaults["taskRetryTimes"],
            1,
            10,
            "taskRetryTimes",
        ),
        "logLevel": log_level,
        "schedule": schedule,
    }

    raw_accounts = payload.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise ValueError("accounts 必须是 JSON 数组")
    if len(raw_accounts) > 20:
        raise ValueError("最多支持 20 个账号")

    accounts = []
    seen_ids = set()
    for raw_account in raw_accounts:
        if not isinstance(raw_account, dict):
            raise ValueError("每个账号必须是 JSON 对象")
        unique_id = str(raw_account.get("unique_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", unique_id):
            raise ValueError("抖音号只能包含字母、数字、下划线或短横线")
        if unique_id in seen_ids:
            raise ValueError(f"账号 {unique_id} 重复")
        seen_ids.add(unique_id)

        username = str(raw_account.get("username", "")).strip()[:120]
        if not username:
            raise ValueError(f"账号 {unique_id} 缺少用户名")
        targets = raw_account.get("targets", [])
        if isinstance(targets, str):
            targets = [item.strip() for item in targets.replace("，", ",").split(",")]
        if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
            raise ValueError(f"账号 {unique_id} 的 targets 必须是字符串数组")
        targets = [item.strip()[:120] for item in targets if item.strip()][:100]

        raw_cookies = raw_account.get("cookies")
        if raw_cookies is None or (isinstance(raw_cookies, str) and not raw_cookies.strip()):
            previous = current_accounts.get(unique_id, {})
            raw_cookies = previous.get("cookies")
        cookies = _normalise_cookies(raw_cookies, username)
        accounts.append(
            {
                "unique_id": unique_id,
                "username": username,
                "targets": targets,
                "cookies": cookies,
            }
        )

    return {"settings": settings, "accounts": accounts}


def public_config(config: dict) -> dict:
    settings = deepcopy(DEFAULT_CONFIG["settings"])
    settings.update(deepcopy(config.get("settings", {})))
    return {
        "settings": settings,
        "accounts": [
            {
                "unique_id": account.get("unique_id", ""),
                "username": account.get("username", ""),
                "targets": account.get("targets", []),
                "hasCookies": bool(account.get("cookies")),
                "cookieCount": len(account.get("cookies", [])),
            }
            for account in config.get("accounts", [])
            if isinstance(account, dict)
        ],
    }
