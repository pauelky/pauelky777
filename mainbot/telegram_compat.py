from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aiogram import Bot as AiogramBot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import (
    TelegramAPIError as TelegramError,
    TelegramBadRequest as BadRequest,
    TelegramForbiddenError as Forbidden,
    TelegramRetryAfter as RetryAfter,
)
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery as AiogramCallbackQuery,
    FSInputFile,
    InlineKeyboardButton as AiogramInlineKeyboardButton,
    InlineKeyboardMarkup as AiogramInlineKeyboardMarkup,
    KeyboardButton as AiogramKeyboardButton,
    Message as AiogramMessage,
    ReplyKeyboardMarkup as AiogramReplyKeyboardMarkup,
)


@dataclass(slots=True)
class KeyboardButton:
    text: str


@dataclass(slots=True)
class InlineKeyboardButton:
    text: str
    callback_data: Optional[str] = None
    url: Optional[str] = None


@dataclass(slots=True)
class ReplyKeyboardMarkup:
    keyboard: List[List[KeyboardButton]]
    resize_keyboard: bool = False


@dataclass(slots=True)
class InlineKeyboardMarkup:
    inline_keyboard: List[List[InlineKeyboardButton]]


def _convert_button(button: Any) -> Any:
    if isinstance(button, KeyboardButton):
        return AiogramKeyboardButton(text=button.text)
    if isinstance(button, InlineKeyboardButton):
        return AiogramInlineKeyboardButton(
            text=button.text,
            callback_data=button.callback_data,
            url=button.url,
        )
    return button


def convert_reply_markup(markup: Any) -> Any:
    if markup is None:
        return None
    if isinstance(markup, ReplyKeyboardMarkup):
        keyboard = [[_convert_button(button) for button in row] for row in markup.keyboard]
        return AiogramReplyKeyboardMarkup(
            keyboard=keyboard,
            resize_keyboard=markup.resize_keyboard,
        )
    if isinstance(markup, InlineKeyboardMarkup):
        inline_keyboard = [[_convert_button(button) for button in row] for row in markup.inline_keyboard]
        return AiogramInlineKeyboardMarkup(inline_keyboard=inline_keyboard)
    return markup


def _read_file_like(value: Any) -> bytes:
    position = None
    if hasattr(value, "tell"):
        try:
            position = value.tell()
        except Exception:
            position = None

    try:
        if hasattr(value, "seek"):
            value.seek(0)
        data = value.read()
    finally:
        if position is not None and hasattr(value, "seek"):
            try:
                value.seek(position)
            except Exception:
                pass

    if isinstance(data, str):
        return data.encode("utf-8")
    return data


