from __future__ import annotations
import asyncio
import calendar
import base64
import glob
import hashlib
import html
import aiofiles          # pip install aiofiles
import io
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import sys
import time
import uuid
import zipfile
import gzip
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List, TYPE_CHECKING
from functools import lru_cache
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from threading import Lock as ThreadingLock
from urllib.parse import urlparse
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

import aiosqlite
import importlib

sqlite_utils = None
try:
    sqlite_utils = importlib.import_module("sqlite_utils")
    SQLITE_UTILS_AVAILABLE = True
except ImportError:
    SQLITE_UTILS_AVAILABLE = False

pylibmc = None
try:
    pylibmc = importlib.import_module("pylibmc")
    MEMCACHED_AVAILABLE = True
    MC_CLIENT = pylibmc.Client(['127.0.0.1'], binary=True, behaviors={"tcp_nodelay": True, "ketama": True})
except Exception:
    MEMCACHED_AVAILABLE = False
    MC_CLIENT = None

import qrcode
from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)
from functools import wraps
from .aiogram_compat import (
    Application,
    ContextTypes,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    NetworkError,
    ParseMode,
    RetryAfter,
    TimedOut,
    Update,
    WebAppInfo,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from telethon.tl import functions, types
from telethon import utils
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
# Optional dotenv
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# =============================================================================
# 0. РРњРџРћР РўР« Рё РљРћРќР¤РР“РЈР РђР¦РРЇ
# =============================================================================

class ConfigError(Exception):
    pass


def _configure_stdio_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except Exception:
                pass


def _resolve_log_level(env_name: str, default: int) -> int:
    raw_value = str(os.getenv(env_name, "") or "").strip()
    if not raw_value:
        return default

    if raw_value.isdigit():
        return int(raw_value)

    normalized = raw_value.upper()
    return getattr(logging, normalized, default)


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: Tuple[int, ...]
    base_dir: str
    sessions_dir: str
    media_dir: str
    logs_dir: str
    db_path: str

    qr_timeout: int = 300
    download_media: bool = True
    max_concurrent: int = 4
    restart_delay: int = 4
    resend_cooldown: int = 1 * 60
    sendcode_unavailable_block: int = 90
    send_code_retries: int = 3
    send_code_retry_delay: float = 2.0

    executor_workers: int = 6
    max_deleted_ids: int = 150
    max_group_details: int = 20

    tz_name: str = "Europe/Moscow"
    allowed_chat_ids: Optional[Tuple[int, ...]] = None
    alert_chat_id: Optional[int] = None
    bot_username: str = ""

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            logging.getLogger("bot_main").warning(
                "Timezone %s is unavailable; falling back to UTC. Install tzdata to restore named zones.",
                self.tz_name,
            )
            return timezone.utc

    @property
    def allowed_chat_ids_set(self) -> Optional[set[int]]:
        if self.allowed_chat_ids is None:
            return None
        return set(self.allowed_chat_ids)

    @staticmethod
    def from_env() -> "Config":
        API_ID_RAW = os.getenv("TG_API_ID", "")
        API_HASH = os.getenv("TG_API_HASH", "")
        BOT_TOKEN = os.getenv("SOO_BOT_TOKEN", os.getenv("BOT_TOKEN", ""))
        if not (API_ID_RAW and API_HASH and BOT_TOKEN):
            raise ConfigError(
                "Set TG_API_ID, TG_API_HASH and SOO_BOT_TOKEN (or BOT_TOKEN) in environment. "
                "Get credentials at https://my.telegram.org/apps"
            )

        try:
            API_ID = int(API_ID_RAW)
        except Exception:
            raise ConfigError("TG_API_ID must be integer")

        ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
        admin_ids = tuple(int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip())

        BASE_DIR = os.getenv("BASE_DIR", os.getcwd())
        SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
        MEDIA_DIR = os.path.join(BASE_DIR, "media")
        LOGS_DIR = os.path.join(BASE_DIR, "logs")
        DB_PATH = os.path.join(SESSIONS_DIR, "bot_database.sqlite")

        allowed_raw = os.getenv("ALLOWED_CHAT_IDS", "") or os.getenv("ALLOWED_CHAT_IDS_LIST", "")
        allowed = None
        if allowed_raw:
            parts = [p.strip() for p in re.split(r"[,\s]+", allowed_raw) if p.strip()]
            try:
                allowed = tuple(int(x) for x in parts)
            except Exception:
                allowed = None

        bot_username = str(os.getenv("BOT_USERNAME", "") or "").strip().lstrip("@")

        alert_chat_id_raw = str(os.getenv("ALERT_CHAT_ID", "") or "").strip()
        alert_chat_id: Optional[int] = None
        if alert_chat_id_raw:
            try:
                alert_chat_id = int(alert_chat_id_raw)
            except Exception:
                alert_chat_id = None

        return Config(
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            admin_ids=admin_ids,
            base_dir=BASE_DIR,
            sessions_dir=SESSIONS_DIR,
            media_dir=MEDIA_DIR,
            logs_dir=LOGS_DIR,
            db_path=DB_PATH,
            tz_name=os.getenv("TIMEZONE", "Europe/Moscow"),
            allowed_chat_ids=allowed,
            alert_chat_id=alert_chat_id,
            bot_username=bot_username,
        )

_configure_stdio_utf8()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("telethon").setLevel(_resolve_log_level("TELETHON_LOG_LEVEL", logging.WARNING))
logging.getLogger("telethon.network").setLevel(
    _resolve_log_level("TELETHON_NETWORK_LOG_LEVEL", logging.WARNING)
)
logging.getLogger("httpx").setLevel(_resolve_log_level("HTTPX_LOG_LEVEL", logging.WARNING))

logger = logging.getLogger("bot_main")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "arcee-ai/trinity-large-preview:free")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
if not OPENROUTER_API_KEY:
    logger.warning("OPENROUTER_API_KEY is not set; AI assistant endpoints will return errors until configured.")

try:
    CONFIG = Config.from_env()
except Exception as exc:
    logger.critical("Config error: %s", exc)
    raise SystemExit(1)

for p in (CONFIG.sessions_dir, CONFIG.media_dir, CONFIG.logs_dir):
    Path(p).mkdir(parents=True, exist_ok=True)

welcome_message = (
    "<b>Привет! Я SavedBot.</b>\n\n"
    "Кратко, что я умею:\n"
    "• сохраняет удалённые и изменённые сообщения\n"
    "• хранит одноразовые медиа и историю правок\n"
    "• показывает архив в Mini App\n"
    "• помогает быстро найти нужный чат или файл\n\n"
    "Выберите способ подключения ниже."
)

BOT_COMMANDS_BRIEF = (
    "\n\n<b>Команды:</b>\n"
    "• /start — главное меню\n"
    "• /stats — статистика\n"
    "• /set — настройки\n"
    "• /logout — выйти"
)
# ---- end shim ----
# ----------------------------
# Constants & small helpers
# ----------------------------
AUTH_LOGS_SUBDIR = "auth_attempts"
USER_LOG_NAMES: Dict[int, str] = {} 
LAST_AUTH_BROADCAST_TS: Optional[float] = None
CHAT_LISTEN_SETTING_KEYS: Tuple[str, ...] = (
    "allow_private",
    "allow_groups",
    "allow_supergroups",
    "allow_channels",
    "allow_bots",
)
CHAT_LISTEN_DEFAULTS: Dict[str, int] = {
    "allow_private": 1,
    "allow_groups": 1,
    "allow_supergroups": 1,
    "allow_channels": 1,
    "allow_bots": 0,
}


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _normalize_listen_settings_row(row: Any) -> Dict[str, int]:
    if not row:
        return dict(CHAT_LISTEN_DEFAULTS)
    values = dict(CHAT_LISTEN_DEFAULTS)
    keys = row.keys() if hasattr(row, "keys") else CHAT_LISTEN_SETTING_KEYS
    for key in CHAT_LISTEN_SETTING_KEYS:
        try:
            if key in keys:
                raw = row[key] if hasattr(row, "keys") else row[list(CHAT_LISTEN_SETTING_KEYS).index(key)]
                values[key] = 1 if int(raw or 0) else 0
        except Exception:
            values[key] = int(CHAT_LISTEN_DEFAULTS[key])
    return values


