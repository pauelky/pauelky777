"""
Универсальный Telegram-бот: вопросы, фото, голос, изображения, погода и стили ответа.
"""
import hashlib
import os
import sys
import atexit
import re
import html
import json
import sqlite3
import base64
import random
import logging
import asyncio
import subprocess
import contextlib
import time
import re
import urllib.parse
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread, Lock, Event
from typing import Any, Callable, Literal
from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery as AiogramCallbackQuery
from aiogram.types import Message as AiogramMessage
from deep_translator import GoogleTranslator
from telegram_compat import (
    BadRequest,
    Bot,
    CallbackQuery,
    ChatAction,
    ContextTypes,
    Forbidden,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ParseMode,
    ReplyKeyboardMarkup,
    RetryAfter,
    TelegramError,
    Update,
)

from abc import ABC, abstractmethod
ThreadLock = Lock

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import torch
except ImportError:
    torch = None

if torch is not None:
    try:
        print(torch.cuda.is_available())  # Should return True
        if torch.cuda.is_available():
            print(torch.cuda.get_device_name(0))  # Shows your GPU
    except Exception:
        pass



try:
    import httpx
except ImportError:
    print("Установите: pip install httpx")
    raise

try:
    import edge_tts
except Exception:
    # edge-tts нужен для TTS; если не нужен — можно отключить вызовы TTS
    edge_tts = None


try:
    RetryAfter = RetryAfter
    TelegramError = TelegramError
except Exception:
    pass

try:
    import h2  # noqa: F401
except ImportError:
    _HTTPX_HTTP2_AVAILABLE = False
else:
    _HTTPX_HTTP2_AVAILABLE = True

_HTTPX_HTTP2_WARNING_EMITTED = False


def _httpx_http2_enabled() -> bool:
    global _HTTPX_HTTP2_WARNING_EMITTED
    if _HTTPX_HTTP2_AVAILABLE:
        return True
    if not _HTTPX_HTTP2_WARNING_EMITTED:
        logging.warning("h2 is not installed, falling back to HTTP/1.1 for httpx clients")
        _HTTPX_HTTP2_WARNING_EMITTED = True
    return False

# ===== Переводчик =====

def translate_to_english(text: str) -> str:
    try:
        return GoogleTranslator(source='auto', target='en').translate(text)
    except Exception as e:
        logging.warning(f"Translation failed: {e}")
        return text

# ---- Стадии прогресса при генерации ответа ----
STAGES = [
    (0, 14, "🌐 Подключаюсь к модели…"),
    (15, 39, "🔎 Разбираю запрос…"),
    (40, 69, "🧠 Думаю над ответом…"),
    (70, 94, "✍️ Формулирую ответ…"),
    (95, 99, "🔎 Перепроверяю финальную версию…"),
    (100, 100, "✅ Ответ готов"),
]

def stage_for(percent: int) -> str:
    p = max(0, min(100, int(percent)))
    for start, end, name in STAGES:
        if start <= p <= end:
            return name
    return "Обработка"

def sanitize_stage(stage: str | None, max_len: int = 200) -> str | None:
    if not stage:
        return None
    s = "".join(ch for ch in stage.strip() if ord(ch) >= 32)
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s or None

def clean_text_for_tts(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'[*_`#]', '', text)
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    return text.replace('\n', ' ').strip()

def clean_reply_for_display(text: str | None) -> str:
    """Очистка ответа для отображения/отправки в Telegram.

    Преобразует LaTeX-обозначения, убирает markdown/кодовые блоки,
    экранирует HTML-сущности и возвращает аккуратно обрезанную строку.
    """
    if text is None:
        return ""

    # приводим к строке
    s = str(text)

    # 🔥 ИСПРАВЛЕНИЕ: убираем ТОЛЬКО плохие control-символы,
    # но ОБЯЗАТЕЛЬНО сохраняем переносы строк \n и \r
    s = "".join(ch for ch in s if ord(ch) >= 32 or ch in '\n\r')

    # Разворачиваем HTML-сущности (&amp; → & и т.п.)
    s = html.unescape(s)
    s = s.replace("\xa0", " ")

    # Telegram хуже воспринимает HTML-обрывки и <br> внутри обычного текста.
    br_placeholder = "TGHTMLBRPLACEHOLDER"
    s = re.sub(r"(?i)<br\s*/?>", br_placeholder, s)
    s = re.sub(r"(?i)</?(?:p|div|section|article|ul|ol|li|table|thead|tbody|tr|th|td)\b[^>]*>", "\n", s)

    # --- Удаляем многострочные код-блоки ```...```
    s = re.sub(r'```[\w]*\n?(.*?)```', r'\1', s, flags=re.DOTALL)

    # --- Удаляем $$...$$ и $...$ (матем. окружения)
    s = re.sub(r'\$\$(.*?)\$\$', r'\1', s, flags=re.DOTALL)
    s = re.sub(r'\$(.*?)\$', r'\1', s, flags=re.DOTALL)

    # --- \frac{a}{b} в†' a/b
    s = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1/\2', s)

    # --- \command{...} в†' ...
    s = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', s)

    # --- \alpha в†' alpha
    s = re.sub(r'\\([a-zA-Z]+)', r'\1', s)

    # убрать фигурные скобки (заменяем на круглые для читаемости)
    s = s.replace('{', '(').replace('}', ')')

    # Убираем markdown выделения **bold**, *italic*, _under_, `code`
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
    s = re.sub(r'\*(.+?)\*', r'\1', s)
    s = re.sub(r'_(.+?)_', r'\1', s)
    s = re.sub(r'`(.+?)`', r'\1', s)

    # Убираем заголовки Markdown (#, ## ...)
    s = re.sub(r'^[ \t]*#{1,6}\s*', '', s, flags=re.MULTILINE)

    # Таблицы в Telegram выглядят тяжело и неуместно — переводим их в читаемые блоки.
    lines = s.splitlines()
    rebuilt: list[str] = []
    idx = 0
    table_separator_re = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")

    def _split_table_row(row_text: str) -> list[str]:
        raw_row = row_text.strip().strip("|")
        return [re.sub(r"\s+", " ", cell).strip() for cell in raw_row.split("|")]

    while idx < len(lines):
        current = lines[idx]
        if (
            "|" in current
            and idx + 1 < len(lines)
            and table_separator_re.match(lines[idx + 1] or "")
        ):
            table_rows = [_split_table_row(current)]
            idx += 2
            while idx < len(lines):
                candidate = lines[idx]
                if "|" not in candidate or len(candidate.strip().strip("|").split("|")) < 2:
                    break
                table_rows.append(_split_table_row(candidate))
                idx += 1

            headers = [cell or f"Поле {pos + 1}" for pos, cell in enumerate(table_rows[0])]
            data_rows = table_rows[1:]
            blocks: list[str] = []
            for row_index, row in enumerate(data_rows, start=1):
                normalized_row = list(row[: len(headers)]) + [""] * max(0, len(headers) - len(row))
                primary_header = headers[0].strip().lower()
                primary_value = normalized_row[0].strip().replace(br_placeholder, "\n")
                block_lines: list[str] = []
                if primary_header in {"шаг", "этап", "step"} and primary_value:
                    block_lines.append(primary_value)
                elif primary_value:
                    block_lines.append(f"{headers[0]}: {primary_value}")
                else:
                    block_lines.append(f"Пункт {row_index}")

                for header, value in zip(headers[1:], normalized_row[1:]):
                    cell_text = (value or "").strip()
                    if not cell_text:
                        continue
                    cell_text = cell_text.replace(br_placeholder, "\n")
                    cell_text = cell_text.replace(" – ", "; ").replace(" — ", "; ")
                    block_lines.append(f"{header}: {cell_text}")
                blocks.append("\n".join(block_lines).strip())

            if blocks:
                if rebuilt and rebuilt[-1].strip():
                    rebuilt.append("")
                rebuilt.extend(blocks)
                rebuilt.append("")
                continue

        rebuilt.append(current)
        idx += 1

    s = "\n".join(rebuilt)
    s = s.replace(br_placeholder, "\n")
    s = re.sub(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$", "", s, flags=re.MULTILINE)

    # Нормализуем двойные-слэши `\/` → `/`
    s = s.replace('\\/', '/')

    # Дополнительно: красиво нормализуем переносы строк (чтобы абзацы были чёткими)
    # Убираем лишние пробелы и оставляем ровно двойной \n между абзацами
    s = re.sub(r'\n\s*\n', '\n\n', s)          # нормализуем пустые строки
    s = re.sub(r' +', ' ', s)                  # убираем лишние пробелы в строках
    s = s.strip()

    # (опционально) Ограничение длины — раскомментируй если нужно
    # MAX_LEN = 4000
    # if len(s) > MAX_LEN:
    #     s = s[:MAX_LEN-3] + "..."

    return s

def normalize_progress_item(item: Any) -> tuple[int, str | None]:
    if item is None:
        return 0, None
    if isinstance(item, dict):
        p_val = 0
        stage_val = None
        if "percent" in item:
            try:
                p_val = int(float(item["percent"]))
            except Exception:
                p_val = 0
        for key in ("status_text", "text", "stage"):
            if key in item and isinstance(item[key], str):
                stage_val = sanitize_stage(item[key])
                if stage_val:
                    break
        return max(0, min(100, p_val)), stage_val
    if isinstance(item, (int, float)):
        return max(0, min(100, int(item))), None
    if isinstance(item, str):
        s = item.strip()
        if not s:
            return 0, None
        try:
            return max(0, min(100, int(float(s)))), None
        except ValueError:
            return 0, sanitize_stage(s)
    try:
        return max(0, min(100, int(float(item)))), None
    except Exception:
        logging.debug("normalize_progress_item: unknown item type %r", item)
        return 0, None


def format_progress_stage_text(item: Any, default_text: str = "🧠 Обрабатываю запрос…") -> str:
    percent, stage = normalize_progress_item(item)
    if stage:
        return stage
    if item is None:
        return default_text
    return stage_for(percent)


def is_progress_item_done(item: Any) -> bool:
    if isinstance(item, dict):
        if item.get("done") is True:
            return True
        try:
            if int(float(item.get("percent", 0))) >= 100:
                return True
        except Exception:
            pass
        combined = " ".join(
            part
            for part in (
                item.get("status_text"),
                item.get("text"),
                item.get("stage"),
            )
            if isinstance(part, str)
        ).lower()
        return "готов" in combined
    if isinstance(item, (int, float)):
        return int(float(item)) >= 100
    if isinstance(item, str):
        return "готов" in item.lower()
    return False


def make_progress_payload(status_text: str, *, done: bool = False, percent: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status_text": sanitize_stage(status_text) or "🧠 Обрабатываю запрос…"}
    if percent is not None:
        payload["percent"] = max(0, min(100, int(percent)))
    if done:
        payload["done"] = True
        if "percent" not in payload:
            payload["percent"] = 100
    return payload

def _format_assistant_reply(text: str) -> tuple[str, None]:
    return clean_reply_for_display(text), None


def _is_html_parse_mode(parse_mode: Any) -> bool:
    value = str(parse_mode or "").strip().upper()
    return value.endswith("HTML")


_SAFE_HTML_TOKENS = (
    "<b>", "</b>",
    "<strong>", "</strong>",
    "<i>", "</i>",
    "<em>", "</em>",
    "<u>", "</u>",
    "<s>", "</s>",
    "<code>", "</code>",
    "<pre>", "</pre>",
)


def sanitize_html_message(text: str | None) -> str:
    raw = "" if text is None else str(text)
    protected = raw
    placeholders: dict[str, str] = {}
    for idx, token in enumerate(_SAFE_HTML_TOKENS):
        placeholder = f"@@HTML_TOKEN_{idx}@@"
        placeholders[placeholder] = token
        protected = protected.replace(token, placeholder)

    protected = "".join(ch for ch in protected if ord(ch) >= 32 or ch in "\n\r\t")
    protected = html.escape(protected, quote=False)

    for placeholder, token in placeholders.items():
        protected = protected.replace(placeholder, token)

    protected = re.sub(r"\n\s*\n\s*\n+", "\n\n", protected).strip()
    return protected


# -------------------------------
# Автоматический санитайзер для telegram_compat / aiogram
# -------------------------------
def apply_text_sanitizer():
    # Сохраняем оригиналы
    orig_bot_send_message = Bot.send_message
    orig_bot_send_photo = Bot.send_photo
    orig_bot_edit_message_text = Bot.edit_message_text
    orig_message_reply_text = Message.reply_text
    orig_message_edit_text = Message.edit_text
    orig_callback_edit = CallbackQuery.edit_message_text

    async def _safe_call(orig_fn, *args, **kwargs):
        try:
            return await orig_fn(*args, **kwargs)
        except BadRequest:
            # Фолбэк: убираем опасные символы и parse_mode
            if 'text' in kwargs and isinstance(kwargs['text'], str):
                kwargs['text'] = re.sub(r'[<>]', '', kwargs['text'])
            if 'caption' in kwargs and isinstance(kwargs['caption'], str):
                kwargs['caption'] = re.sub(r'[<>]', '', kwargs['caption'])
            kwargs.pop('parse_mode', None)
            return await orig_fn(*args, **kwargs)

    # Патчим методы напрямую
    async def patched_bot_send_message(self, *args, **kwargs):
        html_mode = _is_html_parse_mode(kwargs.get('parse_mode'))
        if 'text' in kwargs and isinstance(kwargs['text'], str):
            kwargs['text'] = sanitize_html_message(kwargs['text']) if html_mode else clean_reply_for_display(kwargs['text'])
        elif len(args) >= 2 and isinstance(args[1], str):
            kwargs['text'] = sanitize_html_message(args[1]) if html_mode else clean_reply_for_display(args[1])
            args = (args[0],) + args[2:]
        if not html_mode:
            kwargs.pop('parse_mode', None)
        return await _safe_call(orig_bot_send_message, self, *args, **kwargs)

    async def patched_bot_send_photo(self, *args, **kwargs):
        html_mode = _is_html_parse_mode(kwargs.get('parse_mode'))
        if 'caption' in kwargs and isinstance(kwargs['caption'], str):
            kwargs['caption'] = sanitize_html_message(kwargs['caption']) if html_mode else clean_reply_for_display(kwargs['caption'])
        elif len(args) >= 3 and isinstance(args[2], str):
            kwargs['caption'] = sanitize_html_message(args[2]) if html_mode else clean_reply_for_display(args[2])
            args = args[:2] + args[3:]
        if not html_mode:
            kwargs.pop('parse_mode', None)
        return await _safe_call(orig_bot_send_photo, self, *args, **kwargs)

    async def patched_bot_edit_message_text(self, *args, **kwargs):
        html_mode = _is_html_parse_mode(kwargs.get('parse_mode'))
        if 'text' in kwargs and isinstance(kwargs['text'], str):
            kwargs['text'] = sanitize_html_message(kwargs['text']) if html_mode else clean_reply_for_display(kwargs['text'])
        elif len(args) >= 1 and isinstance(args[0], str):
            args = ((sanitize_html_message(args[0]) if html_mode else clean_reply_for_display(args[0])),) + args[1:]
        if not html_mode:
            kwargs.pop('parse_mode', None)
        return await _safe_call(orig_bot_edit_message_text, self, *args, **kwargs)

    async def patched_message_reply_text(self, *args, **kwargs):
        html_mode = _is_html_parse_mode(kwargs.get('parse_mode'))
        if len(args) >= 1 and isinstance(args[0], str):
            args = ((sanitize_html_message(args[0]) if html_mode else clean_reply_for_display(args[0])),) + args[1:]
        elif 'text' in kwargs and isinstance(kwargs['text'], str):
            kwargs['text'] = sanitize_html_message(kwargs['text']) if html_mode else clean_reply_for_display(kwargs['text'])
        if not html_mode:
            kwargs.pop('parse_mode', None)
        return await _safe_call(orig_message_reply_text, self, *args, **kwargs)

    async def patched_message_edit_text(self, *args, **kwargs):
        html_mode = _is_html_parse_mode(kwargs.get('parse_mode'))
        if len(args) >= 1 and isinstance(args[0], str):
            args = ((sanitize_html_message(args[0]) if html_mode else clean_reply_for_display(args[0])),) + args[1:]
        elif 'text' in kwargs and isinstance(kwargs['text'], str):
            kwargs['text'] = sanitize_html_message(kwargs['text']) if html_mode else clean_reply_for_display(kwargs['text'])
        if not html_mode:
            kwargs.pop('parse_mode', None)
        return await _safe_call(orig_message_edit_text, self, *args, **kwargs)

    async def patched_callback_edit(self, *args, **kwargs):
        html_mode = _is_html_parse_mode(kwargs.get('parse_mode'))
        if len(args) >= 1 and isinstance(args[0], str):
            args = ((sanitize_html_message(args[0]) if html_mode else clean_reply_for_display(args[0])),) + args[1:]
        elif 'text' in kwargs and isinstance(kwargs['text'], str):
            kwargs['text'] = sanitize_html_message(kwargs['text']) if html_mode else clean_reply_for_display(kwargs['text'])
        if not html_mode:
            kwargs.pop('parse_mode', None)
        return await _safe_call(orig_callback_edit, self, *args, **kwargs)

    # Применяем патчи
    Bot.send_message = patched_bot_send_message
    Bot.send_photo = patched_bot_send_photo
    Bot.edit_message_text = patched_bot_edit_message_text
    Message.reply_text = patched_message_reply_text
    Message.edit_text = patched_message_edit_text
    CallbackQuery.edit_message_text = patched_callback_edit

apply_text_sanitizer()


# ------------------------ CONFIG ------------------------
def _env_str(name: str, default: str = "") -> str:
    """
    Безопасно получить строку из окружения:
    - никогда не возвращает None
    - обрезает пробелы по краям
    """
    return os.getenv(name, default).strip()


# Загружаем .env заранее, чтобы конфиг ниже сразу видел актуальные значения.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Ключи и токены берём из окружения / .env.
# MAIN_BOT_TOKEN — приоритетный токен именно для main.py.
TELEGRAM_TOKEN = _env_str(
    "MAIN_BOT_TOKEN",
    _env_str("TELEGRAM_TOKEN", _env_str("BOT_TOKEN", "")),
)
OPENROUTER_API_KEY = _env_str("OPENROUTER_API_KEY", "")
OPENROUTER_DIRECT_URL = _env_str("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_PROXY_URL = _env_str("OPENROUTER_PROXY_URL", "")
OPENROUTER_PROXY_TOKEN = _env_str("OPENROUTER_PROXY_TOKEN", "")
OPENROUTER_URL = OPENROUTER_PROXY_URL or OPENROUTER_DIRECT_URL
DEFAULT_MAINBOT_MODEL = "baidu/cobuddy:free"
OPENROUTER_MODEL = _env_str("OPENROUTER_MODEL", DEFAULT_MAINBOT_MODEL)
# Модель с поддержкой зрения для анализа фото (если не задана — используется OPENROUTER_MODEL)
OPENROUTER_VISION_MODEL = _env_str("OPENROUTER_VISION_MODEL", DEFAULT_MAINBOT_MODEL)
_MODEL_FAST_TEXT_EXPLICIT = bool(_env_str("MODEL_FAST_TEXT", ""))
MODEL_FAST_TEXT = _env_str("MODEL_FAST_TEXT", OPENROUTER_MODEL)
MODEL_STRONG_TEXT = _env_str("MODEL_STRONG_TEXT", OPENROUTER_MODEL)
MODEL_VISION = _env_str("MODEL_VISION", OPENROUTER_VISION_MODEL or OPENROUTER_MODEL)
MODEL_MATH = _env_str("MODEL_MATH", MODEL_STRONG_TEXT)
MODEL_BACKUP_TEXT = _env_str("MODEL_BACKUP_TEXT", DEFAULT_MAINBOT_MODEL)
OPENROUTER_VISION_MODEL = MODEL_VISION
OPENROUTER_PROXY_MONITOR_INTERVAL = float(os.getenv("OPENROUTER_PROXY_MONITOR_INTERVAL", "20") or "20")
OPENWEATHER_API_KEY = _env_str("OPENWEATHER_API_KEY", "")
OCR_API_URL = _env_str("OCR_API_URL", "https://api.ocr.space/parse/image")
OCR_API_KEY = _env_str("OCR_API_KEY", "")
OCR_PROVIDER = _env_str("OCR_PROVIDER", "ocr_space").strip().lower()
OCR_SERVICE_TIMEOUT = float(os.getenv("OCR_SERVICE_TIMEOUT", "25") or "25")
OCR_SERVICE_MIN_ACCEPT_SCORE = float(os.getenv("OCR_SERVICE_MIN_ACCEPT_SCORE", "108") or "108")
OCR_SERVICE_IMAGE_MAX_DIMENSION = int(os.getenv("OCR_SERVICE_IMAGE_MAX_DIMENSION", "2200") or "2200")
OCR_SPACE_LANGUAGE = _env_str("OCR_SPACE_LANGUAGE", "auto").strip().lower() or "auto"
OCR_SPACE_ENGINE_PRIMARY = int(os.getenv("OCR_SPACE_ENGINE_PRIMARY", "3") or "3")
OCR_SPACE_ENGINE_FALLBACK = int(os.getenv("OCR_SPACE_ENGINE_FALLBACK", "2") or "2")

OWNER_ID = int(os.environ.get("OWNER_ID", "0") or "0")
REQUIRED_CHAT_ID = os.environ.get("REQUIRED_CHAT_ID", "@pauelkyy_group")
OPENROUTER_MAX_CONCURRENT = int(os.getenv("OPENROUTER_MAX_CONCURRENT", "3"))  # Увеличено с 4 до 15
MAX_MSG_LEN = 4000
DEFAULT_PROGRESS_MIN_INTERVAL = float(os.getenv("DEFAULT_PROGRESS_MIN_INTERVAL", "0.9"))

# ---- Параметры стриминга текста ----
ENABLE_STREAMING = os.getenv("ENABLE_STREAMING", "true").lower() == "true"  # Включен! Постепенное появление слов в ответе
STREAM_DELAY = float(os.getenv("STREAM_DELAY", "0.04"))  # Задержка между накоплением символов (более плавный эффект)
STREAM_CHUNK_SIZE = int(os.getenv("STREAM_CHUNK_SIZE", "45"))  # Меньше символов = более мягкий стриминг
STREAM_MIN_UPDATE_INTERVAL = float(os.getenv("STREAM_MIN_UPDATE_INTERVAL", "0.9"))  # Мин. интервал между редактированиями (защита от rate limit)
STREAM_EDIT_MIN_INTERVAL = float(os.getenv("STREAM_EDIT_MIN_INTERVAL", "0.6"))
STREAM_EDIT_MIN_DELTA_CHARS = int(os.getenv("STREAM_EDIT_MIN_DELTA_CHARS", "40"))
STREAM_MIN_TEXT_LEN = int(os.getenv("STREAM_MIN_TEXT_LEN", "80"))
STREAM_MAX_TEXT_LEN = int(os.getenv("STREAM_MAX_TEXT_LEN", "2200"))
STREAM_HTML_ANSWERS = os.getenv("STREAM_HTML_ANSWERS", "true").lower() == "true"
STREAM_COMPACT_ANSWERS = os.getenv("STREAM_COMPACT_ANSWERS", "true").lower() == "true"
TYPING_HEARTBEAT_INTERVAL = float(os.getenv("TYPING_HEARTBEAT_INTERVAL", "4.2"))
STATUS_MESSAGE_DELETE_DELAY = float(os.getenv("STATUS_MESSAGE_DELETE_DELAY", "0.7"))
OPENROUTER_HISTORY_LIMIT = int(os.getenv("OPENROUTER_HISTORY_LIMIT", "16"))
OPENROUTER_CHAT_MAX_TOKENS = int(os.getenv("OPENROUTER_CHAT_MAX_TOKENS", "2200"))
OPENROUTER_CHAT_TIMEOUT = int(os.getenv("OPENROUTER_CHAT_TIMEOUT", "120"))
HANDLE_MESSAGE_TIMEOUT = float(os.getenv("HANDLE_MESSAGE_TIMEOUT", "260") or "260")
HANDLE_VOICE_TIMEOUT = float(os.getenv("HANDLE_VOICE_TIMEOUT", "260") or "260")
OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_SIMPLE = float(os.getenv("OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_SIMPLE", "18") or "18")
OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_COMPLEX = float(os.getenv("OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_COMPLEX", "60") or "60")
OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_MATH = float(os.getenv("OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_MATH", "45") or "45")
OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_VISION = float(os.getenv("OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_VISION", "55") or "55")
OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_SIMPLE = float(os.getenv("OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_SIMPLE", "24") or "24")
OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_COMPLEX = float(os.getenv("OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_COMPLEX", "40") or "40")
OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_MATH = float(os.getenv("OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_MATH", "35") or "35")
OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_VISION = float(os.getenv("OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_VISION", "45") or "45")
OPENROUTER_MIN_FALLBACK_TIMEOUT = float(os.getenv("OPENROUTER_MIN_FALLBACK_TIMEOUT", "24") or "24")
MODEL_DEGRADED_COOLDOWN_SECONDS = int(os.getenv("MODEL_DEGRADED_COOLDOWN_SECONDS", "300") or "300")
MODEL_SLOW_RESPONSE_THRESHOLD = float(os.getenv("MODEL_SLOW_RESPONSE_THRESHOLD", "12") or "12")
OPENROUTER_KEYS_FILE = _env_str("OPENROUTER_KEYS_FILE", _env_str("OPENROUTER_PROXY_KEYS_FILE", "openrouter_keys.txt"))
OPENROUTER_KEY_RATE_LIMIT_COOLDOWN = int(os.getenv("OPENROUTER_KEY_RATE_LIMIT_COOLDOWN", "60") or "60")
OPENROUTER_KEY_PROVIDER_RATE_LIMIT_COOLDOWN = int(os.getenv("OPENROUTER_KEY_PROVIDER_RATE_LIMIT_COOLDOWN", "4") or "4")
OPENROUTER_KEY_SERVER_COOLDOWN = int(os.getenv("OPENROUTER_KEY_SERVER_COOLDOWN", "20") or "20")
OPENROUTER_KEY_TRANSPORT_COOLDOWN = int(os.getenv("OPENROUTER_KEY_TRANSPORT_COOLDOWN", "15") or "15")
OPENROUTER_KEY_CLIENT_COOLDOWN = int(os.getenv("OPENROUTER_KEY_CLIENT_COOLDOWN", "5") or "5")
OPENROUTER_KEY_POOL_MIN_PER_KEY_TIMEOUT = float(os.getenv("OPENROUTER_KEY_POOL_MIN_PER_KEY_TIMEOUT", "4") or "4")

TELEGRAM_TOKEN = _env_str(
    "MAIN_BOT_TOKEN",
    TELEGRAM_TOKEN or _env_str("TELEGRAM_TOKEN", _env_str("BOT_TOKEN", "")),
)
OPENROUTER_API_KEY = _env_str("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
OPENROUTER_DIRECT_URL = _env_str("OPENROUTER_URL", OPENROUTER_DIRECT_URL)
OPENROUTER_PROXY_URL = _env_str("OPENROUTER_PROXY_URL", OPENROUTER_PROXY_URL)
OPENROUTER_PROXY_TOKEN = _env_str("OPENROUTER_PROXY_TOKEN", OPENROUTER_PROXY_TOKEN)
OPENROUTER_URL = OPENROUTER_PROXY_URL or OPENROUTER_DIRECT_URL
OPENWEATHER_API_KEY = _env_str("OPENWEATHER_API_KEY", OPENWEATHER_API_KEY)
if not OWNER_ID:
    OWNER_ID = int(os.environ.get("OWNER_ID", "0") or "0")

MODEL_FAST_TEXT = _env_str("MODEL_FAST_TEXT", MODEL_FAST_TEXT)
MODEL_STRONG_TEXT = _env_str("MODEL_STRONG_TEXT", MODEL_STRONG_TEXT or OPENROUTER_MODEL)
MODEL_VISION = _env_str("MODEL_VISION", MODEL_VISION or OPENROUTER_VISION_MODEL or OPENROUTER_MODEL)
MODEL_MATH = _env_str("MODEL_MATH", MODEL_MATH or MODEL_STRONG_TEXT)
MODEL_BACKUP_TEXT = _env_str("MODEL_BACKUP_TEXT", MODEL_BACKUP_TEXT)
OPENROUTER_VISION_MODEL = MODEL_VISION

def _parse_id_csv(raw: str | None) -> set[int]:
    result: set[int] = set()
    for chunk in re.split(r"[\s,;]+", raw or ""):
        part = (chunk or "").strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


ADMIN_USER_IDS: set[int] = _parse_id_csv(os.getenv("ADMIN_USER_IDS", ""))
if OWNER_ID:
    ADMIN_USER_IDS.add(int(OWNER_ID))
ADMIN_MODEL_CALLBACK_PREFIX = "admin_model|strong|"


def get_admin_model_presets() -> list[str]:
    presets: list[str] = []
    for candidate in (
        DEFAULT_MAINBOT_MODEL,
        MODEL_STRONG_TEXT,
        OPENROUTER_MODEL,
        MODEL_FAST_TEXT,
        MODEL_MATH,
        MODEL_BACKUP_TEXT,
        MODEL_VISION,
    ):
        normalized = (candidate or "").strip()
        if not normalized or normalized in presets:
            continue
        presets.append(normalized)
    return presets


def get_admin_models_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    presets = get_admin_model_presets()
    for idx, model_id in enumerate(presets):
        marker = "✅ " if model_id == MODEL_STRONG_TEXT else ""
        label = f"{marker}{model_id}"
        if len(label) > 34:
            label = label[:31] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"{ADMIN_MODEL_CALLBACK_PREFIX}{idx}")])
    return InlineKeyboardMarkup(rows)


def get_openrouter_request_url() -> str:
    return (OPENROUTER_PROXY_URL or OPENROUTER_DIRECT_URL or OPENROUTER_URL).strip()


def is_openrouter_proxy_enabled() -> bool:
    return bool(OPENROUTER_PROXY_URL)


def has_openrouter_auth_config() -> bool:
    return bool(OPENROUTER_PROXY_URL or OPENROUTER_API_KEY or OPENROUTER_KEYS_FILE)


_OPENROUTER_KEY_POOL_LOCK = ThreadLock()
_OPENROUTER_KEY_POOL: list[dict[str, Any]] = []


def _build_openrouter_key_entry(
    label: str,
    api_key: str,
    *,
    enabled: bool = True,
    status: str = "active",
    cooldown_until: float = 0.0,
    success_count: int = 0,
    failure_count: int = 0,
    last_error: str = "",
    last_status: int | None = None,
) -> dict[str, Any]:
    return {
        "label": (label or "key").strip(),
        "api_key": (api_key or "").strip(),
        "enabled": bool(enabled),
        "status": (status or "active").strip(),
        "cooldown_until": float(cooldown_until or 0.0),
        "success_count": int(success_count or 0),
        "failure_count": int(failure_count or 0),
        "last_error": (last_error or "").strip(),
        "last_status": int(last_status) if last_status is not None else None,
    }


def _iter_configured_openrouter_keys() -> list[tuple[str, str]]:
    configured: list[tuple[str, str]] = []
    if OPENROUTER_API_KEY:
        configured.append(("main", OPENROUTER_API_KEY))

    keys_path = (OPENROUTER_KEYS_FILE or "").strip()
    if keys_path:
        try:
            with open(keys_path, "r", encoding="utf-8") as fh:
                for index, raw_line in enumerate(fh, start=1):
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        label, api_key = line.split("=", 1)
                    else:
                        label, api_key = f"key{index}", line
                    normalized_key = (api_key or "").strip()
                    if normalized_key:
                        configured.append(((label or f"key{index}").strip(), normalized_key))
        except FileNotFoundError:
            pass
        except Exception:
            logging.exception("Failed to read OpenRouter keys file: %s", keys_path)
    return configured


def reload_openrouter_key_pool() -> dict[str, Any]:
    configured = _iter_configured_openrouter_keys()
    existing_by_key: dict[str, dict[str, Any]] = {}
    with _OPENROUTER_KEY_POOL_LOCK:
        for item in _OPENROUTER_KEY_POOL:
            api_key = str(item.get("api_key") or "").strip()
            if api_key:
                existing_by_key[api_key] = dict(item)

        refreshed: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for label, api_key in configured:
            normalized_key = (api_key or "").strip()
            if not normalized_key or normalized_key in seen_keys:
                continue
            seen_keys.add(normalized_key)
            previous = existing_by_key.get(normalized_key, {})
            refreshed.append(
                _build_openrouter_key_entry(
                    label,
                    normalized_key,
                    enabled=bool(previous.get("enabled", True)),
                    status=str(previous.get("status") or "active"),
                    cooldown_until=float(previous.get("cooldown_until", 0.0) or 0.0),
                    success_count=int(previous.get("success_count", 0) or 0),
                    failure_count=int(previous.get("failure_count", 0) or 0),
                    last_error=str(previous.get("last_error") or ""),
                    last_status=previous.get("last_status"),
                )
            )
        _OPENROUTER_KEY_POOL[:] = refreshed
        snapshot = [dict(item) for item in _OPENROUTER_KEY_POOL]

    logging.info("OpenRouter direct key sync complete: %s keys loaded", len(snapshot))
    return {
        "loaded_keys": len(snapshot),
        "keys": snapshot,
        "stats": get_openrouter_keys_stats(snapshot),
    }


def _get_openrouter_key_pool_snapshot() -> list[dict[str, Any]]:
    with _OPENROUTER_KEY_POOL_LOCK:
        return [dict(item) for item in _OPENROUTER_KEY_POOL]


def get_openrouter_keys_stats(snapshot: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    keys = snapshot if snapshot is not None else _get_openrouter_key_pool_snapshot()
    now = time.time()
    buckets = {
        "active": 0,
        "cooldown": 0,
        "disabled": 0,
    }
    for item in keys:
        if not item.get("enabled", True):
            buckets["disabled"] += 1
        elif float(item.get("cooldown_until", 0.0) or 0.0) > now:
            buckets["cooldown"] += 1
        else:
            buckets["active"] += 1
    return {"total": len(keys), "buckets": buckets}


def get_openrouter_keys_snapshot() -> dict[str, Any]:
    snapshot = _get_openrouter_key_pool_snapshot()
    now = time.time()
    keys: list[dict[str, Any]] = []
    for item in snapshot:
        cooldown_until = float(item.get("cooldown_until", 0.0) or 0.0)
        key_view = dict(item)
        key_view.pop("api_key", None)
        key_view["cooldown_seconds"] = max(0, int(cooldown_until - now))
        keys.append(key_view)
    return {
        "keys": keys,
        "stats": get_openrouter_keys_stats(snapshot),
    }


def _touch_openrouter_key(
    label: str,
    *,
    enabled: bool | None = None,
    status: str | None = None,
    cooldown_seconds: int | None = None,
    last_error: str | None = None,
    last_status: int | None = None,
    success_delta: int = 0,
    failure_delta: int = 0,
) -> None:
    now = time.time()
    with _OPENROUTER_KEY_POOL_LOCK:
        for item in _OPENROUTER_KEY_POOL:
            if str(item.get("label") or "") != str(label):
                continue
            if enabled is not None:
                item["enabled"] = bool(enabled)
            if status is not None:
                item["status"] = status
            if cooldown_seconds is not None:
                item["cooldown_until"] = now + max(0, int(cooldown_seconds))
            if last_error is not None:
                item["last_error"] = (last_error or "")[:500]
            if last_status is not None:
                item["last_status"] = int(last_status)
            if success_delta:
                item["success_count"] = int(item.get("success_count", 0) or 0) + int(success_delta)
            if failure_delta:
                item["failure_count"] = int(item.get("failure_count", 0) or 0) + int(failure_delta)
            return


def mark_openrouter_key_success(label: str) -> None:
    _touch_openrouter_key(
        label,
        enabled=True,
        status="active",
        cooldown_seconds=0,
        last_error="",
        last_status=200,
        success_delta=1,
    )


def mark_openrouter_key_failure(
    label: str,
    *,
    status_code: int | None = None,
    error_text: str = "",
    cooldown_seconds_override: int | None = None,
    status_override: str | None = None,
) -> None:
    status = int(status_code or 0)
    error_text = (error_text or "").strip()
    if status in {401, 402, 403}:
        _touch_openrouter_key(
            label,
            enabled=False,
            status=status_override or ("disabled" if status in {401, 403} else "budget_exhausted"),
            last_error=error_text,
            last_status=status or None,
            failure_delta=1,
        )
        return
    if status == 429:
        _touch_openrouter_key(
            label,
            enabled=True,
            status=status_override or "rate_limited",
            cooldown_seconds=cooldown_seconds_override if cooldown_seconds_override is not None else OPENROUTER_KEY_RATE_LIMIT_COOLDOWN,
            last_error=error_text,
            last_status=status,
            failure_delta=1,
        )
        return
    if status in {0, 408, 409, 425, 598, 599}:
        _touch_openrouter_key(
            label,
            enabled=True,
            status=status_override or "transport_error",
            cooldown_seconds=cooldown_seconds_override if cooldown_seconds_override is not None else OPENROUTER_KEY_TRANSPORT_COOLDOWN,
            last_error=error_text,
            last_status=status or None,
            failure_delta=1,
        )
        return
    if 500 <= status <= 599:
        _touch_openrouter_key(
            label,
            enabled=True,
            status=status_override or "upstream_error",
            cooldown_seconds=cooldown_seconds_override if cooldown_seconds_override is not None else OPENROUTER_KEY_SERVER_COOLDOWN,
            last_error=error_text,
            last_status=status,
            failure_delta=1,
        )
        return
    _touch_openrouter_key(
        label,
        enabled=True,
        status=status_override or "request_error",
        cooldown_seconds=cooldown_seconds_override if cooldown_seconds_override is not None else OPENROUTER_KEY_CLIENT_COOLDOWN,
        last_error=error_text,
        last_status=status or None,
        failure_delta=1,
    )


def get_openrouter_key_candidates() -> list[dict[str, Any]]:
    snapshot = _get_openrouter_key_pool_snapshot()
    if not snapshot:
        with contextlib.suppress(Exception):
            reload_openrouter_key_pool()
        snapshot = _get_openrouter_key_pool_snapshot()
    now = time.time()
    active = [item for item in snapshot if item.get("enabled", True) and float(item.get("cooldown_until", 0.0) or 0.0) <= now]
    if active:
        return active
    terminal_statuses = {"daily_limit", "budget_exhausted", "disabled"}
    enabled = [
        item
        for item in snapshot
        if item.get("enabled", True) and str(item.get("status") or "").strip().lower() not in terminal_statuses
    ]
    if enabled:
        return sorted(enabled, key=lambda item: float(item.get("cooldown_until", 0.0) or 0.0))
    enabled = [item for item in snapshot if item.get("enabled", True)]
    return enabled or snapshot


def should_rotate_openrouter_key_after_response(status_code: int | None, response_text: str | None) -> bool:
    status = int(status_code or 0)
    if status in {401, 402, 403, 408, 409, 425, 429, 500, 502, 503, 504, 598, 599}:
        return True
    normalized = (response_text or "").lower()
    return (
        "rate limit" in normalized
        or "temporarily unavailable" in normalized
        or "credit" in normalized
        or "quota" in normalized
        or "budget" in normalized
    )


def extract_openrouter_error_payload(response: httpx.Response | None) -> tuple[int | None, str]:
    if response is None:
        return None, ""
    with contextlib.suppress(Exception):
        payload = response.json()
        if isinstance(payload, dict):
            error_obj = payload.get("error")
            if isinstance(error_obj, dict):
                code_raw = error_obj.get("code") or error_obj.get("status") or response.status_code
                try:
                    code = int(code_raw)
                except Exception:
                    code = int(getattr(response, "status_code", 0) or 0) or None
                message = str(error_obj.get("message") or error_obj.get("detail") or "").strip()
                if message:
                    return code, message
    return int(getattr(response, "status_code", 0) or 0) or None, (getattr(response, "text", "") or "")[:2000]


def get_openrouter_rate_limit_policy(response: httpx.Response | None, status_code: int | None, error_text: str | None) -> tuple[int | None, str | None]:
    status = int(status_code or 0)
    if status != 429 or response is None:
        return None, None

    normalized = (error_text or "").lower()
    if "temporarily rate-limited upstream" in normalized or "please retry shortly" in normalized:
        return OPENROUTER_KEY_PROVIDER_RATE_LIMIT_COOLDOWN, "provider_rate_limited"
    if "free-models-per-day" not in normalized and "x-ratelimit-reset" not in normalized:
        with contextlib.suppress(Exception):
            payload = response.json()
            error_obj = payload.get("error") if isinstance(payload, dict) else None
            metadata = error_obj.get("metadata") if isinstance(error_obj, dict) else None
            headers = metadata.get("headers") if isinstance(metadata, dict) else None
            if isinstance(headers, dict):
                reset_raw = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
                if reset_raw:
                    normalized += " x-ratelimit-reset"
            provider_name = str(metadata.get("provider_name") or "").strip().lower() if isinstance(metadata, dict) else ""
            raw_message = str(metadata.get("raw") or "").strip().lower() if isinstance(metadata, dict) else ""
            if provider_name and "temporarily rate-limited upstream" in raw_message:
                return OPENROUTER_KEY_PROVIDER_RATE_LIMIT_COOLDOWN, "provider_rate_limited"
    if "free-models-per-day" not in normalized and "x-ratelimit-reset" not in normalized:
        return None, None

    reset_raw = None
    with contextlib.suppress(Exception):
        payload = response.json()
        error_obj = payload.get("error") if isinstance(payload, dict) else None
        metadata = error_obj.get("metadata") if isinstance(error_obj, dict) else None
        headers = metadata.get("headers") if isinstance(metadata, dict) else None
        if isinstance(headers, dict):
            reset_raw = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")

    cooldown_seconds = None
    if reset_raw is not None:
        with contextlib.suppress(Exception):
            reset_ts = float(reset_raw)
            if reset_ts > 10_000_000_000:
                reset_ts = reset_ts / 1000.0
            cooldown_seconds = max(60, int(reset_ts - time.time()))
    if cooldown_seconds is None:
        cooldown_seconds = 3600
    return cooldown_seconds, "daily_limit"


def build_openrouter_headers(api_key: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    selected_key = (api_key or "").strip()
    if selected_key:
        headers["Authorization"] = f"Bearer {selected_key}"
    elif is_openrouter_proxy_enabled():
        if OPENROUTER_PROXY_TOKEN:
            headers["Authorization"] = f"Bearer {OPENROUTER_PROXY_TOKEN}"
    elif OPENROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {OPENROUTER_API_KEY}"
    return headers


with contextlib.suppress(Exception):
    reload_openrouter_key_pool()


def get_admin_user_ids() -> set[int]:
    ids = set(ADMIN_USER_IDS)
    if OWNER_ID:
        ids.add(int(OWNER_ID))
    return ids


def is_admin_user_id(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return int(user_id) in get_admin_user_ids()


def _normalize_model_routing_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def classify_task_type(question: str, has_image: bool) -> Literal["vision", "simple", "complex", "math"]:
    if has_image:
        return "vision"

    normalized = _normalize_model_routing_text(question)
    if not normalized:
        return "simple"

    math_markers = (
        "формула",
        "интеграл",
        "производн",
        "закон ома",
        "реши",
        "решение",
        "вычисли",
        "посчитай",
        "чему рав",
    )
    if re.search(r"[=+\-/*^]|√|π|∫", normalized) or any(marker in normalized for marker in math_markers):
        return "math"

    complex_markers = (
        "подробно",
        "сравни",
        "проанализируй",
        "архитектур",
        "по шагам",
        "обоснуй",
        "почему",
        "докажи",
        "стратег",
        "оптимиз",
    )
    if len(normalized) > 140 or len(normalized.split()) >= 20:
        return "complex"
    if "\n" in question:
        return "complex"
    if any(marker in normalized for marker in complex_markers):
        return "complex"
    return "simple"


def choose_model(question: str, has_image: bool) -> str:
    task_type = classify_task_type(question, has_image)
    if task_type == "vision":
        return MODEL_VISION or OPENROUTER_VISION_MODEL or OPENROUTER_MODEL
    if task_type == "math":
        return MODEL_MATH or MODEL_STRONG_TEXT or OPENROUTER_MODEL
    if task_type == "simple":
        fast_candidate = (MODEL_FAST_TEXT or "").strip()
        backup_candidate = (MODEL_BACKUP_TEXT or "").strip()
        if not _MODEL_FAST_TEXT_EXPLICIT and fast_candidate and backup_candidate and fast_candidate == OPENROUTER_MODEL:
            return backup_candidate
        return fast_candidate or MODEL_STRONG_TEXT or OPENROUTER_MODEL
    return MODEL_STRONG_TEXT or OPENROUTER_MODEL


def is_model_not_found_response(status_code: int | None, response_text: str | None) -> bool:
    if int(status_code or 0) != 404:
        return False
    normalized = (response_text or "").lower()
    return "no endpoints found" in normalized or '"code":404' in normalized


def fallback_model_for_unavailable(preferred_model: str | None) -> str | None:
    preferred = (preferred_model or "").strip()
    candidates = [
        (MODEL_BACKUP_TEXT or "").strip(),
        (MODEL_STRONG_TEXT or "").strip(),
        (OPENROUTER_MODEL or "").strip(),
        DEFAULT_MAINBOT_MODEL,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if preferred and candidate == preferred:
            continue
        return candidate
    return None


def fallback_model_for_runtime_issue(preferred_model: str | None, task_type: str | None = None) -> str | None:
    preferred = (preferred_model or "").strip()
    candidates: list[str] = []
    fast_candidate = (MODEL_FAST_TEXT or "").strip()
    backup_candidate = (MODEL_BACKUP_TEXT or "").strip()
    if not _MODEL_FAST_TEXT_EXPLICIT and fast_candidate and backup_candidate and fast_candidate == OPENROUTER_MODEL:
        fast_candidate = backup_candidate
    if (task_type or "").strip() == "simple":
        candidates.extend([
            fast_candidate,
            backup_candidate,
            DEFAULT_MAINBOT_MODEL,
            (MODEL_STRONG_TEXT or "").strip(),
            (OPENROUTER_MODEL or "").strip(),
            fast_candidate,
        ])
    else:
        candidates.extend([
            backup_candidate,
            (MODEL_STRONG_TEXT or "").strip(),
            DEFAULT_MAINBOT_MODEL,
            (OPENROUTER_MODEL or "").strip(),
            fast_candidate,
        ])
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if preferred and candidate == preferred:
            continue
        return candidate
    return None


_MODEL_DEGRADED_UNTIL: dict[str, float] = {}
_MODEL_DEGRADED_LOCK = ThreadLock()


def is_model_temporarily_degraded(model_id: str | None) -> bool:
    normalized = (model_id or "").strip()
    if not normalized:
        return False
    now = time.time()
    with _MODEL_DEGRADED_LOCK:
        expires_at = float(_MODEL_DEGRADED_UNTIL.get(normalized, 0.0) or 0.0)
        if expires_at <= now:
            _MODEL_DEGRADED_UNTIL.pop(normalized, None)
            return False
        return True


def mark_model_temporarily_degraded(model_id: str | None, reason: str, cooldown_seconds: int | None = None) -> None:
    normalized = (model_id or "").strip()
    if not normalized:
        return
    cooldown = int(cooldown_seconds or MODEL_DEGRADED_COOLDOWN_SECONDS or 300)
    expires_at = time.time() + max(30, cooldown)
    with _MODEL_DEGRADED_LOCK:
        _MODEL_DEGRADED_UNTIL[normalized] = expires_at
    logging.warning("Model cooldown enabled: model=%s cooldown=%ss reason=%s", normalized, cooldown, reason)


def clear_model_degraded_flag(model_id: str | None) -> None:
    normalized = (model_id or "").strip()
    if not normalized:
        return
    with _MODEL_DEGRADED_LOCK:
        _MODEL_DEGRADED_UNTIL.pop(normalized, None)


def get_effective_model_for_request(preferred_model: str | None, task_type: str | None = None) -> str | None:
    preferred = (preferred_model or "").strip()
    if preferred and not is_model_temporarily_degraded(preferred):
        return preferred
    fallback_model = fallback_model_for_runtime_issue(preferred, task_type=task_type)
    if preferred and fallback_model and fallback_model != preferred:
        logging.info("ModelRouter bypass: model=%s is on cooldown, using %s", preferred, fallback_model)
    return fallback_model or preferred


def get_primary_attempt_timeout(total_timeout: float, task_type: str | None = None) -> float:
    normalized = (task_type or "").strip().lower()
    if normalized == "simple":
        preferred = OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_SIMPLE
    elif normalized == "math":
        preferred = OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_MATH
    elif normalized == "vision":
        preferred = OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_VISION
    else:
        preferred = OPENROUTER_PRIMARY_ATTEMPT_TIMEOUT_COMPLEX
    total = max(8.0, float(total_timeout or OPENROUTER_CHAT_TIMEOUT or 120))
    return max(8.0, min(total, float(preferred or total)))


def get_fallback_attempt_timeout(total_timeout: float, task_type: str | None = None) -> float:
    normalized = (task_type or "").strip().lower()
    if normalized == "simple":
        preferred = OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_SIMPLE
    elif normalized == "math":
        preferred = OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_MATH
    elif normalized == "vision":
        preferred = OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_VISION
    else:
        preferred = OPENROUTER_FALLBACK_ATTEMPT_TIMEOUT_COMPLEX
    total = max(8.0, float(total_timeout or OPENROUTER_CHAT_TIMEOUT or 120))
    return max(8.0, min(total, float(preferred or OPENROUTER_MIN_FALLBACK_TIMEOUT or total)))


def get_attempt_request_timeout(
    total_timeout: float,
    *,
    task_type: str | None = None,
    attempt_index: int = 0,
    started_at: float | None = None,
) -> float:
    total = max(8.0, float(total_timeout or OPENROUTER_CHAT_TIMEOUT or 120))
    elapsed = max(0.0, time.perf_counter() - started_at) if started_at else 0.0
    remaining = max(8.0, total - elapsed)
    if attempt_index <= 0:
        return max(8.0, min(remaining, get_primary_attempt_timeout(total, task_type=task_type)))
    return max(8.0, min(remaining, get_fallback_attempt_timeout(total, task_type=task_type)))


def is_empty_success_openrouter_response(response: httpx.Response | None) -> bool:
    if response is None:
        return False
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        return False
    with contextlib.suppress(Exception):
        return not bool((getattr(response, "text", "") or "").strip())
    return False


def is_empty_openrouter_reply(reply: str | None) -> bool:
    normalized = (reply or "").strip()
    return normalized in {"", "{}", "[]", "null"}


def should_retry_model_after_response(status_code: int | None, response_text: str | None) -> bool:
    status = int(status_code or 0)
    if status in {408, 409, 425, 429, 500, 502, 503, 504, 598, 599}:
        return True
    normalized = (response_text or "").lower()
    return (
        "temporarily unavailable" in normalized
        or "temporarily unavailable" in normalized
        or "rate limit" in normalized
        or "upstream_unavailable" in normalized
        or "all openrouter proxy keys are temporarily unavailable" in normalized
    )


def get_local_smalltalk_reply(text: str | None) -> str | None:
    normalized = _normalize_model_routing_text(text)
    if not normalized:
        return None
    normalized = normalized.strip("!?.,:; ")

    if normalized in {"привет", "здравствуй", "здравствуйте", "хай", "hello", "hi"}:
        return "Привет! Чем могу помочь? 🙂"
    if normalized in {"как дела", "как твои дела", "как у тебя дела"}:
        return "У меня все хорошо, спасибо 🙂 Чем помочь?"
    if normalized in {"спасибо", "спс", "благодарю"}:
        return "Пожалуйста 🙂 Если хочешь, можем продолжить."
    return None


_OPENROUTER_RUNTIME_LOCK = ThreadLock()
_OPENROUTER_ACTIVE_REQUESTS = 0
_OPENROUTER_LATENCIES_MS: deque[float] = deque(maxlen=50)
_OPENROUTER_ERROR_COUNTS: dict[str, int] = {"402": 0, "429": 0}


def _openrouter_runtime_request_started() -> float:
    global _OPENROUTER_ACTIVE_REQUESTS
    started_at = time.perf_counter()
    with _OPENROUTER_RUNTIME_LOCK:
        _OPENROUTER_ACTIVE_REQUESTS += 1
    return started_at


def _openrouter_runtime_request_finished(started_at: float, status_code: int | None) -> None:
    global _OPENROUTER_ACTIVE_REQUESTS
    elapsed_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
    with _OPENROUTER_RUNTIME_LOCK:
        _OPENROUTER_ACTIVE_REQUESTS = max(0, _OPENROUTER_ACTIVE_REQUESTS - 1)
        _OPENROUTER_LATENCIES_MS.append(elapsed_ms)
        if status_code in (402, 429):
            _OPENROUTER_ERROR_COUNTS[str(status_code)] = _OPENROUTER_ERROR_COUNTS.get(str(status_code), 0) + 1


def get_openrouter_runtime_snapshot() -> dict[str, Any]:
    with _OPENROUTER_RUNTIME_LOCK:
        latencies = list(_OPENROUTER_LATENCIES_MS)
        avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0
        return {
            "active_requests": int(_OPENROUTER_ACTIVE_REQUESTS),
            "avg_latency_ms": float(avg_latency_ms),
            "sample_size": int(len(latencies)),
            "errors_402": int(_OPENROUTER_ERROR_COUNTS.get("402", 0)),
            "errors_429": int(_OPENROUTER_ERROR_COUNTS.get("429", 0)),
        }


def _get_proxy_admin_base_url() -> str | None:
    target = (OPENROUTER_PROXY_URL or "").strip()
    if not target:
        return None
    try:
        parsed = urllib.parse.urlparse(target)
    except Exception:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def _build_proxy_admin_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if OPENROUTER_PROXY_TOKEN:
        headers["Authorization"] = f"Bearer {OPENROUTER_PROXY_TOKEN}"
    return headers


async def fetch_proxy_health() -> dict[str, Any] | None:
    base_url = _get_proxy_admin_base_url()
    if not base_url:
        return None
    client = await async_get_httpx_client(timeout=8)
    response = await client.get(f"{base_url}/health", headers=_build_proxy_admin_headers())
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


async def fetch_proxy_keys_snapshot() -> dict[str, Any] | None:
    base_url = _get_proxy_admin_base_url()
    if not base_url:
        return None
    client = await async_get_httpx_client(timeout=8)
    response = await client.get(f"{base_url}/internal/keys", headers=_build_proxy_admin_headers())
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


async def reload_proxy_keys_snapshot() -> dict[str, Any] | None:
    base_url = _get_proxy_admin_base_url()
    if not base_url:
        return None
    client = await async_get_httpx_client(timeout=10)
    response = await client.post(f"{base_url}/internal/reload-keys", headers=_build_proxy_admin_headers())
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def _get_local_openrouter_proxy_health_url() -> str | None:
    if not OPENROUTER_PROXY_URL:
        return None
    try:
        parsed = urllib.parse.urlparse(OPENROUTER_PROXY_URL)
    except Exception:
        return None

    host = (parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost"}:
        return None

    scheme = (parsed.scheme or "http").strip().lower() or "http"
    port = parsed.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{port}/health"


_OPENROUTER_PROXY_PROCESS: Any = None
_OPENROUTER_PROXY_PROCESS_LOCK = ThreadLock()
_OPENROUTER_PROXY_MONITOR_THREAD: Thread | None = None
_OPENROUTER_PROXY_MONITOR_STOP = Event()


def _is_openrouter_proxy_healthy(health_url: str | None = None, timeout: float = 3.0) -> bool:
    target = health_url or _get_local_openrouter_proxy_health_url()
    if not target:
        return True
    try:
        response = httpx.get(target, timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def _spawn_local_openrouter_proxy():
    global _OPENROUTER_PROXY_PROCESS
    proxy_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openrouter_proxy.py")
    if not os.path.exists(proxy_script):
        logging.error("Local OpenRouter proxy script not found: %s", proxy_script)
        return None

    popen_kwargs: dict[str, Any] = {
        "cwd": os.path.dirname(proxy_script),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )

    try:
        process = subprocess.Popen([sys.executable, proxy_script], **popen_kwargs)
        with _OPENROUTER_PROXY_PROCESS_LOCK:
            _OPENROUTER_PROXY_PROCESS = process
        logging.info("Spawned local OpenRouter proxy process pid=%s", getattr(process, "pid", "?"))
        return process
    except Exception:
        logging.exception("Failed to start local OpenRouter proxy")
        return None


def ensure_openrouter_proxy_running(start_timeout: float = 15.0) -> bool:
    global _OPENROUTER_PROXY_PROCESS
    health_url = _get_local_openrouter_proxy_health_url()
    if not health_url:
        return True

    if _is_openrouter_proxy_healthy(health_url):
        return True

    process = None
    with _OPENROUTER_PROXY_PROCESS_LOCK:
        managed_process = _OPENROUTER_PROXY_PROCESS
        if managed_process is not None and getattr(managed_process, "poll", None):
            if managed_process.poll() is not None:
                _OPENROUTER_PROXY_PROCESS = None
            else:
                process = managed_process

    if process is None:
        logging.warning("Local OpenRouter proxy is not responding, attempting to start it automatically")
        process = _spawn_local_openrouter_proxy()
        if process is None:
            return False
    else:
        logging.warning("Local OpenRouter proxy health check failed, waiting for managed process pid=%s", getattr(process, "pid", "?"))

    deadline = time.time() + max(3.0, float(start_timeout))
    while time.time() < deadline:
        if _is_openrouter_proxy_healthy(health_url):
            logging.info("Local OpenRouter proxy became ready at %s", health_url)
            return True
        if getattr(process, "poll", None) and process.poll() is not None:
            with _OPENROUTER_PROXY_PROCESS_LOCK:
                if _OPENROUTER_PROXY_PROCESS is process:
                    _OPENROUTER_PROXY_PROCESS = None
            break
        time.sleep(0.5)

    logging.error("Local OpenRouter proxy did not become ready within %.1f seconds", start_timeout)
    return False


def _shutdown_managed_openrouter_proxy() -> None:
    global _OPENROUTER_PROXY_PROCESS
    process = None
    with _OPENROUTER_PROXY_PROCESS_LOCK:
        process = _OPENROUTER_PROXY_PROCESS
        _OPENROUTER_PROXY_PROCESS = None

    if process is None:
        return

    try:
        if getattr(process, "poll", None) and process.poll() is None:
            logging.info("Stopping managed OpenRouter proxy pid=%s", getattr(process, "pid", "?"))
            process.terminate()
            process.wait(timeout=5)
    except Exception:
        with contextlib.suppress(Exception):
            process.kill()


def _openrouter_proxy_monitor_worker() -> None:
    interval = max(5.0, float(OPENROUTER_PROXY_MONITOR_INTERVAL))
    while not _OPENROUTER_PROXY_MONITOR_STOP.wait(interval):
        try:
            if not ensure_openrouter_proxy_running(start_timeout=10.0):
                logging.error("OpenRouter proxy monitor could not restore local proxy connectivity")
        except Exception:
            logging.exception("OpenRouter proxy monitor failed")


def start_openrouter_proxy_supervisor() -> None:
    global _OPENROUTER_PROXY_MONITOR_THREAD
    if not _get_local_openrouter_proxy_health_url():
        return
    thread = _OPENROUTER_PROXY_MONITOR_THREAD
    if thread is not None and thread.is_alive():
        return

    _OPENROUTER_PROXY_MONITOR_STOP.clear()
    thread = Thread(target=_openrouter_proxy_monitor_worker, name="openrouter-proxy-monitor", daemon=True)
    _OPENROUTER_PROXY_MONITOR_THREAD = thread
    thread.start()
    logging.info("Started OpenRouter proxy supervisor thread")


def stop_openrouter_proxy_supervisor() -> None:
    _OPENROUTER_PROXY_MONITOR_STOP.set()
    _shutdown_managed_openrouter_proxy()

# ------------------------ Файлы хранения ------------------------
USER_FILE = "users.json"
BANNED_FILE = "banned.json"
ACTIVITY_FILE = "user_activity.json"
CONVERSATIONS_FILE = "conversations.json"
CHAT_STORE_FILE = "chat_sessions.db"

# ------------------------ Имя процесса / логирование ------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot_errors.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

def log_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error("Необработанное исключение", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = log_exception


def log_user_action(user_id: int, action: str, **extra: Any) -> None:
    """Пишет в лог user_id и тип действия для отладки и анализа сбоев."""
    parts = [f"user_id={user_id}", f"action={action}"]
    for k, v in extra.items():
        safe = str(v)[:200] if v is not None else ""
        parts.append(f"{k}={safe}")
    logging.info(" | ".join(parts))


# ======================= Хранилища =======================
user_histories: dict[str, list] = {}
guess_games: dict[int, int] = {}

# ------------------------ Утилиты для хранения и кэша ------------------------
class UsersStore:
    def __init__(self, path=USER_FILE):
        self.path = path
        self._data = {} 
        self._dirty = False
        self._lock = asyncio.Lock()
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}
        else:
            self._data = {}

    async def save_if_needed(self):
        async with self._lock:
            if not self._dirty:
                return
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                self._dirty = False
            except Exception as e:
                logging.exception("UsersStore save error: %s", e)

    def get_all(self):
        return self._data

    def get_user(self, user_id: int):
        uid = str(user_id)
        return self._data.get(uid, {})

    def ensure_user(self, user_id: int, username=None, first_name=None):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {
                "username": username,
                "first_name": first_name,
                "joined": datetime.now().isoformat(),
                "daily_enabled": True,
                "voice": False,
            }
            self._dirty = True
        return self._data[uid]

    def set_field(self, user_id: int, key: str, value):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid][key] = value
        self._dirty = True

    def toggle_voice(self, user_id: int) -> bool:
        uid = str(user_id)
        if uid not in self._data:
            self.ensure_user(user_id)
        current = bool(self._data[uid].get("voice", False))
        self._data[uid]["voice"] = not current
        self._dirty = True
        return self._data[uid]["voice"]

    def save_now(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            self._dirty = False
        except Exception as e:
            logging.exception("UsersStore save_now error: %s", e)

def _normalize_chat_title_text(value: str | None, fallback: str = "Новый чат", max_len: int = 60) -> str:
    text = re.sub(r"\s+", " ", (value or "")).strip()
    return (text[:max_len].strip() or fallback)


def _derive_auto_chat_title(value: str | None, fallback: str = "Новый чат") -> str:
    text = clean_reply_for_display(value or "")
    if not text:
        return fallback
    first_line = text.splitlines()[0].strip()
    first_line = re.sub(r"^(👤|🤖|📷|🎤)\s*", "", first_line).strip()
    first_line = re.sub(r"\s+", " ", first_line)
    return (first_line[:48].strip(" .,:;!-") or fallback)[:48]


class ConversationStore:
    def __init__(self, path=CHAT_STORE_FILE, legacy_path=CONVERSATIONS_FILE):
        self.path = path
        self.legacy_path = legacy_path
        self._lock = ThreadLock()
        self._init_db()
        self._migrate_legacy_json_if_needed()

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    title_locked INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    last_preview TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_state (
                    user_id INTEGER PRIMARY KEY,
                    active_chat_id INTEGER NOT NULL,
                    FOREIGN KEY(active_chat_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_key TEXT NOT NULL,
                    source_msg_id INTEGER,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    summary TEXT,
                    tags TEXT,
                    importance REAL DEFAULT 0.5,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated ON chat_sessions(user_id, updated_at DESC, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_memory_lookup ON chat_memory(user_id, chat_key, created_at DESC)")
            conn.commit()

    def _generate_public_id(self, conn) -> str:
        for _ in range(32):
            public_id = str(random.randint(100000, 999999))
            exists = conn.execute("SELECT 1 FROM chat_sessions WHERE public_id = ?", (public_id,)).fetchone()
            if not exists:
                return public_id
        return str(int(time.time() * 1000))[-6:]

    def _row_to_chat(self, row, active_chat_id: int | None = None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "public_id": str(row["public_id"]),
            "user_id": int(row["user_id"]),
            "title": row["title"] or "Новый чат",
            "title_locked": bool(row["title_locked"]),
            "summary": row["summary"] or "",
            "last_preview": row["last_preview"] or "",
            "message_count": int(row["message_count"] or 0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "is_active": bool(active_chat_id is not None and int(row["id"]) == int(active_chat_id)),
        }

    def _get_active_chat_id_conn(self, conn, user_id: int) -> int | None:
        row = conn.execute("SELECT active_chat_id FROM chat_state WHERE user_id = ?", (int(user_id),)).fetchone()
        return int(row["active_chat_id"]) if row else None

    def _get_chat_conn(self, conn, user_id: int, chat_id: int):
        return conn.execute(
            "SELECT * FROM chat_sessions WHERE user_id = ? AND id = ?",
            (int(user_id), int(chat_id)),
        ).fetchone()

    def _set_active_chat_conn(self, conn, user_id: int, chat_id: int) -> None:
        conn.execute(
            """
            INSERT INTO chat_state(user_id, active_chat_id)
            VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET active_chat_id = excluded.active_chat_id
            """,
            (int(user_id), int(chat_id)),
        )

    def _create_chat_conn(self, conn, user_id: int, title: str | None = None, *, make_active: bool = True) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        clean_title = _normalize_chat_title_text(title, fallback="Новый чат")
        title_locked = 0 if clean_title == "Новый чат" else 1
        public_id = self._generate_public_id(conn)
        cur = conn.execute(
            """
            INSERT INTO chat_sessions(public_id, user_id, title, title_locked, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (public_id, int(user_id), clean_title, int(title_locked), now, now),
        )
        chat_id = int(cur.lastrowid)
        if make_active:
            self._set_active_chat_conn(conn, user_id, chat_id)
        row = self._get_chat_conn(conn, user_id, chat_id)
        return self._row_to_chat(row, active_chat_id=chat_id)

    def _ensure_active_chat_conn(self, conn, user_id: int) -> dict[str, Any]:
        active_chat_id = self._get_active_chat_id_conn(conn, user_id)
        row = None
        if active_chat_id is not None:
            row = self._get_chat_conn(conn, user_id, active_chat_id)
        if row:
            return self._row_to_chat(row, active_chat_id=active_chat_id)
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
        if row:
            active_chat_id = int(row["id"])
            self._set_active_chat_conn(conn, user_id, active_chat_id)
            return self._row_to_chat(row, active_chat_id=active_chat_id)
        return self._create_chat_conn(conn, user_id, title="Новый чат", make_active=True)

    def _touch_chat_conn(self, conn, chat_id: int, *, title: str | None = None, title_locked: int | None = None, last_preview: str | None = None) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [datetime.now(UTC).isoformat()]
        if title is not None:
            sets.append("title = ?")
            params.append(_normalize_chat_title_text(title))
        if title_locked is not None:
            sets.append("title_locked = ?")
            params.append(int(title_locked))
        if last_preview is not None:
            sets.append("last_preview = ?")
            params.append(last_preview[:160])
        params.append(int(chat_id))
        conn.execute(f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id = ?", params)

    def _refresh_message_count_conn(self, conn, chat_id: int) -> int:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM chat_messages WHERE session_id = ?", (int(chat_id),)).fetchone()
        count = int(row["cnt"] if row else 0)
        conn.execute("UPDATE chat_sessions SET message_count = ? WHERE id = ?", (count, int(chat_id)))
        return count

    def _migrate_legacy_json_if_needed(self) -> None:
        if not os.path.exists(self.legacy_path):
            return
        try:
            with open(self.legacy_path, "r", encoding="utf-8") as f:
                legacy_data = json.load(f)
        except Exception:
            logging.debug("Legacy conversations.json migration skipped", exc_info=True)
            return

        if not isinstance(legacy_data, dict) or not legacy_data:
            return

        with self._lock:
            with self._connect() as conn:
                has_any = conn.execute("SELECT 1 FROM chat_sessions LIMIT 1").fetchone()
                if has_any:
                    return
                for raw_user_id, messages in legacy_data.items():
                    try:
                        user_id = int(raw_user_id)
                    except Exception:
                        continue
                    if not isinstance(messages, list):
                        continue
                    chat = self._create_chat_conn(conn, user_id, title="Импортированный чат", make_active=True)
                    first_user_message = ""
                    for entry in messages[-1500:]:
                        if not isinstance(entry, dict):
                            continue
                        role = str(entry.get("role") or "assistant")[:32]
                        text = clean_reply_for_display(entry.get("text") or "")
                        if not text:
                            continue
                        created_at = entry.get("time") or datetime.now(UTC).isoformat()
                        conn.execute(
                            "INSERT INTO chat_messages(session_id, role, text, created_at) VALUES(?, ?, ?, ?)",
                            (int(chat["id"]), role, text, created_at),
                        )
                        if role == "user" and not first_user_message:
                            first_user_message = text
                    self._refresh_message_count_conn(conn, int(chat["id"]))
                    if first_user_message:
                        self._touch_chat_conn(
                            conn,
                            int(chat["id"]),
                            title=_derive_auto_chat_title(first_user_message, fallback=chat["title"]),
                            title_locked=0,
                            last_preview=clean_reply_for_display(first_user_message)[:160],
                        )
                conn.commit()

    def get_active_chat(self, user_id: int) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                return self._ensure_active_chat_conn(conn, user_id)

    def get_active_chat_id(self, user_id: int) -> int:
        return int(self.get_active_chat(user_id)["id"])

    async def create_chat(self, user_id: int, title: str | None = None, *, make_active: bool = True) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                chat = self._create_chat_conn(conn, user_id, title=title, make_active=make_active)
                conn.commit()
                return chat

    def list_user_chats(self, user_id: int, limit: int = 12, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                active_chat_id = self._get_active_chat_id_conn(conn, user_id)
                if active_chat_id is None:
                    active_chat_id = self._ensure_active_chat_conn(conn, user_id)["id"]
                    conn.commit()
                rows = conn.execute(
                    """
                    SELECT * FROM chat_sessions
                    WHERE user_id = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (int(user_id), int(limit), int(offset)),
                ).fetchall()
                return [self._row_to_chat(row, active_chat_id=active_chat_id) for row in rows]

    def count_user_chats(self, user_id: int) -> int:
        with self._lock:
            with self._connect() as conn:
                self._ensure_active_chat_conn(conn, user_id)
                row = conn.execute("SELECT COUNT(*) AS cnt FROM chat_sessions WHERE user_id = ?", (int(user_id),)).fetchone()
                return int(row["cnt"] if row else 0)

    def get_chat(self, user_id: int, chat_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = self._get_chat_conn(conn, user_id, chat_id)
                active_chat_id = self._get_active_chat_id_conn(conn, user_id)
                return self._row_to_chat(row, active_chat_id=active_chat_id)

    async def set_active_chat(self, user_id: int, chat_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = self._get_chat_conn(conn, user_id, chat_id)
                if not row:
                    return None
                self._set_active_chat_conn(conn, user_id, chat_id)
                conn.commit()
                return self._row_to_chat(row, active_chat_id=chat_id)

    async def rename_chat(self, user_id: int, chat_id: int, title: str) -> dict[str, Any] | None:
        clean_title = _normalize_chat_title_text(title)
        with self._lock:
            with self._connect() as conn:
                row = self._get_chat_conn(conn, user_id, chat_id)
                if not row:
                    return None
                self._touch_chat_conn(conn, chat_id, title=clean_title, title_locked=1)
                conn.commit()
                row = self._get_chat_conn(conn, user_id, chat_id)
                return self._row_to_chat(row, active_chat_id=self._get_active_chat_id_conn(conn, user_id))

    async def delete_chat(self, user_id: int, chat_id: int) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                row = self._get_chat_conn(conn, user_id, chat_id)
                if not row:
                    return self._ensure_active_chat_conn(conn, user_id)

                conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (int(chat_id),))
                conn.execute("DELETE FROM chat_sessions WHERE id = ? AND user_id = ?", (int(chat_id), int(user_id)))

                remaining = conn.execute(
                    "SELECT * FROM chat_sessions WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (int(user_id),),
                ).fetchone()
                if remaining:
                    new_active = int(remaining["id"])
                    self._set_active_chat_conn(conn, user_id, new_active)
                    conn.commit()
                    return self._row_to_chat(remaining, active_chat_id=new_active)

                chat = self._create_chat_conn(conn, user_id, title="Новый чат", make_active=True)
                conn.commit()
                return chat

    async def add_message(self, user_id: int, role: str, text: str, chat_id: int | None = None):
        clean_text = clean_reply_for_display(text)
        if not clean_text:
            return
        role_value = (role or "assistant").strip()[:32] or "assistant"
        preview = clean_text.replace("\n", " ")[:160]
        with self._lock:
            with self._connect() as conn:
                chat = self._ensure_active_chat_conn(conn, user_id) if chat_id is None else self._row_to_chat(self._get_chat_conn(conn, user_id, chat_id), active_chat_id=self._get_active_chat_id_conn(conn, user_id))
                if not chat:
                    chat = self._create_chat_conn(conn, user_id, title="Новый чат", make_active=True)
                conn.execute(
                    "INSERT INTO chat_messages(session_id, role, text, created_at) VALUES(?, ?, ?, ?)",
                    (int(chat["id"]), role_value, clean_text, datetime.now(UTC).isoformat()),
                )
                message_count = self._refresh_message_count_conn(conn, int(chat["id"]))
                chat_row = self._get_chat_conn(conn, user_id, int(chat["id"]))
                title = chat_row["title"] if chat_row else chat["title"]
                title_locked = int(chat_row["title_locked"]) if chat_row else int(chat["title_locked"])
                if role_value == "user" and not title_locked and message_count <= 2:
                    title = _derive_auto_chat_title(clean_text, fallback=title)
                self._touch_chat_conn(conn, int(chat["id"]), title=title, title_locked=title_locked, last_preview=preview)
                conn.commit()

    def get_recent_model_messages(self, user_id: int, chat_id: int | None = None, limit: int = 16) -> list[dict[str, str]]:
        with self._lock:
            with self._connect() as conn:
                chat = self._ensure_active_chat_conn(conn, user_id) if chat_id is None else self._row_to_chat(self._get_chat_conn(conn, user_id, chat_id), active_chat_id=self._get_active_chat_id_conn(conn, user_id))
                if not chat:
                    return []
                rows = conn.execute(
                    """
                    SELECT role, text FROM chat_messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(chat["id"]), int(limit)),
                ).fetchall()
                messages = [{"role": row["role"], "content": row["text"]} for row in reversed(rows)]
                return messages

    def add_chat_memory(
        self,
        user_id: int,
        chat_key: str,
        *,
        source_msg_id: int | None,
        role: str,
        content: str,
        summary: str | None = None,
        tags: str | None = None,
        importance: float = 0.5,
        created_at: str | None = None,
    ) -> None:
        clean_content = clean_reply_for_display(content or "")
        if not clean_content:
            return
        chat_key_value = (chat_key or "").strip()[:120]
        if not chat_key_value:
            return
        role_value = (role or "memory").strip()[:32] or "memory"
        summary_value = clean_reply_for_display(summary or "")[:400] if summary else ""
        tags_value = clean_reply_for_display(tags or "")[:200] if tags else ""
        created_value = created_at or datetime.now(UTC).isoformat()
        importance_value = max(0.0, min(1.0, float(importance)))
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO chat_memory(
                        user_id, chat_key, source_msg_id, role, content, summary, tags, importance, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(user_id),
                        chat_key_value,
                        int(source_msg_id) if source_msg_id is not None else None,
                        role_value,
                        clean_content[:4000],
                        summary_value or None,
                        tags_value or None,
                        importance_value,
                        created_value,
                    ),
                )
                conn.commit()

    def get_chat_memory(self, user_id: int, chat_key: str, limit: int = 120) -> list[dict[str, Any]]:
        chat_key_value = (chat_key or "").strip()[:120]
        if not chat_key_value:
            return []
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, user_id, chat_key, source_msg_id, role, content, summary, tags, importance, created_at
                    FROM chat_memory
                    WHERE user_id = ? AND chat_key = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(user_id), chat_key_value, max(1, int(limit))),
                ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "chat_key": row["chat_key"],
                "source_msg_id": row["source_msg_id"],
                "role": row["role"],
                "content": row["content"] or "",
                "summary": row["summary"] or "",
                "tags": row["tags"] or "",
                "importance": float(row["importance"] or 0.5),
                "created_at": row["created_at"] or "",
            }
            for row in rows
        ]

    def get_user_history(self, user_id: int, chat_id: int | None = None, limit: int | None = None):
        with self._lock:
            with self._connect() as conn:
                chat = self._ensure_active_chat_conn(conn, user_id) if chat_id is None else self._row_to_chat(self._get_chat_conn(conn, user_id, chat_id), active_chat_id=self._get_active_chat_id_conn(conn, user_id))
                if not chat:
                    return []
                sql = "SELECT role, text, created_at AS time FROM chat_messages WHERE session_id = ? ORDER BY id ASC"
                params: list[Any] = [int(chat["id"])]
                if limit:
                    sql = "SELECT role, text, created_at AS time FROM (SELECT role, text, created_at, id FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
                    params.append(int(limit))
                rows = conn.execute(sql, params).fetchall()
                return [{"role": row["role"], "text": row["text"], "time": row["time"]} for row in rows]

    def get_chat_messages_page(self, user_id: int, chat_id: int, page: int = 0, per_page: int = 8) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = self._get_chat_conn(conn, user_id, chat_id)
                if not row:
                    return None
                chat = self._row_to_chat(row, active_chat_id=self._get_active_chat_id_conn(conn, user_id))
                total_messages = int(chat["message_count"])
                total_pages = max(1, (total_messages + per_page - 1) // per_page)
                current_page = max(0, min(int(page), total_pages - 1))
                offset = current_page * int(per_page)
                rows = conn.execute(
                    """
                    SELECT role, text, created_at AS time FROM chat_messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (int(chat_id), int(per_page), int(offset)),
                ).fetchall()
                messages = [
                    {"role": item["role"], "text": item["text"], "time": item["time"]}
                    for item in reversed(rows)
                ]
                return {
                    "chat": chat,
                    "messages": messages,
                    "page": current_page,
                    "per_page": int(per_page),
                    "total_pages": total_pages,
                    "total_messages": total_messages,
                }

    async def clear_user(self, user_id: int, chat_id: int | None = None):
        """Очищает историю активного или указанного чата пользователя."""
        with self._lock:
            with self._connect() as conn:
                chat = self._ensure_active_chat_conn(conn, user_id) if chat_id is None else self._row_to_chat(self._get_chat_conn(conn, user_id, chat_id), active_chat_id=self._get_active_chat_id_conn(conn, user_id))
                if not chat:
                    return
                conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (int(chat["id"]),))
                conn.execute(
                    """
                    UPDATE chat_sessions
                    SET message_count = 0,
                        last_preview = '',
                        summary = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (datetime.now(UTC).isoformat(), int(chat["id"])),
                )
                conn.commit()

users_store = UsersStore()
conversation_store = ConversationStore()

# ------------------------ Legacy simple file helpers (часть старого кода оставлена) ------------------------
def load_users():
    if os.path.exists(USER_FILE):
        try:
            with open(USER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_users(users):
    try:
        with open(USER_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_banned():
    if os.path.exists(BANNED_FILE):
        try:
            with open(BANNED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_banned(banned):
    try:
        with open(BANNED_FILE, "w", encoding="utf-8") as f:
            json.dump(banned, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

banned_users = load_banned()

# ======================= Логирование активности =======================
def log_user_activity(user_id: int, username: str, text: str):
    data = {}
    if os.path.exists(ACTIVITY_FILE):
        try:
            with open(ACTIVITY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    if str(user_id) not in data:
        data[str(user_id)] = []

    data[str(user_id)].append({
        "username": username,
        "text": text,
        "time": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    })

    try:
        with open(ACTIVITY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка при логировании активности пользователя {user_id}: {e}")


# ======================= Кнопки главного меню =======================
MENU_BUTTON_DIALOG = "💬 Диалог с ИИ"
MENU_BUTTON_CHAT = "🗂 Мои чаты"
MENU_BUTTON_ROLE = "🪄 Режим ответа"
MENU_BUTTON_PHOTO = "🖼 Разобрать фото"
MENU_BUTTON_IMAGE = "🎨 Создать изображение"
MENU_BUTTON_WEATHER = "🌦 Погода"
MENU_BUTTON_FACT = "🧠 Факт дня"
MENU_BUTTON_HELP = "✨ Возможности"
CHAT_MANAGER_CALLBACK_PREFIX = "chatmgr|"
CHAT_RENAME_STATE_KEY = "awaiting_chat_rename_id"
CHAT_RENAME_PAGE_KEY = "awaiting_chat_rename_page"
CHAT_LIST_PAGE_SIZE = 8
CHAT_HISTORY_PAGE_SIZE = 6

main_keyboard_buttons = [
    [MENU_BUTTON_DIALOG, MENU_BUTTON_CHAT],
    [MENU_BUTTON_PHOTO, MENU_BUTTON_IMAGE],
    [MENU_BUTTON_ROLE, MENU_BUTTON_WEATHER],
    [MENU_BUTTON_FACT, MENU_BUTTON_HELP],
]

# ------------------------ Роли GPT (дополнения к системному промпту) ------------------------
# role_id -> (отображаемое имя, дополнение к system prompt)
ROLES: dict[str, tuple[str, str]] = {
    "neutral": ("⚖️ Сбалансированно", ""),
    "short": ("⚡ Коротко", "Отвечай коротко и ёмко: сначала вывод, затем 1-2 ключевые детали без повторов."),
    "detailed": ("📚 Подробно", "Отвечай развёрнуто: объясняй шаги, добавляй контекст и примеры, но сохраняй структуру и ясность."),
    "teacher": ("🧠 Как учитель", "Объясняй терпеливо и по шагам: сначала идея, затем детали, затем короткий пример."),
    "programmer": ("💻 Как разработчик", "Отвечай как сильный инженер: логика, структура, допущения, граничные случаи и практические шаги."),
    "scientist": ("🔬 Научно", "Тон точный и аккуратный: отделяй факты от гипотез, указывай ограничения и уровень уверенности."),
    "friend": ("🤝 По-человечески", "Общайся тепло и понятно, без лишнего официоза, но сохраняй точность и здравый смысл."),
    "socratic": ("🎯 Вопросами", "Помогай через наводящие вопросы и короткие подсказки, чтобы пользователь сам дошёл до вывода."),
    "editor": ("✏️ Как редактор", "Фокус на формулировке: ясность, стиль, краткость, структура и улучшение текста."),
    "analyst": ("📊 Как аналитик", "Сравнивай варианты, плюсы, минусы, риски и последствия. Давай структурированный вывод."),
    "child": ("🧒 Очень просто", "Объясняй максимально простыми словами, короткими предложениями и на бытовых примерах."),
    "facts": ("📌 Только факты", "Отвечай фактами без воды. Если уверенности мало — прямо говори об этом."),
    "storyteller": ("🎭 Через историю", "Подавай материал живо: через пример, мини-сюжет или образ, но без потери точности."),
    "motivator": ("💪 С поддержкой", "Поддерживай и направляй к действию: меньше лозунгов, больше реальных следующих шагов."),
    "critic": ("🔍 Критически", "Спокойно указывай на слабые места, противоречия и риски, а затем предлагай улучшения."),
}

def get_ask_question_keyboard():
    """Быстрый выбор формата ответа."""
    rows = [
        [
            InlineKeyboardButton("📝 Текстом", callback_data="ask_mode|text"),
            InlineKeyboardButton("🗣 Голосом", callback_data="ask_mode|voice"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def get_role_keyboard():
    """Клавиатура выбора стиля ответа."""
    role_ids = [
        "neutral",
        "short",
        "detailed",
        "teacher",
        "programmer",
        "analyst",
        "scientist",
        "friend",
        "editor",
        "child",
        "facts",
        "storyteller",
        "motivator",
        "critic",
        "socratic",
    ]
    rows = []
    row = []
    for rid in role_ids:
        if rid in ROLES:
            row.append(InlineKeyboardButton(ROLES[rid][0], callback_data=f"role|{rid}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def get_ask_mode_keyboard():
    """Выбор формата ответа: текст или голос."""
    return get_ask_question_keyboard()

ANSWER_REWRITE_PROMPTS: dict[str, str] = {
    "more": (
        "Сделай новую версию ответа заметно подробнее. "
        "Раскрой тему глубже, добавь важные детали, 1-2 коротких примера и шаги, если это уместно. "
        "Не уходи в сторону от исходного вопроса. Итоговый ответ держи в пределах 3200 символов."
    ),
    "brief": (
        "Сделай новую версию ответа максимально краткой и ёмкой. "
        "Оставь только суть: 2-4 предложения или короткий список до 4 пунктов, если так понятнее. "
        "Без вступлений, повторов и воды. Итоговый ответ держи в пределах 1200 символов."
    ),
    "simple": (
        "Сделай новую версию ответа проще для новичка. "
        "Пиши простыми словами, убери жаргон и сложные термины или сразу коротко расшифруй их. "
        "Если уместно, используй одну бытовую аналогию. Итоговый ответ держи в пределах 2200 символов."
    ),
}
ANSWER_REWRITE_LABELS: dict[str, str] = {
    "more": "📚 Развернуть",
    "brief": "✂️ Сжать",
    "simple": "✨ Упростить",
}
ANSWER_ACTIONS_CALLBACK_PREFIX = "answer_style|"
ANSWER_ACTIONS_CACHE_LIMIT = int(os.getenv("ANSWER_ACTIONS_CACHE_LIMIT", "500"))
ANSWER_ACTIONS: dict[str, dict[str, Any]] = {}
ANSWER_ACTIONS_LOCK: asyncio.Lock = asyncio.Lock()
DIALOG_FLOW_CALLBACK_PREFIX = "dialog_flow|"
_DIALOG_CONTROL_MESSAGES: dict[str, int] = {}
_DIALOG_CONTROL_LOCK: asyncio.Lock = asyncio.Lock()
COMPACT_ANSWER_MAX_CHARS = int(os.getenv("COMPACT_ANSWER_MAX_CHARS", "260"))
COMPACT_ANSWER_MAX_LINES = int(os.getenv("COMPACT_ANSWER_MAX_LINES", "4"))


def get_answer_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📚 Развернуть", callback_data=f"{ANSWER_ACTIONS_CALLBACK_PREFIX}more"),
            InlineKeyboardButton("✂️ Сжать", callback_data=f"{ANSWER_ACTIONS_CALLBACK_PREFIX}brief"),
            InlineKeyboardButton("✨ Упростить", callback_data=f"{ANSWER_ACTIONS_CALLBACK_PREFIX}simple"),
        ]
    ])

def get_dialog_controls_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➡️ Продолжить", callback_data=f"{DIALOG_FLOW_CALLBACK_PREFIX}continue"),
            InlineKeyboardButton("🚪 Покинуть диалог", callback_data=f"{DIALOG_FLOW_CALLBACK_PREFIX}exit"),
        ]
    ])


def _answer_actions_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


async def store_answer_actions_context(
    chat_id: int,
    message_id: int | None,
    user_id: int,
    question: str,
    answer_text: str,
    reply_style: str | None,
    current_variant: str = "base",
) -> None:
    if not message_id:
        return
    payload = {
        "user_id": user_id,
        "question": (question or "")[:8000],
        "latest_answer": answer_text or "",
        "reply_style": reply_style or "neutral",
        "current_variant": current_variant,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    async with ANSWER_ACTIONS_LOCK:
        ANSWER_ACTIONS[_answer_actions_key(chat_id, message_id)] = payload
        while len(ANSWER_ACTIONS) > ANSWER_ACTIONS_CACHE_LIMIT:
            ANSWER_ACTIONS.pop(next(iter(ANSWER_ACTIONS)))


async def get_answer_actions_context(chat_id: int, message_id: int) -> dict[str, Any] | None:
    async with ANSWER_ACTIONS_LOCK:
        data = ANSWER_ACTIONS.get(_answer_actions_key(chat_id, message_id))
        return dict(data) if isinstance(data, dict) else None

def _dialog_control_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def _history_state_key(user_id: int, chat_session_id: int | None) -> str:
    return f"{int(user_id)}:{int(chat_session_id or 0)}"


def build_memory_chat_key(user_id: int, chat_session_id: int | None) -> str:
    return f"{int(user_id)}:{int(chat_session_id or 0)}"


def _memory_normalize_text(value: str | None) -> str:
    normalized = re.sub(r"\s+", " ", clean_reply_for_display(value or "").strip().lower())
    return re.sub(r"[^\w\sа-яё]", "", normalized, flags=re.IGNORECASE).strip()


def _memory_tokenize(value: str | None) -> set[str]:
    text = _memory_normalize_text(value)
    return {token for token in re.findall(r"[a-zа-яё0-9]{3,}", text, flags=re.IGNORECASE)}


def _memory_extract_tags(text: str, *, limit: int = 6) -> list[str]:
    tokens = []
    for token in re.findall(r"[a-zа-яё0-9]{4,}", (text or "").lower(), flags=re.IGNORECASE):
        if token in {"который", "которые", "можно", "нужно", "чтобы", "потом", "этого", "этой", "этот"}:
            continue
        tokens.append(token)
        if len(tokens) >= 30:
            break
    freq: dict[str, int] = {}
    for token in tokens:
        freq[token] = freq.get(token, 0) + 1
    ranked = sorted(freq.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _ in ranked[: max(1, int(limit))]]


def memory_extract_facts(text: str) -> list[str]:
    cleaned = clean_reply_for_display(text or "")
    if not cleaned:
        return []

    parts: list[str] = []
    for block in cleaned.splitlines():
        for candidate in re.split(r"(?<=[.!?…])\s+", block.strip()):
            normalized = re.sub(r"\s+", " ", candidate).strip(" -•\t")
            if not normalized:
                continue
            if len(normalized) < 14:
                continue
            low = normalized.lower()
            if low.startswith(("вот ", "конечно", "давай", "если хочешь", "могу")):
                continue
            parts.append(normalized[:200])

    unique: list[str] = []
    seen: set[str] = set()
    for item in parts:
        norm = _memory_normalize_text(item)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        unique.append(item)
        if len(unique) >= 10:
            break
    return unique


def memory_score_item(query: str, item_text: str, importance: float, age_hours: float) -> float:
    query_tokens = _memory_tokenize(query)
    item_tokens = _memory_tokenize(item_text)
    lexical_overlap = 0.0
    if query_tokens and item_tokens:
        common = len(query_tokens & item_tokens)
        lexical_overlap = common / max(len(query_tokens), 1)
    decay = min(1.5, max(0.0, float(age_hours)) / 72.0)
    return lexical_overlap + float(importance) - decay


def memory_store_turn(
    user_id: int,
    chat_key: str,
    msg_id: int | None,
    role: str,
    text: str,
    tags: list[str] | str | None = None,
    importance: float = 0.5,
) -> None:
    if conversation_store is None:
        return
    clean_text = clean_reply_for_display(text or "")
    if not clean_text:
        return

    tag_values = tags if isinstance(tags, list) else _memory_extract_tags(clean_text)
    if isinstance(tag_values, str):
        tag_values = [item.strip() for item in re.split(r"[,;]", tag_values) if item.strip()]
    tags_str = ", ".join(tag_values[:8]) if tag_values else ""

    existing = conversation_store.get_chat_memory(user_id, chat_key, limit=120)
    existing_norm = {
        _memory_normalize_text((item.get("summary") or "") + " " + (item.get("content") or ""))
        for item in existing
    }
    existing_norm.discard("")

    facts = memory_extract_facts(clean_text)
    summary = facts[0] if facts else clean_text[:200]
    turn_norm = _memory_normalize_text(summary)
    if turn_norm not in existing_norm:
        conversation_store.add_chat_memory(
            user_id,
            chat_key,
            source_msg_id=msg_id,
            role=role,
            content=clean_text[:4000],
            summary=summary,
            tags=tags_str,
            importance=importance,
        )
        existing_norm.add(turn_norm)

    for fact in facts:
        fact_norm = _memory_normalize_text(fact)
        if not fact_norm or fact_norm in existing_norm:
            continue
        conversation_store.add_chat_memory(
            user_id,
            chat_key,
            source_msg_id=msg_id,
            role=f"{role}_fact",
            content=fact,
            summary=fact,
            tags=tags_str,
            importance=min(1.0, importance + 0.1),
        )
        existing_norm.add(fact_norm)


def memory_get_context(user_id: int, chat_key: str, query: str, limit: int = 8) -> list[str]:
    if conversation_store is None:
        return []
    rows = conversation_store.get_chat_memory(user_id, chat_key, limit=max(80, int(limit) * 18))
    if not rows:
        return []

    now = datetime.now(UTC)
    ranked: list[tuple[float, str, str]] = []
    for item in rows:
        text = clean_reply_for_display(item.get("summary") or item.get("content") or "")
        if not text:
            continue
        created_raw = item.get("created_at") or ""
        age_hours = 0.0
        with contextlib.suppress(Exception):
            created_dt = datetime.fromisoformat(created_raw)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=UTC)
            age_hours = max(0.0, (now - created_dt).total_seconds() / 3600.0)
        score = memory_score_item(query, text, float(item.get("importance") or 0.5), age_hours)
        ranked.append((score, text[:200], created_raw))

    ranked.sort(key=lambda entry: (entry[0], entry[2]), reverse=True)
    selected: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for score, text, _created_raw in ranked:
        if score < -0.6:
            continue
        normalized = _memory_normalize_text(text)
        if not normalized or normalized in seen:
            continue
        line = text.strip()
        projected = total_chars + len(line) + 3
        if projected > 1200:
            break
        seen.add(normalized)
        selected.append(line)
        total_chars = projected
        if len(selected) >= max(1, int(limit)):
            break
    return selected


def build_memory_system_block(user_id: int, chat_key: str, query: str, limit: int = 8) -> str:
    items = memory_get_context(user_id, chat_key, query, limit=limit)
    if not items:
        return ""
    lines: list[str] = []
    total_chars = 0
    for item in items:
        line = f"- {item}"
        projected = total_chars + len(line) + 1
        if projected > 1200:
            break
        lines.append(line)
        total_chars = projected
    if not lines:
        return ""
    return "Релевантная память пользователя:\n" + "\n".join(lines)


def should_send_compact_answer(question: str, answer_text: str) -> bool:
    text = clean_reply_for_display(answer_text)
    if not text:
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    if len(text) > COMPACT_ANSWER_MAX_CHARS or len(lines) > COMPACT_ANSWER_MAX_LINES:
        return False

    if any(len(line) > 140 for line in lines):
        return False

    bullet_lines = sum(1 for line in lines if re.match(r"^([-•*]|\d+\.)\s+", line))
    if bullet_lines > 3:
        return False

    question_lower = normalize_spaces(question or "").lower()
    answer_lower = text.lower()
    formula_like = bool(re.search(r"[=≈+\-/*^%]", text)) and len(text) <= 180
    ultra_short = len(text) <= 140 and len(lines) <= 2
    direct_question = any(
        token in question_lower
        for token in (
            "формул",
            "чему рав",
            "сколько",
            "как найти",
            "что такое",
            "что значит",
            "перевод",
            "обозначает",
            "ответ",
        )
    )
    direct_answer = (
        answer_lower.startswith(("это ", "формула", "ответ", "перевод"))
        or " = " in text
        or "≈" in text
    )
    return ultra_short or formula_like or (direct_question and len(lines) <= 3) or (direct_answer and len(lines) <= 3)


def should_send_plain_copyable_answer(question: str, answer_text: str) -> bool:
    text = clean_reply_for_display(answer_text)
    if not text:
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    question_lower = normalize_spaces(question or "").lower()
    prompt_request = any(token in question_lower for token in ("промпт", "prompt"))
    direct_result_request = any(
        token in question_lower
        for token in (
            "формула",
            "закон ома",
            "чему рав",
            "найди",
            "посчитай",
            "реши",
            "решение",
            "переведи",
            "перевод",
            "ответ без",
        )
    )
    formula_like = bool(re.search(r"[=≈+\-/*^%]", text))
    compact_shape = len(text) <= 700 and len(lines) <= 8
    return compact_shape and (prompt_request or direct_result_request or formula_like)


def strip_leading_answer_labels(text: str, question: str = "") -> str:
    cleaned = clean_reply_for_display(text)
    if not cleaned:
        return ""

    question_lower = normalize_spaces(question or "").lower()
    patterns = [
        r"^(?:вот\s+)?(?:краткий\s+)?ответ\s*[:\-–—]\s*",
        r"^(?:вот\s+)?решение\s*[:\-–—]\s*",
        r"^(?:вот\s+)?формула\s*[:\-–—]\s*",
        r"^(?:готовый\s+)?промпт\s*[:\-–—]\s*",
        r"^(?:вот\s+)?перевод\s*[:\-–—]\s*",
        r"^(?:коротко|кратко)\s*[:\-–—]\s*",
    ]
    if "закон ома" in question_lower:
        patterns.append(r"^закон\s+ома\s*[:\-–—]\s*")

    result = cleaned.strip()
    for pattern in patterns:
        result = re.sub(pattern, "", result, count=1, flags=re.IGNORECASE).strip()
    return result


def emphasize_main_points_html(text: str) -> str:
    cleaned = clean_reply_for_display(text)
    if not cleaned:
        return ""

    lines = cleaned.splitlines()
    result: list[str] = []
    summary_highlighted = False

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            result.append("")
            continue

        bullet_match = re.match(r"^((?:[-•*]|\d+\.)\s+)([^:]{1,60}:)(\s*.*)$", stripped)
        if bullet_match:
            prefix, label, rest = bullet_match.groups()
            result.append(
                f"{html.escape(prefix, quote=False)}<b>{html.escape(label, quote=False)}</b>{html.escape(rest, quote=False)}"
            )
            continue

        label_match = re.match(r"^([^:\n]{2,60}:)(\s*.*)$", stripped)
        if label_match:
            label, rest = label_match.groups()
            result.append(f"<b>{html.escape(label, quote=False)}</b>{html.escape(rest, quote=False)}")
            continue

        if not summary_highlighted:
            sentence_match = re.match(r"(.{1,180}?[.!?…])(\s+.*)?$", stripped)
            if sentence_match:
                summary, rest = sentence_match.groups()
                result.append(
                    f"<b>{html.escape(summary, quote=False)}</b>{html.escape(rest or '', quote=False)}"
                )
                summary_highlighted = True
                continue

        result.append(html.escape(stripped, quote=False))

    return "\n".join(result).strip()


def prepare_assistant_display(question: str, answer_text: str) -> tuple[str, str | None, bool]:
    cleaned = clean_reply_for_display(answer_text)
    if not cleaned:
        return "", None, False

    if should_send_plain_copyable_answer(question, cleaned):
        return strip_leading_answer_labels(cleaned, question), None, True

    html_text = emphasize_main_points_html(cleaned)
    if html_text:
        return html_text, ParseMode.HTML, False
    return cleaned, None, False


def should_stream_answer(question: str, answer_text: str, parse_mode: str | None = None) -> bool:
    if not ENABLE_STREAMING:
        return False
    if _is_html_parse_mode(parse_mode) and not STREAM_HTML_ANSWERS:
        return False

    text = clean_reply_for_display(answer_text)
    if not text or should_send_plain_copyable_answer(question, text):
        return False
    if should_send_compact_answer(question, text) and not STREAM_COMPACT_ANSWERS:
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(text) < STREAM_MIN_TEXT_LEN or len(text) > STREAM_MAX_TEXT_LEN:
        return False
    if len(lines) > 24:
        return False
    if "```" in answer_text:
        return False
    if re.search(r"(^|\n)\s*[-•*]\s+", text) and len(lines) >= 12:
        return False

    sentence_count = len(re.findall(r"[.!?…](?:\s|$)", text))
    paragraph_count = len([part for part in re.split(r"\n{2,}", text) if part.strip()])
    return sentence_count >= 1 or paragraph_count >= 1

async def _reset_user_dialog_context(user_id: int, chat_id: int | None = None) -> None:
    target_chat_id = int(chat_id or 0)
    if not target_chat_id and conversation_store is not None:
        with contextlib.suppress(Exception):
            target_chat_id = int(conversation_store.get_active_chat_id(user_id))
    try:
        async with _HISTORIES_LOCK:
            user_histories[_history_state_key(user_id, target_chat_id)] = []
    except Exception:
        logging.debug("Failed to reset in-memory dialog history for user %s", user_id, exc_info=True)

    try:
        if conversation_store is not None:
            await conversation_store.clear_user(user_id, chat_id=target_chat_id or None)
    except Exception:
        logging.debug("Failed to reset persisted dialog history for user %s", user_id, exc_info=True)

async def send_dialog_controls_message(bot: Any, chat_id: int, user_id: int) -> int | None:
    if bot is None:
        return None

    key = _dialog_control_key(chat_id, user_id)
    previous_message_id = None
    async with _DIALOG_CONTROL_LOCK:
        previous_message_id = _DIALOG_CONTROL_MESSAGES.get(key)

    if previous_message_id:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=chat_id, message_id=previous_message_id)

    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text="💬 Диалог открыт. Можешь задать следующий вопрос или вернуться в главное меню.",
            reply_markup=get_dialog_controls_keyboard(),
        )
    except Exception:
        logging.exception("Failed to send dialog controls message")
        return None

    async with _DIALOG_CONTROL_LOCK:
        _DIALOG_CONTROL_MESSAGES[key] = getattr(msg, "message_id", None)
    return getattr(msg, "message_id", None)

async def clear_dialog_controls_message(bot: Any, chat_id: int, user_id: int, *, delete_message: bool = True) -> None:
    if bot is None:
        return
    key = _dialog_control_key(chat_id, user_id)
    async with _DIALOG_CONTROL_LOCK:
        message_id = _DIALOG_CONTROL_MESSAGES.pop(key, None)
    if delete_message and message_id:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=chat_id, message_id=message_id)


def build_answer_rewrite_prompt(style_id: str, question: str, current_answer: str) -> str:
    style_prompt = ANSWER_REWRITE_PROMPTS.get(style_id, ANSWER_REWRITE_PROMPTS["more"])
    return (
        "Пользователь уже задал вопрос и получил ответ. "
        "Нужно переписать этот ответ в другом формате, сохранив смысл и фактическую точность.\n\n"
        f"Исходный вопрос:\n{(question or '').strip()}\n\n"
        f"Текущая версия ответа:\n{(current_answer or '').strip()}\n\n"
        f"Инструкция по новой версии:\n{style_prompt}\n\n"
        "Верни только готовый ответ пользователю. "
        "Не добавляй вступлений вроде 'вот подробнее', 'если кратко' или 'объясню проще'. "
        "Если это уместно, можно оставить 0-2 эмодзи на весь ответ, чтобы он выглядел живее, но не перегружай текст. "
        "Пиши на русском."
    )

def get_role_prompt(role_id: str | None) -> str:
    """
    Возвращает текст, который добавляется к системному промпту для выбранной роли.
    Здесь мы тонко настраиваем стиль ответа под каждую роль.
    """
    if not role_id:
        return ""

    if role_id == "neutral":
        return (
            "Режим: сбалансированный профессиональный стиль. "
            "Отвечай спокойно, по существу, понятно и без лишней театральности."
        )

    label, base = ROLES.get(role_id, ("", ""))

    # Базовый текст роли из словаря ROLES
    parts: list[str] = []
    if label:
        parts.append(f"Текущий стиль ответа: {label}.")
    if base:
        parts.append(base)

    # Дополнительные уточнения по конкретным ролям
    if role_id == "short":
        parts.append(
            "Держи ответ максимально компактным: 2–4 предложения, без вступлений и повторов. "
            "Если просят подробности — сначала дай краткий ответ, а затем отдельным абзацем можешь расширить."
        )
    elif role_id == "detailed":
        parts.append(
            "Строй ответ как мини-обзор: сначала общий вывод, затем структурированное объяснение с подзаголовками "
            "или явными блоками по темам."
        )
    elif role_id == "teacher":
        parts.append(
            "Объясняй по шагам: сначала интуитивная идея, затем формальное объяснение, затем простой пример. "
            "Избегай перегрузки терминологией, каждый новый термин коротко поясняй."
        )
    elif role_id == "programmer":
        parts.append(
            "Фокус на точности и инженерной логике. Сначала опиши общую архитектуру/идею, затем возможные граничные "
            "случаи и типовые ошибки. Если не уверен в детали API или версии библиотеки, честно укажи это."
        )
    elif role_id == "scientist":
        parts.append(
            "Разделяй факты, гипотезы и предположения. Явно помечай, где данные надёжны, а где основаны на моделях "
            "или неполной информации. При возможности указывай, какие эксперименты или источники могли бы проверить утверждение."
        )
    elif role_id == "friend":
        parts.append(
            "Поддерживай человека, но всё равно будь честен. Если идея плохая или рискованная, мягко объясни почему "
            "и предложи более безопасную альтернативу."
        )
    elif role_id == "socratic":
        parts.append(
            "Отвечай в первую очередь вопросами и короткими подсказками. Один твой ответ — 1–3 наводящих вопроса и "
            "одно небольшое замечание по направлению мысли. Избегай готовых полных решений, пока пользователь сам не попросит."
        )
    elif role_id == "editor":
        parts.append(
            "Сначала коротко оцени текущий текст (1–2 предложения), затем предложи улучшенную версию. "
            "При необходимости добавь комментарии, почему та или иная правка делает текст лучше."
        )
    elif role_id == "analyst":
        parts.append(
            "Структурируй ответ в формате: контекст → варианты → сравнение (плюсы/минусы, риски, ресурсы) → рекомендация. "
            "Если данных мало, обязательно укажи, какие доп. вопросы нужно прояснить."
        )
    elif role_id == "child":
        parts.append(
            "Избегай длинных предложений. Лучше несколько очень простых фраз подряд. "
            "Используй бытовые примеры: игрушки, школа, семья, прогулки."
        )
    elif role_id == "facts":
        parts.append(
            "Отвечай только фактами: короткие тезисы или нумерованный список. Без вступлений и выводов. "
            "Если информация неточная или неполная — явно укажи это. Не додумывай."
        )
    elif role_id == "storyteller":
        parts.append(
            "Строй ответ как короткую историю с началом, развитием и выводом. "
            "Сохраняй фактическую точность, но подавай материал через образы и метафоры."
        )
    elif role_id == "motivator":
        parts.append(
            "Обязательно включай конкретные следующие шаги, которые человек может сделать в ближайшие 24 часа. "
            "Избегай пустых лозунгов, каждая фраза поддержки должна опираться на реальную возможность действия."
        )
    elif role_id == "critic":
        parts.append(
            "Начинай с краткого пересказа позиции пользователя, чтобы показать, что ты её понял. "
            "Затем спокойно перечисли слабые места и риски, после этого предложи, как можно усилить аргументацию или решение."
        )

    return " ".join(p.strip() for p in parts if p and p.strip())

main_keyboard = [[KeyboardButton(t) for t in row] for row in main_keyboard_buttons]
reply_markup = ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)

# ======================= Кнопки владельца =======================
owner_keyboard_buttons = [
    ["📤 Рассылка", "🗑 Скрыть меню"],
    ["🚫 Бан", "✅ Разбан"],
    ["📨 MsgUser", "✉️ SendTo"],
    ["📊 Статистика", "🧰 Диагностика"],
]

owner_keyboard_buttons_kb = [[KeyboardButton(t) for t in row] for row in owner_keyboard_buttons]
owner_keyboard = ReplyKeyboardMarkup(owner_keyboard_buttons_kb, resize_keyboard=True)

def get_main_keyboard(user_id: int | None = None):
    """Клавиатура: для владельца — админ-меню, для остальных — главное меню."""
    if user_id and user_id == OWNER_ID:
        return owner_keyboard
    return reply_markup

answer_mode_kb = get_ask_question_keyboard

def _format_public_model_name(model_id: str | None) -> str:
    if not model_id:
        return "не указана"
    return model_id.replace(":free", "").replace("/", " / ")

def _format_public_image_engine_name(engine_id: str | None) -> str:
    normalized = (engine_id or "").strip().lower()
    aliases = {
        "stable-diffusion-xl-1024-v1-0": "SDXL 1.0",
        "stable-image-core": "Stable Image Core",
        "stable-image-ultra": "Stable Image Ultra",
        "sd3": "Stable Diffusion 3",
    }
    if normalized in aliases:
        return aliases[normalized]
    if not normalized:
        return "не указана"
    return (engine_id or "").strip().replace("-", " ")


def _get_public_reply_style_label(role_id: str | None) -> str:
    normalized = (role_id or "neutral").strip()
    return ROLES.get(normalized, ROLES["neutral"])[0]


def build_start_text(
    user_obj: dict[str, Any] | None = None,
    *,
    active_chat_title: str | None = None,
    current_role: str | None = None,
) -> str:
    return (
        "Привет! Я ИИ-бот-помощник.\n\n"
        "Что умею:\n"
        "• отвечать на вопросы и вести диалог\n"
        "• понимать голосовые сообщения\n"
        "• разбирать фото, скриншоты и текст на них\n"
        "• создавать изображения по описанию\n"
        "• показывать погоду\n"
        "• сохранять отдельные чаты с историей\n\n"
        "Выбери действие в меню или просто напиши сообщение."
    )


def _get_public_ui_snapshot(user_id: int, user_data: Any | None = None) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    users_store = globals().get("users_store")
    user_obj = users_store.get_user(user_id) if users_store else {}
    active_chat = conversation_store.get_active_chat(user_id) if conversation_store is not None else None

    saved_role = str((user_obj or {}).get("reply_style") or "neutral").strip()
    if saved_role not in ROLES:
        saved_role = "neutral"

    current_role = saved_role
    if user_data is not None:
        try:
            role_candidate = str(user_data.get("reply_style") or "").strip()
        except Exception:
            role_candidate = ""
        if role_candidate in ROLES:
            current_role = role_candidate

    return user_obj or {}, active_chat, current_role


async def send_main_menu_hub(
    bot: Any,
    chat_id: int,
    user_id: int,
    user_data: Any | None = None,
    *,
    include_intro_panel: bool = False,
) -> None:
    if bot is None:
        return
    user_obj, active_chat, current_role = _get_public_ui_snapshot(user_id, user_data)
    await bot.send_message(
        chat_id=chat_id,
        text=build_start_text(
            user_obj,
            active_chat_title=active_chat["title"] if active_chat else None,
            current_role=current_role,
        ),
        reply_markup=get_main_keyboard(user_id),
    )
    if include_intro_panel:
        await bot.send_message(
            chat_id=chat_id,
            text=build_intro_panel_text(),
            reply_markup=get_intro_keyboard(),
        )


def build_help_text() -> str:
    return (
        "✨ Возможности бота\n\n"
        "• 💬 Диалог с ИИ — вопросы, тексты, идеи, объяснения\n"
        "• 🎤 Голосовые — можно говорить вместо текста\n"
        "• 🖼 Фото и скриншоты — распознаю текст и помогаю по задаче\n"
        "• 🎨 Изображения — создаю картинку по описанию\n"
        "• 🗂 Чаты — отдельные диалоги с историей\n"
        "• 🌦 Погода — прогноз по городу\n"
        "• 🧠 Факт дня — короткий интересный факт\n\n"
        "Полезные команды:\n"
        "/start — открыть главное меню\n"
        "/role — выбрать режим ответа\n"
        "/image — запустить генерацию изображения\n"
        "/weather — запросить прогноз\n"
        "/clear — очистить контекст активного чата"
    )

def build_quick_start_text() -> str:
    return (
        "⚡ Быстрый старт\n\n"
        "Выбери один из сценариев ниже:\n"
        "• начать диалог с ИИ\n"
        "• открыть список своих чатов\n"
        "• разобрать фото или скриншот\n"
        "• сгенерировать изображение\n"
        "• поменять режим ответа под задачу\n\n"
        "Если не хочется выбирать, просто пришли сообщение, голосовое или фото — я сам подхвачу нужный сценарий."
    )


def build_intro_panel_text() -> str:
    return (
        "✨ Быстрый доступ\n\n"
        "Ниже собраны самые полезные сценарии. Можно нажать кнопку или сразу написать вопрос в чат."
    )

# ======================= Inline-кнопки для онбординга =======================
def get_intro_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Начать диалог", callback_data="intro|ask"),
            InlineKeyboardButton("🗂 Мои чаты", callback_data="intro|chats"),
        ],
        [
            InlineKeyboardButton("🖼 Разобрать фото", callback_data="intro|photo"),
            InlineKeyboardButton("🎨 Создать изображение", callback_data="intro|image"),
        ],
        [
            InlineKeyboardButton("🪄 Режим ответа", callback_data="intro|roles"),
            InlineKeyboardButton("✨ Возможности", callback_data="show_help_intro"),
        ],
    ])

def get_back_to_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Вернуться в меню", callback_data="back_to_main")]
    ])


def _format_chat_timestamp(value: str | None) -> str:
    if not value:
        return "неизвестно"
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return str(value)[:16]
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def _chat_title_to_tag(value: str | None) -> str:
    raw = re.sub(r"\s+", "_", (value or "").strip().lower())
    raw = re.sub(r"[^0-9a-zа-я_]+", "", raw, flags=re.IGNORECASE)
    return raw[:24] or "chat"


def _trim_chat_button_title(title: str, max_len: int = 26) -> str:
    clean = re.sub(r"\s+", " ", (title or "Новый чат")).strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1].rstrip() + "…"


def _trim_history_message(text: str, max_len: int = 420) -> str:
    clean = clean_reply_for_display(text)
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3].rstrip() + "..."


def build_chat_manager_text(user_id: int, page: int = 0) -> str:
    active_chat = conversation_store.get_active_chat(user_id) if conversation_store is not None else None
    total = conversation_store.count_user_chats(user_id) if conversation_store is not None else 0
    active_title = _normalize_chat_title_text(active_chat["title"], fallback="не выбран") if active_chat else "не выбран"
    return (
        "🗂 Мои чаты\n\n"
        "Отдельные диалоги помогают не смешивать разные темы.\n\n"
        f"Активный чат: {active_title}\n"
        f"Всего чатов: {total}\n\n"
        "Выбери чат из списка или создай новый."
    )


def get_chat_manager_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    page = max(0, int(page))
    total = conversation_store.count_user_chats(user_id) if conversation_store is not None else 0
    total_pages = max(1, (total + CHAT_LIST_PAGE_SIZE - 1) // CHAT_LIST_PAGE_SIZE)
    current_page = min(page, total_pages - 1)
    chats = conversation_store.list_user_chats(user_id, limit=CHAT_LIST_PAGE_SIZE, offset=current_page * CHAT_LIST_PAGE_SIZE) if conversation_store is not None else []
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("➕ Новый чат", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}new|{current_page}"),
            InlineKeyboardButton("⚙️ Настройки", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}settings|{current_page}"),
        ]
    ]
    for chat in chats:
        prefix = "✅ " if chat.get("is_active") else ""
        rows.append([
            InlineKeyboardButton(
                f"{prefix}{_trim_chat_button_title(chat.get('title') or 'Новый чат')}",
                callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}open|{chat['id']}|{current_page}",
            )
        ])
    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if current_page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}list|{current_page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{current_page + 1}/{total_pages}", callback_data="noop"))
        if current_page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}list|{current_page + 1}"))
        rows.append(nav_row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}main")])
    return InlineKeyboardMarkup(rows)


def build_chat_card_text(chat: dict[str, Any] | None) -> str:
    if not chat:
        return "Чат не найден."
    status_label = "активен" if chat.get("is_active") else "доступен для выбора"
    return (
        "📋 Карточка чата\n\n"
        f"ID: {chat['public_id']}\n"
        f"Название: {chat['title']}\n"
        f"Статус: {status_label}\n"
        f"Сообщений: {chat['message_count']}\n"
        f"Создан: {_format_chat_timestamp(chat['created_at'])}\n\n"
        "Что можно сделать:\n"
        "• выбрать этот чат активным\n"
        "• открыть историю сообщений\n"
        "• переименовать или удалить\n\n"
        f"#id{chat['public_id']} #name_{_chat_title_to_tag(chat['title'])}"
    )


def get_chat_card_keyboard(chat_id: int, list_page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выбрать чат", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}select|{int(chat_id)}|{int(list_page)}")],
        [InlineKeyboardButton("📖 История", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}history|{int(chat_id)}|0|{int(list_page)}")],
        [InlineKeyboardButton("📄 Изменить название", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}rename|{int(chat_id)}|{int(list_page)}")],
        [InlineKeyboardButton("🗑 Удалить чат", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}delete|{int(chat_id)}|{int(list_page)}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}list|{int(list_page)}")],
    ])


def build_chat_history_text(user_id: int, chat_id: int, page: int = 0) -> str:
    page_data = conversation_store.get_chat_messages_page(user_id, chat_id, page=page, per_page=CHAT_HISTORY_PAGE_SIZE) if conversation_store is not None else None
    if not page_data:
        return "История этого чата пока недоступна."

    chat = page_data["chat"]
    header = (
        f"📖 История чата «{chat['title']}»\n\n"
        f"Страница {page_data['page'] + 1} из {page_data['total_pages']}\n"
        f"Всего сообщений: {page_data['total_messages']}\n\n"
    )
    if not page_data["messages"]:
        return header + "В этом чате пока нет сообщений. Напиши первый вопрос, и история начнёт заполняться."

    blocks: list[str] = []
    for item in page_data["messages"]:
        role_label = "👤 Ты" if item["role"] == "user" else "🤖 Бот" if item["role"] == "assistant" else f"• {item['role']}"
        blocks.append(f"{role_label}:\n{_trim_history_message(item['text'])}")
    return header + "\n\n".join(blocks)


def get_chat_history_keyboard(chat_id: int, page: int, total_pages: int, list_page: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Старее", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}history|{int(chat_id)}|{int(page - 1)}|{int(list_page)}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️ Новее", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}history|{int(chat_id)}|{int(page + 1)}|{int(list_page)}"))
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}open|{int(chat_id)}|{int(list_page)}")])
    return InlineKeyboardMarkup(rows)


def build_chat_settings_text(user_id: int) -> str:
    active_chat = conversation_store.get_active_chat(user_id) if conversation_store is not None else None
    active_title = _normalize_chat_title_text(active_chat["title"], fallback="не выбран") if active_chat else "не выбран"
    return (
        "⚙️ Настройки чата\n\n"
        "Здесь собраны быстрые действия для активного диалога.\n\n"
        f"Активный чат: {active_title}\n\n"
        "Доступно сейчас:\n"
        "• очистить историю активного чата\n"
        "• вернуться к списку всех чатов"
    )


def get_chat_settings_keyboard(list_page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Очистить активный чат", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}clear_active|{int(list_page)}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}list|{int(list_page)}")],
    ])


def get_chat_delete_confirm_keyboard(chat_id: int, list_page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}confirm_delete|{int(chat_id)}|{int(list_page)}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"{CHAT_MANAGER_CALLBACK_PREFIX}open|{int(chat_id)}|{int(list_page)}")],
    ])

# ======================= Данные (цитаты/факты/наука/случайные открытия) =======================

# ------------------ 100 фактов ------------------
facts = [
"Свет от далёких звёзд показывает, как они выглядели миллионы лет назад.",
"Человеческий мозг тратит больше энергии на мозговую активность, чем сердце.",
"Пчелы облетают 2 миллиона цветков, чтобы сделать килограмм меда.",
"У тигров полосатые не только мех, но и кожа.",
"Крокодилы глотают камни, чтобы нырять глубже.",
"Самки собак кусаются чаще, чем самцы.",
"Рога лосей очень чувствительны.",
"Альбатрос может спать прямо в полёте.",
"Самый большой мозг по отношению к телу — у муравья.",
"70% всех живых существ на Земле — бактерии.",
"Крот может прорыть туннель длиной 76 метров за одну ночь.",
"Кровь кузнечика белого цвета, лобстера — голубого.",
"В Швейцарии запрещено держать дома только одну морскую свинку.",
"Рев тигра может на мгновение парализовать человека.",
"Полет совы абсолютно бесшумен благодаря особым перьям.",
"Предок полярных медведей — бурый медведь.",
"Нужно 40 минут, чтобы сварить страусиное яйцо вкрутую.",
"Зоопарк в Токио закрывается на два месяца каждый год, чтобы животные отдохнули от посетителей.",
"Жирафы были названы «верблюдопардами» европейцами, когда те впервые их увидели.",
"По утрам мы выше примерно на 1 сантиметр.",
"Взрослый человек делает около 23 000 вдохов и выдохов в день.",
"Во рту человека около 40 000 бактерий.",
"У человека около 2000 вкусовых рецепторов.",
"Сильные люди тоже страдают от депрессии.",
"Мы погибаем при утрате всего 12% воды в организме.",
"Недосып делает нас глупее и вызывает ожирение.",
"Голубоглазые люди реже страдают от нарушений зрения.",
"Микеланджело писал фрески Сикстинской капеллы стоя на лесах, а не лежа.",
"Правая часть картины «Сотворение Адама», вероятно, анатомически правильное изображение человеческого мозга.",
"Древние египтяне учили бабуинов прислуживать за столом.",
"За последние 4000 лет не было одомашнено ни одно новое животное.",
"Самый длинный урок в истории длился 54 часа.",
"Горячая вода замерзает на морозе быстрее, чем холодная (эффект Мпембы).",
"Чистая вода — диэлектрик.",
"Только 1,1% водного запаса Земли пригоден для питья.",
"Самый популярный напиток в мире — кофе.",
"Существует ядовитое растение, которое в момент смерти вызывает у жертвы улыбку на лице.",
"Зеленый, желтый и красный сладкий перец — это один и тот же овощ разной степени зрелости.",
]


# ------------------ объединяем списки ------------------
import tempfile
import threading
import contextlib

# используем глобальный facts, если он есть; иначе пустой список
all_items = globals().get("facts", []) if globals().get("facts", None) is not None else []

# ------------------ перемешанный список и индекс ------------------
_shuffled_items = random.sample(all_items, len(all_items)) if all_items else []
_current_index = 0
_items_lock = threading.Lock()  # простая блокировка для get_next_item (потокобезопасность)

# ------------------ функция для выдачи нового элемента ------------------
def get_next_item():
    global _current_index, _shuffled_items, all_items
    # защита: если список пуст — возвращаем None
    if not all_items:
        return None

    with _items_lock:
        # если список закончился или по каким-то причинам пуст — перемешиваем заново
        if _current_index >= len(_shuffled_items) or not _shuffled_items:
            try:
                _shuffled_items = random.sample(list(all_items), len(all_items))
            except Exception:
                # на случай ошибки sample — просто копируем
                _shuffled_items = list(all_items)
            _current_index = 0

        # безопасный доступ к элементу
        try:
            item = _shuffled_items[_current_index]
        except Exception:
            # если что-то пошло не так — восстановим и попытаемся вернуть первый элемент
            _shuffled_items = list(all_items)
            _current_index = 0
            if not _shuffled_items:
                return None
            item = _shuffled_items[0]

        _current_index += 1
        return item

def transcribe_voice_ogg_to_text(ogg_path: str) -> str | None:
    """
    Преобразует OGG-файл в текст через Google STT.
    Требует speech_recognition и pydub.
    """
    try:
        import speech_recognition as sr
        from pydub import AudioSegment
    except ImportError:
        logging.warning("STT dependencies (speech_recognition, pydub) не установлены")
        return None

    if not ogg_path or not os.path.exists(ogg_path):
        return None

    wav_path = ogg_path + ".wav"
    try:
        # конвертация OGG -> WAV
        AudioSegment.from_file(ogg_path).set_frame_rate(16000).set_channels(1).export(wav_path, format="wav")

        r = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = r.record(source)
        return r.recognize_google(audio, language="ru-RU").strip()

    except sr.UnknownValueError:
        return None
    except Exception as e:
        logging.exception("STT error: %s", e)
        return None
    finally:
        with contextlib.suppress(Exception):
            if os.path.exists(wav_path):
                os.remove(wav_path)
# ======================= Edge-TTS =======================
import os
import logging
import contextlib

async def synthesize_to_ogg(text: str, chat_id: int = 0):
    """
    Синтезирует текст через Edge-TTS (Microsoft Neural TTS).
    Возвращает путь к .ogg (libopus) или .mp3, либо None при ошибке.
    """
    clean_text = clean_text_for_tts(text) if text else ""
    if not clean_text:
        logging.error("TTS: текст пуст после очистки")
        return None

    # Ограничение длины
    if len(clean_text) > 1500:
        clean_text = clean_text[:1500] + "..."

    mp3_path = f"voice_{chat_id}.mp3"
    ogg_path = f"voice_{chat_id}.ogg"

    # Выбор голоса через переменную окружения
    edge_voice = os.getenv("EDGE_VOICE", "ru-RU-DmitryNeural").strip()

    try:
        import edge_tts
    except ImportError:
        logging.warning("edge-tts не установлен — синтез невозможен")
        return None

    try:
        communicate = edge_tts.Communicate(clean_text, voice=edge_voice)
        await communicate.save(mp3_path)

        # Конвертация mp3 -> ogg (libopus) через pydub/ffmpeg
        try:
            from pydub import AudioSegment
            AudioSegment.from_file(mp3_path).export(ogg_path, format="ogg", codec="libopus")
            with contextlib.suppress(Exception):
                os.remove(mp3_path)
            return ogg_path
        except Exception as e:
            logging.warning("Edge-TTS: конверсия pydub/ffmpeg не удалась, верну mp3: %s", e)
            return mp3_path

    except Exception as e:
        logging.exception("Edge-TTS error (voice=%s): %s", edge_voice, e)
        return None


def _split_text_for_voice(text: str, max_chunk: int = 1200) -> list[str]:
    """
    Делит текст на части по границам предложений для TTS (ограничение Edge-TTS).
    """
    clean = clean_text_for_tts(text) if text else ""
    if not clean:
        return []
    if len(clean) <= max_chunk:
        return [clean]
    chunks: list[str] = []
    for part in re.split(r'(?<=[.!?\n])\s+', clean):
        part = part.strip()
        if not part:
            continue
        if len(part) > max_chunk:
            for sub in re.split(r'(?<=[,;:])\s+', part):
                sub = sub.strip()
                if not sub:
                    continue
                if chunks and len(chunks[-1]) + len(sub) + 1 <= max_chunk:
                    chunks[-1] += " " + sub
                else:
                    if len(sub) > max_chunk:
                        for i in range(0, len(sub), max_chunk):
                            chunks.append(sub[i:i + max_chunk])
                    else:
                        chunks.append(sub)
        else:
            if chunks and len(chunks[-1]) + len(part) + 1 <= max_chunk:
                chunks[-1] += " " + part
            else:
                chunks.append(part)
    return [c for c in chunks if c.strip()]


async def synthesize_and_send_voice(bot, chat_id: int, text: str) -> bool:
    """
    Синтезирует голосовое сообщение из текста и отправляет в чат.
    Длинный текст разбивается на части.
    """
    chunks = _split_text_for_voice(text, max_chunk=1200)
    if not chunks:
        return False

    sent = 0
    for i, chunk in enumerate(chunks):
        try:
            ogg_path = await synthesize_to_ogg(chunk, chat_id * 1000 + i)
            if ogg_path is None:
                continue
            try:
                with open(ogg_path, "rb") as audio:
                    ext = os.path.splitext(ogg_path)[1].lower()
                    if ext == ".ogg":
                        await bot.send_voice(chat_id=chat_id, voice=audio)
                    else:
                        await bot.send_audio(chat_id=chat_id, audio=audio)
                sent += 1
            finally:
                with contextlib.suppress(Exception):
                    if os.path.exists(ogg_path):
                        os.remove(ogg_path)
        except Exception as e:
            logging.exception("synthesize_and_send_voice chunk %s failed: %s", i, e)
    return sent > 0

# ========== Configuration defaults & globals (backwards-compatible) ==========
DEFAULT_HTTPX_TIMEOUT: float = float(os.getenv("DEFAULT_HTTPX_TIMEOUT", str(max(120, int(OPENROUTER_CHAT_TIMEOUT or 120)))) or str(max(120, int(OPENROUTER_CHAT_TIMEOUT or 120))))

# semaphore for OpenRouter concurrency (preserve global override if present)
OPENROUTER_MAX_CONCURRENT: int = globals().get("OPENROUTER_MAX_CONCURRENT", 3)
OPENROUTER_URL: str = globals().get("OPENROUTER_URL", "")
OPENROUTER_API_KEY: str = globals().get("OPENROUTER_API_KEY", "")

OPENROUTER_SEMAPHORE: asyncio.Semaphore = asyncio.Semaphore(OPENROUTER_MAX_CONCURRENT)

# ---- Ограничитель отправки сообщений Telegram (защита от flood control) ----
TELEGRAM_MAX_PARALLEL_SENDS: int = 3
TELEGRAM_MESSAGE_LIMITER: asyncio.Semaphore = asyncio.Semaphore(TELEGRAM_MAX_PARALLEL_SENDS)
TELEGRAM_MESSAGE_RATE_LIMIT: float = 0.9
TELEGRAM_LAST_MESSAGE_TIME: float = 0.0

# ---- Ограничители для обработки сообщений (защита от race conditions) ----
# Начальное значение будет заменено ниже на TrackedSemaphore.
MESSAGE_PROCESSING_SEMAPHORE: asyncio.Semaphore = asyncio.Semaphore(10)

# ---- Очередь задач для асинхронной обработки запросов ----
QUEST_QUEUE: asyncio.Queue | None = None  # Очередь для вопросов (инициализируется при старте)
QUEUE_WORKERS: int = 30  # Количество рабочих потоков для обработки очереди

async def rate_limited_send_message(bot, chat_id: int, text: str, **kwargs) -> Any | None:
    """
    Отправляет сообщение с защитой от Telegram flood control.
    Соблюдает минимальный интервал между отправками.
    """
    global TELEGRAM_LAST_MESSAGE_TIME
    
    if not bot:
        return None
    
    async with TELEGRAM_MESSAGE_LIMITER:
        try:
            # Ждем минимальный интервал
            current_time = asyncio.get_running_loop().time()
            time_since_last = current_time - TELEGRAM_LAST_MESSAGE_TIME
            
            if time_since_last < TELEGRAM_MESSAGE_RATE_LIMIT:
                await asyncio.sleep(TELEGRAM_MESSAGE_RATE_LIMIT - time_since_last)
            
            # Отправляем с retry
            for attempt in range(2):
                try:
                    result = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
                    TELEGRAM_LAST_MESSAGE_TIME = asyncio.get_running_loop().time()
                    return result
                except RetryAfter as e:
                    wait_time = getattr(e, "retry_after", 1)
                    if attempt == 0:
                        logging.warning("Telegram rate limit, waiting %.1f seconds", wait_time)
                        await asyncio.sleep(min(wait_time + 1, 15))  # Макс. 15 сек ожидания
                    else:
                        raise
        except Exception as e:
            logging.exception("rate_limited_send_message failed: %s", e)
            return None

# Shared httpx async client (single instance for process)
_SHARD_CLIENT_LOCK: asyncio.Lock = asyncio.Lock()
_SYNC_CLIENT_LOCK: ThreadLock = ThreadLock()
_SHARED_HTTPX_CLIENT: httpx.AsyncClient | None = None
_HISTORIES_LOCK: asyncio.Lock = asyncio.Lock()  # Защита доступа к user_histories


def _create_async_client(timeout: float) -> httpx.AsyncClient:
    """Create a fresh AsyncClient with optimized settings for concurrent requests.
    МАСШТАБИРОВАНИЕ: Увеличены лимиты на подключения для параллельной обработки 100+ одновременных запросов.
    """
    # Конфиг для пула подключений: поддержка 100+ одновременных запросов
    limits = httpx.Limits(
        max_connections=200,  # Макс. 200 одновременных подключений (было 100)
        max_keepalive_connections=100,  # Переиспользование подключений (было 50)
        keepalive_expiry=30.0  # TCP Keep-Alive timeout
    )
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        limits=limits,
        http2=_httpx_http2_enabled()  # Используем HTTP/2 для лучшей производительности
    )


async def async_get_httpx_client(timeout: float = DEFAULT_HTTPX_TIMEOUT) -> httpx.AsyncClient:
    """
    Return the global httpx.AsyncClient, creating it if necessary.
    Safe to call from async code: uses an asyncio.Lock to avoid races.
    """
    global _SHARED_HTTPX_CLIENT
    async with _SHARD_CLIENT_LOCK:
        client = _SHARED_HTTPX_CLIENT
        # Check for explicit closed state if available
        if client is None or getattr(client, "is_closed", False):
            logging.debug("Creating new shared AsyncClient (async path)")
            _SHARED_HTTPX_CLIENT = _create_async_client(timeout)
        return _SHARED_HTTPX_CLIENT  # type: ignore[return-value]


def get_httpx_client(timeout: float | None = None) -> httpx.AsyncClient:
    """
    Synchronous factory kept for backwards compatibility.

    IMPORTANT: prefer `await async_get_httpx_client()` when running inside
    an asyncio event loop. This function is thread-safe (uses threading.Lock)
    and will lazily create the same AsyncClient instance. It will not block
    the asyncio lock — but it avoids races between threads.
    """
    global _SHARED_HTTPX_CLIENT
    timeout_val = DEFAULT_HTTPX_TIMEOUT if timeout is None else float(timeout)

    with _SYNC_CLIENT_LOCK:
        client = _SHARED_HTTPX_CLIENT
        if client is None or getattr(client, "is_closed", False):
            logging.debug("Creating new shared AsyncClient (sync path)")
            _SHARED_HTTPX_CLIENT = _create_async_client(timeout_val)
        return _SHARED_HTTPX_CLIENT  # type: ignore[return-value]


async def close_shared_httpx_client() -> None:
    """Close the global httpx.AsyncClient if present.

    Safe to call multiple times. Logs any unexpected exceptions.
    """
    global _SHARED_HTTPX_CLIENT
    async with _SHARD_CLIENT_LOCK:
        client = _SHARED_HTTPX_CLIENT
        if client is not None:
            try:
                await client.aclose()
            except RuntimeError as exc:
                if "Event loop is closed" in str(exc):
                    logging.debug("Shared httpx client closed after event loop shutdown")
                else:
                    logging.exception("Failed to close shared httpx client")
            except Exception:
                logging.exception("Failed to close shared httpx client")
            finally:
                _SHARED_HTTPX_CLIENT = None


# Ensure clean shutdown on process exit (best-effort, non-blocking for main thread)
def _atexit_close_client() -> None:
    try:
        # If running loop is available, schedule closing
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # schedule aclose in running loop — fire-and-forget
            async def _aclose():
                await close_shared_httpx_client()

            try:
                loop.create_task(_aclose())
            except Exception:
                # fallback: synchronous close not possible for AsyncClient
                pass
        else:
            # No running loop — we can run a short loop to close
            try:
                asyncio.run(close_shared_httpx_client())
            except Exception:
                # last-resort: ignore
                pass
    except Exception:
        logging.exception("Error during atexit client close")


atexit.register(_atexit_close_client)


# ========== Metrics (simple, async-safe increments) ==========
_metrics: dict[str, int] = {
    "edits_attempted": 0,
    "edits_succeeded": 0,
    "edits_failed": 0,
    "progress_updates_sent": 0,
}
_METRICS_LOCK: asyncio.Lock = asyncio.Lock()


async def _metrics_inc(key: str, amount: int = 1) -> int:
    """Increment a named metric safely and return the new value."""
    async with _METRICS_LOCK:
        _metrics[key] = _metrics.get(key, 0) + amount
        return _metrics[key]


# expose for backward compatibility if someone imports metrics variable
metrics = _metrics


# ========== Safe Telegram edit/send helpers ==========
async def _exp_backoff_sleep(base: float, attempt: int) -> None:
    """Sleep with exponential backoff + jitter."""
    delay = base * (2 ** attempt) * (0.8 + random.random() * 0.4)
    await asyncio.sleep(delay)


async def safe_edit_message(
    bot: Any,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    max_retries: int = 4,
    base_delay: float = 0.7,
    fallback_send: bool = True,
    reply_markup: Any = None,
    parse_mode: str | None = None,
) -> bool:
    """
    Try to edit a message with retries and exponential backoff. On failures
    optionally sends a new message as a fallback.

    Returns True on success, False otherwise.
    """
    if bot is None:
        return False

    await _metrics_inc("edits_attempted")

    for attempt in range(max_retries):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            await _metrics_inc("edits_succeeded")
            return True
        except RetryAfter as exc:  # rate limit — wait the suggested time
            wait = getattr(exc, "retry_after", 1)
            logging.warning("RetryAfter in safe_edit_message, sleeping %.2fs", wait)
            await asyncio.sleep(wait + 0.1)
        except BadRequest as exc:
            # common: "Message is not modified" or "message too old" — fallback quickly
            logging.debug("BadRequest editing message (chat=%s,msg=%s): %s", chat_id, message_id, exc)
            # small delay for robustness, then break to fallback
            await asyncio.sleep(0.1 + random.random() * 0.2)
            break
        except Forbidden:
            logging.info("Cannot edit message: bot forbidden in chat %s", chat_id)
            return False
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            logging.debug("TelegramError editing message (attempt=%s): %s", attempt, exc)
            await _exp_backoff_sleep(base_delay, attempt)
        except Exception:
            logging.exception("Unexpected error in safe_edit_message (attempt %s)", attempt)
            await _exp_backoff_sleep(base_delay, attempt)

    # fallback: отправка нового сообщения
    if fallback_send:
        try:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            await _metrics_inc("progress_updates_sent")
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Fallback send_message also failed in safe_edit_message")

    await _metrics_inc("edits_failed")
    return False


async def safe_telegram_edit(
    bot: Any,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    fallback_send: bool = False,
    max_retries: int = 3,
    backoff_base: float = 0.4,
) -> Any | None:
    """
    Centralized edit helper: handles RetryAfter, Forbidden, BadRequest.
    Returns edited/sent message object on success, or None on final failure.
    """
    if bot is None:
        return None

    last_exc: BaseException | None = None

    for attempt in range(1, max_retries + 1):
        try:
            result = await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            return result
        except RetryAfter as exc:
            wait = getattr(exc, "retry_after", 1)
            logging.info(
                "safe_telegram_edit: RetryAfter received, waiting %.2fs (chat=%s, msg=%s)",
                wait,
                chat_id,
                message_id,
            )
            await asyncio.sleep(wait + 0.1)
            last_exc = exc
        except Forbidden as exc:
            logging.warning("safe_telegram_edit Forbidden (chat=%s, msg=%s): %s", chat_id, message_id, exc)
            if fallback_send:
                try:
                    return await bot.send_message(chat_id=chat_id, text=text)
                except Exception:
                    logging.exception("safe_telegram_edit: fallback send failed")
            return None
        except BadRequest as exc:
            logging.debug("safe_telegram_edit BadRequest (chat=%s,msg=%s): %s", chat_id, message_id, exc)
            if fallback_send:
                try:
                    return await bot.send_message(chat_id=chat_id, text=text)
                except Exception:
                    logging.exception("safe_telegram_edit: fallback send failed after BadRequest")
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            logging.warning(
                "safe_telegram_edit attempt %s failed (chat=%s,msg=%s): %s",
                attempt,
                chat_id,
                message_id,
                exc,
            )
            await _exp_backoff_sleep(backoff_base, attempt)

    logging.exception(
        "safe_telegram_edit: all attempts failed for chat=%s,msg=%s — last_exc=%s",
        chat_id,
        message_id,
        last_exc,
    )
    return None


# Optional: convenience helper for updating progress text
async def format_progress_text(percent_item: Any, prefix: str = "") -> str:
    """Format a small progress text using normalize_progress_item + stage lookup."""
    p, stage = normalize_progress_item(percent_item)
    if stage:
        return f"{prefix}{stage} ({p}% )"
    return f"{prefix}{stage_for(p)} ({p}%)"



# fallback storages if not defined in globals
user_histories: dict[str, list] = globals().get("user_histories", {})
conversation_store = globals().get("conversation_store", None)

# ProgressManager may be provided by the project; we try to use it if present
ProgressManager = globals().get("ProgressManager", None)

# ----------------------- Configuration defaults & globals -----------------------
OPENROUTER_URL = globals().get("OPENROUTER_URL", "https://api.openrouter.ai/v1/chat/completions")
OPENROUTER_API_KEY = globals().get("OPENROUTER_API_KEY", globals().get("OPENROUTER_KEY", ""))
OPENROUTER_MODEL = globals().get("OPENROUTER_MODEL", DEFAULT_MAINBOT_MODEL)
OPENROUTER_MAX_CONCURRENT = int(globals().get("OPENROUTER_MAX_CONCURRENT", 3))  # Увеличено с 1 до 15 для одновременной обработки
OPENROUTER_SEMAPHORE = globals().get("OPENROUTER_SEMAPHORE", asyncio.Semaphore(OPENROUTER_MAX_CONCURRENT))

# HTTP client factory: если проект предоставляет get_httpx_client — используем, иначе дефолт
def _default_httpx_client(timeout: int = 90) -> httpx.AsyncClient:
    limits = httpx.Limits(
        max_connections=200,
        max_keepalive_connections=100,
        keepalive_expiry=30.0
    )
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        limits=limits,
        http2=_httpx_http2_enabled()
    )

get_httpx_client = globals().get("get_httpx_client", _default_httpx_client)

# ------------------ fake progress updater (PTB async) ------------------
async def fake_progress_updater_ptb(
    bot,
    chat_id: int,
    message_id: int,
    done_event: asyncio.Event,
    min_interval: float = 0.6,
    max_target: int = 97
):
    percent = 0
    last_sent = (-1, "")
    base_increments = [8, 7, 6, 5, 4, 3, 3, 2, 2, 1]
    try:
        i = 0
        while not done_event.is_set():
            if i < len(base_increments):
                percent += base_increments[i]; i += 1
            else:
                step = 1 if percent >= max_target - 5 else random.randint(1, 2)
                percent += step
            percent = min(max_target, percent)

            stage = stage_for(percent)
            if (percent, stage) != last_sent:
                last_sent = (percent, stage)
                text = f"⏳ {percent}% — {stage}"
                try:
                    await safe_edit_message(bot, chat_id, message_id, text, fallback_send=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception("Unexpected error editing progress (fake)")
            await asyncio.sleep(min_interval + random.random() * 0.3)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("fake_progress_updater_ptb crashed")

# ------------------ streaming progress updater (PTB async) ------------------
async def streaming_progress_updater_ptb(
    bot,
    chat_id: int,
    message_id: int,
    progress_queue: "asyncio.Queue[Any]",
    done_event: asyncio.Event,
    min_interval: float = 0.7
):
    last_text = ""
    last_edit_ts = 0.0
    loop = asyncio.get_running_loop()
    try:
        while True:
            if done_event.is_set() and progress_queue.empty():
                break
            try:
                item = await asyncio.wait_for(progress_queue.get(), timeout=0.8)
            except asyncio.TimeoutError:
                await asyncio.sleep(0.05)
                continue
            text = format_progress_stage_text(item)
            now = loop.time()
            if text != last_text and (now - last_edit_ts) >= min_interval:
                last_text = text
                last_edit_ts = now
                try:
                    await safe_edit_message(bot, chat_id, message_id, text, fallback_send=False)
                except RetryAfter as e:
                    wait = getattr(e, "retry_after", 1)
                    logging.info("Telegram RetryAfter in streaming progress: sleeping %s", wait)
                    await asyncio.sleep(wait + 0.1)
                except Exception:
                    logging.exception("Unexpected error editing streaming progress")
            try:
                progress_queue.task_done()
            except Exception:
                pass
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("streaming_progress_updater_ptb crashed")


async def typing_heartbeat_ptb(
    bot,
    chat_id: int,
    done_event: asyncio.Event,
    *,
    action: str = ChatAction.TYPING,
    interval: float = TYPING_HEARTBEAT_INTERVAL,
) -> None:
    if bot is None or chat_id is None:
        return
    try:
        while not done_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action)
            except RetryAfter as exc:
                await asyncio.sleep(getattr(exc, "retry_after", 1) + 0.1)
            except Forbidden:
                return
            except Exception:
                logging.debug("typing_heartbeat_ptb: send_chat_action failed", exc_info=True)

            try:
                await asyncio.wait_for(done_event.wait(), timeout=max(1.5, float(interval)))
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("typing_heartbeat_ptb crashed")


def should_use_visible_status_for_request(text: str, *, source: str = "text") -> bool:
    normalized = normalize_spaces(text or "")
    if source in {"photo", "voice"}:
        return True
    if len(normalized) >= 90 or "\n" in (text or ""):
        return True
    if len(normalized.split()) >= 9:
        return True
    if re.search(r"[=≈+\-/*^%]", normalized):
        return True
    return any(
        token in normalized.lower()
        for token in ("объясни", "подробно", "разбери", "реши", "сравни", "почему", "докажи")
    )


async def start_response_feedback(
    bot,
    chat_id: int,
    *,
    reply_to_message_id: int | None = None,
    initial_text: str = "🧠 Разбираю вопрос…",
    show_status: bool = True,
    chat_action: str = ChatAction.TYPING,
) -> tuple[Any | None, asyncio.Queue[Any] | None, asyncio.Event, Any | None, Any | None]:
    indicator_msg = None
    progress_q: asyncio.Queue[Any] | None = asyncio.Queue() if show_status else None
    done_event = asyncio.Event()
    updater_task = None

    if show_status and bot is not None:
        with contextlib.suppress(Exception):
            indicator_msg = await bot.send_message(
                chat_id=chat_id,
                text=initial_text,
                reply_to_message_id=reply_to_message_id,
            )

    if indicator_msg is not None and progress_q is not None and getattr(indicator_msg, "message_id", None):
        updater_task = asyncio.create_task(
            streaming_progress_updater_ptb(bot, chat_id, indicator_msg.message_id, progress_q, done_event)
        )

    typing_task = None
    if bot is not None:
        typing_task = asyncio.create_task(
            typing_heartbeat_ptb(bot, chat_id, done_event, action=chat_action)
        )

    return indicator_msg, progress_q, done_event, updater_task, typing_task


async def finish_response_feedback(
    bot,
    chat_id: int,
    *,
    indicator_msg: Any | None,
    progress_q: asyncio.Queue[Any] | None,
    done_event: asyncio.Event,
    updater_task: Any | None,
    typing_task: Any | None,
    done_text: str = "✅ Ответ готов",
    delete_indicator: bool = True,
) -> None:
    if progress_q is not None:
        with contextlib.suppress(Exception):
            await progress_q.put(make_progress_payload(done_text, done=True))

    done_event.set()

    if updater_task is not None:
        try:
            await asyncio.wait_for(updater_task, timeout=1.2)
        except asyncio.TimeoutError:
            updater_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.debug("finish_response_feedback: updater_task failed", exc_info=True)

    if typing_task is not None and not typing_task.done():
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task

    if indicator_msg is not None and bot is not None:
        with contextlib.suppress(Exception):
            await safe_edit_message(bot, chat_id, indicator_msg.message_id, done_text, fallback_send=False)
        if delete_indicator:
            await asyncio.sleep(STATUS_MESSAGE_DELETE_DELAY)
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=indicator_msg.message_id)

# ------------------ make_openrouter_progress_helpers ------------------
def make_openrouter_progress_helpers(
    progress_queue: Any,
    bot,
    chat_id: int | None,
    progress_msg,
    stop_event: asyncio.Event
) -> tuple[Callable[[Any], Any], Callable[[], Any]]:
    pm = None
    try:
        if bot and chat_id and ProgressManager:
            try:
                pm = ProgressManager(bot, chat_id, progress_msg)
            except Exception:
                logging.debug("ProgressManager instantiation failed", exc_info=True)
    except Exception:
        logging.debug("No ProgressManager available / failed to instantiate", exc_info=True)

    async def _push_to_queue(payload: dict[str, Any]):
        try:
            if progress_queue is None:
                return
            if isinstance(progress_queue, asyncio.Queue):
                await progress_queue.put(payload); return
            put_coro = getattr(progress_queue, "put", None)
            if callable(put_coro):
                try:
                    res = put_coro(payload)
                    if asyncio.iscoroutine(res):
                        await res; return
                except TypeError:
                    pass
            put_nowait = getattr(progress_queue, "put_nowait", None)
            if callable(put_nowait):
                try:
                    put_nowait(payload); return
                except Exception:
                    pass
            if callable(progress_queue):
                try:
                    progress_queue(payload); return
                except Exception:
                    logging.debug("Sync progress callback raised", exc_info=True)
        except Exception:
            logging.exception("Failed to push progress to progress_queue/callback")

    async def _send_progress(item: Any):
        p, s = normalize_progress_item(item)
        stage = s if s else stage_for(p)
        payload = {"percent": p, "stage": stage, "status_text": stage}
        await _push_to_queue(payload)
        try:
            if pm is not None:
                try:
                    await pm.update(p, stage)
                except Exception:
                    logging.debug("ProgressManager.update failed", exc_info=True)
            elif progress_msg is not None and bot is not None:
                text = stage
                try:
                    await safe_edit_message(bot, chat_id, progress_msg.message_id, text)
                except RetryAfter as e:
                    await asyncio.sleep(getattr(e, "retry_after", 1) + 0.1)
                except Exception:
                    logging.exception("Failed to edit progress message in _send_progress")
        except Exception:
            logging.exception("Unexpected error in _send_progress")

    async def _progress_updater():
        staged_messages = [
            "🌐 Подключаюсь к модели…",
            "🧠 Думаю над ответом…",
            "✍️ Формулирую ответ…",
            "🔎 Перепроверяю детали…",
        ]
        stage_idx = 0
        if pm is not None:
            try:
                await pm.start()
            except Exception:
                logging.debug("ProgressManager.start failed", exc_info=True)
        try:
            while not stop_event.is_set():
                await _send_progress(make_progress_payload(staged_messages[stage_idx]))
                stage_idx = min(stage_idx + 1, len(staged_messages) - 1)
                await asyncio.sleep(2.2 + random.random() * 0.5)
        except asyncio.CancelledError:
            return
        except Exception:
            logging.exception("progress_updater crashed")
        finally:
            if pm is not None:
                try:
                    await pm.stop()
                except Exception:
                    logging.debug("ProgressManager.stop failed", exc_info=True)

    return _send_progress, _progress_updater

# ------------------------ HTTP helper with retry ------------------------
async def _http_post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict,
    json_data: dict,
    request_timeout: float | httpx.Timeout | None = None,
    retries: int = 3,
    base_backoff: float = 0.6
) -> httpx.Response:
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = await client.post(url, headers=headers, json=json_data, timeout=request_timeout)
            return resp
        except (httpx.TransportError, httpx.RequestError) as e:
            last_exc = e
            sleep_for = base_backoff * (2 ** (attempt - 1)) * (0.8 + random.random() * 0.4)
            error_text = str(e).strip() or type(e).__name__
            logging.warning("HTTP POST attempt %s failed, sleeping %.2fs: %s", attempt, sleep_for, error_text)
            if attempt >= retries:
                break
            await asyncio.sleep(sleep_for)
    if last_exc:
        raise last_exc
    raise RuntimeError("Unknown error in _http_post_with_retry")


async def _openrouter_post_with_key_pool(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_data: dict,
    request_timeout: float,
    retries: int,
    base_backoff: float,
) -> tuple[httpx.Response, str]:
    if is_openrouter_proxy_enabled():
        response = await _http_post_with_retry(
            client,
            url,
            headers=build_openrouter_headers(),
            json_data=json_data,
            request_timeout=request_timeout,
            retries=retries,
            base_backoff=base_backoff,
        )
        return response, "proxy"

    candidates = get_openrouter_key_candidates()
    if not candidates:
        raise RuntimeError("Нет доступных OpenRouter API ключей")

    request_budget = max(6.0, float(request_timeout or OPENROUTER_CHAT_TIMEOUT or 120))
    budget_started_at = time.perf_counter()
    last_exc: Exception | None = None
    last_response: httpx.Response | None = None
    last_label = "key"
    for index, item in enumerate(candidates):
        label = str(item.get("label") or "key")
        api_key = str(item.get("api_key") or "").strip()
        if not api_key:
            continue
        last_label = label
        elapsed = max(0.0, time.perf_counter() - budget_started_at)
        remaining_budget = max(0.0, request_budget - elapsed)
        if remaining_budget <= 0.5:
            break
        remaining_candidates = max(1, len(candidates) - index)
        per_key_timeout = min(
            remaining_budget,
            max(float(OPENROUTER_KEY_POOL_MIN_PER_KEY_TIMEOUT or 4.0), remaining_budget / remaining_candidates),
        )
        try:
            response = await _http_post_with_retry(
                client,
                url,
                headers=build_openrouter_headers(api_key),
                json_data=json_data,
                request_timeout=per_key_timeout,
                retries=max(1, int(retries)),
                base_backoff=base_backoff,
            )
        except (httpx.TimeoutException, httpx.TransportError, httpx.RequestError) as exc:
            last_exc = exc
            mark_openrouter_key_failure(label, status_code=598, error_text=str(exc))
            logging.warning("OpenRouter direct key failed: key=%s error=%s", label, type(exc).__name__)
            continue

        embedded_status, embedded_message = extract_openrouter_error_payload(response)
        if embedded_message and should_rotate_openrouter_key_after_response(embedded_status, embedded_message):
            last_response = response
            cooldown_override, status_override = get_openrouter_rate_limit_policy(response, embedded_status, embedded_message)
            mark_openrouter_key_failure(
                label,
                status_code=embedded_status,
                error_text=embedded_message,
                cooldown_seconds_override=cooldown_override,
                status_override=status_override,
            )
            logging.warning("OpenRouter direct key got embedded error: key=%s status=%s", label, embedded_status)
            continue

        status = int(getattr(response, "status_code", 0) or 0)
        text_pre = (getattr(response, "text", "") or "")[:2000]
        if should_rotate_openrouter_key_after_response(status, text_pre):
            last_response = response
            cooldown_override, status_override = get_openrouter_rate_limit_policy(response, status, text_pre)
            mark_openrouter_key_failure(
                label,
                status_code=status,
                error_text=text_pre,
                cooldown_seconds_override=cooldown_override,
                status_override=status_override,
            )
            logging.warning("OpenRouter direct key got retryable status: key=%s status=%s", label, status)
            continue

        mark_openrouter_key_success(label)
        return response, label

    if last_response is not None:
        return last_response, last_label
    if last_exc is not None:
        raise last_exc
    if time.perf_counter() - budget_started_at >= request_budget:
        raise httpx.ReadTimeout(f"OpenRouter key pool budget exhausted after {request_budget:.1f}s")
    raise RuntimeError("Не удалось выполнить запрос ни одним OpenRouter API ключом")

def _extract_reply_from_openrouter(resp_json: dict) -> str:
    try:
        choices = resp_json.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            msg = first.get("message") if isinstance(first, dict) else None
            if isinstance(msg, dict):
                cont = msg.get("content")
                if isinstance(cont, str) and cont.strip():
                    return cont
            txt = first.get("text")
            if isinstance(txt, str) and txt.strip():
                return txt
        for key in ("text", "message", "output", "generated_text", "result"):
            v = resp_json.get(key)
            if isinstance(v, str) and v.strip():
                return v
        output = resp_json.get("output")
        if isinstance(output, dict):
            choices2 = output.get("choices")
            if isinstance(choices2, list) and choices2:
                c0 = choices2[0]
                if isinstance(c0, dict):
                    for k in ("content", "text"):
                        if k in c0 and isinstance(c0[k], str):
                            return c0[k]
        return json.dumps(resp_json)[:10000]
    except Exception:
        logging.exception("Failed to extract reply from OpenRouter JSON")
        return json.dumps(resp_json)[:10000]

async def chat_with_openrouter(
    user_id: int,
    message: str,
    bot=None,
    chat_id: int | None = None,
    progress_queue=None,
    timeout: int = OPENROUTER_CHAT_TIMEOUT,
    context: str | None = None,
    persist_history: bool = True,
    **kwargs,
) -> str:
    """
    Надёжный слой поверх OpenRouter:
    - аккуратная история диалога
    - единый системный промпт с опциональным контекстом роли
    - ограничение параллельных запросов семафором
    - устойчивый HTTP-клиент с повторными попытками
    - поддержка прогресса (через очередь или отдельное сообщение)
    """

    # 1. История активного чата пользователя
    chat_session_id = None
    history_key = None
    if persist_history and "conversation_store" in globals() and conversation_store is not None:
        with contextlib.suppress(Exception):
            chat_session_id = kwargs.get("chat_session_id") or conversation_store.get_active_chat_id(user_id)
    history_key = _history_state_key(user_id, chat_session_id)

    try:
        async with _HISTORIES_LOCK:
            history = list(user_histories.get(history_key, []))
    except Exception:
        history = []

    if persist_history and not history and "conversation_store" in globals() and conversation_store is not None:
        with contextlib.suppress(Exception):
            history = list(conversation_store.get_recent_model_messages(user_id, chat_session_id, OPENROUTER_HISTORY_LIMIT))

    history = history[-OPENROUTER_HISTORY_LIMIT:]
    request_history = history + [{"role": "user", "content": message}]
    if persist_history:
        try:
            async with _HISTORIES_LOCK:
                user_histories[history_key] = request_history[-OPENROUTER_HISTORY_LIMIT:]
        except Exception:
            pass

    has_image = bool(kwargs.get("has_image", False))
    task_type = classify_task_type(message, has_image)
    requested_model = str((kwargs.get("model") or choose_model(message, has_image=has_image) or OPENROUTER_MODEL)).strip()
    chosen_model = str((get_effective_model_for_request(requested_model, task_type=task_type) or requested_model or OPENROUTER_MODEL)).strip()
    logging.info(
        "ModelRouter: type=%s model=%s user=%s chat=%s",
        task_type,
        chosen_model,
        user_id,
        chat_session_id or chat_id or "-",
    )

    # 3. Системный промпт
    base_system_prompt = (
"Ты — сильный универсальный ИИ-помощник в Telegram, но главное — ты лучший друг пользователя. "
    "Отвечай так, как будто ты реально его кореш: быстро, по делу, с душой, точно и по-человечески.\n\n"

    "Как мы общаемся:\n"
    "• Сразу давай самый полезный и прямой ответ, без лишней прелюдии.\n"
    "• Если тема сложная — сначала коротко и ясно по сути, потом при необходимости разбери по шагам.\n"
    "• Всегда держи баланс: точность + понятность + нормальный человеческий вайб.\n\n"

    "Что важно всегда соблюдать:\n"
    "1. Никогда не выдумывай факты, цифры и события.\n"
    "2. Если не уверен — честно скажи и объясни, в чём сомневаешься.\n"
    "3. Не раздувай ответы. Простой вопрос — короткий ответ.\n"
    "4. Просит решение — давай готовое решение по существу.\n"
    "5. Просит объяснение — делай его простым, структурированным и без воды.\n"
    "6. Прислал текст, фото или голос — сначала пойми смысл, потом уже отвечай по делу.\n"
    "7. Увидел ошибку или риск — спокойно и по-дружески скажи об этом.\n"
    "8. Просит формулу, промпт, перевод, расчёт — начинай сразу с ответа, без «вот решение» и прочей ерунды.\n"
    "9. Ты в Telegram: никаких таблиц, HTML, сложных схем. Только короткие абзацы и простые списки.\n\n"

    "Стиль:\n"
    "• Пиши естественно, уверенно и по-доброму, как лучший друг.\n"
    "• Без канцелярита, пафоса и пустых фраз.\n"
    "• Можно добавить 0–3 эмодзи на весь ответ, если они реально делают текст теплее и живее.\n"
    "• Не лепи эмодзи в каждую строку и не используй их в серьёзных моментах.\n"
    "• Никогда не играй роль персонажа. Просто будь очень сильным, надёжным и приятным корешем."
    )



    chat_memory_key = build_memory_chat_key(user_id, chat_session_id)
    memory_block = build_memory_system_block(user_id, chat_memory_key, message, limit=8) if persist_history else ""

    system_parts: list[str] = []
    if context:
        system_parts.append(str(context).strip())
    if memory_block:
        system_parts.append(memory_block)
    system_parts.append(base_system_prompt)
    system_text = "\n\n".join(part for part in system_parts if part)

    system_prompt = {"role": "system", "content": system_text}
    messages = [system_prompt] + request_history[-OPENROUTER_HISTORY_LIMIT:]

    headers = build_openrouter_headers()
    data = {"model": chosen_model, "messages": messages, "max_tokens": OPENROUTER_CHAT_MAX_TOKENS}

    progress_msg = None
    stop_event = asyncio.Event()
    updater_task = None
    send_progress = None
    response_status_code: int | None = None
    request_started_at = _openrouter_runtime_request_started()
    reply = "Сервис ответов временно недоступен. Пожалуйста, попробуйте через минуту."

    try:
        # 4. Если внешняя очередь прогресса не передана — создаём собственный индикатор
        if bot and chat_id and progress_queue is None:
            with contextlib.suppress(Exception):
                progress_msg = await bot.send_message(chat_id=chat_id, text="🧠 Разбираю вопрос…")

        send_progress, progress_updater_coro = make_openrouter_progress_helpers(
            progress_queue, bot, chat_id, progress_msg, stop_event
        )

        if (bot and chat_id) or (progress_queue is not None):
            updater_task = asyncio.create_task(progress_updater_coro())
        if send_progress is not None:
            with contextlib.suppress(Exception):
                await send_progress(make_progress_payload("🔎 Собираю контекст ответа…"))

        # 5. HTTP‑клиент
        created_client = False
        try:
            if send_progress is not None:
                with contextlib.suppress(Exception):
                    await send_progress(make_progress_payload("🌐 Подключаюсь к модели…"))
            async_client = await async_get_httpx_client(timeout=timeout)
        except Exception:
            logging.exception("Failed to acquire shared AsyncClient, using a temporary client")
            async_client = _default_httpx_client(timeout=timeout)
            created_client = True

        async def _request_once(client: httpx.AsyncClient, model_id: str, request_timeout: float) -> httpx.Response:
            data["model"] = model_id
            response, key_label = await _openrouter_post_with_key_pool(
                client,
                get_openrouter_request_url(),
                json_data=data,
                request_timeout=request_timeout,
                retries=1,
                base_backoff=0.6,
            )
            logging.info("OpenRouter request path: model=%s key=%s timeout=%.1fs", model_id, key_label, request_timeout)
            return response

        async def _request_with_runtime_fallback(client: httpx.AsyncClient, initial_model: str) -> tuple[httpx.Response, str]:
            current_model = initial_model
            attempted_models: set[str] = set()
            fallback_started_at = time.perf_counter()
            attempt_index = 0
            while True:
                attempted_models.add(current_model)
                request_timeout = get_attempt_request_timeout(
                    float(timeout),
                    task_type=task_type,
                    attempt_index=attempt_index,
                    started_at=fallback_started_at,
                )
                attempt_started_at = time.perf_counter()
                try:
                    response = await _request_once(client, current_model, request_timeout=request_timeout)
                except (httpx.TimeoutException, httpx.TransportError, httpx.RequestError) as exc:
                    mark_model_temporarily_degraded(current_model, f"{type(exc).__name__}: {exc}")
                    fallback_model = fallback_model_for_runtime_issue(current_model, task_type=task_type)
                    if fallback_model and fallback_model not in attempted_models:
                        logging.warning(
                            "Model %s request failed (%s) after %.1fs, retrying with runtime fallback %s",
                            current_model,
                            type(exc).__name__,
                            request_timeout,
                            fallback_model,
                        )
                        if send_progress is not None:
                            with contextlib.suppress(Exception):
                                await send_progress(make_progress_payload("🔁 Основная модель тормозит, переключаюсь на резервную…"))
                        current_model = fallback_model
                        attempt_index += 1
                        continue
                    raise
                attempt_elapsed = max(0.0, time.perf_counter() - attempt_started_at)
                if task_type == "simple" and attempt_elapsed >= MODEL_SLOW_RESPONSE_THRESHOLD:
                    mark_model_temporarily_degraded(current_model, f"slow_simple_response:{attempt_elapsed:.2f}s")
                else:
                    clear_model_degraded_flag(current_model)

                status_pre = getattr(response, "status_code", None)
                text_pre = getattr(response, "text", "")[:2000]
                if is_model_not_found_response(status_pre, text_pre) or is_empty_success_openrouter_response(response):
                    fallback_model = fallback_model_for_runtime_issue(current_model, task_type=task_type)
                    if fallback_model and fallback_model not in attempted_models:
                        logging.warning("Model %s returned unusable response, retrying with %s", current_model, fallback_model)
                        if send_progress is not None:
                            with contextlib.suppress(Exception):
                                await send_progress(make_progress_payload("🔁 Повторяю запрос на резервной модели…"))
                        current_model = fallback_model
                        attempt_index += 1
                        continue
                if should_retry_model_after_response(status_pre, text_pre):
                    mark_model_temporarily_degraded(current_model, f"retryable_status:{status_pre}")
                    fallback_model = fallback_model_for_runtime_issue(current_model, task_type=task_type)
                    if fallback_model and fallback_model not in attempted_models:
                        logging.warning("Model %s returned retryable status=%s, retrying with %s", current_model, status_pre, fallback_model)
                        if send_progress is not None:
                            with contextlib.suppress(Exception):
                                await send_progress(make_progress_payload("🔁 Основная модель нестабильна, перехожу на запасную…"))
                        current_model = fallback_model
                        attempt_index += 1
                        continue
                return response, current_model

        # 6. Основной запрос с семафором и ретраями
        async with OPENROUTER_SEMAPHORE:
            if send_progress is not None:
                with contextlib.suppress(Exception):
                    await send_progress(make_progress_payload("🧠 Думаю над ответом…"))
            if created_client:
                async with async_client:
                    resp, chosen_model = await _request_with_runtime_fallback(async_client, chosen_model)
                    status_pre = getattr(resp, "status_code", None)
                    text_pre = getattr(resp, "text", "")[:2000]
                    if is_model_not_found_response(status_pre, text_pre):
                        fallback_model = fallback_model_for_unavailable(chosen_model)
                        if fallback_model:
                            logging.warning("Model %s unavailable, retry with fallback %s", chosen_model, fallback_model)
                            chosen_model = fallback_model
                            if send_progress is not None:
                                with contextlib.suppress(Exception):
                                    await send_progress(make_progress_payload("🔁 Переключаю модель и повторяю запрос…"))
                            resp = await _request_once(async_client, chosen_model, request_timeout=max(OPENROUTER_MIN_FALLBACK_TIMEOUT, float(timeout)))
            else:
                resp, chosen_model = await _request_with_runtime_fallback(async_client, chosen_model)
                status_pre = getattr(resp, "status_code", None)
                text_pre = getattr(resp, "text", "")[:2000]
                if is_model_not_found_response(status_pre, text_pre):
                    fallback_model = fallback_model_for_unavailable(chosen_model)
                    if fallback_model:
                        logging.warning("Model %s unavailable, retry with fallback %s", chosen_model, fallback_model)
                        chosen_model = fallback_model
                        if send_progress is not None:
                            with contextlib.suppress(Exception):
                                await send_progress(make_progress_payload("🔁 Переключаю модель и повторяю запрос…"))
                        resp = await _request_once(async_client, chosen_model, request_timeout=max(OPENROUTER_MIN_FALLBACK_TIMEOUT, float(timeout)))

        # 7. Обработка ответа
        status = getattr(resp, "status_code", None)
        response_status_code = int(status) if isinstance(status, int) else None
        text_snippet = getattr(resp, "text", "")[:2000] if resp is not None else ""
        if status is None or status >= 400:
            logging.error("[OpenRouter] non-2xx status: %s %s", status, text_snippet)
        else:
            try:
                if send_progress is not None:
                    with contextlib.suppress(Exception):
                        await send_progress(make_progress_payload("✍️ Формулирую ответ…"))
                resp_json = resp.json()
            except Exception:
                logging.exception("Failed to parse OpenRouter JSON response")
                resp_json = {}
            reply = _extract_reply_from_openrouter(resp_json)
            if is_empty_openrouter_reply(reply):
                fallback_model = fallback_model_for_runtime_issue(chosen_model, task_type=task_type)
                if fallback_model and fallback_model != chosen_model:
                    logging.warning("Model %s returned empty reply payload, retrying with %s", chosen_model, fallback_model)
                    if send_progress is not None:
                        with contextlib.suppress(Exception):
                            await send_progress(make_progress_payload("🔁 Уточняю ответ на резервной модели…"))
                    if created_client:
                        async with _default_httpx_client(timeout=timeout) as retry_client:
                            resp = await _request_once(retry_client, fallback_model, request_timeout=max(OPENROUTER_MIN_FALLBACK_TIMEOUT, float(timeout)))
                    else:
                        resp = await _request_once(async_client, fallback_model, request_timeout=max(OPENROUTER_MIN_FALLBACK_TIMEOUT, float(timeout)))
                    chosen_model = fallback_model
                    response_status_code = int(getattr(resp, "status_code", 0) or 0)
                    try:
                        resp_json = resp.json()
                    except Exception:
                        logging.exception("Failed to parse fallback OpenRouter JSON response")
                        resp_json = {}
                    reply = _extract_reply_from_openrouter(resp_json)

    except Exception as e:
        logging.exception("[OpenRouter] Ошибка запроса/обработки: %s", e)

    finally:
        # 8. Завершаем прогресс и сохраняем ответ
        stop_event.set()
        _openrouter_runtime_request_finished(request_started_at, response_status_code)
        if send_progress is not None:
            with contextlib.suppress(Exception):
                await send_progress(make_progress_payload("✅ Ответ готов", done=True))

        if updater_task:
            try:
                updater_task.cancel()
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(updater_task, timeout=2.0)
            except Exception:
                logging.exception("Error cancelling updater_task")

        if progress_msg is not None and bot is not None:
            with contextlib.suppress(Exception):
                await safe_edit_message(bot, chat_id, progress_msg.message_id, "✅ Ответ готов", fallback_send=False)

        if persist_history:
            try:
                updated_history = request_history + [{"role": "assistant", "content": reply}]
                async with _HISTORIES_LOCK:
                    user_histories[history_key] = updated_history[-OPENROUTER_HISTORY_LIMIT:]
            except Exception:
                pass

            try:
                if "conversation_store" in globals() and hasattr(conversation_store, "add_message"):
                    await conversation_store.add_message(user_id, "user", message, chat_id=chat_session_id)
                    await conversation_store.add_message(user_id, "assistant", reply, chat_id=chat_session_id)
            except Exception:
                logging.debug("Failed to persist assistant message")

            with contextlib.suppress(Exception):
                memory_store_turn(
                    user_id,
                    chat_memory_key,
                    msg_id=None,
                    role="user",
                    text=message,
                    importance=0.68,
                )
                memory_store_turn(
                    user_id,
                    chat_memory_key,
                    msg_id=None,
                    role="assistant",
                    text=reply,
                    importance=0.78,
                )

    return reply


# ----------------------- End of progress updaters / OpenRouter integration ----------------------- что можем улучшить
# ----------------------- Streaming text response -----------------------

def _is_non_fatal_edit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "not modified" in text or "message is not modified" in text or "flood" in text


async def _text_chunk_iterator(text: str, *, chunk_size: int = 65, delay: float = STREAM_DELAY):
    pointer = 0
    total = len(text)
    while pointer < total:
        next_pointer = min(pointer + max(20, int(chunk_size)), total)
        yield text[pointer:next_pointer]
        pointer = next_pointer
        if pointer < total and delay > 0:
            await asyncio.sleep(delay)


async def stream_answer_chunks_ptb(
    bot: Any,
    chat_id: int,
    message_id: int,
    chunk_async_iter: Any,
    parse_mode: str | None,
    reply_markup_final: Any = None,
    *,
    min_interval: float = STREAM_EDIT_MIN_INTERVAL,
    min_delta_chars: int = STREAM_EDIT_MIN_DELTA_CHARS,
) -> str:
    if not bot or not message_id:
        return ""

    html_mode = _is_html_parse_mode(parse_mode)
    buffer = ""
    last_sent = ""
    last_edit_ts = 0.0
    loop = asyncio.get_running_loop()

    async def _send_candidate(candidate_text: str, *, final: bool = False) -> bool:
        prepared = sanitize_html_message(candidate_text) if html_mode else clean_reply_for_display(candidate_text)
        if not prepared:
            return False
        if not final and prepared == last_sent:
            return False
        try:
            sent = await safe_edit_message(
                bot,
                chat_id,
                message_id,
                prepared,
                fallback_send=False,
                reply_markup=reply_markup_final if final else None,
                parse_mode=parse_mode if html_mode else None,
                max_retries=2 if not final else 3,
                base_delay=0.4,
            )
            return bool(sent)
        except RetryAfter as exc:
            await asyncio.sleep(getattr(exc, "retry_after", 1) + 0.1)
            return False
        except BadRequest as exc:
            if _is_non_fatal_edit_error(exc):
                return False
            logging.debug("stream_answer_chunks_ptb BadRequest: %s", exc)
            return False
        except Forbidden:
            return False
        except Exception:
            logging.debug("stream_answer_chunks_ptb edit failed", exc_info=True)
            return False

    try:
        async for chunk in chunk_async_iter:
            if not chunk:
                continue
            buffer += str(chunk)
            now = loop.time()
            if (len(buffer) - len(last_sent)) < max(1, int(min_delta_chars)) and (now - last_edit_ts) < max(0.2, float(min_interval)):
                continue
            if await _send_candidate(buffer, final=False):
                last_sent = sanitize_html_message(buffer) if html_mode else clean_reply_for_display(buffer)
                last_edit_ts = loop.time()
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("stream_answer_chunks_ptb iterator failed")

    final_text = buffer or last_sent
    if not final_text:
        return ""
    await _send_candidate(final_text, final=True)
    return sanitize_html_message(final_text) if html_mode else clean_reply_for_display(final_text)

async def stream_text_response(
    bot: Any,
    chat_id: int,
    text: str,
    *,
    delay: float = STREAM_DELAY,
    chunk_size: int = STREAM_CHUNK_SIZE,
    min_update_interval: float = STREAM_MIN_UPDATE_INTERVAL,
    reply_markup: Any = None,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
) -> int | None:
    """
    Стриминг ответа: сначала быстрый вывод «черновика», затем плавная дорисовка.
    Минимизирует количество edit-запросов и учитывает ограничения Telegram.
    """
    if not text or not bot:
        return None

    try:
        # Первое сообщение — уже с начальной частью текста, а не просто значок ожидания
        preview_len = max(chunk_size, 100)
        preview_text = text[:preview_len]
        msg = await bot.send_message(
            chat_id=chat_id,
            text=preview_text,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
        )
        message_id = msg.message_id

        accumulated = preview_text
        text_len = len(text)
        last_update_time = asyncio.get_running_loop().time()
        last_update_len = len(accumulated)
        min_change_threshold = max(20, chunk_size // 2)

        idx = preview_len
        while idx < text_len:
            next_idx = min(idx + chunk_size, text_len)
            accumulated = text[:next_idx]
            current_time = asyncio.get_running_loop().time()
            time_since_last_update = current_time - last_update_time
            text_change = len(accumulated) - last_update_len

            needs_update = (
                time_since_last_update >= min_update_interval
                or text_change >= min_change_threshold
                or next_idx >= text_len
            )

            if needs_update and len(accumulated) > last_update_len:
                try:
                    await safe_edit_message(
                        bot,
                        chat_id,
                        message_id,
                        accumulated,
                        fallback_send=False,
                        reply_markup=None,
                    )
                    last_update_time = asyncio.get_running_loop().time()
                    last_update_len = len(accumulated)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.debug("Error updating streamed message: %s", e)

            idx = next_idx
            if idx < text_len:
                await asyncio.sleep(delay)

        # Финальный апдейт с клавиатурой (если изменилась только разметка/клавиатура — Telegram может вернуть "message is not modified")
        try:
            await safe_edit_message(
                bot,
                chat_id,
                message_id,
                text,
                fallback_send=False,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logging.debug("Error finalizing streamed message: %s", e)

        return message_id

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.exception("stream_text_response failed: %s", e)
        return None


# ----------------------- send_text_chunks (improved) -----------------------
def chunk_text_by_nearest_newline(text: str, max_len: int):
    parts: list[str] = []
    i = 0
    L = len(text)
    while i < L:
        if L - i <= max_len:
            parts.append(text[i:])
            break
        end = i + max_len
        window = text[i:end]
        nl = window.rfind("\n")
        if nl != -1 and nl > 0:
            split = i + nl + 1
        else:
            sp = window.rfind(" ")
            split = i + sp + 1 if sp != -1 and sp > 0 else end
        parts.append(text[i:split])
        i = split
    return parts

async def send_text_chunks(
    bot,
    chat_id: int,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
    use_streaming: bool | None = None,
    reply_to_message_id: int | None = None,
) -> int | None:
    """
    Надёжная отправка длинных текстов по частям (фиксирует ошибки парсинга, RetryAfter, Forbidden).
    Поддерживает эффект стриминга для одиночных сообщений.
    Сохраняет в conversation_store последним сообщением от assistant (если переменная доступна).
    
    Args:
        bot: Telegram bot instance
        chat_id: ID чата
        text: Текст для отправки
        reply_markup: Клавиатура
        parse_mode: Режим парсинга
        use_streaming: Использовать ли эффект стриминга (по умолчанию = ENABLE_STREAMING)
    """
    if not text:
        return None

    # Определяем, использовать ли стриминг
    if use_streaming is None:
        use_streaming = ENABLE_STREAMING

    if len(text) > STREAM_MAX_TEXT_LEN:
        use_streaming = False

    last_message_id: int | None = None
    parts = chunk_text_by_nearest_newline(text, MAX_MSG_LEN)

    # Стриминг только для одиночного сообщения
    if use_streaming and len(parts) == 1:
        try:
            placeholder = await bot.send_message(
                chat_id=chat_id,
                text="💬 Пишу ответ…",
                reply_to_message_id=reply_to_message_id,
            )
            last_message_id = getattr(placeholder, "message_id", None)
            chunk_iter = _text_chunk_iterator(
                text,
                chunk_size=max(STREAM_CHUNK_SIZE, STREAM_EDIT_MIN_DELTA_CHARS),
                delay=STREAM_DELAY,
            )
            streamed_text = await stream_answer_chunks_ptb(
                bot,
                chat_id,
                int(last_message_id or 0),
                chunk_iter,
                parse_mode,
                reply_markup_final=reply_markup,
                min_interval=STREAM_EDIT_MIN_INTERVAL,
                min_delta_chars=STREAM_EDIT_MIN_DELTA_CHARS,
            )
            if not streamed_text and last_message_id:
                await safe_edit_message(
                    bot,
                    chat_id,
                    int(last_message_id),
                    text,
                    fallback_send=False,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
        except Exception as e:
            logging.warning("Streaming failed, falling back to regular send: %s", e)
            # Fallback на обычную отправку
            try:
                if parse_mode:
                    sent_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                        reply_to_message_id=reply_to_message_id,
                    )
                else:
                    sent_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
                last_message_id = getattr(sent_msg, "message_id", None)
            except Exception as ex:
                logging.exception("Fallback send also failed: %s", ex)
    else:
        # Обычная отправка по частям для длинных текстов
        for idx, p in enumerate(parts):
            is_last = (idx == len(parts) - 1)
            try:
                if parse_mode:
                    try:
                        if is_last:
                            sent_msg = await bot.send_message(
                                chat_id=chat_id,
                                text=p,
                                reply_markup=reply_markup,
                                parse_mode=parse_mode,
                                reply_to_message_id=reply_to_message_id,
                            )
                            last_message_id = getattr(sent_msg, "message_id", None)
                        else:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=p,
                                parse_mode=parse_mode,
                                reply_to_message_id=reply_to_message_id,
                            )
                    except BadRequest as e:
                        # Частая причина: некорректные entity / разметка. Отправляем без parse_mode.
                        logging.warning("BadRequest while sending with parse_mode: %s. Falling back to plain text.", e)
                        if is_last:
                            sent_msg = await bot.send_message(
                                chat_id=chat_id,
                                text=p,
                                reply_markup=reply_markup,
                                reply_to_message_id=reply_to_message_id,
                            )
                            last_message_id = getattr(sent_msg, "message_id", None)
                        else:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=p,
                                reply_to_message_id=reply_to_message_id,
                            )
                else:
                    if is_last:
                        sent_msg = await bot.send_message(
                            chat_id=chat_id,
                            text=p,
                            reply_markup=reply_markup,
                            reply_to_message_id=reply_to_message_id,
                        )
                        last_message_id = getattr(sent_msg, "message_id", None)
                    else:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=p,
                            reply_to_message_id=reply_to_message_id,
                        )

            except RetryAfter as e:
                wait = getattr(e, "retry_after", 1)
                logging.warning("RetryAfter during send_text_chunks, sleeping %s s", wait)
                await asyncio.sleep(wait + 1)
                # пробуем снова один раз
                try:
                    sent_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=p,
                        reply_to_message_id=reply_to_message_id,
                    )
                    if is_last:
                        last_message_id = getattr(sent_msg, "message_id", None)
                except Exception as ex:
                    logging.exception("Failed to send after RetryAfter: %s", ex)

            except Forbidden:
                logging.info("Bot was blocked by user %s or no permission, skipping further messages to this chat", chat_id)
                return  # прекращаем попытки отправки

            except Exception as e:
                logging.exception("Ошибка отправки чанка: %s", e)

    # История чатов сохраняется точечно в местах, где известен активный чат.

    return last_message_id


async def send_answer_with_actions(
    bot: Any,
    chat_id: int,
    user_id: int,
    question: str,
    answer_text: str,
    *,
    parse_mode: str | None = None,
    reply_style: str | None = None,
    use_streaming: bool | None = None,
    dialog_active: bool = False,
    reply_to_message_id: int | None = None,
) -> int | None:
    prepared_text, prepared_parse_mode, force_plain_copyable = prepare_assistant_display(question, answer_text)
    display_text = prepared_text or clean_reply_for_display(answer_text)
    effective_parse_mode = prepared_parse_mode if prepared_parse_mode is not None else parse_mode

    compact_answer = should_send_compact_answer(question, answer_text) or force_plain_copyable
    if use_streaming is None:
        use_streaming = should_stream_answer(question, display_text, effective_parse_mode)
    else:
        use_streaming = bool(use_streaming) and should_stream_answer(question, display_text, effective_parse_mode)

    message_id = await send_text_chunks(
        bot,
        chat_id,
        display_text,
        reply_markup=None if compact_answer else get_answer_actions_keyboard(),
        parse_mode=effective_parse_mode,
        use_streaming=use_streaming,
        reply_to_message_id=reply_to_message_id,
    )
    if not compact_answer:
        await store_answer_actions_context(
            chat_id,
            message_id,
            user_id=user_id,
            question=question,
            answer_text=answer_text,
            reply_style=reply_style,
        )
    if dialog_active:
        await send_dialog_controls_message(bot, chat_id, user_id)
    return message_id

# ------------------ End send_text_chunks ------------------

# ----------------------- Owner / admin commands (IMPROVED) -----------------------

# Предполагается, что в глобале есть:
# OWNER_ID, owner_keyboard, reply_markup, users_store, banned_users, save_banned,
# synthesize_to_ogg, synthesize_and_send_voice, facts (list), users_store.get_all(),
# send_current_weather_text_for_city, users_store.get_user, users_store.ensure_user,
# get_intro_keyboard, reply_markup, get_back_to_main_keyboard, send_weather, get_intro_keyboard

async def is_owner(update: Update) -> bool:
    try:
        return bool(update.effective_user and update.effective_user.id == globals().get("OWNER_ID"))
    except Exception:
        return False


def _admin_reply_markup(update: Update) -> Any:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id and int(user_id) == int(OWNER_ID or 0):
        return globals().get("owner_keyboard")
    return get_main_keyboard(user_id)


async def require_admin(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if is_admin_user_id(user_id):
        return True
    if update.effective_message:
        with contextlib.suppress(Exception):
            await update.effective_message.reply_text("⛔ Нет доступа")
    return False

async def require_subscription(update: Update, context: any) -> bool:
    """Check if user is in the required chat/group. Return True if subscribed, False otherwise."""
    try:
        required_chat = globals().get("REQUIRED_CHAT_ID", "@pauelkyy_group")
        user_id = update.effective_user.id if update.effective_user else None
        
        if not user_id:
            if update.message:
                await update.message.reply_text("Не удалось определить пользователя. Попробуй ещё раз.")
            return False
        
        # For now, allow all users (you can add actual subscription check here)
        # This is a placeholder implementation
        return True
    except Exception as e:
        logging.exception("Error in require_subscription: %s", e)
        return True

# ---- Broadcast (text or voice) ----
async def broadcast(update: Update, context: any):
    if not await is_owner(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Напиши текст: /broadcast <текст> или /broadcast voice <текст> для голосовой рассылки",
            reply_markup=globals().get("owner_keyboard")
        )
        return

    args = context.args[:]
    first = args[0].lower()
    voice_mode = False
    if first in ("voice", "-v", "tts"):
        voice_mode = True
        text = " ".join(args[1:]).strip()
    else:
        text = " ".join(args).strip()

    if not text:
        await update.message.reply_text("Напиши текст рассылки после команды.", reply_markup=globals().get("owner_keyboard"))
        return

    users = globals().get("users_store").get_all() if globals().get("users_store") else {}
    count = 0

    # Small rate-limit to avoid flood and trigger RetryAfter less often
    async def _sleep_short():
        await asyncio.sleep(0.09)

    if voice_mode:
        ogg_path = await synthesize_to_ogg(text, 0)
        if ogg_path is None:
            await update.message.reply_text(
                "Не удалось сгенерировать голос. Проверьте edge-tts и ffmpeg.",
                reply_markup=globals().get("owner_keyboard")
            )
            return

        for user_id in list(users.keys()):
            try:
                uid = int(user_id)
                if uid in globals().get("banned_users", []):
                    continue
                try:
                    with open(ogg_path, "rb") as audio:
                        await context.bot.send_voice(chat_id=uid, voice=audio)
                    count += 1
                except Exception as e_send:
                    logging.exception("[BROADCAST] voice send failed to %s: %s", uid, e_send)
                    # fallback to text
                    try:
                        await context.bot.send_message(chat_id=uid, text=text)
                        count += 1
                    except Exception:
                        logging.debug("[BROADCAST] fallback text failed for %s", uid)
                await _sleep_short()
            except Exception:
                continue

        # cleanup temp file
        with contextlib.suppress(Exception):
            if os.path.exists(ogg_path):
                os.remove(ogg_path)

        await update.message.reply_text(f"Голосовая рассылка завершена. Доставлено: {count} пользователям.", reply_markup=globals().get("owner_keyboard"))
        return

    # Text broadcast
    for user_id in list(users.keys()):
        try:
            uid = int(user_id)
            if uid in globals().get("banned_users", []):
                continue
            # respect user's daily_enabled if stored
            user_meta = users.get(user_id, {}) or {}
            if user_meta.get("daily_enabled") is False:
                continue
            try:
                await context.bot.send_message(chat_id=uid, text=f"📬 Сообщение от администрации:\n\n{text}")
                count += 1
            except Exception:
                logging.exception("[BROADCAST] text send failed to %s", uid)
            await _sleep_short()
        except Exception:
            continue

    await update.message.reply_text(f"Рассылка завершена. Доставлено: {count} пользователям.", reply_markup=globals().get("owner_keyboard"))

# ---- Ban / Unban ----
async def ban_user(update: Update, context: any):
    if not await is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Формат: /ban <user_id>", reply_markup=globals().get("owner_keyboard"))
        return
    try:
        user_id = int(context.args[0])
        if user_id not in globals().get("banned_users", []):
            globals().get("banned_users").append(user_id)
            try:
                save_banned(globals().get("banned_users"))
            except Exception:
                logging.exception("save_banned failed")
        await update.message.reply_text(f"Пользователь {user_id} заблокирован ✅", reply_markup=globals().get("owner_keyboard"))
    except ValueError:
        await update.message.reply_text("Введи корректный ID пользователя.", reply_markup=globals().get("owner_keyboard"))

async def unban_user(update: Update, context: any):
    if not await is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Напиши: /unban <user_id>", reply_markup=globals().get("owner_keyboard"))
        return
    try:
        user_id = int(context.args[0])
        if user_id in globals().get("banned_users", []):
            globals().get("banned_users").remove(user_id)
            try:
                save_banned(globals().get("banned_users"))
            except Exception:
                logging.exception("save_banned failed")
        await update.message.reply_text(f"Пользователь {user_id} разблокирован ✅", reply_markup=globals().get("owner_keyboard"))
    except ValueError:
        await update.message.reply_text("Введи корректный ID пользователя.", reply_markup=globals().get("owner_keyboard"))

# ---- Stats / ping / uptime / errors ----
async def stats(update: Update, context: any):
    if not await is_owner(update):
        return
    users = globals().get("users_store").get_all() if globals().get("users_store") else {}
    total_users = len(users)
    total_banned = len(globals().get("banned_users", []))
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    new_users = 0
    for u in users.values():
        joined = u.get("joined")
        if not joined:
            continue
        try:
            joined_dt = datetime.fromisoformat(joined)
            if joined_dt > week_ago:
                new_users += 1
        except Exception:
            continue
    msg = (
        f"📊 Статистика бота:\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"🚫 В бане: {total_banned}\n"
        f"🆕 Новых за 7 дней: {new_users}"
    )
    await update.message.reply_text(msg, reply_markup=globals().get("owner_keyboard"))

bot_start_time = datetime.now()

async def ping(update: Update, context: any):
    if not await is_owner(update):
        return
    await update.message.reply_text("🏓 Pong — всё работает 👍", reply_markup=globals().get("owner_keyboard"))

async def uptime(update: Update, context: any):
    if not await is_owner(update):
        return
    delta = datetime.now() - bot_start_time
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    await update.message.reply_text(f"⏱ Бот в работе уже: {hours} ч {minutes} мин {seconds} сек", reply_markup=globals().get("owner_keyboard"))

async def admin_command(update: Update, context: any):
    if not await require_admin(update):
        return
    text = (
        "🛠 Admin-панель\n\n"
        "Доступные команды:\n"
        "/status — текущее состояние бота\n"
        "/keys — состояние прямых OpenRouter ключей\n"
        "/models — активные модели роутера\n"
        "/reloadkeys — перезагрузить пул ключей из env/файла"
    )
    await update.message.reply_text(text, reply_markup=_admin_reply_markup(update))


async def models_command(update: Update, context: any):
    if not await require_admin(update):
        return
    presets = get_admin_model_presets()
    presets_text = "\n".join(f"• <code>{html.escape(model_id)}</code>" for model_id in presets)
    text = (
        "🧠 Модели роутера\n\n"
        f"FAST: <code>{html.escape(MODEL_FAST_TEXT)}</code>\n"
        f"STRONG: <code>{html.escape(MODEL_STRONG_TEXT)}</code>\n"
        f"MATH: <code>{html.escape(MODEL_MATH)}</code>\n"
        f"VISION: <code>{html.escape(MODEL_VISION)}</code>\n\n"
        f"Базовая OPENROUTER_MODEL: <code>{html.escape(OPENROUTER_MODEL)}</code>\n\n"
        "Доступные варианты для STRONG:\n"
        f"{presets_text}\n\n"
        "Нажми кнопку ниже, чтобы переключить STRONG/основную модель."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_models_keyboard(),
    )

async def status(update: Update, context: any):
    if not await require_admin(update):
        return

    semaphore = MESSAGE_PROCESSING_SEMAPHORE
    total_handlers = int(getattr(semaphore, "limit", 50) or 50)
    if hasattr(semaphore, "available") and callable(semaphore.available):
        active_handlers = await semaphore.available()
    else:
        active_handlers = getattr(semaphore, "_value", total_handlers)
    current_processing = max(0, total_handlers - active_handlers)

    active_locks = len(_USER_LOCKS) if "_USER_LOCKS" in globals() else 0
    runtime = get_openrouter_runtime_snapshot()
    key_snapshot = get_openrouter_keys_snapshot()
    key_stats = key_snapshot.get("stats", {}) if isinstance(key_snapshot, dict) else {}

    delta = datetime.now() - bot_start_time
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    avg_latency_ms = runtime.get("avg_latency_ms", 0.0)
    avg_latency_text = f"{avg_latency_ms:.0f} ms" if avg_latency_ms else "н/д"
    local_402 = int(runtime.get("errors_402", 0))
    local_429 = int(runtime.get("errors_429", 0))
    active_keys = int((key_stats.get("buckets", {}) or {}).get("active", 0))
    cooldown_keys = int((key_stats.get("buckets", {}) or {}).get("cooldown", 0))
    disabled_keys = int((key_stats.get("buckets", {}) or {}).get("disabled", 0))

    msg = f"""📊 <b>Статус бота</b>

⚙️ <b>Обработчики сообщений:</b>
  • Активных: <b>{current_processing}/{total_handlers}</b>
  • Свободных слотов: <b>{active_handlers}</b>
  • Активных пользователей: <b>{active_locks}</b>
  • Активных OpenRouter-запросов: <b>{runtime.get("active_requests", 0)}</b>

📨 <b>Telegram и latency:</b>
  • Rate limit: {TELEGRAM_MESSAGE_RATE_LIMIT:.2f}s
  • Параллельных отправок: до {TELEGRAM_MAX_PARALLEL_SENDS}
  • Средняя latency LLM (последние {runtime.get("sample_size", 0)}): <b>{avg_latency_text}</b>

🔁 <b>Ошибки лимитов:</b>
  • OpenRouter 402/429: <b>{local_402}/{local_429}</b>

🔐 <b>Пул ключей:</b>
  • Активных: <b>{active_keys}</b>
  • На cooldown: <b>{cooldown_keys}</b>
  • Отключённых: <b>{disabled_keys}</b>

⏱ <b>Аптайм:</b> {hours}ч {minutes}м {seconds}с

✅ <b>Режим OpenRouter:</b> {"через прокси" if is_openrouter_proxy_enabled() else "напрямую"}"""

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=_admin_reply_markup(update))

async def keys_command(update: Update, context: any):
    if not await require_admin(update):
        return
    payload = get_openrouter_keys_snapshot()
    if not payload:
        await update.message.reply_text("Данные по ключам пока недоступны.", reply_markup=_admin_reply_markup(update))
        return

    keys = payload.get("keys", []) if isinstance(payload, dict) else []
    stats = payload.get("stats", {}) if isinstance(payload, dict) else {}
    lines = [
        "🔐 Ключи OpenRouter",
        f"Всего: {stats.get('total', 0)}",
        f"Buckets: {json.dumps(stats.get('buckets', {}), ensure_ascii=False)}",
        "",
    ]
    for item in keys[:18]:
        enabled = bool(item.get("enabled"))
        status_label = str(item.get("status") or "unknown")
        cooldown = int(item.get("cooldown_seconds") or 0)
        icon = "🟢" if enabled and status_label == "active" else ("🟡" if enabled else "🔴")
        lines.append(
            f"{icon} {item.get('label', 'key')} | status={status_label} | cd={cooldown}s | ok={item.get('success_count', 0)} | err={item.get('failure_count', 0)}"
        )
    if len(keys) > 18:
        lines.append(f"… и ещё {len(keys) - 18} ключей")
    await update.message.reply_text("\n".join(lines)[:3900], reply_markup=_admin_reply_markup(update))


async def reloadkeys_command(update: Update, context: any):
    if not await require_admin(update):
        return
    try:
        payload = reload_openrouter_key_pool()
    except Exception as exc:
        await update.message.reply_text(f"Не получилось обновить ключи: {exc}", reply_markup=_admin_reply_markup(update))
        return
    if not payload:
        await update.message.reply_text("Пул ключей не вернул данные после перезагрузки.", reply_markup=_admin_reply_markup(update))
        return

    loaded = payload.get("loaded_keys", 0)
    stats = payload.get("stats", {})
    text = (
        "✅ Ключи перезагружены.\n\n"
        f"Загружено: {loaded}\n"
        f"Buckets: {json.dumps(stats.get('buckets', {}), ensure_ascii=False)}"
    )
    await update.message.reply_text(text, reply_markup=_admin_reply_markup(update))


async def errors(update: Update, context: any):
    if not await is_owner(update):
        return
    log_file = "bot_errors.log"
    if not os.path.exists(log_file):
        await update.message.reply_text("Записанных ошибок пока нет — всё спокойно 👍", reply_markup=globals().get("owner_keyboard"))
        return
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        last_lines = "".join(lines[-50:]) if lines else "Нет записей."
        if len(last_lines) > 3900:
            last_lines = last_lines[-3900:]
            last_lines = "…(фрагмент лога)\n" + last_lines
        await update.message.reply_text(f"Последние ошибки:\n\n{last_lines}", reply_markup=globals().get("owner_keyboard"))
    except Exception as e:
        logging.exception("Failed to read log file: %s", e)
        await update.message.reply_text(f"Не получилось прочитать лог: {e}", reply_markup=globals().get("owner_keyboard"))

# ---- Small helper to fetch current weather text for a city ----
async def send_current_weather_text_for_city(city: str) -> str:
    if not city:
        city = "Moscow"
    city_quoted = urllib.parse.quote(city)
    url = f"http://api.openweathermap.org/data/2.5/weather?q={city_quoted}&appid={globals().get('OPENWEATHER_API_KEY')}&units=metric&lang=ru"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
        if resp.status_code == 200 and "main" in data:
            temp = round(data["main"]["temp"])
            feels = round(data["main"].get("feels_like", temp))
            desc = data.get("weather", [{}])[0].get("description", "").capitalize()
            return f"🌤 Сейчас в {city.capitalize()}: {temp}°C (ощущается {feels}°C), {desc}"
        else:
            return "Не удалось получить данные по этому городу."
    except Exception:
        logging.exception("send_current_weather_text_for_city failed")
        return "Ошибка при запросе погоды. Попробуйте позже."
logger = logging.getLogger(__name__)

# ---------------- imports ----------------
import os
import re
import asyncio
import random
import hashlib
import logging

import httpx

# ---------------- config / константы ----------------
STABILITY_ENGINE_ID = os.getenv("STABILITY_ENGINE_ID", "stable-diffusion-xl-1024-v1-0").strip() or "stable-diffusion-xl-1024-v1-0"
STABILITY_API_URL = os.getenv(
    "STABILITY_API_URL",
    f"https://api.stability.ai/v1/generation/{STABILITY_ENGINE_ID}/text-to-image",
).strip()
STABILITY_API_KEY = os.getenv("STABILITY_API_KEY", "").strip()
if not STABILITY_API_KEY:
    raise ValueError("STABILITY_API_KEY не задан в переменных окружения! Установи через .env или export.")

TIMEOUT = 120
CACHE_DIR = "image_cache"
MIN_BYTES = 1_500
MAX_RETRIES = 4
_SEM = asyncio.Semaphore(3)

# Управляемые дефолты
DEFAULT_ASPECT_RATIO = "1:1"
DEFAULT_OUTPUT_FORMAT = "png"

# Генерация подсказок
REALISM = "sharp focus, RAW photo, 8k"
DETAIL = "micro details, photorealistic, highly detailed"
NEGATIVE = "blurry, low quality, worst quality, deformed, jpeg artifacts"

ALLOWED_ASPECT_RATIOS = {
    "16:9", "1:1", "21:9", "2:3", "3:2", "4:5", "5:4", "9:16", "9:21"
}

SDXL_DIMENSIONS_BY_RATIO: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "21:9": (1536, 640),
    "2:3": (832, 1216),
    "3:2": (1216, 832),
    "4:5": (896, 1152),
    "5:4": (1152, 896),
    "9:16": (768, 1344),
    "9:21": (640, 1536),
}

# ---------------- вспомогательные функции ----------------
def _has_cyrillic(t: str) -> bool:
    return bool(re.search(r"[а-яА-ЯёЁ]", t))

def _short(t: str, n: int) -> str:
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0]

def _remove_brands(t: str) -> str:
    return re.sub(
        r"\b(BMW|Mercedes|Audi|Ferrari|Lamborghini|Porsche|M5|F90|AMG|RS)\b",
        "",
        t,
        flags=re.I,
    )

def _simplify(t: str) -> str:
    t = _remove_brands(t)
    t = re.sub(r",.*", "", t)
    return _short(t, 120)

def _hash(p: str, ratio: str) -> str:
    return hashlib.sha256(f"{p}|{ratio}".encode()).hexdigest()

def _ensure_cache_dir() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)

async def _cache_get(k: str) -> bytes | None:
    p = os.path.join(CACHE_DIR, f"{k}.bin")
    if os.path.exists(p):
        return await asyncio.to_thread(lambda: open(p, "rb").read())
    return None

async def _cache_set(k: str, b: bytes) -> None:
    _ensure_cache_dir()
    await asyncio.to_thread(lambda: open(os.path.join(CACHE_DIR, f"{k}.bin"), "wb").write(b))

async def _backoff(i: int) -> None:
    await asyncio.sleep(min(0.7 * (2 ** i) + random.random() * 1.5, 12))

async def _translate_to_english(text: str) -> str:
    if not text or not text.strip() or not _has_cyrillic(text):
        return text.strip()

    try:
        from deep_translator import GoogleTranslator
        def _sync_translate(t: str) -> str:
            return GoogleTranslator(source="auto", target="en").translate(t) or t
        translated = await asyncio.to_thread(_sync_translate, text)
        if translated and isinstance(translated, str) and len(translated) > 5:
            logging.debug("Перевод промпта: %s → %s", text, translated)
            return translated
    except Exception as e:
        logging.debug("Перевод не удался (deep_translator): %s", e)
    return text.strip()


def _get_sdxl_dimensions(aspect_ratio: str) -> tuple[int, int]:
    return SDXL_DIMENSIONS_BY_RATIO.get(aspect_ratio, SDXL_DIMENSIONS_BY_RATIO[DEFAULT_ASPECT_RATIO])


def _extract_stability_errors(response: httpx.Response) -> list[str]:
    with contextlib.suppress(Exception):
        payload = response.json()
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list):
                return [str(item) for item in errors if str(item).strip()]
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return [message.strip()]
    preview = response.text[:500].replace("\n", " ") if response.text else "<no body>"
    return [preview]

# ---------------- основной вызов API ----------------
async def _call_stability_core(
    client: httpx.AsyncClient,
    prompt: str,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    negative_prompt: str = NEGATIVE,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    seed: int | None = None,
) -> bytes | None:
    """
    Вызов Stability API для text-to-image.
    Для SDXL 1.0 используем официальный v1 endpoint /v1/generation/{engine_id}/text-to-image.
    """
    if not prompt.strip():
        logging.warning("Пустой prompt — пропускаем")
        return None

    headers = {
        "Authorization": f"Bearer {STABILITY_API_KEY}",
        "Accept": "application/json",
    }
    width, height = _get_sdxl_dimensions(aspect_ratio)
    text_prompts: list[dict[str, Any]] = [{"text": prompt.strip(), "weight": 1}]
    if negative_prompt and negative_prompt.strip():
        text_prompts.append({"text": negative_prompt.strip(), "weight": -1})

    payload: dict[str, Any] = {
        "text_prompts": text_prompts,
        "cfg_scale": 7,
        "height": height,
        "width": width,
        "samples": 1,
        "steps": 30,
    }

    if seed is not None:
        payload["seed"] = int(seed)

    logging.debug(
        "Stability запрос:\n"
        f"  prompt       : {prompt[:100]}{'...' if len(prompt) > 100 else ''}\n"
        f"  aspect_ratio : {aspect_ratio}\n"
        f"  size         : {width}x{height}\n"
        f"  engine_id    : {STABILITY_ENGINE_ID}\n"
        f"  negative     : {negative_prompt[:80] if negative_prompt else ''}\n"
        f"  output_fmt   : {output_format}\n"
        f"  seed         : {seed if seed is not None else 'random'}"
    )

    try:
        response = await client.post(
            STABILITY_API_URL,
            headers=headers,
            json=payload,
            timeout=httpx.Timeout(TIMEOUT),
        )

        status = response.status_code
        if status == 200:
            with contextlib.suppress(Exception):
                payload_json = response.json()
                artifacts = payload_json.get("artifacts") if isinstance(payload_json, dict) else None
                if isinstance(artifacts, list):
                    for artifact in artifacts:
                        if not isinstance(artifact, dict):
                            continue
                        if artifact.get("finishReason") not in {None, "SUCCESS"}:
                            continue
                        raw_base64 = artifact.get("base64")
                        if isinstance(raw_base64, str) and raw_base64.strip():
                            image_bytes = base64.b64decode(raw_base64)
                            if len(image_bytes) >= MIN_BYTES:
                                logging.info("Успех: изображение получено (%d байт)", len(image_bytes))
                                return image_bytes
            logging.warning("Stability вернул 200, но без валидного изображения")
            return None

        if status == 400:
            err = _extract_stability_errors(response)
            logging.warning("Stability 400: %s", err)
            if "aspect_ratio" in str(err).lower() or "height" in str(err).lower() or "width" in str(err).lower():
                logging.warning("Проблема с размерами/соотношением: aspect_ratio=%s -> %sx%s", aspect_ratio, width, height)
            if "prompt" in str(err).lower():
                logging.warning("Проблема с prompt — слишком короткий/длинный?")
            return None

        if status == 402:
            logging.error("402 Payment Required — пополни кредиты: https://platform.stability.ai/account/credits")
            return None

        if status in (401, 403):
            logging.error("Auth error %d — проверь ключ", status)
            return None

        if status == 429:
            logging.warning("429 Rate limit — пауза 70 сек")
            await asyncio.sleep(70)
            return None

        preview = response.text[:500].replace("\n", " ") if response.text else "<no body>"
        logging.warning("Stability %d: %s", status, preview)
        return None

    except httpx.TimeoutException:
        logging.warning("Timeout Stability")
        return None
    except Exception as e:
        logging.exception("Ошибка в вызове Stability: %s", e)
        return None

# ---------------- high-level генерация ----------------
async def generate_image(
    prompt: str,
    *,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    high_quality: bool = False,
    use_cache: bool = True,
    seed: int | None = None, 
) -> bytes | None:
    if not prompt or not prompt.strip():
        logging.debug("Пустой промпт")
        return None

    if aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        logging.warning("Недопустимый aspect_ratio %s → fallback на %s", aspect_ratio, DEFAULT_ASPECT_RATIO)
        aspect_ratio = DEFAULT_ASPECT_RATIO

    orig_prompt = prompt.strip()
    prompt_en = await _translate_to_english(orig_prompt)

    final_prompt = f"{prompt_en}, {REALISM}"
    if high_quality:
        final_prompt += f", {DETAIL}"

    neg = NEGATIVE

    cache_key = _hash(final_prompt + neg + "|" + STABILITY_API_URL, aspect_ratio)

    if use_cache:
        cached = await _cache_get(cache_key)
        if cached and len(cached) > MIN_BYTES:
            logging.info("Из кэша: %s", cache_key)
            return cached

    async with _SEM:
        async with httpx.AsyncClient(http2=_httpx_http2_enabled()) as client:
            for attempt in range(1, MAX_RETRIES + 1):
                logging.info("Попытка %d/%d | prompt: %s", attempt, MAX_RETRIES, _short(final_prompt, 70))

                img_bytes = await _call_stability_core(
                    client,
                    prompt=final_prompt,
                    aspect_ratio=aspect_ratio,
                    negative_prompt=neg,
                    seed=seed,
                )

                if img_bytes:
                    if use_cache:
                        await _cache_set(cache_key, img_bytes)
                    logging.info("Генерация удалась (попытка %d)", attempt)
                    return img_bytes

                if attempt < MAX_RETRIES:
                    await _backoff(attempt)

            logging.error("Не удалось сгенерировать после %d попыток", MAX_RETRIES)
            return None
# ---- Owner helper actions (msguser / sendto) ----
async def owner_msguser(update: Update, context: any):
    if not await is_owner(update):
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /msguser <user_id> <текст>", reply_markup=globals().get("owner_keyboard"))
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Введи корректный ID пользователя.", reply_markup=globals().get("owner_keyboard"))
        return
    text = " ".join(context.args[1:]).strip()
    if not text:
        await update.message.reply_text("Напиши текст сообщения после ID пользователя.", reply_markup=globals().get("owner_keyboard"))
        return
    try:
        await context.bot.send_message(chat_id=target_id, text=text)
        await update.message.reply_text(f"Сообщение отправлено пользователю {target_id}.", reply_markup=globals().get("owner_keyboard"))
    except Exception as e:
        logging.exception("owner_msguser failed: %s", e)
        await update.message.reply_text(f"Не удалось отправить: {e}", reply_markup=globals().get("owner_keyboard"))

async def owner_sendto(update: Update, context: any):
    if not await is_owner(update):
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Формат: /sendto <user_id> <motivation|fact|weather|text|voice> [город или текст] [voice]",
            reply_markup=globals().get("owner_keyboard")
        )
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Введи корректный ID пользователя.", reply_markup=globals().get("owner_keyboard"))
        return

    cmd = context.args[1].lower()
    extra = context.args[2:]
    voice_flag = False
    if extra and extra[-1].lower() == "voice":
        voice_flag = True
        extra = extra[:-1]

    try:
        # MOTIVATION
        if cmd in ("motivation", "motiva", "mot"):
            motivations = globals().get("motivations") or ["Держись — всё получится.", "Маленький шаг лучше, чем отсутствие движения."]
            text = random.choice(motivations)
            if voice_flag:
                ogg = await synthesize_to_ogg(text, target_id)
                if ogg:
                    with open(ogg, "rb") as audio:
                        await context.bot.send_voice(chat_id=target_id, voice=audio)
                    with contextlib.suppress(Exception):
                        os.remove(ogg)
                else:
                    await context.bot.send_message(chat_id=target_id, text=text)
            else:
                await context.bot.send_message(chat_id=target_id, text=text)
            await update.message.reply_text(f"Мотивация отправлена пользователю {target_id}.", reply_markup=globals().get("owner_keyboard"))

        # FACT
        elif cmd in ("fact", "f"):
            facts_list = globals().get("facts", []) or ["Интересный факт: мир больше, чем кажется."]
            text = random.choice(facts_list)
            if voice_flag:
                ogg = await synthesize_to_ogg(text, target_id)
                if ogg:
                    with open(ogg, "rb") as audio:
                        await context.bot.send_voice(chat_id=target_id, voice=audio)
                    with contextlib.suppress(Exception):
                        os.remove(ogg)
                else:
                    await context.bot.send_message(chat_id=target_id, text=text)
            else:
                await context.bot.send_message(chat_id=target_id, text=text)
            await update.message.reply_text(f"Факт отправлен пользователю {target_id}.", reply_markup=globals().get("owner_keyboard"))

        # WEATHER
        elif cmd in ("weather", "w"):
            city = " ".join(extra).strip() if extra else None
            if not city:
                users = globals().get("users_store").get_all() if globals().get("users_store") else {}
                city = users.get(str(target_id), {}).get("city", "Moscow")
            weather_text = await send_current_weather_text_for_city(city)
            if voice_flag:
                ogg = await synthesize_to_ogg(weather_text, target_id)
                if ogg:
                    with open(ogg, "rb") as audio:
                        await context.bot.send_voice(chat_id=target_id, voice=audio)
                    with contextlib.suppress(Exception):
                        os.remove(ogg)
                else:
                    await context.bot.send_message(chat_id=target_id, text=weather_text)
            else:
                await context.bot.send_message(chat_id=target_id, text=weather_text)
            await update.message.reply_text(f"✅ Отправлена погода ({city}) пользователю {target_id}.", reply_markup=globals().get("owner_keyboard"))

        # TEXT
        elif cmd in ("text", "t"):
            if not extra:
                await update.message.reply_text("Для типа text напиши текст сообщения после команды.", reply_markup=globals().get("owner_keyboard"))
                return
            message = " ".join(extra).strip()
            if voice_flag:
                ogg = await synthesize_to_ogg(message, target_id)
                if ogg:
                    with open(ogg, "rb") as audio:
                        await context.bot.send_voice(chat_id=target_id, voice=audio)
                    with contextlib.suppress(Exception):
                        os.remove(ogg)
                else:
                    await context.bot.send_message(chat_id=target_id, text=message)
            else:
                await context.bot.send_message(chat_id=target_id, text=message)
            await update.message.reply_text(f"Текст отправлен пользователю {target_id}.", reply_markup=globals().get("owner_keyboard"))

        # VOICE
        elif cmd == "voice":
            if not extra:
                await update.message.reply_text("Для voice напиши текст: /sendto <user_id> voice <текст>", reply_markup=globals().get("owner_keyboard"))
                return
            message = " ".join(extra).strip()
            ogg = await synthesize_to_ogg(message, target_id)
            if ogg:
                with open(ogg, "rb") as audio:
                    await context.bot.send_voice(chat_id=target_id, voice=audio)
                with contextlib.suppress(Exception):
                    os.remove(ogg)
            else:
                await context.bot.send_message(chat_id=target_id, text=message)
            await update.message.reply_text(f"✅ Голосовое сообщение отправлено пользователю {target_id}.", reply_markup=globals().get("owner_keyboard"))
        else:
            await update.message.reply_text("Тип не распознан — используй: motivation|fact|weather|text|voice", reply_markup=globals().get("owner_keyboard"))

    except Exception as e:
        logging.exception("owner_sendto failed: %s", e)
        await update.message.reply_text(f"Не удалось отправить: {e}", reply_markup=globals().get("owner_keyboard"))

# ---- start / profile / help (owner-aware) ----
async def start(update: Update, context: any):
    if not await require_subscription(update, context):
        return

    user_id = update.effective_user.id
    globals().get("users_store").ensure_user(user_id, username=update.effective_user.username, first_name=update.effective_user.first_name)
    user_obj = globals().get("users_store").get_user(user_id) or {}
    active_chat = conversation_store.get_active_chat(user_id) if conversation_store is not None else None
    saved_role = str(user_obj.get("reply_style") or "neutral").strip()
    if saved_role not in ROLES:
        saved_role = "neutral"
    if not context.user_data.get("reply_style"):
        context.user_data["reply_style"] = saved_role
    current_role = str(context.user_data.get("reply_style") or saved_role or "neutral").strip()
    if current_role not in ROLES:
        current_role = "neutral"
    start_text = build_start_text(
        user_obj,
        active_chat_title=active_chat["title"] if active_chat else None,
        current_role=current_role,
    )
                  
    seen_onboarding = bool(user_obj.get("seen_onboarding", False))
    try:
        msg = update.effective_message or update.message
    except Exception:
        msg = update.message
    
    if not seen_onboarding:
        for retry_count in range(3):
            try:
                base_keyboard = globals().get("owner_keyboard") if update.effective_user and update.effective_user.id == globals().get("OWNER_ID") else globals().get("reply_markup")
                await msg.reply_text(start_text, reply_markup=base_keyboard)
                await msg.reply_text(
                    build_intro_panel_text(),
                    reply_markup=globals().get("get_intro_keyboard")(),
                )
                break
            except RetryAfter as e:
                wait_time = getattr(e, "retry_after", 1)
                if retry_count < 2:
                    logging.warning("Telegram flood control in /start, retry in %.1f seconds", wait_time)
                    await asyncio.sleep(min(wait_time + 1, 10))
                else:
                    logging.error("Max retries exceeded for /start reply")
                    try:
                        await msg.reply_text("Сейчас не получилось отправить стартовое сообщение. Попробуй ещё раз через минуту.")
                    except:
                        pass
            except Exception as e:
                logging.exception("/start reply failed: %s", e)
                break
    else:
        for retry_count in range(3):
            try:
                if update.effective_user and update.effective_user.id == globals().get("OWNER_ID"):
                    await msg.reply_text(start_text, reply_markup=globals().get("owner_keyboard"))
                else:
                    await msg.reply_text(start_text, reply_markup=globals().get("reply_markup"))
                break
            except RetryAfter as e:
                wait_time = getattr(e, "retry_after", 1)
                if retry_count < 2:
                    logging.warning("Telegram flood control in /start (else), retry in %.1f seconds", wait_time)
                    await asyncio.sleep(min(wait_time + 1, 10))
                else:
                    logging.error("Max retries exceeded for /start reply (else)")
                    try:
                        await msg.reply_text("Сейчас не получилось показать меню. Попробуй ещё раз через минуту.")
                    except:
                        pass
            except BadRequest:
                try:
                    if update.effective_user and update.effective_user.id == globals().get("OWNER_ID"):
                        await msg.reply_text(start_text, reply_markup=globals().get("owner_keyboard"))
                    else:
                        await msg.reply_text(start_text, reply_markup=globals().get("reply_markup"))
                except Exception as e:
                    logging.exception("BadRequest fallback in /start failed: %s", e)
                break
            except Exception as e:
                logging.exception("/start reply failed: %s", e)
                break

async def profile_command(update: Update, context: any):
    if not await require_subscription(update, context):
        return
    if not update.effective_user:
        return
    user_id = update.effective_user.id
    u = globals().get("users_store").get_user(user_id) or {}
    first_name = u.get("first_name") or update.effective_user.first_name or ""
    username = u.get("username") or (update.effective_user.username or "")
    joined = u.get("joined", "—")
    daily_enabled = u.get("daily_enabled", True)
    voice_on = bool(u.get("voice", False))

    profile_lines = [
        "👤 Твой профиль",
        "",
        f"Имя: {first_name or '—'}",
        f"Юзернейм: @{username}" if username else "Юзернейм: —",
        f"С нами с: {joined}",
        "",
        f"📩 Рассылки: {'включены' if daily_enabled else 'выключены'}",
        f"🗣 Голосовые ответы: {'включены' if voice_on else 'выключены'}",
    ]
    profile_text = "\n".join(profile_lines)
    await update.effective_message.reply_text(profile_text, reply_markup=globals().get("reply_markup"))

async def help_command(update: Update, context: any):
    if not await require_subscription(update, context):
        return
    help_text = build_help_text()
    try:
        if update.effective_user and update.effective_user.id == globals().get("OWNER_ID"):
            await update.effective_message.reply_text(help_text, reply_markup=globals().get("owner_keyboard"))
        else:
            await update.effective_message.reply_text(help_text, reply_markup=globals().get("reply_markup"))
    except BadRequest:
        if update.effective_user and update.effective_user.id == globals().get("OWNER_ID"):
            await update.effective_message.reply_text(help_text, reply_markup=globals().get("owner_keyboard"))
        else:
            await update.effective_message.reply_text(help_text, reply_markup=globals().get("reply_markup"))


async def role_command(update: Update, context: any):
    """Команда /role — выбор стиля ответа."""
    if not await require_subscription(update, context):
        return
    current = context.user_data.get("reply_style", "neutral")
    r = ROLES.get(current, ("⚖️ Сбалансированно", ""))
    text = (
        "Выбери режим ответа.\n\n"
        f"Сейчас выбран: *{r[0]}*\n\n"
        "Режим влияет на подачу: можно отвечать кратко, подробно, как учитель, как аналитик, как редактор и не только."
    )
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_role_keyboard()
    )


# ---- Команда сброса контекста ----
async def clear_context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс истории активного чата: бот «забудет» предыдущие сообщения только в выбранном чате."""
    if not update.effective_user:
        return
    user_id = update.effective_user.id
    try:
        active_chat = conversation_store.get_active_chat(user_id) if conversation_store is not None else None
        history_key = _history_state_key(user_id, active_chat["id"] if active_chat else None)
        async with _HISTORIES_LOCK:
            if history_key in user_histories:
                user_histories[history_key] = []
        if conversation_store is not None:
            await conversation_store.clear_user(user_id, chat_id=active_chat["id"] if active_chat else None)
        log_user_action(user_id, "clear_context")
        title = active_chat["title"] if active_chat else "текущего чата"
        await update.message.reply_text(f"🧹 История чата «{title}» очищена. Следующий вопрос начнём с чистого листа.")
    except Exception as e:
        logging.exception("clear_context failed for user %s: %s", user_id, e)
        await update.message.reply_text("Не удалось очистить контекст. Попробуй ещё раз чуть позже.")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


async def handle_chat_manager_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> bool:
    query = update.callback_query
    user = update.effective_user
    if data == "noop":
        return True
    if not data.startswith(CHAT_MANAGER_CALLBACK_PREFIX):
        return False
    if not query or not user:
        return True

    payload = data[len(CHAT_MANAGER_CALLBACK_PREFIX):]
    parts = payload.split("|")
    action = (parts[0] if parts else "").strip().lower()
    user_id = user.id

    async def edit_inline(text: str, reply_markup=None):
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
        except Exception:
            with contextlib.suppress(Exception):
                await query.message.reply_text(text, reply_markup=reply_markup)

    if action == "main":
        with contextlib.suppress(Exception):
            await query.edit_message_text("🏠 Возвращаю в главное меню.")
        with contextlib.suppress(Exception):
            await send_main_menu_hub(context.bot, query.message.chat.id, user_id, context.user_data)
        return True

    if action == "list":
        page = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        await edit_inline(build_chat_manager_text(user_id, page), reply_markup=get_chat_manager_keyboard(user_id, page))
        return True

    if action == "settings":
        page = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        await edit_inline(build_chat_settings_text(user_id), reply_markup=get_chat_settings_keyboard(page))
        return True

    if action == "clear_active":
        page = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        active_chat = conversation_store.get_active_chat(user_id) if conversation_store is not None else None
        if active_chat:
            await _reset_user_dialog_context(user_id, chat_id=active_chat["id"])
        await edit_inline(
            build_chat_settings_text(user_id) + "\n\n🧹 История активного чата очищена.",
            reply_markup=get_chat_settings_keyboard(page),
        )
        return True

    if action == "new":
        page = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        new_chat = await conversation_store.create_chat(user_id, title="Новый чат", make_active=True) if conversation_store is not None else None
        context.user_data["dialog_active"] = False
        context.user_data["awaiting_question"] = False
        context.user_data.pop("answer_mode", None)
        if query.message:
            await clear_dialog_controls_message(context.bot, query.message.chat.id, user_id)
        if new_chat:
            with contextlib.suppress(Exception):
                await query.message.reply_text("✅ Новый чат создан и выбран. Можешь сразу писать в него.")
            await edit_inline(build_chat_card_text(new_chat), reply_markup=get_chat_card_keyboard(new_chat["id"], page))
        else:
            await edit_inline("Не получилось создать новый чат. Попробуй ещё раз.")
        return True

    if action == "open":
        chat_id = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        page = _safe_int(parts[2] if len(parts) > 2 else 0, 0)
        chat = conversation_store.get_chat(user_id, chat_id) if conversation_store is not None else None
        if not chat:
            await edit_inline("Чат не найден.", reply_markup=get_chat_manager_keyboard(user_id, page))
            return True
        await edit_inline(build_chat_card_text(chat), reply_markup=get_chat_card_keyboard(chat_id, page))
        return True

    if action == "select":
        chat_id = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        page = _safe_int(parts[2] if len(parts) > 2 else 0, 0)
        chat = await conversation_store.set_active_chat(user_id, chat_id) if conversation_store is not None else None
        context.user_data["dialog_active"] = False
        context.user_data["awaiting_question"] = False
        context.user_data.pop("answer_mode", None)
        if query.message:
            await clear_dialog_controls_message(context.bot, query.message.chat.id, user_id)
        if not chat:
            await edit_inline("Не удалось выбрать чат. Попробуй ещё раз.", reply_markup=get_chat_manager_keyboard(user_id, page))
            return True
        with contextlib.suppress(Exception):
            await query.message.reply_text("✅ Чат выбран успешно.")
        await edit_inline(build_chat_card_text(chat), reply_markup=get_chat_card_keyboard(chat_id, page))
        return True

    if action == "history":
        chat_id = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        history_page = _safe_int(parts[2] if len(parts) > 2 else 0, 0)
        list_page = _safe_int(parts[3] if len(parts) > 3 else 0, 0)
        page_data = conversation_store.get_chat_messages_page(user_id, chat_id, page=history_page, per_page=CHAT_HISTORY_PAGE_SIZE) if conversation_store is not None else None
        if not page_data:
            await edit_inline("История этого чата пока недоступна.", reply_markup=get_chat_card_keyboard(chat_id, list_page))
            return True
        await edit_inline(
            build_chat_history_text(user_id, chat_id, history_page),
            reply_markup=get_chat_history_keyboard(chat_id, page_data["page"], page_data["total_pages"], list_page),
        )
        return True

    if action == "rename":
        chat_id = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        page = _safe_int(parts[2] if len(parts) > 2 else 0, 0)
        chat = conversation_store.get_chat(user_id, chat_id) if conversation_store is not None else None
        if not chat:
            await edit_inline("Чат не найден.", reply_markup=get_chat_manager_keyboard(user_id, page))
            return True
        context.user_data[CHAT_RENAME_STATE_KEY] = chat_id
        context.user_data[CHAT_RENAME_PAGE_KEY] = page
        with contextlib.suppress(Exception):
            await query.message.reply_text(f"📄 Напиши новое название для чата «{chat['title']}».")
        return True

    if action == "delete":
        chat_id = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        page = _safe_int(parts[2] if len(parts) > 2 else 0, 0)
        chat = conversation_store.get_chat(user_id, chat_id) if conversation_store is not None else None
        if not chat:
            await edit_inline("Чат не найден.", reply_markup=get_chat_manager_keyboard(user_id, page))
            return True
        await edit_inline(
            f"🗑 Удалить чат «{chat['title']}»?\n\nИстория этого чата будет удалена.",
            reply_markup=get_chat_delete_confirm_keyboard(chat_id, page),
        )
        return True

    if action == "confirm_delete":
        chat_id = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
        page = _safe_int(parts[2] if len(parts) > 2 else 0, 0)
        context.user_data["dialog_active"] = False
        context.user_data["awaiting_question"] = False
        context.user_data.pop("answer_mode", None)
        if query.message:
            await clear_dialog_controls_message(context.bot, query.message.chat.id, user_id)
        async with _HISTORIES_LOCK:
            user_histories.pop(_history_state_key(user_id, chat_id), None)
        await conversation_store.delete_chat(user_id, chat_id) if conversation_store is not None else None
        with contextlib.suppress(Exception):
            await query.message.reply_text("🗑 Чат удалён.")
        await edit_inline(build_chat_manager_text(user_id, page), reply_markup=get_chat_manager_keyboard(user_id, page))
        return True

    return False


# ---- Improved callback_query handler (Final Version) ----
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user = query.from_user
    data = (query.data or "").strip()

    logging.info("CallbackQuery from user=%s (%s): data=%s", 
                 user.id if user else None, user.username if user else None, data)

    # Убираем "часики" на кнопке сразу
    with contextlib.suppress(Exception):
        await query.answer()

    # Вспомогательная функция для редактирования сообщения с фолбэком
    async def edit_or_reply(text, reply_markup=None):
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except Exception:
            with contextlib.suppress(Exception):
                await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

    try:
        if data.startswith(ADMIN_MODEL_CALLBACK_PREFIX):
            if not is_admin_user_id(user.id if user else None):
                with contextlib.suppress(Exception):
                    await query.answer("⛔ Нет доступа", show_alert=True)
                return
            suffix = data[len(ADMIN_MODEL_CALLBACK_PREFIX):].strip()
            try:
                preset_idx = int(suffix)
            except Exception:
                return
            presets = get_admin_model_presets()
            if preset_idx < 0 or preset_idx >= len(presets):
                return

            selected_model = presets[preset_idx]
            global MODEL_STRONG_TEXT, OPENROUTER_MODEL, MODEL_MATH
            previous_strong = MODEL_STRONG_TEXT
            MODEL_STRONG_TEXT = selected_model
            OPENROUTER_MODEL = selected_model
            if MODEL_MATH == previous_strong:
                MODEL_MATH = selected_model
            global MODEL_FAST_TEXT
            MODEL_FAST_TEXT = selected_model

            status_text = (
                "✅ Основная модель обновлена.\n\n"
                f"FAST: <code>{html.escape(MODEL_FAST_TEXT)}</code>\n"
                f"STRONG: <code>{html.escape(MODEL_STRONG_TEXT)}</code>\n"
                f"MATH: <code>{html.escape(MODEL_MATH)}</code>\n"
                f"VISION: <code>{html.escape(MODEL_VISION)}</code>"
            )
            with contextlib.suppress(Exception):
                await query.answer("Модель переключена")
            try:
                await query.edit_message_text(
                    status_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_admin_models_keyboard(),
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await query.message.reply_text(
                        status_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_admin_models_keyboard(),
                    )
            return

        if data == "intro|chats":
            try:
                await query.edit_message_text(
                    build_chat_manager_text(user.id if user else 0, 0),
                    reply_markup=get_chat_manager_keyboard(user.id if user else 0, 0),
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await query.message.reply_text(
                        build_chat_manager_text(user.id if user else 0, 0),
                        reply_markup=get_chat_manager_keyboard(user.id if user else 0, 0),
                    )
            return

        if data == "intro|ask":
            active_chat = conversation_store.get_active_chat(user.id if user else 0) if conversation_store is not None else None
            context.user_data["dialog_active"] = True
            context.user_data["awaiting_question"] = False
            if query.message and user:
                await clear_dialog_controls_message(context.bot, query.message.chat.id, user.id)
            chat_title = active_chat["title"] if active_chat else "текущем чате"
            await edit_or_reply(
                f"💬 Открываю диалог с ИИ в чате *{chat_title}*.\n\nВыбери, как удобнее получать ответы.",
                reply_markup=get_ask_question_keyboard(),
            )
            return

        if data == "intro|photo":
            await edit_or_reply(
                "🖼 Разбор фото готов.\n\nОтправь фото или скриншот прямо в чат, а я распознаю текст и помогу по содержанию.",
                reply_markup=get_back_to_main_keyboard(),
            )
            return

        if data == "intro|image":
            context.user_data["awaiting_image_prompt"] = True
            await edit_or_reply(
                "🎨 Генерация изображений включена.\n\nНапиши, что хочешь увидеть: сюжет, стиль, детали, атмосферу или формат кадра.",
                reply_markup=get_back_to_main_keyboard(),
            )
            return

        if await handle_chat_manager_callback(update, context, data):
            return

        # 1. --- ask_mode|: выбор формата ответа (текст / голос) ---
        if data.startswith("ask_mode|"):
            mode = data.split("|")[1]
            context.user_data["answer_mode"] = mode
            context.user_data["dialog_active"] = True
            context.user_data["awaiting_question"] = True
            mode_labels = {"voice": "🗣 Голосом", "text": "📝 Текстом"}
            label = mode_labels.get(mode, "📝 Текстом")
            active_chat = conversation_store.get_active_chat(user.id if user else 0) if conversation_store is not None else None
            chat_hint = f"\nТекущий чат: *{active_chat['title']}*." if active_chat else ""
            role_hint = ""
            if context.user_data.get("reply_style") and context.user_data["reply_style"] != "neutral":
                r = ROLES.get(context.user_data["reply_style"])
                role_hint = f"\nТекущий стиль: *{r[0]}*." if r else ""
            await edit_or_reply(
                f"💬 Диалог с ИИ открыт. Буду отвечать *{label}*.{chat_hint}{role_hint}\n\n"
                "Теперь напиши первый вопрос."
            )
            return

        if data == "intro|roles":
            current_role = context.user_data.get("reply_style", "neutral")
            current_label = ROLES.get(current_role, ("⚖️ Сбалансированно", ""))[0]
            await edit_or_reply(
                f"Выбери режим ответа.\n\nСейчас выбран: *{current_label}*.",
                reply_markup=get_role_keyboard(),
            )
            return

        # 2. --- role|: выбор роли GPT (стиль ответа), доступен по /role ---
        if data.startswith("role|"):
            role_id = data.split("|", 1)[1]
            if role_id not in ROLES:
                role_id = "neutral"
            context.user_data["reply_style"] = role_id
            r = ROLES.get(role_id, ("⚖️ Сбалансированно", ""))
            users_store = globals().get("users_store")
            if users_store and user:
                with contextlib.suppress(Exception):
                    users_store.set_field(user.id, "reply_style", role_id)
            await edit_or_reply(
                f"🪄 Режим ответа изменён: *{r[0]}*.\n\n"
                "Теперь можно задать вопрос, открыть диалог или вернуться в меню."
            )
            return

        if data.startswith(DIALOG_FLOW_CALLBACK_PREFIX):
            action = data.split("|", 1)[1].strip().lower()
            if action == "continue":
                context.user_data["dialog_active"] = True
                context.user_data["awaiting_question"] = True
                with contextlib.suppress(Exception):
                    await query.edit_message_text(
                        "✨ Диалог продолжается. Напиши следующий вопрос.",
                        reply_markup=get_dialog_controls_keyboard(),
                    )
                return

            if action == "exit":
                context.user_data["dialog_active"] = False
                context.user_data["awaiting_question"] = False
                if query.message and user:
                    await clear_dialog_controls_message(
                        context.bot,
                        query.message.chat.id,
                        user.id,
                        delete_message=False,
                    )
                with contextlib.suppress(Exception):
                    await query.edit_message_text("👋 Диалог завершён. Возвращаю в главное меню.")
                with contextlib.suppress(Exception):
                    await send_main_menu_hub(context.bot, query.message.chat.id, user.id if user else 0, context.user_data)
                return

        if data.startswith(ANSWER_ACTIONS_CALLBACK_PREFIX):
            style_id = data.split("|", 1)[1].strip().lower()
            if style_id not in ANSWER_REWRITE_PROMPTS:
                return
            if not query.message:
                return

            chat_id = query.message.chat.id
            message_id = query.message.message_id
            answer_ctx = await get_answer_actions_context(chat_id, message_id)
            if not answer_ctx:
                await query.message.reply_text("Контекст этого ответа уже потерялся. Задай вопрос ещё раз, и я соберу новую версию.")
                return

            if answer_ctx.get("user_id") not in (None, user.id if user else None):
                await query.message.reply_text("Эти кнопки относятся к ответу другого пользователя.")
                return

            loading_text = f"⏳ Переформулирую ответ: {ANSWER_REWRITE_LABELS.get(style_id, style_id)}…"
            with contextlib.suppress(Exception):
                await query.edit_message_text(loading_text, reply_markup=get_answer_actions_keyboard())

            rewrite_prompt = build_answer_rewrite_prompt(
                style_id,
                answer_ctx.get("question", ""),
                answer_ctx.get("latest_answer", "") or (query.message.text or ""),
            )
            reply_style = answer_ctx.get("reply_style")
            role_ctx = get_role_prompt(reply_style) if "get_role_prompt" in globals() else None

            try:
                if "chat_with_openrouter" in globals() and callable(globals()["chat_with_openrouter"]):
                    new_reply = await globals()["chat_with_openrouter"](
                        answer_ctx.get("user_id", user.id if user else 0),
                        rewrite_prompt,
                        context=role_ctx,
                        persist_history=False,
                    )
                else:
                    new_reply = "Не получилось переформулировать ответ прямо сейчас. Попробуй ещё раз чуть позже."
            except Exception as e:
                logging.exception("Answer rewrite failed: %s", e)
                new_reply = "Не получилось переформулировать ответ прямо сейчас. Попробуй ещё раз чуть позже."

            formatted_reply, _ = _format_assistant_reply(new_reply) if "_format_assistant_reply" in globals() else (clean_reply_for_display(new_reply), None)
            if len(formatted_reply) > 3800:
                formatted_reply = formatted_reply[:3797].rstrip() + "..."

            target_message_id = message_id
            try:
                await query.edit_message_text(formatted_reply, reply_markup=get_answer_actions_keyboard())
            except Exception:
                fallback_msg = await query.message.reply_text(formatted_reply, reply_markup=get_answer_actions_keyboard())
                target_message_id = getattr(fallback_msg, "message_id", message_id)

            await store_answer_actions_context(
                chat_id,
                target_message_id,
                user_id=answer_ctx.get("user_id", user.id if user else 0),
                question=answer_ctx.get("question", ""),
                answer_text=formatted_reply,
                reply_style=reply_style,
                current_variant=style_id,
            )
            return

        # 3. --- Вспомогательные действия ---
        if data == "show_help_intro":
            help_text = build_help_text()
            
            # Отмечаем, что пользователь видел онбординг
            users_store = globals().get("users_store")
            if users_store:
                with contextlib.suppress(Exception):
                    users_store.set_field(user.id, "seen_onboarding", True)

            back_kb = globals().get("get_back_to_main_keyboard")()
            await edit_or_reply(help_text, reply_markup=back_kb)
            return

        if data == "back_to_main":
            with contextlib.suppress(Exception):
                await query.edit_message_text("🏠 Возвращаю в главное меню.")
            if query.message and user:
                with contextlib.suppress(Exception):
                    await send_main_menu_hub(context.bot, query.message.chat.id, user.id, context.user_data)
                return
            get_main_kb = globals().get("get_main_keyboard")
            kb = get_main_kb(user.id) if callable(get_main_kb) else globals().get("reply_markup")
            await edit_or_reply("✨ Главное меню снова под рукой. Выбери сценарий ниже или просто отправь сообщение.", reply_markup=kb)
            return

        # 4. --- Погода и сложные команды через "|" ---
        if "|" in data:
            parts = data.split("|", 1)
            cmd, arg = parts[0], parts[1]
            if cmd in ("today", "tomorrow", "5days"):
                send_weather = globals().get("send_weather")
                if send_weather:
                    await send_weather(update, arg, mode=cmd)
                return

        # Если кнопка не опознана
        logging.warning("Unhandled callback data: %s", data)
        back_kb = globals().get("get_back_to_main_keyboard")()
        await edit_or_reply("Эта кнопка пока не подключена. Вернись в меню и выбери другое действие.", reply_markup=back_kb)

    except Exception as e:
        logging.exception("Exception in handle_callback_query: %s", e)
        with contextlib.suppress(Exception):
            await query.message.reply_text("Что-то пошло не так. Попробуй ещё раз.")

async def ocr_image_to_text(image_bytes: bytes) -> str | None:
    try:
        text, error, _, _ = await asyncio.to_thread(_ocr_extract_and_correct_text, image_bytes)
    except Exception:
        logging.exception("ocr_image_to_text failed")
        return None
    if error:
        logging.debug("ocr_image_to_text returned error: %s", error)
        return None
    return normalize_spaces(text or "") or None
# ======================= Погода =======================
async def send_weather(update: Update, city: str, mode: str = "today"):
    if not update.message and not update.callback_query:
        return

    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            data_parts = query.data.split("|")
            if len(data_parts) >= 2:
                mode = data_parts[0]
                city = data_parts[1]
            target = query.message
        else:
            target = update.message

        city_clean = (city or "").strip()[:100]
        if not city_clean:
            await target.reply_text("Напиши название города.")
            return

        city_quoted = urllib.parse.quote(city_clean)
        forecast_url = (
            "http://api.openweathermap.org/data/2.5/forecast"
            f"?q={city_quoted}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            forecast_resp = await client.get(forecast_url)

        if forecast_resp.status_code != 200:
            await target.reply_text("Не нашёл такой город. Проверь написание или попробуй другой вариант.")
            return

        forecast_data = forecast_resp.json()
        if "list" not in forecast_data:
            await target.reply_text("Погодный сервис временно недоступен. Попробуй позже.")
            return

        weather_icons = {
            "ясно": "☀️",
            "clear": "☀️",
            "облачно": "☁️",
            "пасмурно": "☁️",
            "cloud": "☁️",
            "дождь": "🌧",
            "rain": "🌧",
            "гроза": "⛈",
            "thunder": "⛈",
            "снег": "❄️",
            "snow": "❄️",
            "туман": "🌫",
            "mist": "🌫",
            "fog": "🌫"
        }

        msg = f"📍 Погода в {city_clean.capitalize()}:\n"

        today = datetime.now().date()
        if mode == "today":
            dates_to_show = [today]
        elif mode == "tomorrow":
            dates_to_show = [today + timedelta(days=1)]
        else:
            dates_to_show = [today + timedelta(days=i) for i in range(5)]

        periods_labels = {
            "Ночь 🌃": (0, 6),
            "Утро 🌅": (6, 12),
            "День 🌞": (12, 18),
            "Вечер 🌙": (18, 24)
        }

        forecasts_by_date = {}

        for f in forecast_data.get("list", []):
            try:
                dt = datetime.strptime(f.get("dt_txt", ""), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

            if dt.date() not in dates_to_show:
                continue

            for label, (start, end) in periods_labels.items():
                if start <= dt.hour < end:
                    forecasts_by_date.setdefault(dt.date(), {}).setdefault(label, []).append(f)

        for date in dates_to_show:
            day_data = forecasts_by_date.get(date)
            if not day_data:
                continue

            msg += f"\n📆 {date.strftime('%d.%m (%a)')}\n"

            for period_name, period_list in day_data.items():
                if not period_list:
                    continue

                temps = []
                feels = []
                winds = []
                rains = []

                for p in period_list:
                    temps.append(p["main"].get("temp", 0))
                    feels.append(p["main"].get("feels_like", 0))
                    winds.append(p.get("wind", {}).get("speed", 0))
                    rains.append(p.get("rain", {}).get("3h", 0))

                weather_desc = (
                    period_list[0]
                    .get("weather", [{}])[0]
                    .get("description", "Без данных")
                    .capitalize()
                )

                temp_min = round(min(temps))
                temp_max = round(max(temps))
                avg_feels = round(sum(feels) / len(feels))
                avg_wind = round(sum(winds) / len(winds))
                rain_total = round(sum(rains), 1)

                icon = "🌤"
                desc_lower = weather_desc.lower()
                for key, value in weather_icons.items():
                    if key in desc_lower:
                        icon = value
                        break

                if temp_max <= 0:
                    outfit = "🧥 Тёплая куртка"
                elif temp_max <= 10:
                    outfit = "🧥 Куртка"
                elif temp_max <= 20:
                    outfit = "👕 Лёгкая одежда"
                else:
                    outfit = "☀️ Футболка"

                if avg_wind >= 8:
                    outfit += " + ветровка"
                if rain_total > 0:
                    outfit += " + зонт"

                rain_msg = f"🌧 {rain_total} мм" if rain_total > 0 else "Без осадков"

                msg += (
                    f"\n{period_name}: {icon} {weather_desc}\n"
                    f"🌡 {temp_min}…{temp_max}°C (ощ. {avg_feels}°C)\n"
                    f"💨 {avg_wind} м/с | {rain_msg}\n"
                    f"{outfit}\n"
                )

        keyboard_buttons = [[
            InlineKeyboardButton("📅 Сегодня", callback_data=f"today|{city_clean}"),
            InlineKeyboardButton("🌅 Завтра", callback_data=f"tomorrow|{city_clean}"),
            InlineKeyboardButton("📊 5 дней", callback_data=f"5days|{city_clean}")
        ]]
        reply_kb = InlineKeyboardMarkup(keyboard_buttons)

        if update.callback_query:
            await target.edit_text(msg, reply_markup=reply_kb)
        else:
            await target.reply_text(msg, reply_markup=reply_kb)

    except Exception as e:
        logging.exception("Ошибка в send_weather")
        try:
            if update.callback_query:
                await update.callback_query.message.reply_text("Не получилось получить прогноз. Попробуй ещё раз чуть позже.")
            elif update.message:
                await update.message.reply_text("Не получилось получить прогноз. Попробуй ещё раз чуть позже.")
        except Exception:
            pass


# ======================= Обработчик кнопок погоды =======================
async def weather_button_handler(update: Update, context: any):
    try:
        query = update.callback_query
        await query.answer()
        mode, city = query.data.split("|", 1)
        await send_weather(update, city, mode=mode)
    except Exception as e:
        logging.exception("weather_button_handler: %s", e)
        with contextlib.suppress(Exception):
            if update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_text("Не получилось обновить прогноз. Попробуй ещё раз.")


#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Переписанный Telegram-бот (основные части): обработка текста, голоса, фото (OCR -> OpenRouter),
генерация изображений, админ-команды, прогресс-индикация, семафор и слабые локи.
"""

import asyncio
import contextlib
import io
import logging
import os
import random
import sys
import tempfile
import re
from weakref import WeakValueDictionary

# Зависимости для OCR / изображений
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat
import pytesseract
try:
    import pymorphy3 as pymorphy_backend
    _MORPH_BACKEND_NAME = "pymorphy3"
except ImportError:
    import pymorphy2 as pymorphy_backend
    _MORPH_BACKEND_NAME = "pymorphy2"

# HTTP для OpenRouter
import aiohttp

# Telegram / aiogram OCR helpers

# -----------------------
# Настройки tesseract / морфо
# -----------------------
# Для переносимой сборки сначала ищем локальный bundled Tesseract,
# затем системную установку и только потом надеемся на PATH.
_BUNDLE_ROOT = os.path.dirname(os.path.abspath(__file__))
_TESSERACT_CANDIDATES = [
    os.getenv("TESSERACT_CMD", "").strip(),
    os.path.join(_BUNDLE_ROOT, "tools", "tesseract", "tesseract.exe"),
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    "tesseract",
    "tesseract.exe",
]
for _candidate in _TESSERACT_CANDIDATES:
    if not _candidate:
        continue
    if os.path.isfile(_candidate) or _candidate.lower() in {"tesseract", "tesseract.exe"}:
        pytesseract.pytesseract.tesseract_cmd = _candidate
        break

OCR_TESSDATA_DIR = os.getenv("OCR_TESSDATA_DIR", "").strip() or os.path.join(_BUNDLE_ROOT, "tessdata_best")
if not os.path.isdir(OCR_TESSDATA_DIR):
    _bundled_tesseract_tessdata = os.path.join(_BUNDLE_ROOT, "tools", "tesseract", "tessdata")
    if os.path.isdir(_bundled_tesseract_tessdata):
        OCR_TESSDATA_DIR = _bundled_tesseract_tessdata
if os.path.isdir(OCR_TESSDATA_DIR):
    os.environ["TESSDATA_PREFIX"] = OCR_TESSDATA_DIR

def _with_tessdata_dir(config: str) -> str:
    if not os.path.isdir(OCR_TESSDATA_DIR):
        return config
    return f"{config} --tessdata-dir {OCR_TESSDATA_DIR}".strip()

def _detect_available_tesseract_languages() -> set[str]:
    if os.path.isdir(OCR_TESSDATA_DIR):
        local_langs = {
            os.path.splitext(name)[0]
            for name in os.listdir(OCR_TESSDATA_DIR)
            if name.endswith(".traineddata")
        }
        if local_langs:
            return local_langs
    try:
        return {lang.strip() for lang in pytesseract.get_languages(config="") if lang.strip()}
    except Exception as exc:
        logging.warning("Не удалось получить список языков Tesseract: %s", exc)
        return {"rus", "eng"}

def _build_ocr_language_candidates() -> tuple[str, ...]:
    available = _detect_available_tesseract_languages()
    candidates: list[str] = []

    for lang in ("rus", "rus+eng", "eng", "ukr", "ukr+rus", "ukr+rus+eng"):
        parts = lang.split("+")
        if all(part in available for part in parts):
            candidates.append(lang)

    if not candidates:
        fallback = "rus" if "rus" in available else next(iter(available), "eng")
        candidates.append(fallback)
    return tuple(dict.fromkeys(candidates))

try:
    morph = pymorphy_backend.MorphAnalyzer()
except Exception as exc:
    logging.warning("%s is unavailable, OCR word normalization will use fallback mode: %s", _MORPH_BACKEND_NAME, exc)
    morph = None

# -----------------------
# Константы и конфиг
# -----------------------
SEMAPHORE_LIMIT = 100
MAX_IMAGE_BYTES = 8_000_000  # 8 MB
OCR_LANG_CANDIDATES = _build_ocr_language_candidates()
OCR_LANG = OCR_LANG_CANDIDATES[0]
OCR_CONFIG_DEFAULT = "--oem 3 --psm 6"
OCR_CONFIGS = (
    "--oem 3 --psm 6",
    "--oem 1 --psm 6",
    "--oem 3 --psm 4",
    "--oem 3 --psm 11",
    "--oem 3 --psm 12",
)
IMAGE_SCALE_SMALL = 2.5
IMAGE_SCALE_LARGE = 2.0
OCR_MIN_MEAN_CONFIDENCE = 35.0
OCR_VARIANT_LIMIT = 5
OCR_FAST_PATH_MIN_CONFIDENCE = float(os.getenv("OCR_FAST_PATH_MIN_CONFIDENCE", "82"))
OCR_FAST_PATH_MIN_SCORE = float(os.getenv("OCR_FAST_PATH_MIN_SCORE", "115"))
OCR_SKIP_VISION_MIN_CONFIDENCE = float(os.getenv("OCR_SKIP_VISION_MIN_CONFIDENCE", "82"))
OCR_SKIP_VISION_MIN_SCORE = float(os.getenv("OCR_SKIP_VISION_MIN_SCORE", "115"))
PHOTO_VISION_TIMEOUT = int(os.getenv("PHOTO_VISION_TIMEOUT", "18"))
PHOTO_VISION_ANSWER_TIMEOUT = int(os.getenv("PHOTO_VISION_ANSWER_TIMEOUT", "22"))
PHOTO_LLM_TIMEOUT = int(os.getenv("PHOTO_LLM_TIMEOUT", "35"))
VISION_COOLDOWN_SECONDS = int(os.getenv("VISION_COOLDOWN_SECONDS", "180"))
_VISION_UNAVAILABLE_UNTIL = 0.0

# OPENROUTER_API_KEY и OPENROUTER_MODEL предполагаются уже определёнными в проекте/окружении.
OPENROUTER_API_KEY = globals().get("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = globals().get("OPENROUTER_MODEL") or os.getenv("OPENROUTER_MODEL") or DEFAULT_MAINBOT_MODEL
OPENROUTER_VISION_MODEL = (
    globals().get("OPENROUTER_VISION_MODEL")
    or os.getenv("OPENROUTER_VISION_MODEL")
    or OPENROUTER_MODEL
)
OPENROUTER_ENDPOINT = (
    globals().get("OPENROUTER_PROXY_URL")
    or os.getenv("OPENROUTER_PROXY_URL")
    or globals().get("OPENROUTER_URL")
    or os.getenv("OPENROUTER_URL")
    or globals().get("OPENROUTER_DIRECT_URL")
    or os.getenv("OPENROUTER_DIRECT_URL")
    or "https://openrouter.ai/api/v1/chat/completions"
)

# Замены букв/цифр для типичных ошибок OCR (latin-lookalike)
LATIN_TO_CYR = {
    'A': 'А', 'B': 'В', 'E': 'Е', 'K': 'К', 'M': 'М', 'H': 'Н', 'O': 'О',
    'P': 'Р', 'C': 'С', 'T': 'Т', 'Y': 'У', 'X': 'Х', 'V': 'В', 'S': 'С',
    'a': 'а', 'e': 'е', 'o': 'о', 'p': 'р', 'c': 'с', 'y': 'у', 'k': 'к',
    'm': 'м', 'x': 'х', 'h': 'н', 'v': 'в', 's': 'с'
}
DIGIT_TO_LETTER = {'0': 'О', '1': 'І', '3': 'З', '4': 'А', '6': 'б', '7': 'Т', '8': 'В'}

# -----------------------
# Утилиты для текста и OCR
# -----------------------
def normalize_spaces(text: str) -> str:
    text = re.sub(r'\s+\n', '\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def normalize_ocr_punctuation(text: str) -> str:
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:!?])([A-Za-zА-Яа-яЁёІіЇїЄєҐґ])", r"\1 \2", text)
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]}])", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

def _looks_like_task_text(text: str) -> bool:
    normalized = normalize_spaces((text or "").lower())
    if not normalized:
        return False

    if normalized.endswith("?"):
        return True

    keyword_pattern = (
        r"\b(реши|решить|решение|вычисли|посчитай|рассчитай|найди|определи|докажи|"
        r"ответь|объясни|переведи|сократи|упрости|построй|составь|напиши|докажите|"
        r"solve|find|calculate|determine|prove|translate|answer|simplify)\b"
    )
    if re.search(keyword_pattern, normalized):
        return True

    if re.search(r"\b(дано|требуется|условие|задача|пример|упражнение|вопрос)\b", normalized):
        return True

    has_math = bool(re.search(r"[=+\-/*^]", normalized))
    has_digits = bool(re.search(r"\d", normalized))
    if has_math and has_digits and (len(normalized) < 240 or "\n" in normalized):
        return True

    return False

def _build_image_user_prompt(question: str | None, recognized_text: str) -> str:
    explicit_prompt = normalize_spaces(question or "")
    if explicit_prompt:
        return explicit_prompt

    if _looks_like_task_text(recognized_text):
        return (
            "Определи основное задание по тексту на изображении и выполни его. "
            "Если это задача, пример, вопрос, инструкция или просьба что-то посчитать, объяснить, "
            "перевести или решить, сначала аккуратно восстанови условие без искажений, затем дай ответ по существу."
        )

    return (
        "Точно перепиши весь текст с изображения, сохранив структуру, переносы строк, числа, "
        "единицы измерения, знаки препинания и формулировки."
    )

def latin_like_to_cyrillic(word: str) -> str:
    res = []
    for ch in word:
        if ch in LATIN_TO_CYR:
            res.append(LATIN_TO_CYR[ch])
        elif ch in DIGIT_TO_LETTER:
            res.append(DIGIT_TO_LETTER[ch])
        else:
            res.append(ch)
    return ''.join(res)

def _count_chars_by_pattern(text: str, pattern: str) -> int:
    return sum(1 for ch in text if re.fullmatch(pattern, ch))

def _preserve_token_case(source: str, replacement: str) -> str:
    if not replacement:
        return replacement
    if source.isupper():
        return replacement.upper()
    if source.istitle():
        return replacement.capitalize()
    return replacement

def _is_code_like_token(token: str) -> bool:
    if not token:
        return False
    alnum = re.sub(r"[^\w]+", "", token, flags=re.UNICODE)
    if len(alnum) < 3:
        return False
    digit_count = sum(ch.isdigit() for ch in alnum)
    upper_count = sum(ch.isupper() for ch in alnum)
    return digit_count >= max(2, len(alnum) // 2) or upper_count >= max(3, len(alnum) - 1)

def _looks_like_cyrillic_word(token: str) -> bool:
    letters_only = ''.join(ch for ch in token if ch.isalpha())
    if len(letters_only) < 2:
        return False
    cyr_count = _count_chars_by_pattern(letters_only, r"[А-Яа-яЁёІіЇїЄєҐґ]")
    latin_count = _count_chars_by_pattern(letters_only, r"[A-Za-z]")
    return cyr_count > 0 and (latin_count == 0 or cyr_count >= latin_count)

def _has_mixed_latin_cyrillic(token: str) -> bool:
    return bool(re.search(r"[A-Za-z]", token)) and bool(re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", token))

def _morph_score(word: str) -> float:
    if morph is None or not word or not re.fullmatch(r"[А-Яа-яЁёІіЇїЄєҐґ-]+", word):
        return 0.0
    try:
        parses = morph.parse(word.lower())
    except Exception:
        return 0.0
    if not parses:
        return 0.0
    return float(getattr(parses[0], "score", 0.0) or 0.0)

def _fix_ocr_token(token: str) -> str:
    if not token or re.fullmatch(r'\W+', token):
        return token
    if _is_code_like_token(token):
        return token

    token_fixed = latin_like_to_cyrillic(token)
    if token_fixed == token:
        return token

    source_has_latin = bool(re.search(r"[A-Za-z]", token))
    source_has_cyr = bool(re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", token))
    fixed_looks_cyr = _looks_like_cyrillic_word(token_fixed)

    if not fixed_looks_cyr and not _has_mixed_latin_cyrillic(token):
        return token

    if morph is None:
        if source_has_latin and (source_has_cyr or _looks_like_cyrillic_word(token_fixed)):
            return _preserve_token_case(token, token_fixed)
        return token

    original_score = _morph_score(token)
    fixed_score = _morph_score(token_fixed)
    if fixed_score >= 0.4 and fixed_score > original_score + 0.15:
        return _preserve_token_case(token, token_fixed)
    if _has_mixed_latin_cyrillic(token) and fixed_score > 0:
        return _preserve_token_case(token, token_fixed)
    return token

def correct_russian_words(text: str) -> str:
    tokens = re.split(r'(\W+)', text)
    corrected = [_fix_ocr_token(tok) for tok in tokens]
    return ''.join(corrected)

def _crop_foreground(img: Image.Image) -> Image.Image:
    inverted = ImageOps.invert(img)
    bbox = inverted.getbbox()
    if not bbox:
        return img

    left, top, right, bottom = bbox
    width, height = img.size
    if (right - left) < max(20, width * 0.2) or (bottom - top) < max(20, height * 0.2):
        return img

    pad_x = max(8, int((right - left) * 0.02))
    pad_y = max(8, int((bottom - top) * 0.02))
    return img.crop((
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(width, right + pad_x),
        min(height, bottom + pad_y),
    ))

def _rotate_image_from_osd(img: Image.Image) -> Image.Image:
    try:
        osd = pytesseract.image_to_osd(img, config=_with_tessdata_dir("--psm 0"))
    except Exception:
        return img

    match = re.search(r"Rotate:\s+(\d+)", osd)
    if not match:
        return img

    rotate = int(match.group(1)) % 360
    if rotate == 0:
        return img
    return img.rotate(-rotate, expand=True, fillcolor=255)

def _resize_for_ocr(img: Image.Image) -> Image.Image:
    w, h = img.size
    scale = IMAGE_SCALE_SMALL if max(w, h) < 1000 else IMAGE_SCALE_LARGE
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return img.resize(new_size, Image.Resampling.LANCZOS)

def _add_ocr_margin(img: Image.Image, fill: int = 255) -> Image.Image:
    width, height = img.size
    pad_x = max(12, int(width * 0.04))
    pad_y = max(12, int(height * 0.04))
    return ImageOps.expand(img, border=(pad_x, pad_y, pad_x, pad_y), fill=fill)

def _binary_threshold(img: Image.Image, shift: int = 0) -> Image.Image:
    mean_value = ImageStat.Stat(img).mean[0]
    threshold = max(90, min(200, int(mean_value - 8 + shift)))
    return img.point(lambda px: 255 if px > threshold else 0, mode="L")

def _otsu_threshold(img: Image.Image) -> Image.Image:
    histogram = img.histogram()
    total = sum(histogram)
    if total <= 0:
        return img

    sum_total = sum(index * count for index, count in enumerate(histogram))
    sum_background = 0.0
    background_weight = 0.0
    max_variance = -1.0
    threshold = 128

    for index, count in enumerate(histogram):
        background_weight += count
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break

        sum_background += index * count
        mean_background = sum_background / background_weight
        mean_foreground = (sum_total - sum_background) / foreground_weight
        variance = background_weight * foreground_weight * (mean_background - mean_foreground) ** 2

        if variance > max_variance:
            max_variance = variance
            threshold = index

    return img.point(lambda px: 255 if px > threshold else 0, mode="L")

def _score_ocr_candidate(text: str, mean_confidence: float) -> float:
    candidate = normalize_spaces(text)
    if not candidate:
        return -1_000_000.0

    total_len = len(candidate)
    lines = [line.strip() for line in candidate.splitlines() if line.strip()]
    words = re.findall(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9]+(?:[-/][A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9]+)*", candidate)

    alpha_count = sum(ch.isalpha() for ch in candidate)
    digit_count = sum(ch.isdigit() for ch in candidate)
    space_count = sum(ch.isspace() for ch in candidate)
    replacement_count = candidate.count("�")
    punctuation_noise = sum(candidate.count(ch) for ch in "[]{}|~`")
    suspicious_symbol_runs = len(re.findall(r"[^\w\s]{4,}", candidate, flags=re.UNICODE))
    suspicious_single_char_runs = len(re.findall(r"(?:\b\S\b(?:\s+|$)){4,}", candidate, flags=re.UNICODE))
    unmatched_brackets = (
        abs(candidate.count("(") - candidate.count(")"))
        + abs(candidate.count("[") - candidate.count("]"))
        + abs(candidate.count("{") - candidate.count("}"))
    )

    avg_word_len = (sum(len(word) for word in words) / len(words)) if words else 0.0
    structure_bonus = sum(1 for line in lines if len(line) >= 6) * 1.4
    lexical_bonus = min(len(words), 32) * 0.55 + min(avg_word_len, 10.0) * 1.1
    density_bonus = min((alpha_count + digit_count + space_count) / max(total_len, 1), 1.0) * 12.0
    task_bonus = 5.0 if _looks_like_task_text(candidate) else 0.0

    penalty = (
        replacement_count * 10.0
        + punctuation_noise * 1.5
        + suspicious_symbol_runs * 4.0
        + suspicious_single_char_runs * 5.0
        + unmatched_brackets * 1.5
    )
    return mean_confidence + lexical_bonus + structure_bonus + density_bonus + task_bonus - penalty

def _prioritize_ocr_languages() -> tuple[str, ...]:
    preferred = (
        "rus+eng",
        "rus",
        "ukr+rus+eng",
        "ukr+rus",
        "eng",
        "ukr",
    )
    ordered = [lang for lang in preferred if lang in OCR_LANG_CANDIDATES]
    if not ordered:
        ordered = list(OCR_LANG_CANDIDATES)
    return tuple(dict.fromkeys(ordered))

def _iter_ocr_search_batches(
    prepared_variants: list[tuple[str, Image.Image]],
) -> list[tuple[list[tuple[str, Image.Image]], tuple[str, ...], tuple[str, ...]]]:
    prioritized_langs = _prioritize_ocr_languages()
    primary_configs = tuple(
        cfg for cfg in (OCR_CONFIG_DEFAULT, "--oem 3 --psm 11")
        if cfg in OCR_CONFIGS
    ) or OCR_CONFIGS
    quick_langs = prioritized_langs[:3] or prioritized_langs
    quick_variants = prepared_variants[:2] or prepared_variants

    batches: list[tuple[list[tuple[str, Image.Image]], tuple[str, ...], tuple[str, ...]]] = [
        (quick_variants, quick_langs, primary_configs),
    ]

    if (
        len(prepared_variants) > len(quick_variants)
        or len(prioritized_langs) > len(quick_langs)
        or len(primary_configs) < len(OCR_CONFIGS)
    ):
        batches.append((prepared_variants, prioritized_langs, OCR_CONFIGS))
    return batches

def _should_finish_ocr_early(best_text: str, best_mean_confidence: float, best_score: float) -> bool:
    if not normalize_spaces(best_text):
        return False
    if len(best_text.strip()) < 24:
        return False
    return (
        best_mean_confidence >= OCR_FAST_PATH_MIN_CONFIDENCE
        and best_score >= OCR_FAST_PATH_MIN_SCORE
    )

def _question_needs_visual_understanding(question: str | None) -> bool:
    normalized = normalize_spaces((question or "").lower())
    if not normalized:
        return False
    visual_patterns = (
        r"что на фото|что изображено|опиши изображение|опиши фото|"
        r"какой цвет|какая схема|какой график|что нарисовано|"
        r"что видно|image|picture|diagram|chart|table"
    )
    return bool(re.search(visual_patterns, normalized))

def _should_try_vision(
    question: str | None,
    recognized_text: str,
    ocr_meta: dict[str, Any] | None = None,
) -> bool:
    if _question_needs_visual_understanding(question):
        return True

    candidate = normalize_spaces(recognized_text or "")
    if not candidate:
        return True

    meta = ocr_meta or {}
    mean_confidence = float(meta.get("mean_confidence") or 0.0)
    score = float(meta.get("score") or 0.0)
    suspicious_single_char_runs = len(re.findall(r"(?:\b\S\b(?:\s+|$)){4,}", candidate, flags=re.UNICODE))
    suspicious_symbols = candidate.count("�") + candidate.count("[неразборчиво]")

    return (
        len(candidate) < 24
        or mean_confidence < OCR_SKIP_VISION_MIN_CONFIDENCE
        or score < OCR_SKIP_VISION_MIN_SCORE
        or suspicious_single_char_runs > 0
        or suspicious_symbols > 0
    )

def _vision_temporarily_unavailable() -> bool:
    return _VISION_UNAVAILABLE_UNTIL > time.monotonic()

def _mark_vision_temporarily_unavailable(cooldown_seconds: int = VISION_COOLDOWN_SECONDS) -> None:
    global _VISION_UNAVAILABLE_UNTIL
    _VISION_UNAVAILABLE_UNTIL = max(_VISION_UNAVAILABLE_UNTIL, time.monotonic() + max(1, cooldown_seconds))

def _prepare_ocr_variants(img: Image.Image) -> list[tuple[str, Image.Image]]:
    base = ImageOps.exif_transpose(img).convert("L")
    base = ImageOps.autocontrast(base)
    base = _crop_foreground(base)
    base = _rotate_image_from_osd(base)
    base = _resize_for_ocr(base)
    base = _add_ocr_margin(base)

    enhanced = ImageEnhance.Contrast(base).enhance(2.2)
    enhanced = ImageEnhance.Sharpness(enhanced).enhance(1.7)
    denoised = enhanced.filter(ImageFilter.MedianFilter(size=3))

    variants: list[tuple[str, Image.Image]] = [
        ("enhanced", denoised),
        ("binary_otsu", _otsu_threshold(denoised)),
        ("binary", _binary_threshold(denoised)),
        ("binary_soft", _binary_threshold(denoised, shift=12)),
        ("high_contrast", ImageEnhance.Contrast(base).enhance(3.0)),
    ]

    if ImageStat.Stat(base).mean[0] < 150:
        inverted = ImageOps.invert(base)
        inverted = ImageEnhance.Contrast(inverted).enhance(2.4)
        inverted = ImageEnhance.Sharpness(inverted).enhance(1.6)
        variants.insert(1, ("inverted", inverted))

    prepared: list[tuple[str, Image.Image]] = []
    seen_signatures: set[tuple[int, int, bytes]] = set()
    for name, variant in variants:
        buf = io.BytesIO()
        variant.save(buf, format="PNG")
        signature = (variant.width, variant.height, buf.getvalue()[:128])
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        prepared.append((name, variant))
        if len(prepared) >= OCR_VARIANT_LIMIT:
            break
    return prepared

def _extract_text_with_confidence(img: Image.Image, config: str, lang: str) -> tuple[str, float]:
    data = pytesseract.image_to_data(
        img,
        lang=lang,
        config=_with_tessdata_dir(config),
        output_type=pytesseract.Output.DICT,
    )
    words = data.get("text", [])
    confs = data.get("conf", [])
    lines: list[str] = []
    current_line: list[str] = []
    previous_line_id: tuple[int, int, int] | None = None

    for idx, word in enumerate(words):
        text = (word or "").strip()
        block_num = int(data["block_num"][idx]) if data.get("block_num") else 0
        par_num = int(data["par_num"][idx]) if data.get("par_num") else 0
        line_num = int(data["line_num"][idx]) if data.get("line_num") else idx
        line_id = (block_num, par_num, line_num)
        if previous_line_id is not None and line_id != previous_line_id:
            if current_line:
                lines.append(' '.join(current_line))
            current_line = []
        previous_line_id = line_id
        if text:
            current_line.append(text)

    if current_line:
        lines.append(' '.join(current_line))

    valid_confs: list[float] = []
    for conf in confs:
        try:
            conf_value = float(conf)
        except (TypeError, ValueError):
            continue
        if conf_value >= 0:
            valid_confs.append(conf_value)

    recognized_text = '\n'.join(lines)
    mean_confidence = sum(valid_confs) / len(valid_confs) if valid_confs else 0.0
    return recognized_text, mean_confidence

# -----------------------
# TrackedSemaphore (без обращения к внутренним полям)
# -----------------------
class TrackedSemaphore:
    def __init__(self, limit: int):
        self._sem = asyncio.Semaphore(limit)
        self.limit = limit
        self._lock = asyncio.Lock()
        self._available = limit

    async def acquire(self):
        await self._sem.acquire()
        async with self._lock:
            self._available -= 1

    def release(self):
        try:
            self._sem.release()
        except ValueError:
            return
        # увеличим асинхронно, чтобы не блокировать текущий поток
        async def _inc():
            async with self._lock:
                self._available += 1
        asyncio.create_task(_inc())

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.release()

    async def available(self) -> int:
        async with self._lock:
            return self._available

    async def used_percent(self) -> float:
        async with self._lock:
            used = self.limit - self._available
        return used / self.limit * 100.0 if self.limit else 0.0

MESSAGE_PROCESSING_SEMAPHORE = TrackedSemaphore(SEMAPHORE_LIMIT)

# -----------------------
# WeakValueDictionary для пользовательских локов
# -----------------------
_USER_LOCKS: "WeakValueDictionary[int, asyncio.Lock]" = WeakValueDictionary()

async def get_user_message_lock(user_id: int) -> asyncio.Lock:
    lock = _USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _USER_LOCKS[user_id] = lock
    return lock

# -----------------------
# Прогресс-бар и предобработка изображения
# -----------------------
def build_progress_bar(percent: float, length: int = 10) -> str:
    if percent is None:
        percent = 0
    percent = max(0, min(100, int(percent)))
    filled = int(percent / 100 * length)
    bar = "█" * filled + "░" * (length - filled)
    return f"{bar} {percent}%"

def preprocess_image_for_ocr(image_bytes: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception:
        return None
    return ImageOps.exif_transpose(img)

def _guess_image_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
        return "image/webp"
    return "image/png"


def _serialize_image_for_ocr_service(img: Image.Image) -> tuple[bytes, str]:
    prepared = ImageOps.exif_transpose(img)
    max_dim = max(prepared.size) if prepared.size else 0
    if max_dim > OCR_SERVICE_IMAGE_MAX_DIMENSION > 0:
        scale = OCR_SERVICE_IMAGE_MAX_DIMENSION / float(max_dim)
        new_size = (
            max(1, int(prepared.width * scale)),
            max(1, int(prepared.height * scale)),
        )
        prepared = prepared.resize(new_size, Image.Resampling.LANCZOS)

    has_alpha = "A" in prepared.getbands()
    target_format = "PNG" if has_alpha else "JPEG"
    target_mime = "image/png" if target_format == "PNG" else "image/jpeg"

    if target_format == "JPEG":
        prepared = prepared.convert("RGB")

    buffer = io.BytesIO()
    save_kwargs = {"format": target_format}
    if target_format == "JPEG":
        save_kwargs.update({"quality": 92, "optimize": True})
    else:
        save_kwargs.update({"optimize": True})
    prepared.save(buffer, **save_kwargs)
    return buffer.getvalue(), target_mime


def _parse_ocr_space_payload(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, "OCR service returned invalid JSON payload."

    parsed_results = payload.get("ParsedResults")
    parsed_texts: list[str] = []
    if isinstance(parsed_results, list):
        for item in parsed_results:
            if not isinstance(item, dict):
                continue
            parsed_text = normalize_ocr_punctuation(normalize_spaces(str(item.get("ParsedText") or "")))
            if parsed_text:
                parsed_texts.append(parsed_text)

    merged_text = normalize_ocr_punctuation(normalize_spaces("\n\n".join(parsed_texts)))
    error_message = payload.get("ErrorMessage")
    error_details = payload.get("ErrorDetails")
    if isinstance(error_message, list):
        error_message = "; ".join(str(item) for item in error_message if item)
    error_text = normalize_spaces(f"{error_message or ''} {error_details or ''}")

    is_errored = bool(payload.get("IsErroredOnProcessing"))
    if merged_text:
        return merged_text, None
    if is_errored:
        return None, error_text or "OCR service failed to parse the image."
    return None, error_text or "OCR service returned empty text."


def _ocr_space_request(image_bytes: bytes, *, engine: int) -> tuple[str | None, str | None, dict[str, Any]]:
    if OCR_PROVIDER != "ocr_space" or not OCR_API_URL or not OCR_API_KEY:
        return None, None, {}

    mime_type = _guess_image_mime_type(image_bytes)
    base64_image = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    payload = {
        "language": OCR_SPACE_LANGUAGE,
        "isOverlayRequired": "false",
        "detectOrientation": "true",
        "scale": "true",
        "OCREngine": str(int(engine)),
        "base64Image": base64_image,
    }

    try:
        response = httpx.post(
            OCR_API_URL,
            headers={"apikey": OCR_API_KEY},
            data=payload,
            timeout=OCR_SERVICE_TIMEOUT,
        )
    except Exception as exc:
        return None, f"OCR service request failed: {type(exc).__name__}", {}

    if response.status_code != 200:
        return None, f"OCR service HTTP {response.status_code}", {"status_code": response.status_code}

    try:
        response_payload = response.json()
    except Exception:
        return None, "OCR service returned non-JSON response.", {"status_code": response.status_code}

    text, error = _parse_ocr_space_payload(response_payload)
    return text, error, {
        "provider": "ocr_space",
        "engine": int(engine),
        "status_code": response.status_code,
    }


def _ocr_service_extract_text(image_bytes: bytes, img: Image.Image) -> tuple[str | None, str | None, list[str], dict[str, Any]]:
    if OCR_PROVIDER != "ocr_space" or not OCR_API_URL or not OCR_API_KEY:
        return None, None, [], {}

    service_variants: list[tuple[str, bytes]] = []
    normalized_bytes, _ = _serialize_image_for_ocr_service(img)
    service_variants.append(("normalized", normalized_bytes))

    grayscale = ImageOps.autocontrast(ImageOps.exif_transpose(img).convert("L"))
    grayscale = _resize_for_ocr(grayscale)
    grayscale = _add_ocr_margin(grayscale)
    enhanced_bytes, _ = _serialize_image_for_ocr_service(grayscale)
    if enhanced_bytes != normalized_bytes:
        service_variants.append(("enhanced", enhanced_bytes))

    engines: list[int] = []
    for engine in (OCR_SPACE_ENGINE_PRIMARY, OCR_SPACE_ENGINE_FALLBACK):
        if int(engine) > 0 and int(engine) not in engines:
            engines.append(int(engine))

    best_text = ""
    best_score = -1_000_000.0
    best_meta: dict[str, Any] = {}
    alternatives: list[str] = []
    seen_texts: set[str] = set()
    last_error = None

    for engine in engines:
        for variant_name, variant_bytes in service_variants:
            text, error, meta = _ocr_space_request(variant_bytes, engine=engine)
            if error and not text:
                last_error = error
                continue
            candidate = normalize_ocr_punctuation(normalize_spaces(text or ""))
            if not candidate:
                continue
            assumed_confidence = 92.0 if engine == 3 else 84.0
            score = _score_ocr_candidate(candidate, assumed_confidence)
            if candidate not in seen_texts:
                seen_texts.add(candidate)
                alternatives.append(candidate)
            if score > best_score:
                best_text = candidate
                best_score = score
                best_meta = {
                    **meta,
                    "variant": variant_name,
                    "mean_confidence": assumed_confidence,
                    "score": score,
                }
            if len(candidate) >= 24 and score >= OCR_SERVICE_MIN_ACCEPT_SCORE:
                logging.info(
                    "OCR service early exit provider=%s engine=%s variant=%s score=%.2f len=%s",
                    meta.get("provider", "ocr_service"),
                    engine,
                    variant_name,
                    score,
                    len(candidate),
                )
                final_alternatives = [item for item in alternatives if item != candidate][:3]
                return candidate, None, final_alternatives, best_meta

    if not best_text:
        return None, last_error, [], {}

    final_alternatives = [item for item in alternatives if item != best_text][:3]
    logging.info(
        "OCR service selected provider=%s engine=%s variant=%s score=%.2f len=%s",
        best_meta.get("provider", "ocr_service"),
        best_meta.get("engine"),
        best_meta.get("variant"),
        best_meta.get("score", best_score),
        len(best_text),
    )
    return best_text, None, final_alternatives, best_meta


def _ocr_extract_with_tesseract(img: Image.Image) -> tuple[str | None, str | None, list[str], dict[str, Any]]:
    try:
        best_text = ""
        best_confidence = -1.0
        best_mean_confidence = 0.0
        best_variant_name = "unknown"
        best_config = OCR_CONFIG_DEFAULT
        best_lang = OCR_LANG
        top_candidates: list[tuple[float, float, str, str, str]] = []
        attempted_jobs: set[tuple[str, str, str]] = set()

        for variants_batch, langs_batch, configs_batch in _iter_ocr_search_batches(_prepare_ocr_variants(img)):
            for variant_name, variant in variants_batch:
                for lang in langs_batch:
                    for config in configs_batch:
                        job_key = (variant_name, lang, config)
                        if job_key in attempted_jobs:
                            continue
                        attempted_jobs.add(job_key)

                        raw_text, mean_confidence = _extract_text_with_confidence(variant, config, lang)
                        candidate = normalize_ocr_punctuation(normalize_spaces(raw_text))
                        if not candidate:
                            continue

                        score = _score_ocr_candidate(candidate, mean_confidence)
                        top_candidates.append((score, mean_confidence, candidate, variant_name, f"{lang} | {config}"))
                        if score > best_confidence:
                            best_text = candidate
                            best_confidence = score
                            best_mean_confidence = mean_confidence
                            best_variant_name = variant_name
                            best_config = config
                            best_lang = lang

            if _should_finish_ocr_early(best_text, best_mean_confidence, best_confidence):
                logging.info(
                    "OCR early exit variant=%s lang=%s config=%s mean_confidence=%.2f score=%.2f",
                    best_variant_name,
                    best_lang,
                    best_config,
                    best_mean_confidence,
                    best_confidence,
                )
                break
    except Exception as e:
        logging.exception("Ошибка Tesseract")
        return None, f"Ошибка распознавания: {e}", [], {}

    cleaned_text = normalize_ocr_punctuation(normalize_spaces(best_text))
    if not cleaned_text.strip():
        return None, "Текст на изображении не распознан.", [], {}

    corrected_text = normalize_ocr_punctuation(normalize_spaces(correct_russian_words(cleaned_text)))
    logging.info(
        "OCR selected variant=%s lang=%s config=%s mean_confidence=%.2f score=%.2f text_length=%s",
        best_variant_name,
        best_lang,
        best_config,
        best_mean_confidence,
        best_confidence,
        len(corrected_text),
    )

    if top_candidates:
        top_candidates.sort(key=lambda item: item[0], reverse=True)
        preview = "; ".join(
            f"{variant}/{descriptor}: mean={mean_confidence:.1f}, len={len(candidate)}"
            for _, mean_confidence, candidate, variant, descriptor in top_candidates[:3]
        )
        logging.info("OCR top candidates: %s", preview)

    alternative_texts: list[str] = []
    seen_texts = {corrected_text}
    for _, _, candidate, _, _ in top_candidates[:4]:
        normalized_candidate = normalize_ocr_punctuation(normalize_spaces(correct_russian_words(candidate)))
        if normalized_candidate and normalized_candidate not in seen_texts:
            seen_texts.add(normalized_candidate)
            alternative_texts.append(normalized_candidate)
        if len(alternative_texts) >= 3:
            break

    if best_mean_confidence < OCR_MIN_MEAN_CONFIDENCE:
        logging.warning(
            "OCR confidence is low for image: %.2f using variant=%s lang=%s config=%s",
            best_mean_confidence,
            best_variant_name,
            best_lang,
            best_config,
        )

    return corrected_text, None, alternative_texts, {
        "mean_confidence": best_mean_confidence,
        "score": best_confidence,
        "variant": best_variant_name,
        "lang": best_lang,
        "config": best_config,
        "provider": "tesseract",
    }


def _ocr_extract_and_correct_text(image_bytes: bytes) -> tuple[str | None, str | None, list[str], dict[str, Any]]:
    img = preprocess_image_for_ocr(image_bytes)
    if img is None:
        return None, "Не удалось открыть изображение.", [], {}

    service_text, service_error, service_alternatives, service_meta = _ocr_service_extract_text(image_bytes, img)
    if service_text and float(service_meta.get("score") or 0.0) >= OCR_SERVICE_MIN_ACCEPT_SCORE:
        return service_text, None, service_alternatives, service_meta

    tesseract_text, tesseract_error, tesseract_alternatives, tesseract_meta = _ocr_extract_with_tesseract(img)
    if not service_text:
        if service_error:
            logging.info("OCR service fallback to Tesseract: %s", service_error)
        return tesseract_text, tesseract_error, tesseract_alternatives, tesseract_meta

    if not tesseract_text:
        return service_text, None, service_alternatives, service_meta

    service_score = float(service_meta.get("score") or -1_000_000.0)
    tesseract_score = float(tesseract_meta.get("score") or -1_000_000.0)
    if service_score >= tesseract_score:
        merged_alternatives = [item for item in [*service_alternatives, tesseract_text, *tesseract_alternatives] if item and item != service_text]
        deduped: list[str] = []
        seen: set[str] = {service_text}
        for item in merged_alternatives:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
            if len(deduped) >= 3:
                break
        return service_text, None, deduped, service_meta

    merged_alternatives = [item for item in [*tesseract_alternatives, service_text, *service_alternatives] if item and item != tesseract_text]
    deduped = []
    seen = {tesseract_text}
    for item in merged_alternatives:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
        if len(deduped) >= 3:
            break
    return tesseract_text, None, deduped, tesseract_meta
# -----------------------
# Вызов OpenRouter (если chat_with_openrouter доступен, можно использовать его)
# -----------------------
async def openrouter_vision_transcribe(
    image_bytes: bytes,
    ocr_hints: list[str] | None = None,
    timeout: int = PHOTO_VISION_TIMEOUT,
) -> str | None:
    if (
        not has_openrouter_auth_config()
        or not OPENROUTER_VISION_MODEL
        or not image_bytes
        or _vision_temporarily_unavailable()
    ):
        return None

    mime_type = "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        mime_type = "image/jpeg"
    elif image_bytes.startswith(b"GIF8"):
        mime_type = "image/gif"
    elif image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
        mime_type = "image/webp"

    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    hint_block = ""
    if ocr_hints:
        hint_lines: list[str] = []
        seen_hints: set[str] = set()
        for candidate in ocr_hints[:4]:
            normalized_candidate = normalize_ocr_punctuation(normalize_spaces(candidate or ""))
            if not normalized_candidate or normalized_candidate in seen_hints:
                continue
            seen_hints.add(normalized_candidate)
            hint_lines.append(f"Вариант {len(hint_lines) + 1}:\n{normalized_candidate}")
        if hint_lines:
            hint_block = (
                "\n\nНиже OCR-подсказки. Используй их только как вспомогательный материал. "
                "Если OCR расходится с изображением, верь самому изображению.\n\n"
                + "\n\n".join(hint_lines)
            )

    payload = {
        "model": OPENROUTER_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Точно перепиши весь видимый текст с изображения.\n"
                            "Требования:\n"
                            "0. Сначала ориентируйся на само изображение, а не на OCR-подсказки.\n"
                            "1. Сохраняй переносы строк и структуру.\n"
                            "2. Сохраняй формулы, единицы измерения, символы и знаки препинания.\n"
                            "2. Не пересказывай и не объясняй.\n"
                            "3. Не выдумывай скрытый текст.\n"
                            "4. Если фрагмент неразборчив, пометь его как [неразборчиво].\n"
                            "5. Верни только сам текст без комментариев."
                            f"{hint_block}"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": 2500,
        "temperature": 0.0,
    }
    try:
        async with _default_httpx_client(timeout=timeout) as client:
            resp, key_label = await _openrouter_post_with_key_pool(
                client,
                OPENROUTER_ENDPOINT,
                json_data=payload,
                request_timeout=float(timeout),
                retries=1,
                base_backoff=0.4,
            )
            logging.info("OpenRouter vision OCR path: model=%s key=%s timeout=%ss", OPENROUTER_VISION_MODEL, key_label, timeout)
            if resp.status_code != 200:
                text = (getattr(resp, "text", "") or "")[:500]
                if resp.status_code in {400, 429, 500, 502, 503, 504}:
                    _mark_vision_temporarily_unavailable()
                logging.warning("Vision OCR returned non-200: %s %s", resp.status_code, text)
                return None
            data = resp.json()
    except asyncio.TimeoutError:
        _mark_vision_temporarily_unavailable(120)
        logging.warning("Vision OCR request timed out after %ss", timeout)
        return None
    except Exception as exc:
        logging.warning("Vision OCR request failed: %s", exc)
        return None

    try:
        content = _extract_reply_from_openrouter(data)
        if isinstance(content, str) and content.strip():
            return normalize_ocr_punctuation(normalize_spaces(content))
    except Exception:
        logging.debug("Vision OCR response parsing failed", exc_info=True)
    return None

async def openrouter_vision_answer(
    image_bytes: bytes,
    recognized_text: str,
    user_prompt: str,
    timeout: int = PHOTO_VISION_ANSWER_TIMEOUT,
) -> str | None:
    if (
        not has_openrouter_auth_config()
        or not OPENROUTER_VISION_MODEL
        or not image_bytes
        or _vision_temporarily_unavailable()
    ):
        return None

    mime_type = "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        mime_type = "image/jpeg"
    elif image_bytes.startswith(b"GIF8"):
        mime_type = "image/gif"
    elif image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
        mime_type = "image/webp"

    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    logging.info("ModelRouter: type=vision model=%s user=%s chat=%s", OPENROUTER_VISION_MODEL, "-", "-")
    payload = {
        "model": OPENROUTER_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Помоги разобрать текст на изображении.\n"
                            "Сначала сверь OCR-подсказку с самой картинкой, затем выполни задачу пользователя.\n"
                            "Если на фото вопрос, пример, упражнение или просьба что-то решить, посчитать, объяснить или перевести — выполни это.\n"
                            "Если явной задачи нет, верни точную расшифровку текста.\n"
                            "Не выдумывай невидимые фрагменты.\n\n"
                            "Отвечай в формате, удобном для Telegram: без таблиц, без HTML-тегов и без широких колонок.\n\n"
                            f"Задача пользователя: {user_prompt}\n\n"
                            "OCR-подсказка (может содержать ошибки, всегда сверяй её с изображением):\n"
                            f"```\n{recognized_text}\n```"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "max_tokens": 3000,
        "temperature": 0.1,
    }
    try:
        async with _default_httpx_client(timeout=timeout) as client:
            resp, key_label = await _openrouter_post_with_key_pool(
                client,
                OPENROUTER_ENDPOINT,
                json_data=payload,
                request_timeout=float(timeout),
                retries=1,
                base_backoff=0.4,
            )
            logging.info("OpenRouter vision answer path: model=%s key=%s timeout=%ss", OPENROUTER_VISION_MODEL, key_label, timeout)
            if resp.status_code != 200:
                text = (getattr(resp, "text", "") or "")[:500]
                if resp.status_code in {400, 429, 500, 502, 503, 504}:
                    _mark_vision_temporarily_unavailable()
                logging.warning("Vision answer returned non-200: %s %s", resp.status_code, text)
                return None
            data = resp.json()
    except asyncio.TimeoutError:
        _mark_vision_temporarily_unavailable(120)
        logging.warning("Vision answer request timed out after %ss", timeout)
        return None
    except Exception as exc:
        logging.warning("Vision answer request failed: %s", exc)
        return None

    try:
        content = _extract_reply_from_openrouter(data)
        return content.strip() if isinstance(content, str) and content.strip() else None
    except Exception:
        logging.debug("Vision answer response parsing failed", exc_info=True)
        return None

def _looks_like_openrouter_failure(text: str | None) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return True
    return (
        normalized.startswith("ошибка")
        or "временно недоступен" in normalized
        or "rate limit" in normalized
        or "timed out" in normalized
    )

async def openrouter_request(
    prompt: str,
    user_id: int,
    progress_q: asyncio.Queue | None = None,
    timeout: int = PHOTO_LLM_TIMEOUT,
    *,
    has_image: bool = False,
    chat_key: str | None = None,
    model: str | None = None,
) -> str:
    """
    Простой wrapper для запроса в OpenRouter (HTTP).
    Использует OPENROUTER_API_KEY и OPENROUTER_MODEL из окружения/глобала.
    Пишет условные этапы в progress_q (если передан).
    """
    if not has_openrouter_auth_config() or not OPENROUTER_MODEL:
        logging.warning("OpenRouter auth config or model not set")
        return "Ошибка: сервер LLM не настроен."

    task_type = classify_task_type(prompt, has_image=has_image)
    requested_model = str((model or choose_model(prompt, has_image=has_image) or OPENROUTER_MODEL)).strip()
    chosen_model = str((get_effective_model_for_request(requested_model, task_type=task_type) or requested_model or OPENROUTER_MODEL)).strip()
    logging.info("ModelRouter: type=%s model=%s user=%s chat=%s", task_type, chosen_model, user_id, chat_key or "-")

    if progress_q:
        await progress_q.put(make_progress_payload("🔎 Анализирую текст и задачу…"))

    resolved_chat_key = (chat_key or "").strip()
    if not resolved_chat_key and conversation_store is not None:
        with contextlib.suppress(Exception):
            active_chat_id = conversation_store.get_active_chat_id(user_id)
            resolved_chat_key = build_memory_chat_key(user_id, active_chat_id)
    memory_block = build_memory_system_block(user_id, resolved_chat_key, prompt, limit=6) if resolved_chat_key else ""

    messages: list[dict[str, str]] = [
        {"role": "system", "content": (
            "Ты помогаешь разбирать текст, распознанный с изображений.\n"
            "Твои задачи:\n"
            "• Переписать текст максимально точно, сохраняя слова, цифры и знаки препинания\n"
            "• Исправлять только явные ошибки распознавания\n"
            "• Сохранять структуру (абзацы, списки, заголовки)\n"
            "• Если в промпте просят решить, объяснить, перевести или выполнить задание по тексту — сделать это после аккуратного восстановления условия\n"
            "• Если текст отсутствует или не читается — честно сообщить\n"
            "• Отвечать только на русском\n"
            "• Не менять имена, даты, артикулы, суммы и номера без явной уверенности\n"
            "• Если фрагмент сомнительный, помечать его как [неразборчиво], а не выдумывать\n"
            "• Если по смыслу нужен только краткий результат, формула, перевод, промпт или решение, начинай сразу с сути без фраз "
            "вроде \"Вот ответ\" или \"Решение:\"\n"
            "• Форматируй ответ под Telegram: без таблиц, без HTML-тегов и без многостолбцовой вёрстки; используй абзацы и списки\n"
            "Без эмодзи, без выдумывания содержания."
        )},
    ]
    if memory_block:
        messages.append({"role": "system", "content": memory_block})
    messages.append({"role": "user", "content": prompt})

    headers = build_openrouter_headers()
    payload = {
        "model": chosen_model,
        "messages": messages,
        "max_tokens": 1800,
        "temperature": 0.0,
    }

    request_started_at = _openrouter_runtime_request_started()
    response_status_code: int | None = None
    try:
        if progress_q:
            await progress_q.put(make_progress_payload("🌐 Подключаюсь к модели…"))

        created_client = False
        try:
            async_client = await async_get_httpx_client(timeout=timeout)
        except Exception:
            logging.exception("openrouter_request: failed to acquire shared AsyncClient, using a temporary client")
            async_client = _default_httpx_client(timeout=timeout)
            created_client = True

        async def _request_once(client: httpx.AsyncClient, model_id: str, request_timeout: float) -> httpx.Response:
            payload["model"] = model_id
            response, key_label = await _openrouter_post_with_key_pool(
                client,
                OPENROUTER_ENDPOINT,
                json_data=payload,
                request_timeout=request_timeout,
                retries=1,
                base_backoff=0.4,
            )
            logging.info("OpenRouter request path: model=%s key=%s timeout=%.1fs", model_id, key_label, request_timeout)
            return response

        async def _request_with_runtime_fallback(client: httpx.AsyncClient, initial_model: str) -> tuple[httpx.Response, str]:
            current_model = initial_model
            attempted_models: set[str] = set()
            fallback_started_at = time.perf_counter()
            attempt_index = 0
            while True:
                attempted_models.add(current_model)
                request_timeout = get_attempt_request_timeout(
                    float(timeout),
                    task_type=task_type,
                    attempt_index=attempt_index,
                    started_at=fallback_started_at,
                )
                attempt_started_at = time.perf_counter()
                try:
                    response = await _request_once(client, current_model, request_timeout=request_timeout)
                except (httpx.TimeoutException, httpx.TransportError, httpx.RequestError) as exc:
                    mark_model_temporarily_degraded(current_model, f"{type(exc).__name__}: {exc}")
                    fallback_model = fallback_model_for_runtime_issue(current_model, task_type=task_type)
                    if fallback_model and fallback_model not in attempted_models:
                        logging.warning(
                            "openrouter_request runtime fallback: %s failed with %s after %.1fs, retrying via %s",
                            current_model,
                            type(exc).__name__,
                            request_timeout,
                            fallback_model,
                        )
                        if progress_q:
                            await progress_q.put(make_progress_payload("🔁 Основная модель отвечает нестабильно, переключаюсь…"))
                        current_model = fallback_model
                        attempt_index += 1
                        continue
                    raise
                attempt_elapsed = max(0.0, time.perf_counter() - attempt_started_at)
                if task_type == "simple" and attempt_elapsed >= MODEL_SLOW_RESPONSE_THRESHOLD:
                    mark_model_temporarily_degraded(current_model, f"slow_simple_response:{attempt_elapsed:.2f}s")
                else:
                    clear_model_degraded_flag(current_model)

                status_pre = getattr(response, "status_code", None)
                text_pre = getattr(response, "text", "")[:2000]
                if is_model_not_found_response(status_pre, text_pre) or is_empty_success_openrouter_response(response):
                    fallback_model = fallback_model_for_runtime_issue(current_model, task_type=task_type)
                    if fallback_model and fallback_model not in attempted_models:
                        logging.warning("openrouter_request unusable response: %s -> %s", current_model, fallback_model)
                        if progress_q:
                            await progress_q.put(make_progress_payload("🔁 Повторяю запрос на резервной модели…"))
                        current_model = fallback_model
                        attempt_index += 1
                        continue
                if should_retry_model_after_response(status_pre, text_pre):
                    mark_model_temporarily_degraded(current_model, f"retryable_status:{status_pre}")
                    fallback_model = fallback_model_for_runtime_issue(current_model, task_type=task_type)
                    if fallback_model and fallback_model not in attempted_models:
                        logging.warning("openrouter_request retryable status=%s: %s -> %s", status_pre, current_model, fallback_model)
                        if progress_q:
                            await progress_q.put(make_progress_payload("🔁 Основная модель нестабильна, перехожу на запасную…"))
                        current_model = fallback_model
                        attempt_index += 1
                        continue
                return response, current_model

        async with OPENROUTER_SEMAPHORE:
            if progress_q:
                await progress_q.put(make_progress_payload("🧠 Готовлю содержательный ответ…"))
            if created_client:
                async with async_client:
                    resp, chosen_model = await _request_with_runtime_fallback(async_client, chosen_model)
                    status_pre = getattr(resp, "status_code", None)
                    text_pre = getattr(resp, "text", "")[:2000]
                    if is_model_not_found_response(status_pre, text_pre):
                        fallback_model = fallback_model_for_unavailable(chosen_model)
                        if fallback_model:
                            logging.warning("openrouter_request model fallback: %s -> %s", chosen_model, fallback_model)
                            chosen_model = fallback_model
                            if progress_q:
                                await progress_q.put(make_progress_payload("🔁 Переключаю модель и повторяю запрос…"))
                            resp = await _request_once(async_client, chosen_model, request_timeout=max(OPENROUTER_MIN_FALLBACK_TIMEOUT, float(timeout)))
            else:
                resp, chosen_model = await _request_with_runtime_fallback(async_client, chosen_model)
                status_pre = getattr(resp, "status_code", None)
                text_pre = getattr(resp, "text", "")[:2000]
                if is_model_not_found_response(status_pre, text_pre):
                    fallback_model = fallback_model_for_unavailable(chosen_model)
                    if fallback_model:
                        logging.warning("openrouter_request model fallback: %s -> %s", chosen_model, fallback_model)
                        chosen_model = fallback_model
                        if progress_q:
                            await progress_q.put(make_progress_payload("🔁 Переключаю модель и повторяю запрос…"))
                        resp = await _request_once(async_client, chosen_model, request_timeout=max(OPENROUTER_MIN_FALLBACK_TIMEOUT, float(timeout)))

        if progress_q:
            await progress_q.put(make_progress_payload("✍️ Формулирую итоговый ответ…"))

        status = getattr(resp, "status_code", None)
        response_status_code = int(status) if isinstance(status, int) else None
        if status is None or status >= 400:
            text = getattr(resp, "text", "")[:4000]
            logging.error("OpenRouter returned non-200: %s %s", status, text)
            return f"Ошибка OpenRouter: HTTP {status}"

        try:
            data = resp.json()
        except Exception:
            logging.exception("openrouter_request: failed to parse JSON response")
            data = {}

        content: str | None = None
        try:
            choices = data.get("choices")
            if choices and isinstance(choices, list) and choices:
                msg = choices[0].get("message", {})
                content = msg.get("content") if isinstance(msg, dict) else None
        except Exception:
            content = None
        if is_empty_openrouter_reply(content):
            fallback_model = fallback_model_for_runtime_issue(chosen_model, task_type=task_type)
            if fallback_model and fallback_model != chosen_model:
                logging.warning("openrouter_request empty reply payload: %s -> %s", chosen_model, fallback_model)
                if progress_q:
                    await progress_q.put(make_progress_payload("🔁 Уточняю итог на резервной модели…"))
                if created_client:
                    async with _default_httpx_client(timeout=timeout) as retry_client:
                        resp = await _request_once(retry_client, fallback_model, request_timeout=max(OPENROUTER_MIN_FALLBACK_TIMEOUT, float(timeout)))
                else:
                    resp = await _request_once(async_client, fallback_model, request_timeout=max(OPENROUTER_MIN_FALLBACK_TIMEOUT, float(timeout)))
                chosen_model = fallback_model
                response_status_code = int(getattr(resp, "status_code", 0) or 0)
                try:
                    data = resp.json()
                except Exception:
                    logging.exception("openrouter_request: failed to parse fallback JSON response")
                    data = {}

        if progress_q:
            await progress_q.put(make_progress_payload("🔎 Финально проверяю формулировки…"))

        try:
            choices = data.get("choices")
            if choices and isinstance(choices, list) and choices:
                msg = choices[0].get("message", {})
                content = msg.get("content") if isinstance(msg, dict) else None
                if content:
                    return content
        except Exception:
            pass

        if isinstance(data, dict):
            for key in ("result", "text", "output", "message"):
                if key in data and isinstance(data[key], str):
                    return data[key]
        return str(data)[:4000]
    except asyncio.TimeoutError:
        logging.exception("openrouter_request timeout")
        return "Ошибка: запрос к OpenRouter занял слишком много времени."
    except httpx.TimeoutException:
        logging.exception("openrouter_request httpx timeout")
        return "Ошибка: запрос к OpenRouter занял слишком много времени."
    except Exception as e:
        logging.exception("openrouter_request failed: %s", e)
        return f"Ошибка при обращении к OpenRouter: {e}"
    finally:
        _openrouter_runtime_request_finished(request_started_at, response_status_code)

# -----------------------
# Анализ изображения: OCR -> коррекция -> отправка в OpenRouter -> ответ
# -----------------------
async def analyze_image_with_openrouter(
    image_bytes: bytes,
    question: str | None,
    user_id: int,
    bot=None,
    chat_id: int | None = None
) -> str:
    if not image_bytes:
        return "Не удалось получить изображение."

    corrected_text, ocr_error, ocr_alternatives, ocr_meta = await asyncio.to_thread(_ocr_extract_and_correct_text, image_bytes)
    if ocr_error:
        vision_only_text = await openrouter_vision_transcribe(image_bytes)
        if not vision_only_text:
            return ocr_error
        corrected_text = vision_only_text
        ocr_error = None
        ocr_alternatives = []
        ocr_meta = {
            "provider": "openrouter_vision",
            "mean_confidence": 88.0,
            "score": _score_ocr_candidate(vision_only_text, 88.0),
            "variant": "vision_only",
        }
    corrected_text = corrected_text or ""
    use_vision = _should_try_vision(question, corrected_text, ocr_meta)
    vision_text = None
    if use_vision:
        vision_text = await openrouter_vision_transcribe(image_bytes, [corrected_text, *ocr_alternatives])
    primary_text = vision_text or corrected_text
    auto_task_detected = _looks_like_task_text(primary_text)
    user_prompt = _build_image_user_prompt(question, primary_text)
    merged_alternatives = list(ocr_alternatives)
    if vision_text and corrected_text and corrected_text != vision_text and corrected_text not in merged_alternatives:
        merged_alternatives.insert(0, corrected_text)

    if not (question or "").strip() and not auto_task_detected:
        return primary_text or "Текст на изображении не распознан."

    alternatives_block = ""
    if merged_alternatives:
        alternatives_block = "\n\nАльтернативные OCR-варианты для сверки спорных мест:\n\n" + "\n\n".join(
            f"Вариант {idx}:\n```\n{candidate}\n```"
            for idx, candidate in enumerate(merged_alternatives[:4], start=1)
        )

    vision_block = ""
    if vision_text:
        vision_block = "Основной текст получен vision-моделью по самому изображению. Используй его как приоритетный источник, а OCR-варианты ниже только для сверки спорных мест.\n\n"

    llm_prompt = (
        f"{vision_block}"
        f"Вот основной текст, распознанный с изображения:\n\n"
        f"```\n{primary_text}\n```\n\n"
        f"{alternatives_block}"
        f"\n\n"
        f"Задача пользователя: {user_prompt}\n\n"
        "Сначала опирайся на основной текст. Если на фото есть задача, вопрос, пример, инструкция или просьба что-то решить, "
        "ответь именно по сути задания. Если явной задачи нет, верни точную расшифровку текста. "
        "Не выдумывай отсутствующие фрагменты и не теряй числа, единицы измерения и формулировки."
    )

    progress_q = None
    done_event = None
    updater_task = None
    indicator_msg = None

    if bot is not None and chat_id is not None:
        progress_q = asyncio.Queue()
        done_event = asyncio.Event()
        try:
            indicator_msg = await bot.send_message(chat_id=chat_id, text="📷 Получил изображение. Начинаю разбор…")
        except Exception:
            indicator_msg = None

        if indicator_msg and getattr(indicator_msg, "message_id", None):
            updater_task = asyncio.create_task(streaming_progress_updater_ptb(bot, chat_id, indicator_msg.message_id, progress_q, done_event))

    try:
        vision_answer = None
        if progress_q:
            with contextlib.suppress(Exception):
                await progress_q.put(make_progress_payload("🔎 Распознаю текст с изображения…"))
        if use_vision and (question or auto_task_detected):
            if progress_q:
                with contextlib.suppress(Exception):
                    await progress_q.put(make_progress_payload("🧠 Сверяю OCR с самим изображением…"))
            vision_answer = await openrouter_vision_answer(image_bytes, primary_text, user_prompt)
            if vision_answer and len(vision_answer.strip()) >= 20:
                return vision_answer

        if progress_q:
            with contextlib.suppress(Exception):
                await progress_q.put(make_progress_payload("✍️ Готовлю решение по содержимому фото…"))
        answer = await openrouter_request(llm_prompt, user_id=user_id, progress_q=progress_q, timeout=PHOTO_LLM_TIMEOUT)

        # Если специализированный OCR/prompt-клиент не справился, пробуем общий клиент как запасной канал.
        if _looks_like_openrouter_failure(answer) and "chat_with_openrouter" in globals() and callable(globals()["chat_with_openrouter"]):
            try:
                answer = await globals()["chat_with_openrouter"](
                    user_id=user_id,
                    message=llm_prompt,
                    bot=bot,
                    chat_id=chat_id,
                    progress_queue=progress_q,
                    context=None,
                    timeout=45,
                    persist_history=False,
                )
            except TypeError:
                answer = await globals()["chat_with_openrouter"](
                    user_id=user_id,
                    message=llm_prompt,
                    bot=bot,
                    chat_id=chat_id,
                    progress_queue=progress_q,
                    context=None,
                    timeout=45,
                )

        if not answer or len(answer.strip()) < 15:
            return "**Распознанный и исправленный текст:**\n\n" + corrected_text
        return answer
    except Exception as e:
        logging.exception("Ошибка запроса к OpenRouter после OCR")
        return "**Распознан и исправлен текст (модель не ответила):**\n\n" + corrected_text
    finally:
        if progress_q:
            with contextlib.suppress(Exception):
                await progress_q.put(make_progress_payload("✅ Разбор изображения готов", done=True))
        if done_event:
            done_event.set()
        if updater_task and not updater_task.done():
            updater_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater_task
        if indicator_msg:
            await asyncio.sleep(STATUS_MESSAGE_DELETE_DELAY)
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=chat_id, message_id=indicator_msg.message_id)

# -----------------------
# Обработчики фото / сообщение / голос (переписаны и аккуратно зарегистрированы)
# -----------------------
async def _handle_photo_message_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    if not await require_subscription(update, context):
        return

    user_id = update.effective_user.id
    if user_id in globals().get("banned_users", set()):
        await update.message.reply_text("Доступ сейчас ограничен. Если это ошибка, напиши администратору.")
        return

    chat_id = update.effective_chat.id
    caption = (update.message.caption or "").strip()
    photo = update.message.photo[-1]

    with contextlib.suppress(Exception):
        await update.message.reply_text("Фото получено. Сначала распознаю текст, затем помогу по содержанию.")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    tmp_path = None
    image_bytes: bytes | None = None
    try:
        file = await context.bot.get_file(photo.file_id)
        tmp_path = os.path.join(tempfile.gettempdir(), f"photo_{chat_id}_{photo.file_unique_id}.jpg")
        await file.download_to_drive(custom_path=tmp_path)
        filesize = await asyncio.to_thread(os.path.getsize, tmp_path) if os.path.exists(tmp_path) else 0
        if filesize > MAX_IMAGE_BYTES:
            with contextlib.suppress(Exception):
                os.remove(tmp_path)
            await update.message.reply_text("Изображение слишком большое. Отправь файл размером до 8 МБ.")
            return
        image_bytes = await asyncio.to_thread(Path(tmp_path).read_bytes)
    except Exception as e:
        logging.exception("handle_photo_message: download failed: %s", e)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            with contextlib.suppress(Exception):
                os.remove(tmp_path)

    if not image_bytes:
        await update.message.reply_text("Не получилось скачать изображение. Попробуй отправить его ещё раз.")
        return

    if "log_user_action" in globals():
        try:
            log_user_action(user_id, "photo")
        except Exception:
            logging.debug("log_user_action failed", exc_info=True)

    try:
        raw_reply = await analyze_image_with_openrouter(image_bytes, caption, user_id, bot=context.bot, chat_id=chat_id)
    except Exception as e:
        logging.exception("analyze_image_with_openrouter failed: %s", e)
        await update.message.reply_text("Не удалось проанализировать изображение. Попробуй ещё раз или отправь более чёткое фото.")
        return

    # форматируем / отправляем ответ
    try:
        formatted, parse_mode = _format_assistant_reply(raw_reply)
    except Exception:
        # если форматирование падает — отправим как plain
        formatted, parse_mode = raw_reply, None

    if conversation_store is not None:
        with contextlib.suppress(Exception):
            photo_prompt = f"📷 Фото: {caption}" if caption else "📷 Пользователь отправил фото или скриншот для разбора."
            await conversation_store.add_message(user_id, "user", photo_prompt)
            await conversation_store.add_message(user_id, "assistant", raw_reply)

    await send_text_chunks(
        context.bot,
        chat_id,
        formatted,
        parse_mode=parse_mode,
        use_streaming=False,
        reply_to_message_id=update.message.message_id if update.message else None,
    )

# Универсальная обработка текстовых сообщений (с проверкой семафора и прогрессом)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id

        queue_percent = await MESSAGE_PROCESSING_SEMAPHORE.used_percent()
        if queue_percent > 80:
            try:
                await update.effective_message.reply_text("Сейчас высокая нагрузка на бота, поэтому ответ может прийти чуть медленнее обычного.")
            except Exception:
                pass

        user_lock = await get_user_message_lock(user_id)

        async with MESSAGE_PROCESSING_SEMAPHORE:
            async with user_lock:
                try:
                    await asyncio.wait_for(_handle_message_impl(update, context), timeout=HANDLE_MESSAGE_TIMEOUT)
                except asyncio.TimeoutError:
                    logging.warning("handle_message timeout for user %s.", user_id)
                    try:
                        await update.effective_message.reply_text("⏱ Обработка заняла слишком много времени. Попробуй ещё раз через минуту.")
                    except Exception:
                        pass
                except Exception as e:
                    logging.exception("handle_message_impl failed: %s", e)
                    try:
                        await update.effective_message.reply_text("❌ Во время обработки произошла ошибка. Попробуй ещё раз.")
                    except Exception:
                        pass
    except Exception as e:
        logging.exception("handle_message outer wrapper failed: %s", e)

# Вставь сюда свою логику _handle_message_impl (я оставляю её функционально прежней, но аккуратно)
async def _handle_message_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    text = (message.text or "").strip()

    format_assistant_reply_md = _format_assistant_reply

    # админ блок
    if update.effective_user and update.effective_user.id == globals().get("OWNER_ID"):
        admin_states = ["awaiting_broadcast", "awaiting_ban", "awaiting_unban", "awaiting_msguser", "awaiting_sendto"]
        for state in admin_states:
            if context.user_data.get(state):
                context.user_data[state] = False
                context.args = text.split() if state != "awaiting_broadcast" else [text]

                if state == "awaiting_broadcast" and "broadcast" in globals(): await broadcast(update, context)
                elif state == "awaiting_ban" and "ban_user" in globals(): await ban_user(update, context)
                elif state == "awaiting_unban" and "unban_user" in globals(): await unban_user(update, context)
                elif state == "awaiting_msguser" and "owner_msguser" in globals(): await owner_msguser(update, context)
                elif state == "awaiting_sendto" and "owner_sendto" in globals(): await owner_sendto(update, context)

                await message.reply_text("Готово ✅", reply_markup=globals().get("owner_keyboard"))
                return

        admin_buttons = {
            "📤 Рассылка": ("awaiting_broadcast", "Напиши текст рассылки. Для голоса: voice <текст>"),
            "🚫 Бан": ("awaiting_ban", "Введи ID пользователя для блокировки."),
            "✅ Разбан": ("awaiting_unban", "Введи ID пользователя для разблокировки."),
            "📨 MsgUser": ("awaiting_msguser", "Формат: <user_id> <текст>"),
            "✉️ SendTo": ("awaiting_sendto", "Формат: <user_id> <тип> [параметры]"),
        }

        if text in admin_buttons:
            state, prompt = admin_buttons[text]
            context.user_data[state] = True
            await message.reply_text(prompt, reply_markup=globals().get("owner_keyboard"))
            return

        if text == "📊 Статистика" and "stats" in globals():
            await stats(update, context)
            return
        if text == "🧰 Диагностика":
            await message.reply_text("Доступные команды: /ping, /uptime, /errors", reply_markup=globals().get("owner_keyboard"))
            return
        if text == "🗑 Скрыть меню":
            await message.reply_text("Меню скрыто. Если понадобится снова, просто нажми /start.", reply_markup=globals().get("reply_markup"))
            return

    # логирование
    if "log_user_activity" in globals():
        try:
            await asyncio.to_thread(
                log_user_activity,
                user_id,
                str(username or "user")[:64],
                (text or "")[:500],
            )
        except Exception:
            logging.debug("log_user_activity failed", exc_info=True)

    # проверка подписки и бана
    if not await require_subscription(update, context):
        return
    if user_id in globals().get("banned_users", set()):
        await message.reply_text("Доступ сейчас ограничен. Если это ошибка, напиши администратору.")
        return

    rename_chat_id = context.user_data.get(CHAT_RENAME_STATE_KEY)
    if rename_chat_id:
        context.user_data.pop(CHAT_RENAME_STATE_KEY, None)
        rename_page = _safe_int(context.user_data.pop(CHAT_RENAME_PAGE_KEY, 0), 0)
        new_title = _normalize_chat_title_text(text, fallback="")
        if not new_title:
            await message.reply_text("Название не должно быть пустым. Попробуй ещё раз.")
            return
        renamed_chat = await conversation_store.rename_chat(user_id, rename_chat_id, new_title) if conversation_store is not None else None
        if not renamed_chat:
            await message.reply_text("Не удалось переименовать чат. Попробуй ещё раз.")
            return
        await message.reply_text("✅ Название чата обновлено.")
        await message.reply_text(
            build_chat_card_text(renamed_chat),
            reply_markup=get_chat_card_keyboard(renamed_chat["id"], rename_page),
        )
        return

    # Ждём ввода города
    if context.user_data.get("awaiting_weather_city"):
        context.user_data["awaiting_weather_city"] = False
        city_input = (text or "").strip()[:100]
        if not city_input:
            await message.reply_text("Напиши название города, и я сразу покажу прогноз.")
            return
        try:
            if "send_weather" in globals():
                await send_weather(update, city_input)
        except Exception as e:
            logging.exception("send_weather from menu failed: %s", e)
            await message.reply_text("Не получилось получить прогноз. Попробуй ещё раз чуть позже.")
        return

    # Ждём промпт для генерации изображения
    if context.user_data.get("awaiting_image_prompt"):
        context.user_data["awaiting_image_prompt"] = False
        prompt_clean = (text or "").strip()[:500]
        if not prompt_clean:
            await message.reply_text("Опиши, что хочешь увидеть. Чем точнее описание, тем лучше результат.")
            return
        chat_id = update.effective_chat.id
        if "log_user_action" in globals():
            log_user_action(user_id, "image_gen", prompt=prompt_clean[:80])
        status_msg = await message.reply_text("Генерирую изображение. Обычно это занимает до минуты.")
        try:
            if "generate_image" in globals():
                image_bytes = await generate_image(prompt_clean)
            else:
                image_bytes = None
            if image_bytes:
                caption = "Готово. Вот изображение по твоему описанию."
                await context.bot.send_photo(chat_id=chat_id, photo=image_bytes, caption=caption)
            else:
                await message.reply_text("Не получилось сгенерировать изображение по этому описанию. Попробуй переформулировать запрос.")
        except Exception as e:
            logging.exception("Image generation failed: %s", e)
            await message.reply_text("Сервис генерации сейчас перегружен. Попробуй ещё раз чуть позже.")
        finally:
            with contextlib.suppress(Exception):
                await status_msg.delete()
        return

    # Ожидаем вопрос от пользователя
    if context.user_data.get("awaiting_question"):
        context.user_data["awaiting_question"] = False
        if len(text) > 8000:
            await message.reply_text("Сообщение получилось слишком длинным. Сократи его до 8000 символов, и я всё обработаю.")
            return
        chat_id = update.effective_chat.id
        if "log_user_action" in globals():
            log_user_action(user_id, "text")
        show_status = should_use_visible_status_for_request(text, source="text")
        indicator_msg, progress_q, done_event, updater_task, typing_task = await start_response_feedback(
            context.bot,
            chat_id,
            reply_to_message_id=message.message_id,
            initial_text="🧠 Разбираю вопрос…",
            show_status=show_status,
        )
        try:
            role_ctx = get_role_prompt(context.user_data.get("reply_style")) if "get_role_prompt" in globals() else None
            used_chat_history_layer = False
            chat_progress_target = progress_q if progress_q is not None else (lambda _payload: None)
            local_smalltalk_reply = get_local_smalltalk_reply(text)
            if local_smalltalk_reply is not None:
                reply = local_smalltalk_reply
            # используем chat_with_openrouter если есть, иначе openrouter_request
            elif "chat_with_openrouter" in globals() and callable(globals()["chat_with_openrouter"]):
                try:
                    reply = await globals()["chat_with_openrouter"](user_id, text, bot=context.bot, chat_id=chat_id, progress_queue=chat_progress_target, context=role_ctx)
                    used_chat_history_layer = True
                except TypeError:
                    reply = await openrouter_request(text, user_id=user_id, progress_q=progress_q)
            else:
                reply = await openrouter_request(text, user_id=user_id, progress_q=progress_q)

            if not used_chat_history_layer and conversation_store is not None:
                with contextlib.suppress(Exception):
                    await conversation_store.add_message(user_id, "user", text)
                    await conversation_store.add_message(user_id, "assistant", reply)

            ans_mode = context.user_data.get("answer_mode", "text")
            md_text, md_mode = format_assistant_reply_md(reply)
            reply_style = context.user_data.get("reply_style")
            dialog_active = bool(context.user_data.get("dialog_active"))

            if ans_mode == "voice":
                success = await synthesize_and_send_voice(context.bot, chat_id, reply) if "synthesize_and_send_voice" in globals() else False
                if success and dialog_active:
                    await send_dialog_controls_message(context.bot, chat_id, user_id)
                if not success:
                    await send_answer_with_actions(
                        context.bot,
                        chat_id,
                        user_id,
                        text,
                        md_text,
                        parse_mode=md_mode,
                        reply_style=reply_style,
                        dialog_active=dialog_active,
                        reply_to_message_id=message.message_id,
                    )
            else:
                await send_answer_with_actions(
                    context.bot,
                    chat_id,
                    user_id,
                    text,
                    md_text,
                    parse_mode=md_mode,
                    reply_style=reply_style,
                    dialog_active=dialog_active,
                    reply_to_message_id=message.message_id,
                )

        except Exception as e:
            logging.error(f"Error in awaiting_question: {e}")
            with contextlib.suppress(Exception):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Не получилось сформировать ответ. Попробуй ещё раз чуть позже.",
                    reply_to_message_id=message.message_id,
                )
        finally:
            await finish_response_feedback(
                context.bot,
                chat_id,
                indicator_msg=indicator_msg,
                progress_q=progress_q,
                done_event=done_event,
                updater_task=updater_task,
                typing_task=typing_task,
                done_text="✅ Ответ готов",
            )
        return

    # кнопочные команды / quick replies
    if text in {"💬 Чат с ИИ", "💭 Новый диалог", MENU_BUTTON_DIALOG}:
        active_chat = conversation_store.get_active_chat(user_id) if conversation_store is not None else None
        context.user_data["dialog_active"] = False
        context.user_data["awaiting_question"] = False
        await clear_dialog_controls_message(context.bot, update.effective_chat.id, user_id)
        await message.reply_text(
            (
                f"💬 Открываю диалог с ИИ в чате «{active_chat['title']}».\n\n"
                "Как удобнее получать ответы?"
            ) if active_chat else "💬 Открываю диалог с ИИ.\n\nКак удобнее получать ответы?",
            reply_markup=get_ask_question_keyboard() if "get_ask_question_keyboard" in globals() else None,
        )
        return

    if text in {"📁 Мои чаты", MENU_BUTTON_CHAT}:
        context.user_data["dialog_active"] = False
        context.user_data["awaiting_question"] = False
        await message.reply_text(
            build_chat_manager_text(user_id, 0),
            reply_markup=get_chat_manager_keyboard(user_id, 0),
        )
        return

    if text in {"🎭 Стиль ответа", MENU_BUTTON_ROLE}:
        if "role_command" in globals():
            await role_command(update, context)
        return

    if text in {"📷 Разобрать фото", MENU_BUTTON_PHOTO}:
        await message.reply_text(
            "🖼 Отправь фото или скриншот прямо сюда.\n\nЯ постараюсь аккуратно распознать текст, восстановить смысл и помочь по содержанию."
        )
        return

    if text in {"🌦 Прогноз погоды", MENU_BUTTON_WEATHER}:
        context.user_data["awaiting_weather_city"] = True
        await message.reply_text("🌦 Напиши название города, и я покажу прогноз.")
        return

    if text in {"🖼 Генерация Изображения", MENU_BUTTON_IMAGE}:
        context.user_data["awaiting_image_prompt"] = True
        await message.reply_text(
            "🎨 Опиши, что хочешь увидеть.\n\nЧем точнее сюжет, стиль, детали и атмосфера, тем лучше получится изображение."
        )
        return

    if text == MENU_BUTTON_FACT:
        facts_list = globals().get("facts", [])
        fact = random.choice(facts_list) if facts_list else "Подборка фактов временно недоступна. Попробуй позже."
        await message.reply_text(f"🧠 Факт дня\n\n{fact}")
        return

    if text in {"❓ Что умею", MENU_BUTTON_HELP}:
        if "help_command" in globals():
            await help_command(update, context)
        return

    if not text:
        return

    if len(text) > 8000:
        await message.reply_text("Сообщение получилось слишком длинным. Сократи его до 8000 символов, и я всё обработаю.")
        return

    chat_id = update.effective_chat.id
    await process_question_and_send_reply(
        context,
        user_id,
        chat_id,
        text,
        reply_to_message_id=message.message_id,
    )
    return

# Голосовые сообщения (логика сохранена)
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id
        user_lock = await get_user_message_lock(user_id)
        async with MESSAGE_PROCESSING_SEMAPHORE:
            async with user_lock:
                try:
                    await asyncio.wait_for(_handle_voice_message_impl(update, context), timeout=HANDLE_VOICE_TIMEOUT)
                except asyncio.TimeoutError:
                    logging.warning("handle_voice_message timeout for user %s", user_id)
                    try:
                        await update.effective_message.reply_text("⏱ Голосовое сообщение обрабатывалось слишком долго. Попробуй ещё раз.")
                    except Exception:
                        pass
                except Exception as e:
                    logging.exception("handle_voice_message_impl failed: %s", e)
                    try:
                        await update.effective_message.reply_text("❌ Не получилось обработать голосовое сообщение. Попробуй ещё раз.")
                    except Exception:
                        pass
    except Exception as e:
        logging.exception("handle_voice_message outer wrapper failed: %s", e)

async def _handle_voice_message_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.voice:
        return
    if not await require_subscription(update, context):
        return
    user_id = update.effective_user.id
    if user_id in globals().get("banned_users", set()):
        await update.message.reply_text("Доступ сейчас ограничен. Если это ошибка, напиши администратору.")
        return

    voice = update.message.voice
    chat_id = update.effective_chat.id
    loop = asyncio.get_running_loop()

    with contextlib.suppress(Exception):
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    ogg_path = None
    try:
        file = await context.bot.get_file(voice.file_id)
        ogg_path = os.path.join(tempfile.gettempdir(), f"voice_{chat_id}_{voice.file_unique_id}.ogg")
        await file.download_to_drive(custom_path=ogg_path)
        text = await loop.run_in_executor(None, transcribe_voice_ogg_to_text, ogg_path) if "transcribe_voice_ogg_to_text" in globals() else None
    except Exception as e:
        logging.exception("handle_voice_message: download or transcribe failed: %s", e)
        text = None
    finally:
        if ogg_path and os.path.exists(ogg_path):
            with contextlib.suppress(Exception):
                os.remove(ogg_path)

    if not text or not text.strip():
        await update.message.reply_text("Не получилось разобрать голосовое сообщение. Попробуй говорить чётче или напиши вопрос текстом.")
        return

    if len(text) > 8000:
        await update.message.reply_text("Распознанный текст получился слишком длинным. Лучше разбить сообщение на части.")
        return

    if "log_user_action" in globals():
        log_user_action(user_id, "voice")
    await process_question_and_send_reply(
        context,
        user_id,
        chat_id,
        text.strip(),
        reply_to_message_id=update.message.message_id if update.message else None,
    )

# process_question_and_send_reply (индикация прогресса и безопасный вызов LLM)
async def process_question_and_send_reply(
    context: Any,
    user_id: int,
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: int | None = None,
) -> None:
    show_status = should_use_visible_status_for_request(text, source="text")
    indicator_msg, progress_q, done_event, updater_task, typing_task = await start_response_feedback(
        context.bot,
        chat_id,
        reply_to_message_id=reply_to_message_id,
        initial_text="🧠 Разбираю вопрос…",
        show_status=show_status,
    )
    try:
        try:
            role_ctx = get_role_prompt(context.user_data.get("reply_style")) if "get_role_prompt" in globals() else None
            used_chat_history_layer = False
            chat_progress_target = progress_q if progress_q is not None else (lambda _payload: None)
            local_smalltalk_reply = get_local_smalltalk_reply(text)
            if local_smalltalk_reply is not None:
                reply = local_smalltalk_reply
            elif "chat_with_openrouter" in globals() and callable(globals()["chat_with_openrouter"]):
                try:
                    reply = await globals()["chat_with_openrouter"](user_id, text, bot=context.bot, chat_id=chat_id, progress_queue=chat_progress_target, context=role_ctx)
                    used_chat_history_layer = True
                except TypeError:
                    reply = await openrouter_request(text, user_id=user_id, progress_q=progress_q)
            else:
                reply = await openrouter_request(text, user_id=user_id, progress_q=progress_q)

            if not used_chat_history_layer and conversation_store is not None:
                with contextlib.suppress(Exception):
                    await conversation_store.add_message(user_id, "user", text)
                    await conversation_store.add_message(user_id, "assistant", reply)

        except asyncio.TimeoutError:
            logging.warning("OpenRouter request timeout for user %s", user_id)
            reply = "Запрос занял слишком много времени. Попробуй ещё раз чуть позже."
        except Exception as e:
            logging.exception("chat_with_openrouter failed: %s", e)
            reply = "Не получилось связаться с ИИ-сервисом. Попробуй ещё раз чуть позже."

        md_text, md_mode = _format_assistant_reply(reply) if "_format_assistant_reply" in globals() else (reply, None)
        mode = context.user_data.pop("answer_mode", "text")
        reply_style = context.user_data.get("reply_style")
        dialog_active = bool(context.user_data.get("dialog_active"))
        try:
            if mode == "voice":
                sent = await synthesize_and_send_voice(context.bot, chat_id, reply) if "synthesize_and_send_voice" in globals() else False
                if sent and dialog_active:
                    await send_dialog_controls_message(context.bot, chat_id, user_id)
                if not sent:
                    await send_answer_with_actions(
                        context.bot,
                        chat_id,
                        user_id,
                        text,
                        md_text,
                        parse_mode=md_mode,
                        reply_style=reply_style,
                        dialog_active=dialog_active,
                        reply_to_message_id=reply_to_message_id,
                    )
            else:
                await send_answer_with_actions(
                    context.bot,
                    chat_id,
                    user_id,
                    text,
                    md_text,
                    parse_mode=md_mode,
                    reply_style=reply_style,
                    dialog_active=dialog_active,
                    reply_to_message_id=reply_to_message_id,
                )
        except Exception as e:
            logging.exception("TTS/send failed in process_question_and_send_reply: %s", e)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=reply,
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception as ex:
                logging.exception("fallback send failed: %s", ex)
    finally:
        await finish_response_feedback(
            context.bot,
            chat_id,
            indicator_msg=indicator_msg,
            progress_q=progress_q,
            done_event=done_event,
            updater_task=updater_task,
            typing_task=typing_task,
            done_text="✅ Ответ готов",
        )

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_weather_city"] = True
    if update.effective_message:
        await update.effective_message.reply_text("Напиши название города, и я покажу прогноз.")


async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_image_prompt"] = True
    if update.effective_message:
        await update.effective_message.reply_text("Опиши, что хочешь увидеть. Чем точнее запрос, тем лучше результат.")


def _extract_command_args(text: str | None) -> list[str]:
    if not text:
        return []
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return []
    return parts[1].split()


def _build_context(bot: Bot, update: Update, *, args: list[str] | None = None) -> ContextTypes.DEFAULT_TYPE:
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    return ContextTypes.DEFAULT_TYPE(
        bot=bot,
        user_id=user_id,
        chat_id=chat_id,
        args=list(args or []),
    )


async def _invoke_compat_handler(
    handler: Callable[..., Any],
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        result = handler(update, context)
        if asyncio.iscoroutine(result):
            await result
    except RetryAfter as error:
        retry_after = getattr(error, "retry_after", 1)
        logging.warning(
            "Aiogram handler %s hit RetryAfter, sleeping %.1f seconds",
            getattr(handler, "__name__", repr(handler)),
            retry_after,
        )
        await asyncio.sleep(min(retry_after + 1, 20))
    except Exception as error:
        logging.error(
            "Unhandled error in handler %s: %s",
            getattr(handler, "__name__", repr(handler)),
            error,
            exc_info=(type(error), error, getattr(error, "__traceback__", None)),
        )


def _wrap_message_handler(
    bot: Bot,
    handler: Callable[..., Any],
    *,
    parse_args: bool = False,
) -> Callable[[AiogramMessage], Any]:
    async def _wrapped(message: AiogramMessage) -> None:
        update = Update.from_message(message, bot)
        args = _extract_command_args(message.text) if parse_args else []
        context = _build_context(bot, update, args=args)
        await _invoke_compat_handler(handler, update, context)

    return _wrapped


def _wrap_callback_handler(bot: Bot, handler: Callable[..., Any]) -> Callable[[AiogramCallbackQuery], Any]:
    async def _wrapped(callback_query: AiogramCallbackQuery) -> None:
        update = Update.from_callback_query(callback_query, bot)
        context = _build_context(bot, update)
        await _invoke_compat_handler(handler, update, context)

    return _wrapped


async def _run_aiogram_polling(token: str) -> None:
    bot = Bot(token)
    dispatcher = Dispatcher()
    router = Router()
    polling_concurrency_limit = int(getattr(MESSAGE_PROCESSING_SEMAPHORE, "limit", SEMAPHORE_LIMIT) or SEMAPHORE_LIMIT)

    command_handlers = [
        ("start", globals().get("start", lambda u, c: None)),
        ("help", globals().get("help_command", lambda u, c: None)),
        ("role", globals().get("role_command", lambda u, c: None)),
        ("broadcast", globals().get("broadcast", lambda u, c: None)),
        ("ban", globals().get("ban_user", lambda u, c: None)),
        ("unban", globals().get("unban_user", lambda u, c: None)),
        ("stats", globals().get("stats", lambda u, c: None)),
        ("profile", globals().get("profile_command", lambda u, c: None)),
        ("ping", globals().get("ping", lambda u, c: None)),
        ("uptime", globals().get("uptime", lambda u, c: None)),
        ("admin", globals().get("admin_command", lambda u, c: None)),
        ("status", globals().get("status", lambda u, c: None)),
        ("keys", globals().get("keys_command", lambda u, c: None)),
        ("models", globals().get("models_command", lambda u, c: None)),
        ("reloadkeys", globals().get("reloadkeys_command", lambda u, c: None)),
        ("errors", globals().get("errors", lambda u, c: None)),
        ("msguser", globals().get("owner_msguser", lambda u, c: None)),
        ("sendto", globals().get("owner_sendto", lambda u, c: None)),
        ("weather", globals().get("weather_command", weather_command)),
        ("image", globals().get("image_command", image_command)),
        ("clear", globals().get("clear_context_command", lambda u, c: None)),
    ]

    for command_name, handler in command_handlers:
        router.message.register(
            _wrap_message_handler(bot, handler, parse_args=True),
            Command(command_name),
        )

    weather_callback_filter = F.data.regexp(r"^(today|tomorrow|5days)\|")
    router.callback_query.register(
        _wrap_callback_handler(bot, globals().get("weather_button_handler", lambda u, c: None)),
        weather_callback_filter,
    )
    router.callback_query.register(
        _wrap_callback_handler(bot, globals().get("handle_callback_query", lambda u, c: None)),
        ~weather_callback_filter,
    )

    router.message.register(_wrap_message_handler(bot, handle_voice_message), F.voice)
    router.message.register(_wrap_message_handler(bot, _handle_photo_message_impl), F.photo)
    router.message.register(
        _wrap_message_handler(bot, handle_message),
        F.text & ~F.text.startswith("/"),
    )

    dispatcher.include_router(router)

    global QUEST_QUEUE
    QUEST_QUEUE = asyncio.Queue(maxsize=500)

    try:
        await bot.raw.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(
            bot.raw,
            handle_as_tasks=True,
            tasks_concurrency_limit=polling_concurrency_limit,
        )
    finally:
        with contextlib.suppress(Exception):
            await close_shared_httpx_client()
        with contextlib.suppress(Exception):
            await bot.close()


_SINGLE_INSTANCE_LOCK_HANDLE = None
_SINGLE_INSTANCE_LOCK_PATH = os.path.join(os.path.dirname(__file__), ".main.py.lock")

def _release_single_instance_lock() -> None:
    global _SINGLE_INSTANCE_LOCK_HANDLE
    handle = _SINGLE_INSTANCE_LOCK_HANDLE
    if handle is None:
        return

    with contextlib.suppress(Exception):
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        handle.close()
    _SINGLE_INSTANCE_LOCK_HANDLE = None

def _acquire_single_instance_lock() -> bool:
    global _SINGLE_INSTANCE_LOCK_HANDLE
    if _SINGLE_INSTANCE_LOCK_HANDLE is not None:
        return True

    handle = open(_SINGLE_INSTANCE_LOCK_PATH, "a+b")
    try:
        handle.seek(0)
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            logging.warning("No file-locking backend is available; single-instance lock is disabled")
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
    except OSError:
        with contextlib.suppress(Exception):
            handle.close()
        return False

    _SINGLE_INSTANCE_LOCK_HANDLE = handle
    atexit.register(_release_single_instance_lock)
    return True

def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
        handlers=[
            logging.FileHandler("bot_errors.log", encoding="utf-8"),
            logging.StreamHandler()
        ]
    )

    def log_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.error("Необработанное исключение", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = log_exception

    if not _acquire_single_instance_lock():
        message = "Бот уже запущен в другом окне или процессе. Останови предыдущий экземпляр и запусти только один."
        logging.error(message)
        print(message)
        return

    TELEGRAM_TOKEN_LOCAL = (
        globals().get("TELEGRAM_TOKEN")
        or os.getenv("MAIN_BOT_TOKEN")
        or os.getenv("TELEGRAM_TOKEN")
        or os.getenv("BOT_TOKEN")
    )
    if not TELEGRAM_TOKEN_LOCAL:
        print("Укажите TELEGRAM_TOKEN в переменных окружения или в файле .env")
        sys.exit(1)

    if is_openrouter_proxy_enabled() and not ensure_openrouter_proxy_running():
        message = (
            "Не удалось запустить локальный OpenRouter proxy. "
            "Проверьте openrouter_proxy.py и переменные OPENROUTER_PROXY_* в .env."
        )
        logging.error(message)
        print(message)
        sys.exit(1)
    if is_openrouter_proxy_enabled():
        atexit.register(stop_openrouter_proxy_supervisor)
        start_openrouter_proxy_supervisor()

    print("Запуск бота — aiogram 3.x...")
    asyncio.run(_run_aiogram_polling(TELEGRAM_TOKEN_LOCAL))


if __name__ == "__main__":
    main()
