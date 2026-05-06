from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot as AiogramBot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery as AiogramCallbackQuery,
    FSInputFile,
    InlineKeyboardButton as AiogramInlineKeyboardButton,
    InlineKeyboardMarkup as AiogramInlineKeyboardMarkup,
    Message as AiogramMessage,
    WebAppInfo as AiogramWebAppInfo,
)


class NetworkError(TelegramNetworkError):
    pass


RetryAfter = TelegramRetryAfter
TimedOut = TelegramNetworkError


_MOJIBAKE_HINT_RE = re.compile(r"(Р.|С.|вЂ|рџ|Ѓ|Ў)")


def _normalize_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if not _MOJIBAKE_HINT_RE.search(text):
        return text
    try:
        fixed = text.encode("cp1251").decode("utf-8")
        return fixed or text
    except Exception:
        return text


# Enhanced mojibake repair: keep this override below legacy implementation.
_MOJIBAKE_HINT_RE = re.compile(r"(вЂ|Â|Ã|Ð|Ñ)")
_RU_CYRILLIC_CHARS = set(
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
)


def _contains_non_russian_cyrillic(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if 0x0400 <= code <= 0x04FF and ch not in _RU_CYRILLIC_CHARS:
            return True
    return False


def _repair_mojibake_once(text: str) -> str:
    try:
        repaired = text.encode("cp1251").decode("utf-8")
        return repaired or text
    except Exception:
        return text


def _normalize_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if not _MOJIBAKE_HINT_RE.search(text) and not _contains_non_russian_cyrillic(text):
        return text
    fixed = text
    for _ in range(2):
        candidate = _repair_mojibake_once(fixed)
        if candidate == fixed:
            break
        fixed = candidate
        if not _MOJIBAKE_HINT_RE.search(fixed) and not _contains_non_russian_cyrillic(fixed):
            break
    return fixed


@dataclass(slots=True)
class WebAppInfo:
    url: str

    def to_aiogram(self) -> AiogramWebAppInfo:
        return AiogramWebAppInfo(url=self.url)


@dataclass(slots=True)
class InlineKeyboardButton:
    text: str
    callback_data: Optional[str] = None
    url: Optional[str] = None
    web_app: Optional[WebAppInfo] = None

    def to_aiogram(self) -> AiogramInlineKeyboardButton:
        return AiogramInlineKeyboardButton(
            text=_normalize_text(self.text),
            callback_data=self.callback_data,
            url=self.url,
            web_app=self.web_app.to_aiogram() if self.web_app else None,
        )


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard: list[list[InlineKeyboardButton]]):
        self.inline_keyboard = inline_keyboard

    def to_aiogram(self) -> AiogramInlineKeyboardMarkup:
        return AiogramInlineKeyboardMarkup(
            inline_keyboard=[
                [button.to_aiogram() if hasattr(button, "to_aiogram") else button for button in row]
                for row in self.inline_keyboard
            ]
        )


def _normalize_reply_markup(reply_markup: Any) -> Any:
    if reply_markup is None:
        return None
    if hasattr(reply_markup, "to_aiogram"):
        normalized = reply_markup.to_aiogram()
    else:
        normalized = reply_markup
    try:
        inline_rows = getattr(normalized, "inline_keyboard", None)
        if inline_rows:
            for row in inline_rows:
                for button in row:
                    if hasattr(button, "text"):
                        button.text = _normalize_text(getattr(button, "text", ""))
    except Exception:
        pass
    return normalized