async def get_user_chat_type_settings(db: "Database", user_id: int) -> Dict[str, int]:
    uid = int(user_id)
    row = await db.fetchone(
        """
        SELECT allow_private, allow_groups, allow_supergroups, allow_channels, allow_bots
        FROM user_chat_type_settings
        WHERE user_id=?
        LIMIT 1
        """,
        (uid,),
    )
    if row:
        return _normalize_listen_settings_row(row)
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT OR IGNORE INTO user_chat_type_settings (
            user_id, allow_private, allow_groups, allow_supergroups, allow_channels, allow_bots, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            int(CHAT_LISTEN_DEFAULTS["allow_private"]),
            int(CHAT_LISTEN_DEFAULTS["allow_groups"]),
            int(CHAT_LISTEN_DEFAULTS["allow_supergroups"]),
            int(CHAT_LISTEN_DEFAULTS["allow_channels"]),
            int(CHAT_LISTEN_DEFAULTS["allow_bots"]),
            now_iso,
            now_iso,
        ),
    )
    created = await db.fetchone(
        """
        SELECT allow_private, allow_groups, allow_supergroups, allow_channels, allow_bots
        FROM user_chat_type_settings
        WHERE user_id=?
        LIMIT 1
        """,
        (uid,),
    )
    return _normalize_listen_settings_row(created)


async def set_user_chat_type_setting(db: "Database", user_id: int, setting_key: str, enabled: bool) -> Dict[str, int]:
    key = str(setting_key or "").strip().lower()
    if key not in CHAT_LISTEN_SETTING_KEYS:
        return await get_user_chat_type_settings(db, int(user_id))
    await get_user_chat_type_settings(db, int(user_id))
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.execute(
        f"UPDATE user_chat_type_settings SET {key}=?, updated_at=? WHERE user_id=?",
        (1 if enabled else 0, now_iso, int(user_id)),
    )
    return await get_user_chat_type_settings(db, int(user_id))


