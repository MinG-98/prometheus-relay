"""Multi-user storage and authorization primitives for the customer portal.

The public console intentionally keeps the first release small: one platform
administrator and one workspace per customer.  The storage model uses
workspaces and memberships instead of hard-coding accounts directly to a
username so teams and more roles can be added without another migration.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet, InvalidToken

from webapp.config_store import (
    DEFAULT_CONFIG,
    _normalise_cookies,
    normalise_config,
)


DATA_DIR = Path(
    os.getenv(
        "PROMETHEUS_RELAY_DATA_DIR",
        os.getenv("DOUYIN_DATA_DIR", "/app/data"),
    )
)
DATABASE_PATH = DATA_DIR / "prometheus-relay.sqlite3"
LEGACY_CONFIG_PATH = DATA_DIR / "config.json"
RUNS_DIR = DATA_DIR / "runs"
LOCK_PATH = DATA_DIR / "task.lock"
SCHEDULER_STATUS_PATH = DATA_DIR / "schedule-status.json"
SESSION_COOKIE_NAME = "prometheus_relay_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_IDLE_SECONDS = 2 * 60 * 60
MAX_RUN_LOG_BYTES = 2 * 1024 * 1024

_DB_LOCK = threading.RLock()
_SCHEMA_VERSION = 1
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{3,64}")
_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

SYSTEM_SETTING_KEYS = (
    "proxyAddress",
    "browserTimeout",
    "friendListTimeout",
    "taskRetryTimes",
    "logLevel",
)
CUSTOMER_SETTING_KEYS = (
    "messageTemplate",
    "hitokotoTypes",
    "matchMode",
    "schedule",
)


class TenantStoreError(ValueError):
    """Base error for user-facing storage validation failures."""


class StorageConfigurationError(RuntimeError):
    """Raised when a required storage secret or migration input is missing."""


class AccountOwnershipError(TenantStoreError):
    """Raised when an account is already owned by another workspace."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
        os.chmod(RUNS_DIR, 0o700)
    except PermissionError:
        pass


def _connect() -> sqlite3.Connection:
    ensure_data_dir()
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