def _normalize_file(
    value: Any,
    *,
    filename: Optional[str] = None,
    default_name: str = "file.bin",
) -> Any:
    if value is None:
        return None
    if isinstance(value, (BufferedInputFile, FSInputFile)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return BufferedInputFile(bytes(value), filename=filename or default_name)
    if isinstance(value, (str, Path)):
        return FSInputFile(str(value))
    if isinstance(value, io.BytesIO):
        current_pos = value.tell()
        try:
            value.seek(0)
            data = value.read()
        finally:
            try:
                value.seek(current_pos)
            except Exception:
                pass
        file_name = filename or Path(getattr(value, "name", default_name)).name
        return BufferedInputFile(data, filename=file_name)
    if hasattr(value, "read"):
        file_name = filename or Path(getattr(value, "name", default_name)).name
        try:
            current_pos = value.tell() if hasattr(value, "tell") else None
        except Exception:
            current_pos = None
        try:
            if hasattr(value, "seek"):
                value.seek(0)
            data = value.read()
        finally:
            if current_pos is not None and hasattr(value, "seek"):
                try:
                    value.seek(current_pos)
                except Exception:
                    pass
        if isinstance(data, str):
            data = data.encode("utf-8")
        return BufferedInputFile(data, filename=file_name)
    return value


class CompatMessage:
    def __init__(self, native_message: AiogramMessage, bot: "CompatBot"):
        self._native_message = native_message
        self._bot = bot

    def __getattr__(self, item: str) -> Any:
        if item == "message_id":
            return self._native_message.message_id
        if item == "id":
            return self._native_message.message_id
        if item == "raw_text":
            return self._native_message.text or self._native_message.caption or ""
        return getattr(self._native_message, item)

    @property
    def text(self) -> str:
        return self._native_message.text or self._native_message.caption or ""

    @property
    def message_id(self) -> int:
        return self._native_message.message_id

    @property
    def id(self) -> int:
        return self._native_message.message_id

    async def reply_text(self, text: str, **kwargs: Any) -> "CompatMessage":
        return await self._bot.send_message(chat_id=self._native_message.chat.id, text=text, **kwargs)


class CompatCallbackQuery:
    def __init__(self, native_query: AiogramCallbackQuery, bot: "CompatBot"):
        self._native_query = native_query
        self._bot = bot

    def __getattr__(self, item: str) -> Any:
        if item == "message" and self._native_query.message is not None:
            return CompatMessage(self._native_query.message, self._bot)
        return getattr(self._native_query, item)

    @property
    def data(self) -> Optional[str]:
        return self._native_query.data

    @property
    def from_user(self) -> Any:
        return self._native_query.from_user

    @property
    def message(self) -> Optional[CompatMessage]:
        if self._native_query.message is None:
            return None
        return CompatMessage(self._native_query.message, self._bot)

    async def answer(self, text: Optional[str] = None, **kwargs: Any) -> Any:
        return await self._native_query.answer(text=text, **kwargs)

    async def edit_message_text(self, text: str, **kwargs: Any) -> Any:
        native_message = self._native_query.message
        if native_message is None:
            return None
        result = await native_message.edit_text(
            text=_normalize_text(text),
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            parse_mode=kwargs.pop("parse_mode", None),
            **kwargs,
        )
        if isinstance(result, AiogramMessage):
            return CompatMessage(result, self._bot)
        return result


class CompatUpdate:
    def __init__(
        self,
        *,
        message: Optional[CompatMessage] = None,
        callback_query: Optional[CompatCallbackQuery] = None,
    ):
        self.message = message
        self.callback_query = callback_query

    @property
    def effective_user(self) -> Any:
        if self.callback_query is not None:
            return self.callback_query.from_user
        if self.message is not None:
            return getattr(self.message, "from_user", None)
        return None

    @property
    def effective_chat(self) -> Any:
        if self.callback_query is not None and self.callback_query.message is not None:
            return getattr(self.callback_query.message, "chat", None)
        if self.message is not None:
            return getattr(self.message, "chat", None)
        return None

    @classmethod
    def from_message(cls, native_message: AiogramMessage, bot: "CompatBot") -> "CompatUpdate":
        return cls(message=CompatMessage(native_message, bot))

    @classmethod
    def from_callback_query(
        cls,
        native_query: AiogramCallbackQuery,
        bot: "CompatBot",
    ) -> "CompatUpdate":
        return cls(callback_query=CompatCallbackQuery(native_query, bot))


class CompatContext:
    def __init__(self, application: "CompatApplication", bot: "CompatBot"):
        self.application = application
        self.bot = bot
        self.bot_data = application.bot_data
        self.error: Optional[BaseException] = None


class ContextTypes:
    DEFAULT_TYPE = CompatContext


class CompatBot:
    def __init__(self, native_bot: AiogramBot):
        self._native_bot = native_bot

    @property
    def native(self) -> AiogramBot:
        return self._native_bot

    async def get_me(self) -> Any:
        return await self._native_bot.get_me()

    async def set_my_commands(self, commands: list[Any], **kwargs: Any) -> Any:
        return await self._native_bot.set_my_commands(commands=commands, **kwargs)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> CompatMessage:
        message = await self._native_bot.send_message(
            chat_id=chat_id,
            text=_normalize_text(text),
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            parse_mode=kwargs.pop("parse_mode", None),
            **kwargs,
        )
        return CompatMessage(message, self)

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> CompatMessage:
        message = await self._native_bot.send_photo(
            chat_id=chat_id,
            photo=_normalize_file(photo, default_name="photo.jpg"),
            caption=_normalize_text(kwargs.pop("caption", None)),
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            parse_mode=kwargs.pop("parse_mode", None),
            **kwargs,
        )
        return CompatMessage(message, self)

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> CompatMessage:
        filename = kwargs.pop("filename", None)
        message = await self._native_bot.send_document(
            chat_id=chat_id,
            document=_normalize_file(document, filename=filename, default_name="document.bin"),
            caption=_normalize_text(kwargs.pop("caption", None)),
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            parse_mode=kwargs.pop("parse_mode", None),
            **kwargs,
        )
        return CompatMessage(message, self)

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> CompatMessage:
        message = await self._native_bot.send_video(
            chat_id=chat_id,
            video=_normalize_file(video, default_name="video.mp4"),
            caption=_normalize_text(kwargs.pop("caption", None)),
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            parse_mode=kwargs.pop("parse_mode", None),
            **kwargs,
        )
        return CompatMessage(message, self)

    async def send_video_note(self, chat_id: int, video_note: Any, **kwargs: Any) -> CompatMessage:
        message = await self._native_bot.send_video_note(
            chat_id=chat_id,
            video_note=_normalize_file(video_note, default_name="video_note.mp4"),
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            **kwargs,
        )
        return CompatMessage(message, self)

    async def send_voice(self, chat_id: int, voice: Any, **kwargs: Any) -> CompatMessage:
        message = await self._native_bot.send_voice(
            chat_id=chat_id,
            voice=_normalize_file(voice, default_name="voice.ogg"),
            caption=_normalize_text(kwargs.pop("caption", None)),
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            parse_mode=kwargs.pop("parse_mode", None),
            **kwargs,
        )
        return CompatMessage(message, self)

    async def send_audio(self, chat_id: int, audio: Any, **kwargs: Any) -> CompatMessage:
        message = await self._native_bot.send_audio(
            chat_id=chat_id,
            audio=_normalize_file(audio, default_name="audio.mp3"),
            caption=_normalize_text(kwargs.pop("caption", None)),
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            parse_mode=kwargs.pop("parse_mode", None),
            **kwargs,
        )
        return CompatMessage(message, self)

    async def send_invoice(
        self,
        chat_id: int,
        title: str,
        description: str,
        payload: str,
        currency: str,
        prices: list[Any],
        **kwargs: Any,
    ) -> CompatMessage:
        message = await self._native_bot.send_invoice(
            chat_id=chat_id,
            title=_normalize_text(title),
            description=_normalize_text(description),
            payload=payload,
            currency=currency,
            prices=prices,
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            **kwargs,
        )
        return CompatMessage(message, self)

    async def answer_pre_checkout_query(
        self,
        pre_checkout_query_id: str,
        ok: bool,
        **kwargs: Any,
    ) -> Any:
        return await self._native_bot.answer_pre_checkout_query(
            pre_checkout_query_id=pre_checkout_query_id,
            ok=ok,
            **kwargs,
        )

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs: Any) -> Any:
        result = await self._native_bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_normalize_text(text),
            reply_markup=_normalize_reply_markup(kwargs.pop("reply_markup", None)),
            parse_mode=kwargs.pop("parse_mode", None),
            **kwargs,
        )
        if isinstance(result, AiogramMessage):
            return CompatMessage(result, self)
        return result

    async def delete_message(self, chat_id: int, message_id: int, **kwargs: Any) -> Any:
        return await self._native_bot.delete_message(chat_id=chat_id, message_id=message_id, **kwargs)


class CompatApplication:
    def __init__(self, token: str):
        native_bot = AiogramBot(
            token=token,
            default=DefaultBotProperties(parse_mode=None),
        )
        self._native_bot = native_bot
        self.bot = CompatBot(native_bot)
        self.bot_data: dict[str, Any] = {}

    @property
    def native_bot(self) -> AiogramBot:
        return self._native_bot

    def build_context(self) -> CompatContext:
        return CompatContext(application=self, bot=self.bot)


Application = CompatApplication
Update = CompatUpdate