async def send_critical_alert(
    bot: Any,
    db: Optional["Database"],
    *,
    error_type: str,
    error_text: str,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    context: str = "",
    extra: Optional[Dict[str, Any]] = None,
    cooldown_sec: int = 180,
    occurred_at: Optional[datetime] = None,
) -> bool:
    """
    Send critical alert to admins. 
    Gracefully degrades if database is locked to prevent cascading failures.
    """
    if bot is None:
        return False
    targets: List[int] = []
    if CONFIG.alert_chat_id is not None:
        targets.append(int(CONFIG.alert_chat_id))
    targets.extend(int(x) for x in CONFIG.admin_ids)
    uniq_targets: List[int] = []
    seen_targets: set[int] = set()
    for target in targets:
        if target not in seen_targets:
            uniq_targets.append(target)
            seen_targets.add(target)
    if not uniq_targets:
        return False

    when_dt = (occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    when_iso = when_dt.isoformat()
    error_type_safe = str(error_type or "UNSPECIFIED").strip() or "UNSPECIFIED"
    context_safe = str(context or "").strip()
    error_compact = " ".join(str(error_text or "").split())
    if len(error_compact) > 700:
        error_compact = error_compact[:700] + "..."
    signature = f"{error_type_safe}|{int(user_id or 0)}|{context_safe}|{error_compact[:240]}"
    fingerprint = hashlib.sha256(signature.encode("utf-8", errors="ignore")).hexdigest()
    payload_data: Dict[str, Any] = {
        "error_type": error_type_safe,
        "user_id": int(user_id) if user_id is not None else None,
        "username": username or "",
        "context": context_safe,
        "error_text": error_compact,
        "extra": extra or {},
    }
    payload_json = json.dumps(payload_data, ensure_ascii=False, default=str)

    should_send = True
    if db is not None:
        try:
            existing = await db.fetchone(
                "SELECT last_sent_at FROM critical_alert_events WHERE fingerprint=? LIMIT 1",
                (fingerprint,),
            )
            last_sent = None
            if existing:
                last_sent_raw = existing["last_sent_at"] if hasattr(existing, "keys") else existing[0]
                last_sent = _parse_iso_datetime(last_sent_raw)
            if last_sent and (when_dt - last_sent).total_seconds() < max(5, int(cooldown_sec)):
                should_send = False
                await db.execute(
                    """
                    UPDATE critical_alert_events
                    SET last_seen_at=?, hit_count=COALESCE(hit_count, 0) + 1, payload_json=?
                    WHERE fingerprint=?
                    """,
                    (when_iso, payload_json, fingerprint),
                )
            elif existing:
                await db.execute(
                    """
                    UPDATE critical_alert_events
                    SET last_seen_at=?, last_sent_at=?, hit_count=COALESCE(hit_count, 0) + 1, payload_json=?
                    WHERE fingerprint=?
                    """,
                    (when_iso, when_iso, payload_json, fingerprint),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO critical_alert_events (
                        fingerprint, user_id, error_type, error_text, context,
                        first_seen_at, last_seen_at, last_sent_at, hit_count, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        fingerprint,
                        int(user_id) if user_id is not None else None,
                        error_type_safe,
                        error_compact,
                        context_safe,
                        when_iso,
                        when_iso,
                        when_iso,
                        payload_json,
                    ),
                )
        except sqlite3.OperationalError as e:
            # GRACEFUL DEGRADATION: If database is locked, skip logging to prevent cascade
            if 'locked' in str(e).lower():
                logger.warning(
                    "Skipping critical_alert DB logging (database locked): "
                    "error_type=%s, user_id=%s, context=%s",
                    error_type_safe, user_id, context_safe
                )
            else:
                logger.exception("Failed to save critical alert event fingerprint=%s", fingerprint)
        except Exception:
            logger.exception("Failed to save critical alert event fingerprint=%s", fingerprint)

    if not should_send:
        return False

    local_ts = when_dt.astimezone(CONFIG.tz).strftime("%d.%m.%Y %H:%M:%S")
    user_line = (
        f"<code>{int(user_id)}</code>"
        if user_id is not None
        else "не определён"
    )
    if username:
        user_line += f" (@{html.escape(str(username), quote=False)})"
    extra_text = ""
    if extra:
        rendered = json.dumps(extra, ensure_ascii=False, default=str)
        if len(rendered) > 1200:
            rendered = rendered[:1200] + "..."
        extra_text = f"\nДополнительно: <code>{html.escape(rendered, quote=False)}</code>"
    body = (
        "🚨 <b>Критичная ошибка</b>\n\n"
        f"Тип: <b>{html.escape(error_type_safe, quote=False)}</b>\n"
        f"Пользователь: {user_line}\n"
        f"Когда: {local_ts} ({html.escape(CONFIG.tz_name, quote=False)})\n"
        f"Контекст: <code>{html.escape(context_safe or '-', quote=False)}</code>\n"
        f"Ошибка: <code>{html.escape(error_compact or 'empty', quote=False)}</code>"
        f"{extra_text}"
    )
    body = repair_mojibake(body)
    sent_any = False
    for target in uniq_targets:
        try:
            await bot.send_message(
                chat_id=target,
                text=body,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent_any = True
        except Exception:
            logger.exception("Failed to deliver critical alert to %s", target)
    return sent_any

async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user:
        return False

    user = update.effective_user
    uid = user.id
    username = user.username or "без имени"

    # ════════════════════════════════════════════════════════════
    # РАЗРЕШЕННЫЕ КОМАНДЫ для всех (даже неодобренных)
    # ════════════════════════════════════════════════════════════
    allowed_commands = {
        "/start",      # Начало
        "/set",        # Центр настроек
        "/profile",    # Профиль
        "/logout",     # Выход
        "/help",       # Помощь
        "/status",     # Статус
        "/stats",      # Статистика
        "/unmute",     # Снять заглушки
        "/cleansessions",  # Удаление сессий
        "/sessions_health",  # Диагностика и cleaner сессий (админ)
        "/cleardb",    # Очистка базы данных (админ)
    }
    
    # Проверяем если это команда (начинается с /)
    if update.message and update.message.text:
        text = update.message.text.split()[0]  # Берем первое слово (саму команду)
        if text in allowed_commands:
            return False
    
    # Callback'и тоже пропускаем (они обрабатываются отдельно)
    if update.callback_query:
        return False

    if uid in CONFIG.admin_ids:
        return False

    app = context.application.bot_data.get("app")
    if not app:
        if update.message:
            await update.message.reply_text("Ошибка инициализации бота.")
        return True
    
    db = app.db
    now = datetime.now(timezone.utc).isoformat()
    try:
        await db.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, last_name, first_seen_at, requested_at, approved, approved_at, approved_by)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0)
            """,
            (uid, user.username, user.first_name, user.last_name, now, now, now),
        )
        await db.execute(
            """
            UPDATE users
            SET
                username=?,
                first_name=?,
                last_name=?,
                requested_at=COALESCE(requested_at, ?),
                approved=1,
                approved_at=COALESCE(approved_at, ?),
                approved_by=COALESCE(approved_by, 0)
            WHERE user_id=?
            """,
            (user.username, user.first_name, user.last_name, now, now, uid),
        )
    except Exception:
        logger.exception("Failed to upsert access row for uid=%s", uid)

    return False

async def notify_admins_new_request(bot, user_id: int, user):
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
    
    # Правильно получаем полное имя пользователя
    full_name = ""
    if hasattr(user, 'first_name') and user.first_name:
        full_name = user.first_name
    if hasattr(user, 'last_name') and user.last_name:
        full_name = (full_name + " " + user.last_name).strip()
    
    text = (
        f"👤 <b>Новый запрос на доступ</b>\n\n"
        f"<b>ID:</b> <code>{user_id}</code>\n"
        f"<b>Username:</b> @{user.username or '—'}\n"
        f"<b>Имя:</b> {html.escape(full_name or '—')}\n"
        f"<b>Время:</b> {now}\n\n"
        f"<i>Требуется одобрение для доступа к функционалу бота</i>"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_user:{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_user:{user_id}")
    ]])

    sent = 0
    for admin_id in CONFIG.admin_ids:
        try:
            await bot.send_message(
                admin_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
            sent += 1
        except Exception as e:
            logger.error("Failed to notify admin %s: %s", admin_id, e)

    logger.debug("Access requests sent to %d admin(s)", sent)

# ----------------------------
# Auth logging utils
# ----------------------------
def get_auth_log_dir(user_id: int, username: Optional[str] = None) -> Path:
    base = Path(CONFIG.logs_dir) / AUTH_LOGS_SUBDIR
    base.mkdir(parents=True, exist_ok=True)

    if username:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", username).strip("_")
        if safe:
            return base / safe

    return base / f"user_{user_id}"


def log_auth_attempt(
    user_id: int,
    username: Optional[str],
    text: str,
    state: str,
    meta: Optional[str] = None,
    result: Optional[str] = None,
) -> None:
    """
    Write an auth attempt to per-user file and to a compact global file.
    Non-fatal: failures only produce internal logs.
    """
    ts = datetime.now(CONFIG.tz).strftime("%Y-%m-%d %H:%M:%S")
    try:
        log_dir = get_auth_log_dir(user_id, username)
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(CONFIG.tz).strftime("%Y-%m-%d")
        filepath = log_dir / f"{today}.log"

        parts = [ts, state, text]
        if meta:
            parts.append(meta)
        if result:
            parts.append(f"result={result}")
        line = " | ".join(parts)

        # Synchronous append is fine for small auth logs; keep it robust.
        with filepath.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        logger.error("Ошибка записи auth-лога для %s (%s): %s", user_id, username, exc)

    # Global compact log (compressed): auth_attempts.log.gz
    try:
        log_line = (
            f"{ts} [AUTH] user_id={user_id} "
            f"username={username} "
            f"state={state} "
            f"text={text} "
            f"{meta or ''} "
            f"result={result or ''}\n"
        )
        gzpath = Path(CONFIG.logs_dir) / "auth_attempts.log.gz"
        with gzip.open(gzpath, "at", encoding="utf-8") as f:
            f.write(log_line)
    except Exception as exc:
        logger.error("Ошибка записи глобального auth-лога gzip для %s (%s): %s", user_id, username, exc)


# ----------------------------
# Per-user logs (outgoing bot messages)
# ----------------------------
def get_user_log_dir(user_id: int, username: Optional[str] = None) -> Path:
    """
    Resolve per-user log directory. Prefer username, fallback to user_<id>.
    """
    global USER_LOG_NAMES

    name: Optional[str] = None
    if username:
        safe = re.sub(r"[^A-Za-z0-9_]", "_", username).strip("_")
        if safe:
            name = safe

    if not name:
        if user_id in USER_LOG_NAMES:
            name = USER_LOG_NAMES[user_id]
        else:
            name = f"user_{user_id}"

    USER_LOG_NAMES[user_id] = name
    path = Path(CONFIG.logs_dir) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_outgoing_message(user_id: int, text: str, username: Optional[str] = None) -> None:
    """
    Append outgoing bot message to per-user daily log. Non-blocking for main logic.
    """
    try:
        log_dir = get_user_log_dir(user_id, username)
        fname = datetime.now(CONFIG.tz).strftime("%Y-%m-%d") + ".log"
        fpath = log_dir / fname
        ts = datetime.now(CONFIG.tz).isoformat()
        clean_text = repair_mojibake(text)
        with fpath.open("a", encoding="utf-8") as f:
            f.write(f"{ts} | {clean_text}\n")

        # Also append gzip per day for compressed archives:
        gzpath = log_dir / (datetime.now(CONFIG.tz).strftime("%Y-%m-%d") + ".log.gz")
        with gzip.open(gzpath, "at", encoding="utf-8") as f:
            f.write(f"{ts} | {clean_text}\n")
    except Exception as exc:
        logger.error("Failed to log message for %s: %s", user_id, exc)


async def send_and_log(
    bot,
    chat_id: int,
    text: str,
    *,
    username: Optional[str] = None,
    reply_markup=None,
    parse_mode: Optional[str] = None,
):
    """
    Helper: log text to per-user log (sync) and send message via bot (async).
    """
    # Logging should not raise
    clean_text = repair_mojibake(text)
    try:
        log_outgoing_message(chat_id, clean_text, username)
    except Exception:
        logger.exception("Failed to write outgoing log")

    return await bot.send_message(
        chat_id=chat_id,
        text=clean_text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )


# ----------------------------
# Core event handler
# ----------------------------
# NOTE: EventHandler class (below) is the primary event processor.
# The old generic handle_event() function has been removed (méz deadcode).

import aiosqlite
from typing import Optional, Tuple, Dict
from datetime import datetime, timezone


class AsyncSQLitePool:
    def __init__(self, path: str, max_size: int = 8):
        self.path = path
        self.max_size = max_size
        self._pool: Optional[asyncio.Queue] = None

    async def init(self):
        if self._pool is not None:
            return
        self._pool = asyncio.Queue(maxsize=self.max_size)
        for _ in range(self.max_size):
            conn = await aiosqlite.connect(self.path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA busy_timeout=15000;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            await conn.commit()
            await self._pool.put(conn)

    async def acquire(self):
        await self.init()
        return await self._pool.get()

    async def release(self, conn):
        await self._pool.put(conn)

    async def close(self):
        if self._pool is None:
            return
        while not self._pool.empty():
            conn = self._pool.get_nowait()
            try:
                await conn.close()
            except Exception:
                pass
        self._pool = None

    async def __aenter__(self):
        return await self.acquire()

    async def __aexit__(self, exc_type, exc, tb):
        raise NotImplementedError("Use explicit release()")


class Database:

    def __init__(self, path: str, config: Config):
        self.path = path
        self.config = config
        self.pool = AsyncSQLitePool(path, max_size=getattr(config, 'max_db_pool', 8))
        self.use_sqlite_utils = SQLITE_UTILS_AVAILABLE
        self.sqlite_utils_db = None
        self._init_conn = None  # Stored connection during schema initialization
        self._write_lock = asyncio.Lock()
        # CRITICAL FIX: Dedicated single-thread executor for SQLite writes
        # This prevents database lock contention from multiple threads writing simultaneously
        self._sqlite_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite-writer-")
        # Sync lock for thread-safe access within executor thread
        self._write_lock_sync = ThreadingLock()

    @property
    def conn(self):
        # If we're in the middle of initialization, return the actual connection
        if self._init_conn is not None:
            return self._init_conn
        
        # Otherwise return compatibility shim for legacy code
        class _ConnProxy:
            def __init__(self, db):
                self.db = db

            async def execute(self, query, params=()):
                return await self.db.execute(query, params)

            async def commit(self):
                return None

        return _ConnProxy(self)

    async def connect(self) -> None:
        if self.use_sqlite_utils:
            # sqlite-utils uses a sync sqlite3 connection; allow executor threads
            # to reuse it safely and serialize access at the coroutine layer.
            sqlite_utils_conn = sqlite3.connect(self.path, check_same_thread=False)
            self.sqlite_utils_db = sqlite_utils.Database(sqlite_utils_conn)

        await self.pool.init()

        # Initialize schema on a dedicated pooled connection (prevent global interleaving)
        self._init_conn = await self.pool.acquire()
        try:
            await self._init_schema()
        finally:
            await self.pool.release(self._init_conn)
            self._init_conn = None

    async def close(self) -> None:
        if self.sqlite_utils_db is not None:
            try:
                self.sqlite_utils_db.conn.close()
            except Exception:
                logger.debug("Failed to close sqlite-utils connection", exc_info=True)
            finally:
                self.sqlite_utils_db = None

        # Shutdown dedicated sqlite executor gracefully
        if self._sqlite_executor is not None:
            try:
                self._sqlite_executor.shutdown(wait=True, timeout=5.0)
            except Exception:
                logger.debug("Failed to shutdown sqlite executor", exc_info=True)

        try:
            await self.pool.close()
        except Exception:
            logger.debug("Failed to close async sqlite pool", exc_info=True)

    async def _init_schema(self):

        async def existing_columns(table: str):
            rows = await self.fetchall(f"PRAGMA table_info({table})")
            return {r[1] for r in rows}

        async def ensure_columns(table: str, columns: Dict[str, str]):
            cols = await existing_columns(table)
            for name, definition in columns.items():
                if name not in cols:
                    try:
                        await self.conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                        )
                    except Exception:
                        pass

        # ---- BOT USERS (фронтенд) ----
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id         INTEGER PRIMARY KEY,
                username        TEXT,
                first_name      TEXT,
                last_name       TEXT,
                first_seen_at   TEXT,
                last_seen_at    TEXT,
                requested_at    TEXT,
                banned          INTEGER DEFAULT 0,   -- 1 = забанен, 0 = активен
                banned_at       TEXT,
                banned_by       INTEGER
            )
            """
        )

        await ensure_columns(
            "bot_users",
            {
                "requested_at": "TEXT",
                "banned": "INTEGER DEFAULT 0",
                "banned_at": "TEXT",
                "banned_by": "INTEGER",
            },
        )

        # Создаём индекс для быстрого поиска по username
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bot_users_username ON bot_users(username)"
        )

        # ---- USERS (улучшенная версия для доступа) ----
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                username        TEXT,
                first_name      TEXT,
                last_name       TEXT,
                first_seen_at   TEXT,
                requested_at    TEXT,           -- когда впервые запросил доступ
                approved        INTEGER DEFAULT 0,   -- 1 = одобрен, 0 = ожидает, -1 = отклонён
                approved_at     TEXT,
                approved_by     INTEGER,        -- кто одобрил (id админа)
                rejected_at     TEXT
            )
            """
        )

        await ensure_columns(
            "users",
            {
                "username": "TEXT",
                "first_name": "TEXT",
                "last_name": "TEXT",
                "first_seen_at": "TEXT",
                "requested_at": "TEXT",
                "approved": "INTEGER DEFAULT 0",
                "approved_at": "TEXT",
                "approved_by": "INTEGER",
                "rejected_at": "TEXT",
            },
        )
        # ---------------- AUTH ----------------

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_state (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                phone TEXT,
                tmp_prefix TEXT,
                phone_code_hash TEXT,
                expires_at REAL,
                awaiting_2fa INTEGER DEFAULT 0,
                resend_allowed_at REAL,
                updated_at TEXT,
                auth_fail_count INTEGER DEFAULT 0,
                banned_until REAL
            )
            """
        )
        await ensure_columns(
            "auth_state",
            {
                "state": "TEXT",
                "phone": "TEXT",
                "tmp_prefix": "TEXT",
                "phone_code_hash": "TEXT",
                "expires_at": "REAL",
                "awaiting_2fa": "INTEGER DEFAULT 0",
                "resend_allowed_at": "REAL",
                "updated_at": "TEXT",
                "auth_fail_count": "INTEGER DEFAULT 0",
                "banned_until": "REAL",
            },
        )

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS critical_alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT UNIQUE NOT NULL,
                user_id INTEGER,
                error_type TEXT NOT NULL,
                error_text TEXT,
                context TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_sent_at TEXT,
                hit_count INTEGER DEFAULT 1,
                payload_json TEXT
            )
            """
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_critical_alert_last_seen ON critical_alert_events(last_seen_at DESC)"
        )

        # ---------------- ACCESS REQUESTS ----------------

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_requests (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                requested_at TEXT,
                status TEXT
            )
            """
        )