@contextmanager
def _connection(write: bool = False) -> Iterator[sqlite3.Connection]:
    with _DB_LOCK:
        connection = _connect()
        try:
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str, fallback: object) -> object:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return deepcopy(fallback)


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_accounts INTEGER NOT NULL DEFAULT 3,
            max_targets_per_account INTEGER NOT NULL DEFAULT 50,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('platform_admin', 'workspace_owner', 'workspace_member')),
            display_name TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS workspace_members (
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('owner', 'member', 'viewer')),
            PRIMARY KEY (workspace_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS workspace_settings (
            workspace_id INTEGER PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
            settings_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            unique_id TEXT NOT NULL COLLATE NOCASE UNIQUE,
            username TEXT NOT NULL,
            cookies_encrypted TEXT NOT NULL,
            cookie_count INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            cookie_updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            target_id TEXT NOT NULL,
            UNIQUE(account_id, target_id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            account_count INTEGER NOT NULL DEFAULT 0,
            target_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            exit_code INTEGER,
            pid INTEGER,
            log_path TEXT,
            summary TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_accounts_workspace ON accounts(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_targets_account ON targets(account_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_runs_workspace_started ON runs(workspace_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
        """
    )
    account_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(accounts)")
    }
    if "cookie_count" not in account_columns:
        connection.execute(
            "ALTER TABLE accounts ADD COLUMN cookie_count INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )


def _is_auth_enabled() -> bool:
    return str(os.getenv("AUTH_ENABLED", "true")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _password_hash(password: str) -> str:
    _validate_password(password)
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return f"scrypt${n}${r}${p}${encode(salt)}${encode(digest)}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        padding = "=" * (-len(raw_salt) % 4)
        salt = base64.urlsafe_b64decode((raw_salt + padding).encode("ascii"))
        padding = "=" * (-len(raw_digest) % 4)
        expected = base64.urlsafe_b64decode((raw_digest + padding).encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, UnicodeError):
        return False


def _validate_username(username: object) -> str:
    value = str(username or "").strip()
    if not _USERNAME_PATTERN.fullmatch(value):
        raise TenantStoreError("用户名需要是 3 到 64 位字母、数字、下划线、点号或短横线")
    return value


def _validate_password(password: object) -> str:
    value = str(password or "")
    if len(value) < 8 or len(value) > 256:
        raise TenantStoreError("密码长度需要在 8 到 256 位之间")
    return value


def _cookie_cipher() -> Fernet:
    raw_key = os.getenv("PROMETHEUS_RELAY_COOKIE_KEY", "").strip()
    if not raw_key:
        raise StorageConfigurationError(
            "未配置 PROMETHEUS_RELAY_COOKIE_KEY，无法安全保存 Cookie"
        )
    try:
        return Fernet(raw_key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise StorageConfigurationError(
            "PROMETHEUS_RELAY_COOKIE_KEY 不是有效的 Fernet 密钥"
        ) from exc


def _encrypt_cookies(cookies: object) -> str:
    normalised = _normalise_cookies(cookies, "账号")
    return _cookie_cipher().encrypt(_json(normalised).encode("utf-8")).decode("ascii")


def _decrypt_cookies(encoded: str) -> list[dict]:
    try:
        payload = _cookie_cipher().decrypt(encoded.encode("ascii"))
        return _normalise_cookies(json.loads(payload.decode("utf-8")), "账号")
    except (InvalidToken, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise StorageConfigurationError("无法解密账号 Cookie，请检查存储密钥") from exc


def _default_settings() -> dict:
    return deepcopy(DEFAULT_CONFIG["settings"])


def _ensure_system_settings(
    connection: sqlite3.Connection,
    source: dict | None = None,
) -> None:
    source = source or _default_settings()
    defaults = _default_settings()
    for key in SYSTEM_SETTING_KEYS:
        value = source.get(key, defaults[key])
        connection.execute(
            "INSERT OR IGNORE INTO system_settings(key, value_json) VALUES(?, ?)",
            (key, _json(value)),
        )


def _system_settings(connection: sqlite3.Connection) -> dict:
    result = {key: _default_settings()[key] for key in SYSTEM_SETTING_KEYS}
    for row in connection.execute("SELECT key, value_json FROM system_settings"):
        if row["key"] in result:
            result[row["key"]] = _load_json(row["value_json"], result[row["key"]])
    return result


def _workspace_settings(connection: sqlite3.Connection, workspace_id: int) -> dict:
    settings = _default_settings()
    row = connection.execute(
        "SELECT settings_json FROM workspace_settings WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    if row:
        raw = _load_json(row["settings_json"], {})
        if isinstance(raw, dict):
            settings.update(raw)
    settings.update(_system_settings(connection))
    return settings


def _workspace_for_user(connection: sqlite3.Connection, user_id: int) -> int:
    row = connection.execute(
        """
        SELECT workspace_id
        FROM workspace_members
        WHERE user_id = ?
        ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'member' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    if not row:
        raise TenantStoreError("当前用户没有可用的工作区")
    return int(row["workspace_id"])


def _user_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    workspace_id = None
    if row["role"] != "platform_admin":
        workspace_id = _workspace_for_user(connection, int(row["id"]))
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "displayName": row["display_name"] or row["username"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "workspaceId": workspace_id,
        "lastLoginAt": row["last_login_at"],
    }


def _create_workspace(
    connection: sqlite3.Connection,
    name: str,
    max_accounts: int = 3,
    max_targets: int = 50,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO workspaces(name, max_accounts, max_targets_per_account, created_at)
        VALUES(?, ?, ?, ?)
        """,
        (name[:120], max_accounts, max_targets, _utc_now()),
    )
    workspace_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO workspace_settings(workspace_id, settings_json) VALUES(?, ?)",
        (workspace_id, _json(_default_settings())),
    )
    return workspace_id


def _create_user_row(
    connection: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    display_name: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO users(username, password_hash, role, display_name, created_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (
            _validate_username(username),
            _password_hash(password),
            role,
            str(display_name or username).strip()[:120],
            _utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def _migrate_legacy_config(
    connection: sqlite3.Connection,
    admin_user_id: int,
    admin_workspace_id: int,
) -> None:
    migration_row = connection.execute(
        "SELECT value FROM metadata WHERE key = 'legacy_migrated'"
    ).fetchone()
    if migration_row and migration_row["value"] == "1":
        return

    if not LEGACY_CONFIG_PATH.exists():
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('legacy_migrated', '0') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        return

    try:
        legacy = json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(legacy, dict):
            raise ValueError("旧配置不是对象")
        normalised = normalise_config(legacy, current=legacy)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise StorageConfigurationError(f"旧配置迁移失败: {exc}") from exc

    for key in SYSTEM_SETTING_KEYS:
        connection.execute(
            "INSERT INTO system_settings(key, value_json) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (key, _json(normalised["settings"].get(key, _default_settings()[key]))),
        )
    connection.execute(
        "UPDATE workspace_settings SET settings_json = ? WHERE workspace_id = ?",
        (_json(normalised["settings"]), admin_workspace_id),
    )
    for account in normalised["accounts"]:
        _insert_or_update_account(
            connection,
            admin_workspace_id,
            account["unique_id"],
            account["username"],
            account["cookies"],
            account.get("targets", []),
        )
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES('legacy_migrated', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    record_audit(
        admin_user_id,
        "legacy_config_migrated",
        "workspace",
        str(admin_workspace_id),
        f"accounts={len(normalised['accounts'])}",
        connection=connection,
    )


def initialize_store() -> None:
    """Create the schema, bootstrap the admin, and migrate legacy JSON once."""
    with _connection(write=True) as connection:
        _create_schema(connection)
        _ensure_system_settings(connection)
        admin_username = _validate_username(os.getenv("ADMIN_USERNAME", "admin"))
        admin_row = connection.execute(
            "SELECT * FROM users WHERE role = 'platform_admin' ORDER BY id LIMIT 1"
        ).fetchone()
        if admin_row is None:
            admin_password = os.getenv("ADMIN_PASSWORD", "")
            if not admin_password:
                if _is_auth_enabled():
                    raise StorageConfigurationError(
                        "首次启动需要配置 ADMIN_PASSWORD"
                    )
                admin_password = secrets.token_urlsafe(32)
            admin_user_id = _create_user_row(
                connection,
                admin_username,
                admin_password,
                "platform_admin",
                "管理员",
            )
            admin_workspace_id = _create_workspace(connection, "管理员工作区", 20, 100)
            connection.execute(
                "INSERT INTO workspace_members(workspace_id, user_id, role) VALUES(?, ?, 'owner')",
                (admin_workspace_id, admin_user_id),
            )
        else:
            admin_user_id = int(admin_row["id"])
            row = connection.execute(
                "SELECT workspace_id FROM workspace_members WHERE user_id = ? LIMIT 1",
                (admin_user_id,),
            ).fetchone()
            admin_workspace_id = int(row["workspace_id"]) if row else _create_workspace(
                connection, "管理员工作区", 20, 100
            )
            if not row:
                connection.execute(
                    "INSERT INTO workspace_members(workspace_id, user_id, role) VALUES(?, ?, 'owner')",
                    (admin_workspace_id, admin_user_id),
                )
        _migrate_legacy_config(connection, admin_user_id, admin_workspace_id)


def get_user_by_id(user_id: int) -> dict | None:
    with _connection() as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_dict(connection, row) if row else None


def authenticate_user(username: object, password: object) -> dict | None:
    value = str(username or "").strip()
    password_value = str(password or "")
    with _connection(write=True) as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username = ?",
            (value,),
        ).fetchone()
        if not row or not row["enabled"] or not _password_matches(password_value, row["password_hash"]):
            return None
        now = _utc_now()
        connection.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        refreshed = connection.execute(
            "SELECT * FROM users WHERE id = ?", (row["id"],)
        ).fetchone()
        record_audit(int(row["id"]), "login", "user", str(row["id"]), connection=connection)
        return _user_dict(connection, refreshed)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    with _connection(write=True) as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at < ?", (_utc_now(),))
        connection.execute(
            """
            INSERT INTO sessions(token_hash, user_id, created_at, last_seen_at, expires_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                token_hash,
                user_id,
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat(),
            ),
        )
    return token


def get_session_user(token: object) -> dict | None:
    if not token:
        return None
    token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    with _connection(write=True) as connection:
        row = connection.execute(
            """
            SELECT s.*, u.*
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if not row or not row["enabled"]:
            return None
        expires_at = _parse_iso(row["expires_at"])
        last_seen_at = _parse_iso(row["last_seen_at"])
        if not expires_at or expires_at <= now:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        if last_seen_at and now - last_seen_at > timedelta(seconds=SESSION_IDLE_SECONDS):
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        connection.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (now.isoformat(), token_hash),
        )
        user_row = connection.execute(
            "SELECT * FROM users WHERE id = ?", (row["user_id"],)
        ).fetchone()
        return _user_dict(connection, user_row) if user_row else None


def revoke_session(token: object) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
    with _connection(write=True) as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def change_password(user_id: int, password: object) -> None:
    value = _validate_password(password)
    with _connection(write=True) as connection:
        if not connection.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise TenantStoreError("用户不存在")
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_password_hash(value), user_id),
        )
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        record_audit(user_id, "change_password", "user", str(user_id), connection=connection)


def workspace_id_for_user(user_id: int) -> int:
    with _connection() as connection:
        return _workspace_for_user(connection, user_id)


def platform_admin_workspace_id() -> int:
    """Return the migrated administrator workspace for legacy one-shot runs."""
    with _connection() as connection:
        row = connection.execute(
            """
            SELECT wm.workspace_id
            FROM workspace_members wm
            JOIN users u ON u.id = wm.user_id
            WHERE u.role = 'platform_admin' AND wm.role = 'owner'
            ORDER BY wm.workspace_id
            LIMIT 1
            """
        ).fetchone()
        if not row:
            raise TenantStoreError("管理员没有可用工作区")
        return int(row["workspace_id"])


def _workspace_limits(connection: sqlite3.Connection, workspace_id: int) -> tuple[int, int, bool]:
    row = connection.execute(
        "SELECT max_accounts, max_targets_per_account, enabled FROM workspaces WHERE id = ?",
        (workspace_id,),
    ).fetchone()
    if not row:
        raise TenantStoreError("工作区不存在")
    return int(row["max_accounts"]), int(row["max_targets_per_account"]), bool(row["enabled"])


def _public_account(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    include_owner: bool = False,
) -> dict:
    targets = [
        target["target_id"]
        for target in connection.execute(
            "SELECT target_id FROM targets WHERE account_id = ? ORDER BY id",
            (row["id"],),
        )
    ]
    result = {
        "unique_id": row["unique_id"],
        "username": row["username"],
        "targets": targets,
        "hasCookies": bool(row["cookies_encrypted"]),
        "cookieCount": int(row["cookie_count"]),
        "enabled": bool(row["enabled"]),
        "cookieUpdatedAt": row["cookie_updated_at"],
    }
    if include_owner:
        owner = connection.execute(
            """
            SELECT u.username, u.display_name
            FROM workspace_members wm
            JOIN users u ON u.id = wm.user_id
            WHERE wm.workspace_id = ? AND wm.role = 'owner'
            LIMIT 1
            """,
            (row["workspace_id"],),
        ).fetchone()
        result["ownerUsername"] = owner["username"] if owner else ""
        result["ownerDisplayName"] = owner["display_name"] if owner else ""
    return result


def get_workspace_config(workspace_id: int, include_cookies: bool = False) -> dict:
    with _connection() as connection:
        settings = _workspace_settings(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM accounts WHERE workspace_id = ? ORDER BY id",
            (workspace_id,),
        ).fetchall()
        accounts = []
        for row in rows:
            targets = [
                target["target_id"]
                for target in connection.execute(
                    "SELECT target_id FROM targets WHERE account_id = ? ORDER BY id",
                    (row["id"],),
                )
            ]
            account = {
                "unique_id": row["unique_id"],
                "username": row["username"],
                "targets": targets,
            }
            if include_cookies:
                account["cookies"] = _decrypt_cookies(row["cookies_encrypted"])
            accounts.append(account)
        return {"settings": settings, "accounts": accounts}


def get_public_workspace_config(workspace_id: int) -> dict:
    with _connection() as connection:
        settings = _workspace_settings(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM accounts WHERE workspace_id = ? ORDER BY id",
            (workspace_id,),
        ).fetchall()
        return {
            "settings": settings,
            "accounts": [_public_account(connection, row) for row in rows],
        }


def _account_row(connection: sqlite3.Connection, unique_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM accounts WHERE unique_id = ?", (unique_id,)
    ).fetchone()


def _insert_or_update_account(
    connection: sqlite3.Connection,
    workspace_id: int,
    unique_id: str,
    username: str,
    cookies: object,
    targets: object,
) -> None:
    encoded = _encrypt_cookies(cookies)
    now = _utc_now()
    row = _account_row(connection, unique_id)
    if row and int(row["workspace_id"]) != workspace_id:
        raise AccountOwnershipError("该抖音账号已绑定其他用户，请联系管理员")
    if row:
        account_id = int(row["id"])
        connection.execute(
            """
            UPDATE accounts
            SET username = ?, cookies_encrypted = ?, cookie_count = ?, cookie_updated_at = ?
            WHERE id = ?
            """,
            (
                str(username).strip()[:120],
                encoded,
                len(_normalise_cookies(cookies, "账号")),
                now,
                account_id,
            ),
        )
    else:
        cursor = connection.execute(
            """
            INSERT INTO accounts(workspace_id, unique_id, username, cookies_encrypted, cookie_count, created_at, cookie_updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                unique_id,
                str(username).strip()[:120],
                encoded,
                len(_normalise_cookies(cookies, "账号")),
                now,
                now,
            ),
        )
        account_id = int(cursor.lastrowid)
    connection.execute("DELETE FROM targets WHERE account_id = ?", (account_id,))
    for target in targets if isinstance(targets, list) else []:
        target_value = str(target or "").strip()[:120]
        if target_value:
            connection.execute(
                "INSERT OR IGNORE INTO targets(account_id, target_id) VALUES(?, ?)",
                (account_id, target_value),
            )


def save_workspace_config(
    workspace_id: int,
    payload: dict,
    role: str = "workspace_owner",
) -> dict:
    current = get_workspace_config(workspace_id, include_cookies=True)
    normalised = normalise_config(payload, current=current)
    max_accounts, max_targets, enabled = _workspace_limits_for_write(workspace_id)
    if not enabled:
        raise TenantStoreError("该客户账号已被管理员禁用")
    if len(normalised["accounts"]) > max_accounts:
        raise TenantStoreError(f"当前客户最多支持 {max_accounts} 个抖音账号")
    for account in normalised["accounts"]:
        if len(account.get("targets", [])) > max_targets:
            raise TenantStoreError(f"每个账号最多支持 {max_targets} 个目标好友")

    if role != "platform_admin":
        raw_settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_settings, dict):
            raw_settings = {}
        for key in SYSTEM_SETTING_KEYS:
            normalised["settings"][key] = current["settings"][key]
        for key in CUSTOMER_SETTING_KEYS:
            if key not in raw_settings:
                normalised["settings"][key] = current["settings"][key]

    desired_ids = {account["unique_id"] for account in normalised["accounts"]}
    with _connection(write=True) as connection:
        for unique_id in desired_ids:
            row = _account_row(connection, unique_id)
            if row and int(row["workspace_id"]) != workspace_id:
                raise AccountOwnershipError("该抖音账号已绑定其他用户，请联系管理员")
        existing_rows = connection.execute(
            "SELECT id, unique_id FROM accounts WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        for row in existing_rows:
            if row["unique_id"] not in desired_ids:
                connection.execute("DELETE FROM accounts WHERE id = ?", (row["id"],))
        connection.execute(
            "INSERT INTO workspace_settings(workspace_id, settings_json) VALUES(?, ?) "
            "ON CONFLICT(workspace_id) DO UPDATE SET settings_json = excluded.settings_json",
            (workspace_id, _json(normalised["settings"])),
        )
        if role == "platform_admin":
            for key in SYSTEM_SETTING_KEYS:
                connection.execute(
                    "INSERT INTO system_settings(key, value_json) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                    (key, _json(normalised["settings"][key])),
                )
        for account in normalised["accounts"]:
            _insert_or_update_account(
                connection,
                workspace_id,
                account["unique_id"],
                account["username"],
                account["cookies"],
                account.get("targets", []),
            )
    return get_public_workspace_config(workspace_id)


def _workspace_limits_for_write(workspace_id: int) -> tuple[int, int, bool]:
    with _connection() as connection:
        return _workspace_limits(connection, workspace_id)


def upsert_scanned_account(
    workspace_id: int,
    unique_id: object,
    username: object,
    cookies: object,
) -> tuple[dict, bool]:
    unique_id_value = str(unique_id or "").strip()
    if not _ID_PATTERN.fullmatch(unique_id_value):
        raise TenantStoreError("未能识别有效的抖音号")
    username_value = str(username or "").strip()[:120]
    if not username_value:
        raise TenantStoreError("未能识别账号昵称")
    normalised_cookies = _normalise_cookies(cookies, username_value)
    with _connection(write=True) as connection:
        max_accounts, _, enabled = _workspace_limits(connection, workspace_id)
        if not enabled:
            raise TenantStoreError("该客户账号已被管理员禁用")
        row = _account_row(connection, unique_id_value)
        if row and int(row["workspace_id"]) != workspace_id:
            raise AccountOwnershipError("该抖音账号已绑定其他用户，请联系管理员")
        created = row is None
        if created:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM accounts WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()["count"]
            if int(count) >= max_accounts:
                raise TenantStoreError(f"当前客户最多支持 {max_accounts} 个抖音账号")
        _insert_or_update_account(
            connection,
            workspace_id,
            unique_id_value,
            username_value,
            normalised_cookies,
            [] if created else [
                item["target_id"]
                for item in connection.execute(
                    "SELECT target_id FROM targets WHERE account_id = ?",
                    (row["id"],),
                )
            ],
        )
        account_row = _account_row(connection, unique_id_value)
        account = _public_account(connection, account_row)
    return account, created


def delete_workspace_account(workspace_id: int, unique_id: object) -> None:
    with _connection(write=True) as connection:
        cursor = connection.execute(
            "DELETE FROM accounts WHERE workspace_id = ? AND unique_id = ?",
            (workspace_id, str(unique_id or "").strip()),
        )
        if cursor.rowcount == 0:
            raise TenantStoreError("账号不存在或不属于当前用户")


def create_customer(
    username: object,
    password: object,
    display_name: object = "",
    max_accounts: int = 3,
    max_targets: int = 50,
    actor_user_id: int | None = None,
) -> dict:
    username_value = _validate_username(username)
    password_value = _validate_password(password)
    try:
        max_accounts_value = max(1, min(int(max_accounts), 20))
        max_targets_value = max(1, min(int(max_targets), 100))
    except (TypeError, ValueError) as exc:
        raise TenantStoreError("客户配额不合法") from exc
    with _connection(write=True) as connection:
        try:
            user_id = _create_user_row(
                connection,
                username_value,
                password_value,
                "workspace_owner",
                str(display_name or username_value).strip()[:120],
            )
        except sqlite3.IntegrityError as exc:
            raise TenantStoreError("用户名已存在") from exc
        workspace_id = _create_workspace(
            connection,
            f"{str(display_name or username_value).strip()[:100]} 的工作区",
            max_accounts_value,
            max_targets_value,
        )
        connection.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role) VALUES(?, ?, 'owner')",
            (workspace_id, user_id),
        )
        record_audit(
            actor_user_id if actor_user_id is not None else user_id,
            "customer_created",
            "user",
            str(user_id),
            connection=connection,
        )
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _admin_user_dict(connection, row)


def _admin_user_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    workspace_id = None
    if row["role"] != "platform_admin":
        workspace_id = _workspace_for_user(connection, int(row["id"]))
    account_count = 0
    max_accounts = None
    max_targets = None
    if workspace_id is not None:
        account_count = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM accounts WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()["count"]
        )
        limits = connection.execute(
            "SELECT max_accounts, max_targets_per_account FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
        if limits:
            max_accounts = int(limits["max_accounts"])
            max_targets = int(limits["max_targets_per_account"])
    last_run = connection.execute(
        """
        SELECT status, exit_code, finished_at
        FROM runs
        WHERE workspace_id = ?
        ORDER BY started_at DESC LIMIT 1
        """,
        (workspace_id or -1,),
    ).fetchone()
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "displayName": row["display_name"] or row["username"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "workspaceId": workspace_id,
        "accountCount": account_count,
        "maxAccounts": max_accounts,
        "maxTargets": max_targets,
        "lastLoginAt": row["last_login_at"],
        "lastRun": _run_public(last_run) if last_run else None,
    }


def list_users() -> list[dict]:
    with _connection() as connection:
        return [
            _admin_user_dict(connection, row)
            for row in connection.execute("SELECT * FROM users ORDER BY role, id")
        ]


def update_customer(
    user_id: int,
    enabled: object | None = None,
    display_name: object | None = None,
    max_accounts: object | None = None,
    max_targets: object | None = None,
    actor_user_id: int | None = None,
) -> dict:
    with _connection(write=True) as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or row["role"] == "platform_admin":
            raise TenantStoreError("只能管理普通客户账号")
        workspace_id = _workspace_for_user(connection, user_id)
        assignments = []
        values: list[object] = []
        if enabled is not None:
            assignments.append("enabled = ?")
            values.append(1 if bool(enabled) else 0)
        if display_name is not None:
            assignments.append("display_name = ?")
            values.append(str(display_name).strip()[:120])
        if assignments:
            values.append(user_id)
            connection.execute(
                f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", values
            )
        if enabled is not None:
            connection.execute(
                "UPDATE workspaces SET enabled = ? WHERE id = ?",
                (1 if bool(enabled) else 0, workspace_id),
            )
        workspace_values = []
        workspace_assignments = []
        if max_accounts is not None:
            workspace_assignments.append("max_accounts = ?")
            workspace_values.append(max(1, min(int(max_accounts), 20)))
        if max_targets is not None:
            workspace_assignments.append("max_targets_per_account = ?")
            workspace_values.append(max(1, min(int(max_targets), 100)))
        if workspace_assignments:
            workspace_values.append(workspace_id)
            connection.execute(
                f"UPDATE workspaces SET {', '.join(workspace_assignments)} WHERE id = ?",
                workspace_values,
            )
        record_audit(
            actor_user_id if actor_user_id is not None else user_id,
            "customer_updated",
            "user",
            str(user_id),
            connection=connection,
        )
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _admin_user_dict(connection, row)


def reset_customer_password(user_id: int, actor_user_id: int | None = None) -> str:
    temporary_password = secrets.token_urlsafe(12)
    with _connection(write=True) as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or row["role"] == "platform_admin":
            raise TenantStoreError("只能重置普通客户密码")
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_password_hash(temporary_password), user_id),
        )
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        record_audit(
            actor_user_id if actor_user_id is not None else user_id,
            "customer_password_reset",
            "user",
            str(user_id),
            connection=connection,
        )
    return temporary_password


def delete_customer(user_id: int, actor_user_id: int | None = None) -> None:
    with _connection(write=True) as connection:
        row = connection.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or row["role"] == "platform_admin":
            raise TenantStoreError("不能删除管理员账号")
        workspace_id = _workspace_for_user(connection, user_id)
        connection.execute(
            "DELETE FROM workspaces WHERE id = ?",
            (workspace_id,),
        )
        record_audit(
            actor_user_id if actor_user_id is not None else user_id,
            "customer_deleted",
            "user",
            str(user_id),
            connection=connection,
        )
        connection.execute("DELETE FROM users WHERE id = ?", (user_id,))


def get_workspace_state(workspace_id: int) -> dict:
    return {
        "config": get_public_workspace_config(workspace_id),
        "limits": get_workspace_limits(workspace_id),
        "status": get_workspace_runtime_status(workspace_id),
        "history": read_workspace_history(workspace_id),
        "scheduler": public_scheduler_status(),
    }


def get_workspace_limits(workspace_id: int) -> dict:
    """Return the quota visible to a workspace console."""
    with _connection() as connection:
        max_accounts, max_targets, enabled = _workspace_limits(connection, workspace_id)
        return {
            "maxAccounts": max_accounts,
            "maxTargets": max_targets,
            "enabled": enabled,
        }


def public_scheduler_status() -> dict:
    """Expose scheduler health without leaking cross-workspace run keys."""
    status = read_scheduler_status()
    return {
        "heartbeatAt": status.get("heartbeatAt"),
        "enabled": bool(status.get("enabled")),
        "state": status.get("state", "idle"),
    }


def _run_public(row: sqlite3.Row) -> dict:
    return {
        "runId": row["id"],
        "trigger": row["trigger"],
        "status": row["status"],
        "running": row["status"] == "running",
        "accountCount": int(row["account_count"]),
        "targetCount": int(row["target_count"]),
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "exitCode": row["exit_code"],
        "pid": row["pid"],
        "summary": row["summary"] or "",
    }


def get_workspace_runtime_status(workspace_id: int) -> dict:
    with _connection() as connection:
        row = connection.execute(
            """
            SELECT * FROM runs
            WHERE workspace_id = ?
            ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, started_at DESC
            LIMIT 1
            """,
            (workspace_id,),
        ).fetchone()
        if not row:
            return {"running": False, "exitCode": None}
        return _run_public(row)


def read_workspace_history(workspace_id: int, max_items: int = 20) -> list[dict]:
    limit = max(1, min(int(max_items), 100))
    with _connection() as connection:
        rows = connection.execute(
            "SELECT * FROM runs WHERE workspace_id = ? ORDER BY started_at DESC LIMIT ?",
            (workspace_id, limit),
        ).fetchall()
        return [_run_public(row) for row in reversed(rows)]


def create_run(
    workspace_id: int,
    trigger: str,
    account_count: int,
    target_count: int,
    pid: int | None = None,
) -> tuple[str, Path]:
    run_id = uuid.uuid4().hex
    log_path = RUNS_DIR / f"{run_id}.log"
    try:
        log_path.touch(mode=0o600, exist_ok=False)
    except FileExistsError as exc:
        raise TenantStoreError("无法创建任务日志") from exc
    with _connection(write=True) as connection:
        connection.execute(
            """
            INSERT INTO runs(id, workspace_id, trigger, status, account_count, target_count, started_at, pid, log_path)
            VALUES(?, ?, ?, 'running', ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                workspace_id,
                trigger,
                account_count,
                target_count,
                _utc_now(),
                pid,
                str(log_path),
            ),
        )
    return run_id, log_path


def update_run_pid(run_id: str, pid: int) -> None:
    with _connection(write=True) as connection:
        connection.execute("UPDATE runs SET pid = ? WHERE id = ?", (pid, run_id))


def finish_run(run_id: str, exit_code: int, summary: str = "") -> None:
    with _connection(write=True) as connection:
        connection.execute(
            """
            UPDATE runs
            SET status = ?, finished_at = ?, exit_code = ?, pid = NULL, summary = ?
            WHERE id = ?
            """,
            (
                "success" if exit_code == 0 else "failed",
                _utc_now(),
                int(exit_code),
                str(summary or "")[:500],
                run_id,
            ),
        )


def read_workspace_log(workspace_id: int, max_bytes: int = MAX_RUN_LOG_BYTES) -> str:
    with _connection() as connection:
        row = connection.execute(
            """
            SELECT log_path FROM runs
            WHERE workspace_id = ?
            ORDER BY started_at DESC LIMIT 1
            """,
            (workspace_id,),
        ).fetchone()
    if not row or not row["log_path"]:
        return ""
    path = Path(row["log_path"])
    try:
        if path.parent.resolve() != RUNS_DIR.resolve():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


def list_enabled_workspaces() -> list[tuple[int, dict]]:
    with _connection() as connection:
        rows = connection.execute(
            "SELECT id FROM workspaces WHERE enabled = 1 ORDER BY id"
        ).fetchall()
        return [
            (int(row["id"]), _workspace_settings(connection, int(row["id"]))["schedule"])
            for row in rows
        ]


def read_scheduler_status() -> dict:
    ensure_data_dir()
    if not SCHEDULER_STATUS_PATH.exists():
        return {"heartbeatAt": None, "enabled": False}
    try:
        value = json.loads(SCHEDULER_STATUS_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"heartbeatAt": None, "enabled": False}
    except (OSError, json.JSONDecodeError):
        return {"heartbeatAt": None, "enabled": False}


def write_scheduler_status(status: dict) -> None:
    ensure_data_dir()
    temporary_path = SCHEDULER_STATUS_PATH.with_suffix(".tmp")
    temporary_path.write_text(_json(status), encoding="utf-8")
    try:
        os.chmod(temporary_path, 0o600)
    except PermissionError:
        pass
    os.replace(temporary_path, SCHEDULER_STATUS_PATH)


def admin_overview() -> dict:
    with _connection() as connection:
        users = connection.execute(
            "SELECT COUNT(*) AS count FROM users WHERE role != 'platform_admin'"
        ).fetchone()["count"]
        enabled_users = connection.execute(
            "SELECT COUNT(*) AS count FROM users WHERE role != 'platform_admin' AND enabled = 1"
        ).fetchone()["count"]
        accounts = connection.execute("SELECT COUNT(*) AS count FROM accounts").fetchone()["count"]
        running = connection.execute(
            "SELECT COUNT(*) AS count FROM runs WHERE status = 'running'"
        ).fetchone()["count"]
        last_run = connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return {
            "customerCount": int(users),
            "enabledCustomerCount": int(enabled_users),
            "accountCount": int(accounts),
            "runningCount": int(running),
            "lastRun": _run_public(last_run) if last_run else None,
        }


def admin_accounts() -> list[dict]:
    with _connection() as connection:
        rows = connection.execute(
            "SELECT * FROM accounts ORDER BY workspace_id, id"
        ).fetchall()
        return [_public_account(connection, row, include_owner=True) for row in rows]


def record_audit(
    user_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: str = "",
    connection: sqlite3.Connection | None = None,
) -> None:
    if connection is not None:
        connection.execute(
            """
            INSERT INTO audit_events(user_id, action, target_type, target_id, detail, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (user_id, action[:120], target_type, target_id, detail[:500], _utc_now()),
        )
        return
    with _connection(write=True) as owned_connection:
        record_audit(
            user_id,
            action,
            target_type,
            target_id,
            detail,
            connection=owned_connection,
        )


def list_audit_events(max_items: int = 100) -> list[dict]:
    limit = max(1, min(int(max_items), 500))
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT a.*, u.username
            FROM audit_events a
            LEFT JOIN users u ON u.id = a.user_id
            ORDER BY a.created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "action": row["action"],
                "targetType": row["target_type"],
                "targetId": row["target_id"],
                "detail": row["detail"],
                "username": row["username"] or "system",
                "createdAt": row["created_at"],
            }
            for row in rows
        ]