def _prepare_file(value: Any, default_name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return BufferedInputFile(bytes(value), filename=default_name)
    if hasattr(value, "read"):
        filename = os.path.basename(getattr(value, "name", "") or default_name)
        data = _read_file_like(value)
        return BufferedInputFile(data, filename=filename)
    if isinstance(value, os.PathLike):
        return FSInputFile(os.fspath(value))
    if isinstance(value, str):
        if os.path.exists(value):
            return FSInputFile(value)
        return value
    return value


class TelegramFile:
    def __init__(self, bot: "Bot", raw_file: Any):
        self._bot = bot
        self._raw_file = raw_file

    def __getattr__(self, item: str) -> Any:
        return getattr(self._raw_file, item)

    async def download_to_drive(self, custom_path: str) -> str:
        await self._bot.raw.download(self._raw_file, destination=custom_path)
        return custom_path


class Bot:
    def __init__(self, token: str, *, default: DefaultBotProperties | None = None):
        self._bot = AiogramBot(token=token, default=default or DefaultBotProperties())

    @property
    def raw(self) -> AiogramBot:
        return self._bot

    @property
    def session(self) -> Any:
        return self._bot.session

    def __getattr__(self, item: str) -> Any:
        return getattr(self._bot, item)

    def _wrap_message(self, message: Any) -> Any:
        if isinstance(message, AiogramMessage):
            return Message(message, self)
        return message

    async def close(self) -> None:
        await self._bot.session.close()

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        chat_id = kwargs.pop("chat_id", None)
        text = kwargs.pop("text", None)
        if args:
            if chat_id is None:
                chat_id = args[0]
            if len(args) > 1 and text is None:
                text = args[1]
        kwargs["reply_markup"] = convert_reply_markup(kwargs.get("reply_markup"))
        message = await self._bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return self._wrap_message(message)

    async def send_photo(self, *args: Any, **kwargs: Any) -> Any:
        chat_id = kwargs.pop("chat_id", None)
        photo = kwargs.pop("photo", None)
        if args:
            if chat_id is None:
                chat_id = args[0]
            if len(args) > 1 and photo is None:
                photo = args[1]
        kwargs["reply_markup"] = convert_reply_markup(kwargs.get("reply_markup"))
        prepared_photo = _prepare_file(photo, default_name="photo.jpg")
        message = await self._bot.send_photo(chat_id=chat_id, photo=prepared_photo, **kwargs)
        return self._wrap_message(message)

    async def send_voice(self, *args: Any, **kwargs: Any) -> Any:
        chat_id = kwargs.pop("chat_id", None)
        voice = kwargs.pop("voice", None)
        if args:
            if chat_id is None:
                chat_id = args[0]
            if len(args) > 1 and voice is None:
                voice = args[1]
        kwargs["reply_markup"] = convert_reply_markup(kwargs.get("reply_markup"))
        prepared_voice = _prepare_file(voice, default_name="voice.ogg")
        message = await self._bot.send_voice(chat_id=chat_id, voice=prepared_voice, **kwargs)
        return self._wrap_message(message)

    async def send_audio(self, *args: Any, **kwargs: Any) -> Any:
        chat_id = kwargs.pop("chat_id", None)
        audio = kwargs.pop("audio", None)
        if args:
            if chat_id is None:
                chat_id = args[0]
            if len(args) > 1 and audio is None:
                audio = args[1]
        kwargs["reply_markup"] = convert_reply_markup(kwargs.get("reply_markup"))
        prepared_audio = _prepare_file(audio, default_name="audio.mp3")
        message = await self._bot.send_audio(chat_id=chat_id, audio=prepared_audio, **kwargs)
        return self._wrap_message(message)

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        text = kwargs.pop("text", None)
        if args and text is None:
            text = args[0]
        if len(args) > 1 and "chat_id" not in kwargs:
            kwargs["chat_id"] = args[1]
        if len(args) > 2 and "message_id" not in kwargs:
            kwargs["message_id"] = args[2]
        kwargs["reply_markup"] = convert_reply_markup(kwargs.get("reply_markup"))
        result = await self._bot.edit_message_text(text=text, **kwargs)
        return self._wrap_message(result)

    async def send_chat_action(self, *args: Any, **kwargs: Any) -> Any:
        chat_id = kwargs.pop("chat_id", None)
        action = kwargs.pop("action", None)
        if args:
            if chat_id is None:
                chat_id = args[0]
            if len(args) > 1 and action is None:
                action = args[1]
        return await self._bot.send_chat_action(chat_id=chat_id, action=action, **kwargs)

    async def get_file(self, file_id: str, **kwargs: Any) -> TelegramFile:
        raw_file = await self._bot.get_file(file_id, **kwargs)
        return TelegramFile(self, raw_file)

    async def delete_message(self, *args: Any, **kwargs: Any) -> Any:
        chat_id = kwargs.pop("chat_id", None)
        message_id = kwargs.pop("message_id", None)
        if args:
            if chat_id is None:
                chat_id = args[0]
            if len(args) > 1 and message_id is None:
                message_id = args[1]
        return await self._bot.delete_message(chat_id=chat_id, message_id=message_id, **kwargs)


class Message:
    def __init__(self, raw_message: AiogramMessage, bot: Bot):
        self._raw_message = raw_message
        self._bot = bot

    def __getattr__(self, item: str) -> Any:
        return getattr(self._raw_message, item)

    async def reply_text(self, text: str, **kwargs: Any) -> Any:
        kwargs.setdefault("reply_to_message_id", self.message_id)
        return await self._bot.send_message(
            chat_id=self.chat.id,
            text=text,
            **kwargs,
        )

    async def edit_text(self, text: str, **kwargs: Any) -> Any:
        return await self._bot.edit_message_text(
            text=text,
            chat_id=self.chat.id,
            message_id=self.message_id,
            **kwargs,
        )


class CallbackQuery:
    def __init__(self, raw_callback_query: AiogramCallbackQuery, bot: Bot):
        self._raw_callback_query = raw_callback_query
        self._bot = bot
        self.message = Message(raw_callback_query.message, bot) if raw_callback_query.message else None

    def __getattr__(self, item: str) -> Any:
        return getattr(self._raw_callback_query, item)

    async def answer(self, *args: Any, **kwargs: Any) -> Any:
        return await self._raw_callback_query.answer(*args, **kwargs)

    async def edit_message_text(self, text: str, **kwargs: Any) -> Any:
        if self.message is not None:
            return await self.message.edit_text(text, **kwargs)
        return await self._bot.edit_message_text(
            text=text,
            inline_message_id=self.inline_message_id,
            **kwargs,
        )


class Update:
    def __init__(
        self,
        *,
        message: AiogramMessage | None = None,
        callback_query: AiogramCallbackQuery | None = None,
        bot: Bot,
    ):
        self.message = Message(message, bot) if message is not None else None
        self.callback_query = CallbackQuery(callback_query, bot) if callback_query is not None else None
        self.effective_message = self.callback_query.message if self.callback_query and self.callback_query.message else self.message
        self.effective_user = (
            message.from_user
            if message is not None
            else callback_query.from_user
            if callback_query is not None
            else None
        )
        self.effective_chat = (
            message.chat
            if message is not None
            else callback_query.message.chat
            if callback_query is not None and callback_query.message is not None
            else None
        )

    @classmethod
    def from_message(cls, message: AiogramMessage, bot: Bot) -> "Update":
        return cls(message=message, bot=bot)

    @classmethod
    def from_callback_query(cls, callback_query: AiogramCallbackQuery, bot: Bot) -> "Update":
        return cls(callback_query=callback_query, bot=bot)


_USER_DATA_STORE: Dict[int, Dict[str, Any]] = {}
_CHAT_DATA_STORE: Dict[int, Dict[str, Any]] = {}


@dataclass(slots=True)
class Context:
    bot: Bot
    user_id: Optional[int] = None
    chat_id: Optional[int] = None
    args: List[str] = field(default_factory=list)
    application: Any = None
    job_queue: Any = None

    @property
    def user_data(self) -> Dict[str, Any]:
        if self.user_id is None:
            return {}
        return _USER_DATA_STORE.setdefault(self.user_id, {})

    @property
    def chat_data(self) -> Dict[str, Any]:
        if self.chat_id is None:
            return {}
        return _CHAT_DATA_STORE.setdefault(self.chat_id, {})


class ContextTypes:
    DEFAULT_TYPE = Context