# ==================== РЎРўРћР РРЎ ====================
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS stories (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id        INTEGER NOT NULL,
                peer_id         INTEGER,
                story_id        INTEGER UNIQUE,
                sender_id       INTEGER,
                sender_name     TEXT,
                sender_username TEXT,
                caption         TEXT,
                media_path      TEXT,
                posted_at       TEXT,
                added_at        TEXT NOT NULL,
                content_type    TEXT DEFAULT '📖 Story'
            )
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stories_owner_added 
            ON stories (owner_id, added_at DESC)
        """)
        # ===============================================

        # ---------------- PENDING ----------------

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                chat_id INTEGER,
                chat_title TEXT,
                chat_username TEXT,
                msg_id INTEGER,
                text TEXT,
                original_text TEXT,
                edit_count INTEGER DEFAULT 0,
                last_edited_at TEXT,
                media_path TEXT,
                sender_id INTEGER,
                sender_username TEXT,
                message_date TEXT,
                added_at TEXT,
                is_disappearing INTEGER DEFAULT 0,
                already_forwarded INTEGER DEFAULT 0
            )
            """
        )

        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_owner_msg ON pending (owner_id, msg_id)"
        )

        await ensure_columns(
            "pending",
            {
                "original_text": "TEXT",
                "edit_count": "INTEGER DEFAULT 0",
                "last_edited_at": "TEXT",
                "is_disappearing": "INTEGER DEFAULT 0",
                "already_forwarded": "INTEGER DEFAULT 0",
                "content_type": "TEXT",
                "views": "INTEGER DEFAULT 0",
                "reactions": "TEXT DEFAULT '{}'",
            },
        )

        # ---------------- CHAT DIALOGS ----------------

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_dialogs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                dialog_type TEXT DEFAULT 'private',
                title TEXT,
                username TEXT,
                photo_url TEXT,
                last_message_id INTEGER,
                last_message_at TEXT,
                last_message_preview TEXT,
                last_sender_id INTEGER,
                last_sender_label TEXT,
                unread_count INTEGER DEFAULT 0,
                oldest_synced_msg_id INTEGER,
                newest_synced_msg_id INTEGER,
                history_complete INTEGER DEFAULT 0,
                sync_error TEXT,
                last_sync_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )

        await ensure_columns(
            "chat_dialogs",
            {
                "dialog_type": "TEXT DEFAULT 'private'",
                "title": "TEXT",
                "username": "TEXT",
                "photo_url": "TEXT",
                "last_message_id": "INTEGER",
                "last_message_at": "TEXT",
                "last_message_preview": "TEXT",
                "last_sender_id": "INTEGER",
                "last_sender_label": "TEXT",
                "unread_count": "INTEGER DEFAULT 0",
                "oldest_synced_msg_id": "INTEGER",
                "newest_synced_msg_id": "INTEGER",
                "history_complete": "INTEGER DEFAULT 0",
                "sync_error": "TEXT",
                "last_sync_at": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )

        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_dialog_owner_chat ON chat_dialogs (owner_id, chat_id)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_dialog_owner_last_message ON chat_dialogs (owner_id, last_message_at DESC)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_dialog_owner_title ON chat_dialogs (owner_id, title)"
        )

        # ---------------- CHAT THREAD (full conversation history) ----------------

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_thread_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                chat_id INTEGER,
                chat_title TEXT,
                chat_username TEXT,
                msg_id INTEGER,
                sender_id INTEGER,
                sender_username TEXT,
                sender_display_name TEXT,
                sender_handle TEXT,
                is_outgoing INTEGER DEFAULT 0,
                reply_to_msg_id INTEGER,
                dialog_type TEXT DEFAULT 'private',
                content_type TEXT,
                text TEXT,
                original_text TEXT,
                media_path TEXT,
                status TEXT DEFAULT 'active',
                edit_count INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                deleted_at TEXT,
                views INTEGER DEFAULT 0,
                reactions TEXT DEFAULT '{}'
            )
            """
        )

        await ensure_columns(
            "chat_thread_messages",
            {
                "chat_title": "TEXT",
                "chat_username": "TEXT",
                "sender_id": "INTEGER",
                "sender_username": "TEXT",
                "sender_display_name": "TEXT",
                "sender_handle": "TEXT",
                "is_outgoing": "INTEGER DEFAULT 0",
                "reply_to_msg_id": "INTEGER",
                "dialog_type": "TEXT DEFAULT 'private'",
                "content_type": "TEXT",
                "text": "TEXT",
                "original_text": "TEXT",
                "media_path": "TEXT",
                "status": "TEXT DEFAULT 'active'",
                "edit_count": "INTEGER DEFAULT 0",
                "created_at": "TEXT",
                "updated_at": "TEXT",
                "deleted_at": "TEXT",
                "views": "INTEGER DEFAULT 0",
                "reactions": "TEXT DEFAULT '{}'",
            },
        )

        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_owner_chat_msg ON chat_thread_messages (owner_id, chat_id, msg_id)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_owner_chat_created ON chat_thread_messages (owner_id, chat_id, created_at DESC)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_owner_status_created ON chat_thread_messages (owner_id, status, created_at DESC)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_chat_id ON chat_thread_messages (chat_id)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_status ON chat_thread_messages (status)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_created_at ON chat_thread_messages (created_at DESC)"
        )

        # Optional revision log for "show history" UX.
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_thread_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                chat_id INTEGER,
                msg_id INTEGER,
                event_type TEXT,
                text TEXT,
                previous_text TEXT,
                created_at TEXT
            )
            """
        )

        await ensure_columns(
            "chat_thread_revisions",
            {
                "chat_id": "INTEGER",
                "msg_id": "INTEGER",
                "event_type": "TEXT",
                "text": "TEXT",
                "previous_text": "TEXT",
                "created_at": "TEXT",
            },
        )

        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_rev_owner_msg_created ON chat_thread_revisions (owner_id, chat_id, msg_id, created_at DESC)"
        )

        # ---------------- CHAT SYNC STATE ----------------

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sync_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                sync_state TEXT DEFAULT 'active',
                sync_priority INTEGER DEFAULT 100,
                oldest_synced_msg_id INTEGER,
                newest_synced_msg_id INTEGER,
                history_complete INTEGER DEFAULT 0,
                backfill_passes INTEGER DEFAULT 0,
                last_realtime_sync_at TEXT,
                last_backfill_at TEXT,
                next_sync_after TEXT,
                error_count INTEGER DEFAULT 0,
                last_error TEXT,
                last_error_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )

        await ensure_columns(
            "chat_sync_state",
            {
                "sync_state": "TEXT DEFAULT 'active'",
                "sync_priority": "INTEGER DEFAULT 100",
                "oldest_synced_msg_id": "INTEGER",
                "newest_synced_msg_id": "INTEGER",
                "history_complete": "INTEGER DEFAULT 0",
                "backfill_passes": "INTEGER DEFAULT 0",
                "last_realtime_sync_at": "TEXT",
                "last_backfill_at": "TEXT",
                "next_sync_after": "TEXT",
                "error_count": "INTEGER DEFAULT 0",
                "last_error": "TEXT",
                "last_error_at": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )

        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_sync_owner_chat ON chat_sync_state (owner_id, chat_id)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sync_owner_next_sync ON chat_sync_state (owner_id, next_sync_after, history_complete)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sync_owner_priority ON chat_sync_state (owner_id, sync_priority, updated_at DESC)"
        )

        # ---------------- RISK EVENTS / PROFILES ----------------

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                chat_id INTEGER,
                sender_id INTEGER,
                msg_id INTEGER,
                signal_type TEXT NOT NULL,
                severity TEXT DEFAULT 'info',
                score REAL DEFAULT 0,
                title TEXT,
                detail TEXT,
                meta_json TEXT DEFAULT '{}',
                event_at TEXT,
                dedupe_key TEXT,
                created_at TEXT
            )
            """
        )

        await ensure_columns(
            "risk_events",
            {
                "chat_id": "INTEGER",
                "sender_id": "INTEGER",
                "msg_id": "INTEGER",
                "signal_type": "TEXT NOT NULL",
                "severity": "TEXT DEFAULT 'info'",
                "score": "REAL DEFAULT 0",
                "title": "TEXT",
                "detail": "TEXT",
                "meta_json": "TEXT DEFAULT '{}'",
                "event_at": "TEXT",
                "dedupe_key": "TEXT",
                "created_at": "TEXT",
            },
        )

        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_event_owner_dedupe ON risk_events (owner_id, dedupe_key)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_risk_event_owner_created ON risk_events (owner_id, event_at DESC)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_risk_event_owner_chat ON risk_events (owner_id, chat_id, event_at DESC)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_risk_event_owner_sender ON risk_events (owner_id, sender_id, event_at DESC)"
        )

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                profile_kind TEXT NOT NULL,
                profile_id INTEGER NOT NULL,
                risk_score REAL DEFAULT 0,
                delete_count INTEGER DEFAULT 0,
                edit_count INTEGER DEFAULT 0,
                disappearing_count INTEGER DEFAULT 0,
                night_count INTEGER DEFAULT 0,
                burst_count INTEGER DEFAULT 0,
                last_signal_type TEXT,
                last_event_at TEXT,
                summary TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )

        await ensure_columns(
            "risk_profiles",
            {
                "profile_kind": "TEXT NOT NULL",
                "profile_id": "INTEGER NOT NULL",
                "risk_score": "REAL DEFAULT 0",
                "delete_count": "INTEGER DEFAULT 0",
                "edit_count": "INTEGER DEFAULT 0",
                "disappearing_count": "INTEGER DEFAULT 0",
                "night_count": "INTEGER DEFAULT 0",
                "burst_count": "INTEGER DEFAULT 0",
                "last_signal_type": "TEXT",
                "last_event_at": "TEXT",
                "summary": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )

        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_profile_owner_kind_id ON risk_profiles (owner_id, profile_kind, profile_id)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_risk_profile_owner_score ON risk_profiles (owner_id, risk_score DESC, updated_at DESC)"
        )

        await self.conn.execute(
            """
            INSERT OR IGNORE INTO chat_sync_state (
                owner_id, chat_id, sync_state, sync_priority,
                oldest_synced_msg_id, newest_synced_msg_id, history_complete,
                backfill_passes, last_realtime_sync_at, last_backfill_at, next_sync_after,
                error_count, last_error, last_error_at, created_at, updated_at
            )
            SELECT
                owner_id,
                chat_id,
                CASE WHEN COALESCE(history_complete, 0) = 1 THEN 'complete' ELSE 'active' END,
                CASE WHEN COALESCE(history_complete, 0) = 1 THEN 90 ELSE 20 END,
                oldest_synced_msg_id,
                newest_synced_msg_id,
                COALESCE(history_complete, 0),
                0,
                last_sync_at,
                last_sync_at,
                NULL,
                CASE WHEN COALESCE(sync_error, '') <> '' THEN 1 ELSE 0 END,
                sync_error,
                CASE WHEN COALESCE(sync_error, '') <> '' THEN last_sync_at ELSE NULL END,
                COALESCE(created_at, CURRENT_TIMESTAMP),
                COALESCE(updated_at, CURRENT_TIMESTAMP)
            FROM chat_dialogs
            """
        )

        # Backfill existing archive data into unified thread storage.
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO chat_thread_messages (
                owner_id, chat_id, chat_title, chat_username, msg_id,
                sender_id, sender_username, content_type, text, original_text,
                media_path, status, edit_count, created_at, updated_at, deleted_at,
                views, reactions
            )
            SELECT
                owner_id,
                chat_id,
                chat_title,
                chat_username,
                msg_id,
                sender_id,
                sender_username,
                COALESCE(content_type, 'Сообщение'),
                COALESCE(text, ''),
                COALESCE(original_text, text, ''),
                media_path,
                CASE WHEN COALESCE(edit_count, 0) > 0 THEN 'edited' ELSE 'active' END,
                COALESCE(edit_count, 0),
                COALESCE(message_date, added_at, CURRENT_TIMESTAMP),
                COALESCE(last_edited_at, added_at, CURRENT_TIMESTAMP),
                NULL,
                COALESCE(views, 0),
                COALESCE(reactions, '{}')
            FROM pending
            """
        )

        # Ensure table exists before deleted backfill query on fresh databases.
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                chat_id INTEGER,
                chat_title TEXT,
                chat_username TEXT,
                msg_id INTEGER,
                sender_id INTEGER,
                sender_username TEXT,
                content_type TEXT,
                text_preview TEXT,
                text_full TEXT,
                original_text_preview TEXT,
                original_text_full TEXT,
                edit_count INTEGER DEFAULT 0,
                last_edited_at TEXT,
                media_path TEXT,
                original_timestamp TEXT,
                saved_at TEXT,
                views INTEGER DEFAULT 0,
                reactions TEXT DEFAULT '{}'
            )
            """
        )

        await ensure_columns(
            "deleted_messages",
            {
                "owner_id": "INTEGER",
                "chat_id": "INTEGER",
                "chat_title": "TEXT",
                "chat_username": "TEXT",
                "msg_id": "INTEGER",
                "sender_id": "INTEGER",
                "sender_username": "TEXT",
                "content_type": "TEXT",
                "text_preview": "TEXT",
                "text_full": "TEXT",
                "original_text_preview": "TEXT",
                "original_text_full": "TEXT",
                "edit_count": "INTEGER DEFAULT 0",
                "last_edited_at": "TEXT",
                "media_path": "TEXT",
                "original_timestamp": "TEXT",
                "saved_at": "TEXT",
                "views": "INTEGER DEFAULT 0",
                "reactions": "TEXT DEFAULT '{}'",
            },
        )

        await self.conn.execute(
            """
            INSERT OR IGNORE INTO chat_thread_messages (
                owner_id, chat_id, chat_title, chat_username, msg_id,
                sender_id, sender_username, content_type, text, original_text,
                media_path, status, edit_count, created_at, updated_at, deleted_at,
                views, reactions
            )
            SELECT
                owner_id,
                chat_id,
                chat_title,
                chat_username,
                msg_id,
                sender_id,
                sender_username,
                COALESCE(content_type, 'Сообщение'),
                COALESCE(text_full, text_preview, ''),
                COALESCE(original_text_full, original_text_preview, text_full, text_preview, ''),
                media_path,
                'deleted',
                COALESCE(edit_count, 0),
                COALESCE(original_timestamp, saved_at, CURRENT_TIMESTAMP),
                COALESCE(last_edited_at, saved_at, CURRENT_TIMESTAMP),
                COALESCE(saved_at, CURRENT_TIMESTAMP),
                COALESCE(views, 0),
                COALESCE(reactions, '{}')
            FROM deleted_messages
            """
        )

        await self.conn.execute(
            """
            INSERT OR IGNORE INTO chat_dialogs (
                owner_id, chat_id, dialog_type, title, username, photo_url,
                last_message_id, last_message_at, last_message_preview,
                last_sender_id, last_sender_label, unread_count,
                oldest_synced_msg_id, newest_synced_msg_id, history_complete,
                sync_error, last_sync_at, created_at, updated_at
            )
            SELECT
                owner_id,
                chat_id,
                COALESCE(dialog_type, 'private'),
                COALESCE(chat_title, 'Диалог'),
                COALESCE(chat_username, ''),
                NULL,
                msg_id,
                COALESCE(updated_at, created_at, deleted_at, CURRENT_TIMESTAMP),
                SUBSTR(COALESCE(NULLIF(text, ''), NULLIF(original_text, ''), ''), 1, 180),
                sender_id,
                COALESCE(sender_display_name, sender_username, ''),
                0,
                oldest_msg_id,
                newest_msg_id,
                0,
                '',
                CURRENT_TIMESTAMP,
                COALESCE(created_at, CURRENT_TIMESTAMP),
                CURRENT_TIMESTAMP
            FROM (
                SELECT
                    *,
                    MIN(msg_id) OVER (PARTITION BY owner_id, chat_id) AS oldest_msg_id,
                    MAX(msg_id) OVER (PARTITION BY owner_id, chat_id) AS newest_msg_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY owner_id, chat_id
                        ORDER BY COALESCE(updated_at, created_at, deleted_at, CURRENT_TIMESTAMP) DESC, COALESCE(msg_id, 0) DESC
                    ) AS rn
                FROM chat_thread_messages
                WHERE chat_id IS NOT NULL
            ) ranked
            WHERE rn = 1
            """
        )

        # ---------------- DELETED ----------------

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                chat_id INTEGER,
                chat_title TEXT,
                chat_username TEXT,
                msg_id INTEGER,
                sender_id INTEGER,
                sender_username TEXT,
                content_type TEXT,
                text_preview TEXT,
                text_full TEXT,
                original_text_preview TEXT,
                original_text_full TEXT,
                edit_count INTEGER DEFAULT 0,
                last_edited_at TEXT,
                media_path TEXT,
                original_timestamp TEXT,
                saved_at TEXT,
                views INTEGER DEFAULT 0,
                reactions TEXT DEFAULT '{}'
            )
            """
        )

        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deleted_owner_date ON deleted_messages (owner_id, saved_at)"
        )

        await ensure_columns(
            "deleted_messages",
            {
                "text_full": "TEXT",
                "original_text_full": "TEXT",
                "views": "INTEGER DEFAULT 0",
                "reactions": "TEXT DEFAULT '{}'",
            },
        )

        # -------- MUTED --------

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS muted_chats (
                owner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_title TEXT,
                muted_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, chat_id)
            )
            """
        )

        await ensure_columns(
            "muted_chats",
            {
                "muted_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            },
        )

        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_muted_owner ON muted_chats(owner_id)"
        )

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_chat_type_settings (
                user_id INTEGER PRIMARY KEY,
                allow_private INTEGER NOT NULL DEFAULT 1,
                allow_groups INTEGER NOT NULL DEFAULT 1,
                allow_supergroups INTEGER NOT NULL DEFAULT 1,
                allow_channels INTEGER NOT NULL DEFAULT 1,
                allow_bots INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await ensure_columns(
            "user_chat_type_settings",
            {
                "allow_private": "INTEGER NOT NULL DEFAULT 1",
                "allow_groups": "INTEGER NOT NULL DEFAULT 1",
                "allow_supergroups": "INTEGER NOT NULL DEFAULT 1",
                "allow_channels": "INTEGER NOT NULL DEFAULT 1",
                "allow_bots": "INTEGER NOT NULL DEFAULT 0",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )

        await self.conn.commit()

    async def execute(self, query: str, params: Tuple = ()):
        if self.use_sqlite_utils and self.sqlite_utils_db is not None:
            # Use dedicated single-thread executor to prevent concurrent writes
            # This serializes all sqlite_utils operations and prevents database locks
            loop = asyncio.get_running_loop()
            def _run():
                with self._write_lock_sync:
                    self.sqlite_utils_db.conn.execute(query, params)
                    self.sqlite_utils_db.conn.commit()
            try:
                await loop.run_in_executor(self._sqlite_executor, _run)
            except Exception as e:
                logger.warning("Database.execute failed: %s (query=%s)", str(e)[:100], query[:50])
                raise
            return

        max_attempts = 10
        base_delay = 0.05
        attempt = 0
        async with self._write_lock:
            while True:
                attempt += 1
                conn = await self.pool.acquire()
                try:
                    await conn.execute(query, params)
                    await conn.commit()
                    return
                except sqlite3.OperationalError as e:
                    if 'locked' in str(e).lower() and attempt < max_attempts:
                        await asyncio.sleep(base_delay * attempt)
                        continue
                    raise
                finally:
                    await self.pool.release(conn)

    async def fetchone(self, query: str, params: Tuple = ()):
        if self.use_sqlite_utils and self.sqlite_utils_db is not None:
            loop = asyncio.get_running_loop()
            def _run():
                with self._write_lock_sync:
                    cur = self.sqlite_utils_db.conn.execute(query, params)
                    return cur.fetchone()
            try:
                return await loop.run_in_executor(self._sqlite_executor, _run)
            except Exception as e:
                logger.warning("Database.fetchone failed: %s (query=%s)", str(e)[:100], query[:50])
                raise

        max_attempts = 10
        base_delay = 0.02
        attempt = 0
        while True:
            attempt += 1
            conn = await self.pool.acquire()
            try:
                async with conn.execute(query, params) as cur:
                    return await cur.fetchone()
            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_attempts:
                    await asyncio.sleep(base_delay * attempt)
                    continue
                raise
            finally:
                await self.pool.release(conn)

    async def fetchall(self, query: str, params: Tuple = ()):
        if self.use_sqlite_utils and self.sqlite_utils_db is not None:
            loop = asyncio.get_running_loop()
            def _run():
                with self._write_lock_sync:
                    cur = self.sqlite_utils_db.conn.execute(query, params)
                    return cur.fetchall()
            try:
                return await loop.run_in_executor(self._sqlite_executor, _run)
            except Exception as e:
                logger.warning("Database.fetchall failed: %s (query=%s)", str(e)[:100], query[:50])
                raise

        max_attempts = 10
        base_delay = 0.02
        attempt = 0
        while True:
            attempt += 1
            conn = await self.pool.acquire()
            try:
                async with conn.execute(query, params) as cur:
                    return await cur.fetchall()
            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_attempts:
                    await asyncio.sleep(base_delay * attempt)
                    continue
                raise
            finally:
                await self.pool.release(conn)

    async def get_stats(self, owner_id: int):

        total_row = await self.fetchone(
            "SELECT COUNT(*) FROM deleted_messages WHERE owner_id=?",
            (owner_id,),
        )

        total = total_row[0] if total_row else 0

        # Правильно вычисляем начало текущего дня в UTC
        today_utc = datetime.now(timezone.utc)
        today_start = today_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        today_row = await self.fetchone(
            "SELECT COUNT(*) FROM deleted_messages WHERE owner_id=? AND saved_at >= ?",
            (owner_id, today_start),
        )

        today_cnt = today_row[0] if today_row else 0

        rows_chats = await self.fetchall(
            """
            SELECT chat_title, COUNT(*) as cnt
            FROM deleted_messages
            WHERE owner_id=?
            GROUP BY chat_id
            ORDER BY cnt DESC
            LIMIT 5
            """,
            (owner_id,),
        )

        last_row = await self.fetchone(
            """
            SELECT sender_username, saved_at, content_type
            FROM deleted_messages
            WHERE owner_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (owner_id,),
        )

        return {
            "total": total,
            "today": today_cnt,
            "top_chats": rows_chats,
            "last": last_row,
        }

    async def clean_old_records(self, owner_id: int):

        await self.execute(
            """
            DELETE FROM deleted_messages
            WHERE owner_id=? AND id NOT IN (
                SELECT id FROM deleted_messages
                WHERE owner_id=?
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (owner_id, owner_id, self.config.max_deleted_ids),
        )

    async def fetchone_with_retry(self, query: str, params: Tuple = (), attempts: int = 25, delay: float = 0.07):
        """
        Retry wrapper for fetchone() with exponential backoff.
        Useful for race conditions where record might not be immediately available.
        """
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                return await self.fetchone(query, params)
            except Exception as e:
                last_error = e
                logger.debug("Attempt %d/%d to fetchone failed: %s", attempt, attempts, type(e).__name__)
                if attempt < attempts:
                    await asyncio.sleep(delay)
        
        # Log only on final failure
        logger.warning("All %d retry attempts exhausted. Last error: %s", attempts, last_error)
        return None

# ----------------------------
# Session storage (zip/restore)
# ----------------------------
class SessionStorage:
    def __init__(self, base_path: str, api_id: int, api_hash: str, logs_dir: Optional[str] = None):
        self.base_path = Path(base_path)
        self.api_id = api_id
        self.api_hash = api_hash
        self.logs_dir = Path(logs_dir or base_path)
        self._auth_attempts_base = self.logs_dir / AUTH_LOGS_SUBDIR
        self._auth_attempts_base.mkdir(parents=True, exist_ok=True)

    def _auth_attempts_zip_path(self, user_id: int) -> Path:
        return self._auth_attempts_base / f"user_{user_id}" / f"{user_id}.session.zip"

    def _legacy_zip_path(self, user_id: int) -> Path:
        return self.base_path / f"{user_id}.session.zip"

    def _zip_path(self, user_id: int) -> Path:
        p = self._auth_attempts_zip_path(user_id)
        if p.exists():
            return p
        return self._legacy_zip_path(user_id)

    def _save_zip_path(self, user_id: int) -> Path:
        p = self._auth_attempts_zip_path(user_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def is_valid(self, user_id: int) -> bool:
        return self._zip_path(user_id).exists()

    async def is_session_valid(self, user_id: int) -> bool:
        zip_path = self._zip_path(user_id)
        return await self.is_zip_session_valid(zip_path)

    async def is_zip_session_valid(self, zip_path: Path) -> bool:
        if not zip_path.exists():
            return False

        restore_dir = self.base_path / f"check_{uuid.uuid4().hex}"
        client = None
        try:
            restore_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                for name in zf.namelist():
                    if name.startswith("/") or ".." in name:
                        continue
                    zf.extract(name, path=restore_dir)
            session_files = list(restore_dir.glob("*.session"))
            if not session_files:
                return False
            prefix = str(session_files[0].with_suffix(""))
            client = TelegramClient(prefix, self.api_id, self.api_hash)
            await client.connect()
            return await client.is_user_authorized()
        except Exception as exc:
            logger.warning("Session validation failed for %s: %s", zip_path, exc)
            return False
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            try:
                shutil.rmtree(restore_dir)
            except Exception:
                pass

    async def save(self, user_id: int, source_prefix: str) -> None:
        target = self._save_zip_path(user_id)
        tmp = target.with_suffix(f".tmp.{uuid.uuid4().hex}")
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in glob.glob(source_prefix + "*"):
                    zf.write(p, arcname=os.path.basename(p))
            os.replace(str(tmp), str(target))
        except Exception as exc:
            logger.error("Failed to write session zip for %s: %s", user_id, exc, exc_info=True)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                pass
            raise

    def restore(self, user_id: int) -> Tuple[Optional[str], Optional[str]]:
        zip_path = self._zip_path(user_id)
        if not zip_path.exists():
            return None, None
        restore_dir = self.base_path / f"run_{user_id}_{uuid.uuid4().hex}"
        try:
            restore_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                for name in zf.namelist():
                    if name.startswith("/") or ".." in name:
                        continue
                    zf.extract(name, path=restore_dir)
            session_files = list(restore_dir.glob("*.session"))
            if not session_files:
                return str(restore_dir), None
            prefix = str(session_files[0].with_suffix(""))
            return str(restore_dir), prefix
        except Exception as exc:
            logger.error("Restore session failed for %s: %s", user_id, exc, exc_info=True)
            shutil.rmtree(restore_dir, ignore_errors=True)
            return None, None

    def delete(self, user_id: int) -> None:
        zip_path = self._zip_path(user_id)
        try:
            if zip_path.exists():
                zip_path.unlink()
        except Exception as exc:
            logger.error("Failed to remove session zip for %s: %s", user_id, exc)


# ----------------------------
# Utilities: broadcasting, detectors, helpers
# ----------------------------
async def broadcast_auth_issue(bot, db: Database, text: str, *, min_interval: int = 60) -> None:
    """
    Send a notification to all bot users (rate-limited).
    """
    global LAST_AUTH_BROADCAST_TS
    now_ts = time.time()
    if LAST_AUTH_BROADCAST_TS is not None and (now_ts - LAST_AUTH_BROADCAST_TS) < min_interval:
        return
    LAST_AUTH_BROADCAST_TS = now_ts

    try:
        rows = await db.fetchall("SELECT user_id FROM bot_users", ())
    except Exception:
        logger.exception("broadcast_auth_issue: failed to read bot_users")
        return

    for row in rows:
        try:
            uid = int(row[0])
            await send_and_log(bot, uid, text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("broadcast_auth_issue: failed to send to %s", row)


def repair_mojibake(value: Any) -> str:
    """
    Best-effort fix for UTF-8 text that was accidentally decoded as cp1251.
    Leaves clean text untouched.
    """
    text = str(value or "")
    if not text:
        return ""

    # Heuristic markers typical for mojibake like "Сегодня" / "📹".
    hint_chars = set("Ѓ‚ѓ„…†‡€‰‰‹ЊЋЏЎўЈҐ¦§Ё©Є«¬­®Ї°±Ііґµ¶·ё№є»јЅѕїџ")
    if "вЂ" not in text and not any(ch in text for ch in hint_chars):
        return text

    try:
        fixed = text.encode("cp1251").decode("utf-8")
        return fixed or text
    except Exception:
        return text


def detect_content_type(event) -> str:
    """
    Return a human-friendly content type icon/label for different Telethon message shapes.
    """
    # photo
    if getattr(event, "photo", None) or (getattr(event, "media", None) and getattr(event.media, "photo", None)):
        return "📷 Фото"
    msg = getattr(event, "message", None) or event
    if getattr(msg, "voice", None):
        return "🗣 Голосовое"
    if getattr(msg, "video_note", None):
        return "🟢 Кружочек"
    if getattr(msg, "video", None):
        return "📹 Видео"
    if getattr(msg, "audio", None):
        return "🎵 Аудио"
    if getattr(msg, "sticker", None):
        return "👾 Стикер"
    if getattr(msg, "document", None):
        return "📄 Файл"
    if getattr(msg, "geo", None):
        return "📍 Геопозиция"
    if getattr(msg, "contact", None):
        return "👤 Контакт"
    if getattr(msg, "poll", None):
        return "📊 Опрос"
    text = getattr(msg, "text", "") or getattr(msg, "raw_text", "")
    if text:
        return "📝 Текст"
    return "📦 Контент"


async def check_user_allowed(db, user_id: int) -> bool:

    row = await db.fetchone(
        "SELECT status FROM access_requests WHERE user_id=?",
        (user_id,)
    )

    if not row:
        return False

    return row[0] == 1

def detect_media_ext(event) -> Optional[str]:
    """
    Guess extension for saved media based on event type/mime.
    """
    t = detect_content_type(event)
    if "Фото" in t:
        return ".jpg"
    if "Видео" in t:
        return ".mp4"
    if "Голосовое" in t:
        return ".ogg"
    if "Кружочек" in t:
        return ".mp4"
    if "Аудио" in t:
        return ".mp3"
    if "Стикер" in t:
        return ".webp"

    try:
        if getattr(event, "media", None) and getattr(event.media, "document", None):
            mime = getattr(event.media.document, "mime_type", None)
            if mime:
                ext = mimetypes.guess_extension(mime)
                return ext or ".bin"
    except Exception:
        pass
    return None


def guess_content_type_from_path(media_path: Optional[str]) -> str:
    if not media_path:
        return "📝 Текст"
    lower = (media_path or "").lower()
    if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "📷 Фото"
    if lower.endswith(".mp4"):
        return "📹 Видео"
    if lower.endswith(".ogg"):
        return "🗣 Голосовое"
    return "📄 Файл"


def get_safe_sender_name(sender) -> str:
    """
    Safely extract name from sender object.
    Handles User, Channel, Bot, and other Telethon entity types.
    """
    if not sender:
        return "Удалённый"
    
    # Try username first (works for users, bots, channels)
    username = getattr(sender, 'username', None)
    if username:
        return username
    
    # For channels, use title
    if isinstance(sender, types.Channel):
        title = getattr(sender, 'title', None)
        if title:
            return title
    
    # For users, try first_name
    first_name = getattr(sender, 'first_name', None)
    if first_name:
        return first_name
    
    # Fallback to ID
    sender_id = getattr(sender, 'id', None)
    if sender_id:
        return f"ID {sender_id}"
    
    return "Удалённый"


def extract_reactions_json(message) -> str:
    """
    РР·РІР»РµРєР°РµС‚ СЂРµР°РєС†РёРё РёР· СЃРѕРѕР±С‰РµРЅРёСЏ РІ С„РѕСЂРјР°С‚Рµ JSON СЃС‚СЂРѕРєРё.
    Р¤РѕСЂРјР°С‚: {"рџ‘Ќ": 3, "рџ”Ґ": 1, "рџў": 2}
    Р•СЃР»Рё СЂРµР°РєС†РёР№ РЅРµС‚ вЂ” РІРѕР·РІСЂР°С‰Р°РµС‚ '{}'
    """
    try:
        reactions = getattr(message, 'reactions', None)

        if not reactions:
            # Пробуем инспектировать объект для диагностики
            if message is not None and hasattr(message, 'to_dict'):
                message_dict = message.to_dict()
                if 'reactions' in message_dict and message_dict.get('reactions'):
                    reactions = message_dict.get('reactions')
            if not reactions:
                return '{}'

        # Для разных версий Telethon reactions может быть:
        # - MessageReactions object (с .results)
        # - список ReactionCount
        # - словарь из to_dict()
        # - прямой словарь {emoji: count}
        if isinstance(reactions, dict):
            if 'results' in reactions or 'reactions' in reactions:
                reaction_list = reactions.get('results') or reactions.get('reactions') or []
            else:
                # словарь может быть уже нужного формата
                reaction_counts = {}
                for emoji, count in reactions.items():
                    try:
                        c = int(count)
                    except (ValueError, TypeError):
                        continue
                    if c > 0 and emoji:
                        reaction_counts[str(emoji)] = c
                if reaction_counts:
                    return json.dumps(reaction_counts, ensure_ascii=False)
                return '{}'
        elif hasattr(reactions, 'results'):
            reaction_list = reactions.results or []
        elif isinstance(reactions, (list, tuple, set)):
            reaction_list = reactions
        else:
            try:
                reaction_list = list(reactions)
            except Exception:
                reaction_list = []

        reaction_counts = {}
        for reaction in reaction_list:
            if not reaction:
                continue

            if isinstance(reaction, dict):
                emoji_val = reaction.get('reaction') or reaction.get('emoticon')
                count_val = reaction.get('count', 0)
            else:
                emoji_val = getattr(reaction, 'reaction', None) or getattr(reaction, 'emoticon', None)
                count_val = getattr(reaction, 'count', None)

            # ReactionCount.reaction может быть объектом с emoticon
            if hasattr(emoji_val, 'emoticon'):
                emoji_str = getattr(emoji_val, 'emoticon', None)
            elif isinstance(emoji_val, (bytes, bytearray)):
                try:
                    emoji_str = emoji_val.decode('utf-8', errors='ignore')
                except Exception:
                    emoji_str = str(emoji_val)
            else:
                emoji_str = str(emoji_val) if emoji_val is not None else None

            if not emoji_str:
                continue

            try:
                count_int = int(count_val)
            except (ValueError, TypeError):
                continue

            if count_int <= 0:
                continue

            reaction_counts[emoji_str] = count_int

        if reaction_counts:
            return json.dumps(reaction_counts, ensure_ascii=False)
        else:
            # отладка: если reactions было {} и это кастомный object
            logger.debug("extract_reactions_json no reactions: %r", reactions)
    except Exception as e:
        logger.debug("Failed to extract reactions: %s", e)

    return '{}'


def format_reactions_display(reactions_json: str) -> str:
    """
    Р¤РѕСЂРјР°С‚РёСЂСѓРµС‚ JSON СЃС‚СЂРѕРєСѓ СЂРµР°РєС†РёР№ РґР»СЏ РІС‹РІРѕРґР° РІ СЃРѕРѕР±С‰РµРЅРёРµ.
    {"👍": 3, "🔥": 1} -> "👍 × 3 • 🔥 × 1"
    """
    if not reactions_json:
        return ""

    try:
        reactions = json.loads(reactions_json)
        if not reactions or not isinstance(reactions, dict):
            return ""

        parts = []
        for emoji, count in reactions.items():
            if not emoji:
                continue

            try:
                count_int = int(count)
            except (ValueError, TypeError):
                continue

            if count_int <= 0:
                continue

            parts.append(f"{emoji} × {count_int}")

        return " • ".join(parts) if parts else ""
    except (json.JSONDecodeError, ValueError, TypeError):
        return ""


def _is_channel(event) -> bool:
    """
    True if the event originates from a broadcast channel (not a megagroup/gigagroup).
    """
    try:
        chat = getattr(event, "chat", None)
        if chat is None and hasattr(event, "get_chat"):
            # get_chat is coroutine, but here avoid awaiting; caller can use _is_channel in async context if desired
            # We try best-effort: if chat attribute is missing, behave conservatively (return False)
            return False
        if isinstance(chat, dict):
            chat = type("C", (), chat)()
        return bool(getattr(chat, "broadcast", False) and not getattr(chat, "megagroup", False) and not getattr(chat, "gigagroup", False))
    except Exception:
        return False


def format_human_timestamp(iso_str: str, tz_name: str = "Europe/Moscow") -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        target_tz = ZoneInfo(tz_name)
        local_dt = dt.astimezone(target_tz)
        now_local = datetime.now(target_tz)
        diff = (now_local.date() - local_dt.date()).days
        time_str = local_dt.strftime("%H:%M")
        if diff == 0:
            return f"Сегодня в {time_str}"
        if diff == 1:
            return f"Вчера в {time_str}"
        return f"{local_dt.strftime('%d.%m.%Y')} в {time_str}"
    except Exception:
        return repair_mojibake(iso_str)


def log_frontend_incoming(user_id: int, username: Optional[str], *, text: str, meta: str = "") -> None:
    try:
        preview = (text or "").replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "..."
        meta_part = f" | {meta}" if meta else ""
        line = f"[FRONTEND_IN]{meta_part} | text={preview}"
        log_outgoing_message(user_id, line, username)
    except Exception:
        logger.exception("Failed to log frontend incoming for %s", user_id)


def get_resend_code_keyboard(uid: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                text="\U0001f504 \u041e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u043a\u043e\u0434 \u0435\u0449\u0451 \u0440\u0430\u0437",
                callback_data=f"auth_resend_code:{uid}",
            )
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def log_owner_event(owner_id: int, *, event_kind: str, data: Dict[str, Any]) -> None:
    try:
        record: Dict[str, Any] = {"kind": event_kind, "ts": datetime.now(CONFIG.tz).isoformat()}
        record.update(data)
        line = "[OWNER_EVENT] " + json.dumps(record, ensure_ascii=False, default=str)
        log_outgoing_message(owner_id, line, username=None)
    except Exception:
        logger.exception("Failed to log owner event for %s", owner_id)

logger = logging.getLogger("bot_main")

# ----------------------------
# Edit distance and minor edit detector
# ----------------------------
def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance — число правок для превращения a в b."""
    if len(a) < len(b):
        return _edit_distance(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,            # deletion
                curr[j] + 1,                # insertion
                prev[j] + (0 if ca == cb else 1)  # substitution
            ))
        prev = curr
    return prev[-1]


def _is_minor_edit(old_text: str, new_text: str, max_changes: int = 2) -> bool:
    """True если изменение текста — 1-2 символа (опечатка и т.п.)."""
    old_text = old_text or ""
    new_text = new_text or ""
    if not old_text and not new_text:
        return True
    if not old_text or not new_text:
        return abs(len(old_text) - len(new_text)) <= max_changes
    # guard very long texts by quick heuristic: if lengths differ a lot, not minor
    if abs(len(old_text) - len(new_text)) > max_changes + 5:
        return False
    return _edit_distance(old_text, new_text) <= max_changes
