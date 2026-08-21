import os, sys
from enum import Enum
import json
import logging
from utils.logger import setup_logger

logger = setup_logger(level=logging.DEBUG)

"""
是否启用调试模式
更详细的日志打印，浏览器操作可视化等
"""
DEBUG = False
config = None
userData = None

SUPPORTED_LOG_LEVELS = {"Debug", "Info", "Warning", "Error"}


def _env_bool(value, default=False):
    """Read a boolean environment value with a safe fallback."""
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


DEBUG = _env_bool(os.getenv("DEBUG"), default=False)


class Environment(Enum):
    GITHUBACTION = "GITHUB_ACTION"  # GitHub Action 运行
    LOCAL = "LOCAL"  # 本地代码运行
    PACKED = "PACKED"  # PyInstaller 打包运行

    def __str__(self):
        return self.value


def _parse_json(value, default, name):
    """Parse a JSON environment variable without aborting all tasks."""
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        logger.warning(f"{name} 不是有效 JSON，使用默认值: {exc}")
        return default


def _parse_json_list(value, default, name):
    parsed = _parse_json(value, default, name)
    if not isinstance(parsed, list):
        logger.warning(f"{name} 必须是 JSON 数组，使用默认值")
        return default
    return parsed


def _normalise_log_level(value):
    level = str(value or "Info").strip().capitalize()
    if level not in SUPPORTED_LOG_LEVELS:
        logger.warning(f"不支持的日志级别 {value!r}，使用 Info")
        return "Info"
    return level


def _decode_escaped_newlines(value):
    """Decode .env newlines without corrupting non-ASCII cookie values."""
    return value.replace("\\r\\n", "\r\n").replace("\\n", "\n").replace("\\r", "\r")


def get_environment():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    elif os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    else:
        return Environment.LOCAL


def get_config():
    """
    获取配置信息
    :return: 配置字典
    """
    global config

    if config is not None:
        return config

    config = {
        "proxyAddress": os.getenv("PROXY_ADDRESS", ""),
        "messageTemplate": os.getenv("MESSAGE_TEMPLATE", "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]"),
        "hitokotoTypes": _parse_json_list(
            os.getenv("HITOKOTO_TYPES"),
            ["文学", "影视", "诗词", "哲学"],
            "HITOKOTO_TYPES",
        ),
        "matchMode": os.getenv("MATCH_MODE", "nickname"),  # 是否使用短 ID 进行好友匹配
        "browserTimeout": int(os.getenv("BROWSER_TIMEOUT", "120000")),  # 浏览器操作超时时间，单位毫秒
        "friendListTimeout": int(os.getenv("FRIEND_LIST_WAIT_TIME", "2000")),  # 好友列表加载超时时间，单位毫秒
        "taskRetryTimes": int(os.getenv("TASK_RETRY_TIMES", "3")),  # 任务重试次数
        "logLevel": _normalise_log_level(os.getenv("LOG_LEVEL", "Info")),  # 日志级别
    }

    return config

def sanitize_cookies(cookies):
    if not isinstance(cookies, list):
        raise ValueError("Cookies 必须是 JSON 数组")

    sanitized = []
    for index, cookie in enumerate(cookies):
        if not isinstance(cookie, dict):
            raise ValueError(f"Cookies 第 {index + 1} 项必须是 JSON 对象")

        cleaned_cookie = dict(cookie)
        # Playwright 可能不支持 Cookie-Editor 导出的 sameSite 字段格式。
        cleaned_cookie.pop("sameSite", None)
        sanitized.append(cleaned_cookie)

    return sanitized


def get_userData():
    """
    获取用户数据目录
    :return: 用户数据目录路径
    """
    global userData

    if userData is not None:
        return userData

    tasks = _parse_json_list(os.getenv("TASKS"), [], "TASKS")

    userData = []

    for task in tasks:
        if not isinstance(task, dict):
            logger.warning("TASKS 中存在非对象任务，已跳过")
            continue

        username = task.get("username", "未知用户")
        unique_id = task.get("unique_id")
        if not unique_id:
            logger.warning(f"{username} 的任务  缺少 unique_id 字段，已跳过")
            continue
        cookies_key = f"cookies_{unique_id}".upper()
        cookies_str = _decode_escaped_newlines(os.getenv(cookies_key, ""))
        if not cookies_str:
            logger.warning(
                f"{username} 的任务 缺少 {cookies_key} 环境变量，已跳过"
            )
            continue
        try:
            cookies = sanitize_cookies(json.loads(cookies_str))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(f"{username} 的任务 {cookies_key} 格式不正确，已跳过: {exc}")
            continue

        targets = task.get("targets", [])
        if not isinstance(targets, list):
            logger.warning(f"{username} 的 targets 必须是 JSON 数组，已使用空列表")
            targets = []

        userData.append(
            {
                "unique_id": unique_id,
                "username": username,
                "cookies": cookies,
                "targets": targets,
            }
        )

    return userData
