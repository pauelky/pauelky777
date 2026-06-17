from .shared import *
from .events_core import *
from .state import *
from .runtime import *
from aiogram import Dispatcher, Router
from aiogram.types import CallbackQuery as AiogramCallbackQuery
from aiogram.types import Message as AiogramMessage
from aiogram.types import BotCommand
from .aiogram_compat import Update

AUTH_CODE_TTL_SEC = 20 * 60
AUTH_2FA_TTL_SEC = 20 * 60
SAVEDBOT_BUILD_LABEL = "free-build-2026-05-26"


def _build_code_sent_text(*, resent: bool = False) -> str:
    title = (
        "\u2705 <b>\u041d\u043e\u0432\u044b\u0439 \u043a\u043e\u0434 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d \u043d\u0430 \u0432\u0430\u0448 \u043d\u043e\u043c\u0435\u0440</b>"
        if resent
        else "\u2705 <b>\u041a\u043e\u0434 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d \u043d\u0430 \u0432\u0430\u0448 \u043d\u043e\u043c\u0435\u0440</b>"
    )
    return (
        f"{title}\n\n"
        "<b>ВНИМАНИЕ!!!!</b>\n"
        "Код нужно вводить строго через пробелы:\n"
        "<code>1 2 3 4 5</code>\n\n"
        "Если в коде 4 или 6 цифр, тоже ставьте пробел между каждой цифрой."
    )


async def _update_auth_message(
    bot,
    app: "App",
    uid: int,
    uname: Optional[str],
    text: str,
    *,
    message_id: Optional[int] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[int]:
    if message_id is not None:
        edited = await _safe_edit_message(
            bot,
            uid,
            message_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        if edited:
            return message_id

    m = await send_and_log(
        bot,
        uid,
        text,
        username=uname,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )
    try:
        app.auth.track_auth_message(uid, m.message_id)
    except Exception:
        logger.debug("Failed to track auth message uid=%s", uid)
    return getattr(m, "message_id", None)


async def _request_telegram_code(app: "App", uid: int, phone: str, *, reuse_existing_client: bool) -> Tuple[Any, str]:
    lock = app.auth.get_user_auth_lock(uid)
    async with lock:
        client, prefix = await asyncio.wait_for(
            app.auth.get_or_create_tmp_client(uid, reuse_existing=reuse_existing_client),
            timeout=20.0,
        )

        last_exc: Optional[BaseException] = None
        for attempt in range(1, CONFIG.send_code_retries + 1):
            try:
                if not client.is_connected():
                    await client.connect()

                code_request = await asyncio.wait_for(
                    client.send_code_request(phone),
                    timeout=45,
                )
                logger.info(
                    "[AUTH] Code request successful for uid=%s attempt=%d tmp_prefix=%s hash_present=%s",
                    uid,
                    attempt,
                    prefix,
                    bool(getattr(code_request, "phone_code_hash", None)),
                )
                return code_request, prefix
            except FloodWaitError:
                raise
            except SendCodeUnavailableError:
                raise
            except PhoneNumberInvalidError:
                raise
            except (ConnectionError, OSError, asyncio.TimeoutError, TimeoutError) as e:
                last_exc = e
                logger.warning("[AUTH] Network/timeout on send_code attempt %d for %s: %s", attempt, uid, type(e).__name__)
                if attempt >= CONFIG.send_code_retries:
                    raise
                await asyncio.sleep(CONFIG.send_code_retry_delay * attempt)
                client, prefix = await asyncio.wait_for(
                    app.auth.get_or_create_tmp_client(uid, reuse_existing=reuse_existing_client),
                    timeout=20.0,
                )
            except Exception as e:
                last_exc = e
                logger.exception("[AUTH] Unexpected error on send_code attempt %d for %s", attempt, uid)
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError("send_code_request returned no result")


def _auth_expires_at(ttl_sec: int) -> float:
    return time.time() + float(ttl_sec)


def build_start_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton("📱 Подключить аккаунт по номеру телефона", callback_data="auth_phone")],
        [InlineKeyboardButton("🗝 Подключить аккаунт по QR-коду", callback_data="auth_qr")],
        [
            InlineKeyboardButton("✨ Посмотреть возможности SavedBot", callback_data="start_advantages"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _build_marker_line() -> str:
    return f"<code>{SAVEDBOT_BUILD_LABEL}</code>"


def _build_set_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 Открыть профиль и статус доступа", callback_data="set_profile")],
            [InlineKeyboardButton("🎚 Настроить прослушивание чатов", callback_data="set_listen_menu")],
            [InlineKeyboardButton("📊 Посмотреть статистику архива", callback_data="stats_panel")],
            [InlineKeyboardButton("🔔 Вернуть уведомления из заглушённых чатов", callback_data="unmute_all")],
            [InlineKeyboardButton("✨ Что умеет SavedBot и чем он полезен", callback_data="set_help")],
            [InlineKeyboardButton("🧭 Вернуться в центр управления", callback_data="main_menu")],
        ]
    )


def _build_set_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Вернуться к настройкам бота", callback_data="set_root")],
            [InlineKeyboardButton("🧭 Открыть центр управления", callback_data="main_menu")],
        ]
    )


def _listen_label(settings: Dict[str, int], key: str, title: str) -> str:
    enabled = bool(int(settings.get(key, CHAT_LISTEN_DEFAULTS.get(key, 0))))
    return f"{'✅' if enabled else '⬜'} {title}"


def _build_listen_settings_keyboard(settings: Dict[str, int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(_listen_label(settings, "allow_private", "Сохранять личные чаты один-на-один"), callback_data="set_listen_toggle:allow_private")],
            [InlineKeyboardButton(_listen_label(settings, "allow_groups", "Сохранять сообщения из групп"), callback_data="set_listen_toggle:allow_groups")],
            [InlineKeyboardButton(_listen_label(settings, "allow_supergroups", "Сохранять сообщения из супергрупп"), callback_data="set_listen_toggle:allow_supergroups")],
            [InlineKeyboardButton(_listen_label(settings, "allow_channels", "Сохранять публикации из каналов"), callback_data="set_listen_toggle:allow_channels")],
            [InlineKeyboardButton(_listen_label(settings, "allow_bots", "Сохранять сообщения от ботов"), callback_data="set_listen_toggle:allow_bots")],
            [InlineKeyboardButton("⬅️ Вернуться к настройкам бота", callback_data="set_root")],
        ]
    )


def _build_main_menu_keyboard(*, session_valid: bool) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if not session_valid:
        rows.extend(
            [
                [InlineKeyboardButton("📱 Подключить аккаунт по номеру телефона", callback_data="auth_phone")],
                [InlineKeyboardButton("🗝 Подключить аккаунт по QR-коду", callback_data="auth_qr")],
            ]
        )

    rows.extend(
        [
            [InlineKeyboardButton("⚙️ Открыть настройки бота", callback_data="set_root")],
            [
                InlineKeyboardButton("📊 Статистика архива", callback_data="stats_panel"),
                InlineKeyboardButton("👤 Профиль", callback_data="set_profile"),
            ],
            [InlineKeyboardButton("🎚 Настроить прослушивание чатов", callback_data="set_listen_menu")],
            [InlineKeyboardButton("📖 Показать полный список команд", callback_data="main_help")],
            [InlineKeyboardButton("🔔 Вернуть заглушённые чаты", callback_data="unmute_all")],
        ]
    )
    if session_valid:
        rows.append([InlineKeyboardButton("🚪 Отключить текущую сессию", callback_data="logout_confirm")])
    return InlineKeyboardMarkup(rows)


def _build_connected_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Посмотреть статистику архива", callback_data="stats_panel"),
                InlineKeyboardButton("⚙️ Открыть настройки бота", callback_data="set_root"),
            ],
            [InlineKeyboardButton("🧭 Открыть центр управления", callback_data="main_menu")],
        ]
    )


def _build_logout_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Да, отключить текущую сессию", callback_data="logout_execute")],
            [InlineKeyboardButton("⬅️ Оставить всё как есть", callback_data="main_menu")],
        ]
    )


def _build_command_center_text(first_name: str, *, session_valid: bool) -> str:
    status = "подключён, архив работает в фоне" if session_valid else "ожидает подключения аккаунта"
    return (
        f"🧭 <b>Центр управления SavedBot для {first_name}</b>\n\n"
        f"Статус: <b>{status}</b>.\n"
        "Все основные действия доступны кнопками ниже.\n\n"
        "<b>Полный набор команд:</b>\n"
        "• /start — приветствие и центр управления\n"
        "• /set — настройки бота и типов чатов\n"
        "• /stats — статистика сохранённого архива\n"
        "• /profile — профиль, ID и статус доступа\n"
        "• /help — справка по возможностям\n"
        "• /unmute — вернуть уведомления из заглушённых чатов\n"
        "• /logout — отключить текущую сессию\n\n"
        "Настройки, статистика и профиль открываются без ввода команд — через inline-кнопки."
    )


def _build_settings_menu_text(first_name: str) -> str:
    return (
        f"⚙️ <b>Настройки SavedBot для {first_name}</b>\n\n"
        "Здесь можно управлять профилем, статистикой, типами чатов и уведомлениями.\n"
        "Выберите нужный раздел — изменения применяются сразу."
    )


def _build_help_text() -> str:
    return (
        "📖 <b>Справка SavedBot</b>\n\n"
        "<b>Что сохраняется:</b>\n"
        "• удалённые сообщения и медиа\n"
        "• правки текста с версией «до» и «после»\n"
        "• одноразовые вложения, документы, голосовые и кружочки\n\n"
        "<b>Команды:</b>\n"
        "• /start — приветствие и центр управления\n"
        "• /set — настройки бота\n"
        "• /stats — статистика архива\n"
        "• /profile — профиль пользователя\n"
        "• /unmute — снять заглушки со всех чатов\n"
        "• /logout — отключить сессию\n\n"
        "Основной сценарий лучше вести кнопками: так меньше ручного ввода и меньше риска ошибиться."
    )


async def _get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    cached = str(context.bot_data.get("bot_username", "") or "").strip()
    if cached:
        return cached
    env_fallback = str(getattr(CONFIG, "bot_username", "") or "").strip().lstrip("@")
    if env_fallback:
        context.bot_data["bot_username"] = env_fallback
        return env_fallback
    try:
        me = await context.bot.get_me()
        value = str(getattr(me, "username", "") or "").strip()
        if value:
            context.bot_data["bot_username"] = value
            return value
    except Exception:
        logger.debug("Failed to resolve bot username", exc_info=True)
    return env_fallback


def _start_advantages_text() -> str:
    return (
        "✨ <b>Почему SavedBot удобен</b>\n\n"
        "• показывает удалённое сообщение отдельной аккуратной карточкой\n"
        "• отделяет текст, медиа и служебную информацию, чтобы не было каши\n"
        "• сохраняет историю правок и подсвечивает, что именно изменилось\n"
        "• позволяет заглушить шумный чат одной кнопкой\n"
        "• даёт статистику по архиву и последним событиям\n\n"
        "Главная идея простая: всё важное остаётся под рукой, а управление не требует лишних команд."
    )


async def _build_set_profile_text(app: "App", user: Any) -> str:
    uid = int(getattr(user, "id", 0) or 0)
    username = str(getattr(user, "username", "") or "").strip()
    profile_row = await app.db.fetchone(
        "SELECT username, first_seen_at FROM bot_users WHERE user_id=? LIMIT 1",
        (uid,),
    )
    stored_username = ""
    first_seen_iso = ""
    if profile_row:
        stored_username = str((profile_row["username"] if hasattr(profile_row, "keys") else profile_row[0]) or "").strip()
        first_seen_iso = str((profile_row["first_seen_at"] if hasattr(profile_row, "keys") else profile_row[1]) or "").strip()

    now_utc = datetime.now(timezone.utc)
    joined_text = "—"
    if first_seen_iso:
        try:
            joined_dt = datetime.fromisoformat(first_seen_iso.replace("Z", "+00:00"))
            if joined_dt.tzinfo is None:
                joined_dt = joined_dt.replace(tzinfo=timezone.utc)
            joined_text = joined_dt.astimezone(app.config.tz).strftime("%d.%m.%Y")
        except Exception:
            joined_text = first_seen_iso[:10] if len(first_seen_iso) >= 10 else first_seen_iso

    display_username = stored_username or username or "—"
    display_username_view = f"@{display_username}" if display_username != "—" else "—"

    return (
        f"👤 <b>Профиль пользователя</b>\n\n"
        f"• Имя: <b>{html_escape(getattr(user, 'full_name', '') or getattr(user, 'first_name', '') or '—')}</b>\n"
        f"• Username: <b>{html_escape(display_username_view)}</b>\n"
        f"• User ID: <code>{uid}</code>\n"
        f"• С нами с: <b>{html_escape(joined_text)}</b>\n"
        "• Доступ: <b>открыт</b>"
    )


async def _is_session_active(app: "App", uid: int) -> bool:
    try:
        if not app.storage.is_valid(uid):
            return False
    except Exception:
        return False

    try:
        app.watcher_service.ensure(uid)
    except Exception:
        logger.debug("watcher_service.ensure failed uid=%s", uid)

    try:
        return bool(await app.storage.is_session_valid(uid))
    except Exception:
        logger.debug("session validation failed uid=%s", uid)
        return False


async def _disconnect_user_session(app: "App", uid: int) -> None:
    try:
        await app.watcher_service.stop(uid)
    except Exception:
        logger.debug("Failed to stop watcher during logout for uid=%s", uid)

    try:
        app.storage.delete(uid)
    except Exception:
        logger.debug("Failed to delete storage during logout for uid=%s", uid)

    try:
        await set_state(app.db, uid, AuthState.IDLE)
    except Exception:
        logger.debug("Failed to set auth state to IDLE for uid=%s", uid)


async def _build_stats_text(app: "App", uid: int) -> str:
    stats = await app.db.get_stats(uid)

    last_txt = "—"
    if stats.get("last"):
        sender, date, ctype = stats["last"]
        ts = format_human_timestamp(date, app.config.tz_name)
        last_txt = f"{html_escape(ctype)} от {html_escape(sender or 'Unknown')} ({html_escape(ts)})"

    top_text = "\n".join(
        f" {idx}. {html_escape(title)} — <b>{cnt}</b>"
        for idx, (title, cnt) in enumerate(stats.get("top_chats", []), 1)
    ) or "Пока пусто"

    return (
        "📊 <b>Статистика архива SavedBot</b>\n\n"
        f"Всего сохранено событий: <b>{stats.get('total', 0)}</b>\n"
        f"Сохранено сегодня: <b>{stats.get('today', 0)}</b>\n\n"
        f"<b>Топ чатов по сохранениям:</b>\n{top_text}\n\n"
        f"<b>Последнее событие:</b> {last_txt}"
    )


async def _clear_muted_chats(app: "App", uid: int) -> int:
    row = await app.db.fetchone("SELECT COUNT(*) FROM muted_chats WHERE owner_id=?", (uid,))
    count = int(row[0]) if row and row[0] is not None else 0
    await app.db.execute("DELETE FROM muted_chats WHERE owner_id=?", (uid,))
    return count


def _build_unmute_text(count: int) -> str:
    if count == 0:
        return (
            "🔔 <b>Все уведомления уже активны</b>\n\n"
            "Заглушённых чатов не найдено. Новые удалённые и отредактированные сообщения будут приходить как обычно."
        )
    return (
        f"🔔 <b>Уведомления возвращены</b>\n\n"
        f"Сняты заглушки с чатов: <b>{count}</b>.\n"
        "Новые удалённые и отредактированные сообщения из них снова будут приходить сюда."
    )


def _next_local_run(hour: int, minute: int, tz_name: str) -> datetime:
    try:
        target_tz = ZoneInfo(tz_name)
    except Exception:
        target_tz = timezone.utc
    now_local = datetime.now(target_tz)
    run_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run_local <= now_local:
        run_local = run_local + timedelta(days=1)
    return run_local


async def _build_daily_report_text(app: "App") -> str:
    try:
        tz = ZoneInfo(app.config.tz_name)
    except Exception:
        tz = timezone.utc
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).isoformat()
    end_utc = end_local.astimezone(timezone.utc).isoformat()

    total_messages_row = await app.db.fetchone(
        "SELECT COUNT(*) FROM chat_thread_messages WHERE created_at >= ? AND created_at < ?",
        (start_utc, end_utc),
    )
    total_deleted_row = await app.db.fetchone(
        "SELECT COUNT(*) FROM deleted_messages WHERE saved_at >= ? AND saved_at < ?",
        (start_utc, end_utc),
    )
    total_errors_row = await app.db.fetchone(
        "SELECT COUNT(*) FROM critical_alert_events WHERE last_seen_at >= ? AND last_seen_at < ?",
        (start_utc, end_utc),
    )
    top_deleters = await app.db.fetchall(
        """
        SELECT owner_id, COUNT(*) AS cnt
        FROM deleted_messages
        WHERE saved_at >= ? AND saved_at < ?
        GROUP BY owner_id
        ORDER BY cnt DESC
        LIMIT 5
        """,
        (start_utc, end_utc),
    )
    top_active = await app.db.fetchall(
        """
        SELECT owner_id, COUNT(*) AS cnt
        FROM chat_thread_messages
        WHERE created_at >= ? AND created_at < ?
        GROUP BY owner_id
        ORDER BY cnt DESC
        LIMIT 5
        """,
        (start_utc, end_utc),
    )
    top_errors = await app.db.fetchall(
        """
        SELECT error_type, COUNT(*) AS cnt
        FROM critical_alert_events
        WHERE last_seen_at >= ? AND last_seen_at < ?
        GROUP BY error_type
        ORDER BY cnt DESC
        LIMIT 5
        """,
        (start_utc, end_utc),
    )

    user_ids = set()
    for row in top_deleters or []:
        user_ids.add(int(row["owner_id"] if hasattr(row, "keys") else row[0]))
    for row in top_active or []:
        user_ids.add(int(row["owner_id"] if hasattr(row, "keys") else row[0]))
    username_map: Dict[int, str] = {}
    if user_ids:
        placeholders = ", ".join(["?"] * len(user_ids))
        rows = await app.db.fetchall(
            f"SELECT user_id, username FROM bot_users WHERE user_id IN ({placeholders})",
            tuple(user_ids),
        )
        for row in rows or []:
            uid = int(row["user_id"] if hasattr(row, "keys") else row[0])
            uname = str((row["username"] if hasattr(row, "keys") else row[1]) or "").strip()
            username_map[uid] = uname

    def _format_user_line(uid: int, cnt: int) -> str:
        uname = username_map.get(uid, "")
        if uname:
            return f"• <code>{uid}</code> (@{html_escape(uname)}) — <b>{cnt}</b>"
        return f"• <code>{uid}</code> — <b>{cnt}</b>"

    deleters_lines = "\n".join(
        _format_user_line(
            int(row["owner_id"] if hasattr(row, "keys") else row[0]),
            int(row["cnt"] if hasattr(row, "keys") else row[1]),
        )
        for row in (top_deleters or [])
    ) or "• Нет данных"

    active_lines = "\n".join(
        _format_user_line(
            int(row["owner_id"] if hasattr(row, "keys") else row[0]),
            int(row["cnt"] if hasattr(row, "keys") else row[1]),
        )
        for row in (top_active or [])
    ) or "• Нет данных"

    error_lines = "\n".join(
        f"• {html_escape(str(row['error_type'] if hasattr(row, 'keys') else row[0]) or 'UNKNOWN')} — <b>{int(row['cnt'] if hasattr(row, 'keys') else row[1])}</b>"
        for row in (top_errors or [])
    ) or "• Нет данных"

    total_messages = int(total_messages_row[0]) if total_messages_row else 0
    total_deleted = int(total_deleted_row[0]) if total_deleted_row else 0
    total_errors = int(total_errors_row[0]) if total_errors_row else 0
    analysis_bits: List[str] = []
    if total_errors > 0:
        analysis_bits.append("есть критичные ошибки, приоритет — стабилизация авторизации и сетевых запросов")
    if total_deleted > total_messages and total_messages > 0:
        analysis_bits.append("удалений больше, чем новых сообщений — проверьте активность в наиболее проблемных чатах")
    if total_messages == 0 and total_deleted == 0:
        analysis_bits.append("активность низкая, стоит проверить состояние watcher и охват подключенных пользователей")
    if not analysis_bits:
        analysis_bits.append("система работает стабильно, явных аномалий по сводным метрикам нет")

    return (
        f"📈 <b>Ежедневная AI-аналитика ({start_local.strftime('%d.%m.%Y')})</b>\n\n"
        f"<b>Сводка:</b>\n"
        f"• Сообщений за день: <b>{total_messages}</b>\n"
        f"• Удалений за день: <b>{total_deleted}</b>\n"
        f"• Критичных ошибок: <b>{total_errors}</b>\n\n"
        f"<b>Кто больше всех удалял:</b>\n{deleters_lines}\n\n"
        f"<b>Самые активные пользователи:</b>\n{active_lines}\n\n"
        f"<b>Топ ошибок:</b>\n{error_lines}\n\n"
        f"<b>AI-вывод:</b> {html_escape('; '.join(analysis_bits))}."
    )


async def _daily_report_loop(application: Application) -> None:
    while True:
        run_local = _next_local_run(hour=21, minute=0, tz_name=CONFIG.tz_name)
        sleep_for = max(1.0, (run_local - datetime.now(run_local.tzinfo)).total_seconds())
        await asyncio.sleep(sleep_for)

        app: Optional[App] = application.bot_data.get("app")
        if app is None:
            continue
        try:
            text = await _build_daily_report_text(app)
            targets: List[int] = []
            if CONFIG.alert_chat_id is not None:
                targets.append(int(CONFIG.alert_chat_id))
            targets.extend(int(x) for x in CONFIG.admin_ids)
            seen: set[int] = set()
            for target in targets:
                if target in seen:
                    continue
                seen.add(target)
                await application.bot.send_message(
                    chat_id=target,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        except Exception as exc:
            logger.exception("Daily AI report failed")
            try:
                await send_critical_alert(
                    application.bot,
                    app.db if app else None,
                    error_type="DAILY_AI_REPORT_FAILED",
                    error_text=str(exc),
                    user_id=None,
                    context="daily_report_loop",
                )
            except Exception:
                logger.debug("Failed to send critical alert for daily report failure", exc_info=True)


async def register_and_notify_new_user(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database) -> bool:
    """
    Save frontend user in DB and notify admins on first interaction.
    Returns True if user was inserted (new), False otherwise.
    """
    user = update.effective_user
    if not user:
        return False

    uid = user.id
    uname = user.username
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        row = await db.fetchone("SELECT user_id FROM bot_users WHERE user_id=?", (uid,))
        is_new = row is None

        if is_new:
            await db.execute(
                "INSERT INTO bot_users (user_id, username, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (uid, uname, now_iso, now_iso),
            )
        else:
            await db.execute(
                "UPDATE bot_users SET username=?, last_seen_at=? WHERE user_id=?",
                (uname, now_iso, uid),
            )

        if is_new:
            display_name = uname or f"ID {uid}"
            msg_text = (
                f"👤 <b>Новый пользователь бота</b>\n"
                f"ID: <code>{uid}</code>\n"
                f"Username: {html.escape(display_name, quote=False)}"
            )

            for admin_id in CONFIG.admin_ids:
                try:
                    await send_and_log(
                        context.bot,
                        admin_id,
                        msg_text,
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    logger.exception(
                        "Failed to notify admin %s about new user %s",
                        admin_id,
                        uid
                    )

        return is_new

    except Exception:
        logger.exception("register_and_notify_new_user error for %s", uid)
        return False
# --- Commands & helpers ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    user = update.effective_user
    uid  = user.id
    uname = user.username

    app = context.bot_data.get("app")
    if not app:
        if update.message:
            await update.message.reply_text("❌ Ошибка инициализации. Попробуйте позже.")
        return

    log_frontend_incoming(uid, uname, text="/start", meta="cmd=/start")
    is_new = await register_and_notify_new_user(update, context, app.db)

    # ------------------------------------------------
    # SESSION CHECK
    # ------------------------------------------------

    session_valid = False

    if app.storage.is_valid(uid):
        session_valid = await _is_session_active(app, uid)

    if not session_valid and app.storage.is_valid(uid):
        # Сессия могла устареть между запусками: чистим её и предлагаем подключиться заново.
        try:
            app.storage.delete(uid)
        except Exception:
            logger.debug("storage delete failed uid=%s", uid)

        try:
            await app.watcher_service.stop(uid)
        except Exception:
            logger.debug("watcher stop failed uid=%s", uid)

    if session_valid:
        text = (
            "✅ <b>SavedBot подключён и работает</b>\n\n"
            "Архив уже слушает выбранные чаты в фоне.\n"
            "Статистику, профиль и настройки можно открыть кнопками ниже.\n\n"
            f"{_build_marker_line()}"
        )

        await send_and_log(
            context.bot,
            uid,
            text,
            username=uname,
            reply_markup=_build_connected_keyboard(),
            parse_mode=ParseMode.HTML
        )
    else:
        m = await send_and_log(
            context.bot,
            uid,
            f"{welcome_message}\n\n{_build_marker_line()}",
            username=uname,
            reply_markup=build_start_keyboard(),
            parse_mode=ParseMode.HTML
        )

        try:
            if hasattr(app, "auth") and callable(getattr(app.auth, "track_auth_message", None)):
                app.auth.track_auth_message(uid, m.message_id)
        except Exception:
            log_context = "welcome" if is_new else "auth"
            logger.debug("Failed to track %s message uid=%s", log_context, uid)

async def cleansessions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in CONFIG.admin_ids:
        if update.message:
            await update.message.reply_text("Эта команда доступна только администраторам.")
        return

    keyboard = [
        [
            InlineKeyboardButton("❌ УДАЛИТЬ ВСЕ .session файлы", callback_data="confirm_cleansessions"),
            InlineKeyboardButton("Отмена", callback_data="cancel_cleansessions")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⚠️ <b>Внимание — удаляются только файлы .session и .session-journal</b>\n\n"
        "Будут удалены:\n"
        "• все *.session\n"
        "• все *.session-journal\n"
        "в папках:\n"
        "  - sessions/\n"
        "  - logs/auth_attempts/user_*/\n\n"
        "Архивы .session.zip и другие файлы останутся нетронутыми.\n\n"
        "После этого все активные сессии будут остановлены, и пользователям придётся заново авторизоваться.\n\n"
        "Подтвердите действие, если вы понимаете последствия.",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )

async def sessions_health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in CONFIG.admin_ids:
        if update.message:
            await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    app: App = context.bot_data.get("app")
    if not app:
        if update.message:
            await update.message.reply_text("❌ Приложение не готово.")
        return

    await app.auth.session_housekeeping_once(trigger="admin_command")
    snap = await app.auth.get_session_health_snapshot()
    text = (
        "🧹 <b>Session health report</b>\n\n"
        f"• tmp files: <b>{int(snap.get('tmp_files', 0))}</b>\n"
        f"• session zip (main): <b>{int(snap.get('session_zip_main', 0))}</b>\n"
        f"• session zip (logs): <b>{int(snap.get('session_zip_logs', 0))}</b>\n"
        f"• auth states pending: <b>{int(snap.get('auth_states_pending', 0))}</b>\n"
        f"• active tmp clients: <b>{int(snap.get('active_tmp_clients', 0))}</b>"
    )
    await send_and_log(context.bot, user.id, text, parse_mode=ParseMode.HTML)


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app: App = context.bot_data.get("app")
    if not app or not update.effective_user:
        return
    uid = update.effective_user.id
    uname = update.effective_user.username if update.effective_user else None

    log_frontend_incoming(uid, uname, text="/logout", meta="cmd=/logout")

    await _disconnect_user_session(app, uid)
    text = (
        "❎ <b>Текущая сессия отключена</b>\n\n"
        "SavedBot больше не слушает ваш аккаунт. Архивные записи остаются в базе, а для продолжения можно подключиться заново."
    )

    m = await send_and_log(context.bot, uid, text, username=uname, reply_markup=build_start_keyboard(), parse_mode=ParseMode.HTML)
    try:
        if hasattr(app, "auth") and callable(getattr(app.auth, "track_auth_message", None)):
            app.auth.track_auth_message(uid, m.message_id)
    except Exception:
        logger.debug("Failed to track logout auth message for uid=%s", uid)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    app: App = context.bot_data.get("app")
    if not app:
        return
    uid = update.effective_user.id
    uname = update.effective_user.username if update.effective_user else None

    log_frontend_incoming(uid, uname, text="/stats", meta="cmd=/stats")

    text = await _build_stats_text(app, uid)
    session_valid = await _is_session_active(app, uid)

    await send_and_log(
        context.bot,
        uid,
        text,
        username=uname,
        parse_mode=ParseMode.HTML,
        reply_markup=_build_main_menu_keyboard(session_valid=session_valid),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = int(update.effective_user.id)
    uname = update.effective_user.username if update.effective_user else None
    app: App = context.bot_data.get("app")
    log_frontend_incoming(uid, uname, text="/help", meta="cmd=/help")

    session_valid = await _is_session_active(app, uid) if app else False
    await send_and_log(
        context.bot,
        uid,
        _build_help_text(),
        username=uname,
        parse_mode=ParseMode.HTML,
        reply_markup=_build_main_menu_keyboard(session_valid=session_valid),
    )


async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    app: App = context.bot_data.get("app")
    if not app:
        return
    uid = int(update.effective_user.id)
    uname = update.effective_user.username if update.effective_user else None
    log_frontend_incoming(uid, uname, text="/set", meta="cmd=/set")

    first_name = html_escape(update.effective_user.first_name or "друг")
    await send_and_log(
        context.bot,
        uid,
        _build_settings_menu_text(first_name),
        username=uname,
        parse_mode=ParseMode.HTML,
        reply_markup=_build_set_root_keyboard(),
    )


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    app: App = context.bot_data.get("app")
    if not app:
        return

    user = update.effective_user
    uid = int(user.id)
    uname = user.username
    log_frontend_incoming(uid, uname, text="/profile", meta="cmd=/profile")

    text = await _build_set_profile_text(app, user)
    session_valid = await _is_session_active(app, uid)
    await send_and_log(
        context.bot,
        uid,
        text,
        username=uname,
        parse_mode=ParseMode.HTML,
        reply_markup=_build_main_menu_keyboard(session_valid=session_valid),
    )


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    app: App = context.bot_data.get("app")
    if not app:
        return
    uid = update.effective_user.id
    uname = update.effective_user.username if update.effective_user else None

    log_frontend_incoming(uid, uname, text="/unmute", meta="cmd=/unmute")

    count = await _clear_muted_chats(app, uid)
    session_valid = await _is_session_active(app, uid)
    await send_and_log(
        context.bot,
        uid,
        _build_unmute_text(count),
        username=uname,
        parse_mode=ParseMode.HTML,
        reply_markup=_build_main_menu_keyboard(session_valid=session_valid),
    )


async def cleardb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    if uid not in CONFIG.admin_ids:
        if update.message:
            await update.message.reply_text("❌ Доступно только администраторам.")
        return

    app: App = context.bot_data.get("app")
    if not app:
        await update.message.reply_text("❌ Приложение не готово.")
        return

    try:
        for table in ("pending", "deleted_messages", "stories", "users", "bot_users"):
            await app.db.execute(f"DELETE FROM {table}")
        await app.db.execute("VACUUM")

        await send_and_log(
            context.bot,
            uid,
            "✅ База данных очищена (pending, deleted_messages, stories, users, bot_users).",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await send_and_log(
            context.bot,
            uid,
            f"❌ Ошибка при очистке базы: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )


# --- Message Handler (Auth Flow) ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    uid = user.id
    uname = user.username

    app: App = context.bot_data.get("app")
    if not app:
        logger.debug("text_handler: app missing in bot_data")
        return

    text = (update.message.text or "").strip()

    # Ensure user registered and track UI message for cleanup
    is_new = await register_and_notify_new_user(update, context, app.db)
    try:
        app.auth.track_auth_message(uid, update.message.message_id)
    except Exception:
        logger.debug("Failed to track auth message for uid=%s", uid)

    # ... дальше весь остальной код без изменений

    info = await get_state(app.db, uid)
    state = info.get("state", AuthState.IDLE)

    attempt = int(info.get("auth_fail_count") or 0) + 1

    if state in (AuthState.WAIT_PHONE, AuthState.CODE_SENT, AuthState.WAIT_2FA):
        log_auth_attempt(user_id=uid, username=uname, text=text, state=state, meta=f"attempt={attempt}")
        logger.info("[AUTH] user=%s state=%s attempt=%d text=%s", uid, state, attempt, (text[:80] + "...") if len(text) > 80 else text)

    banned_until = info.get("banned_until")
    if banned_until:
        try:
            banned_ts = float(banned_until)
        except Exception:
            banned_ts = 0.0
        now_ts = time.time()
        if banned_ts and now_ts < banned_ts:
            wait_sec = max(int(banned_ts - now_ts), 1)
            msg_text = (
                "⛔ <b>Временная блокировка авторизации</b>\n\n"
                f"Слишком много неудачных попыток. Подождите ещё <b>{wait_sec} сек.</b> и повторите через /start."
            )
            m = await send_and_log(context.bot, uid, msg_text, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.warning("[AUTH] user=%s is banned until %s", uid, banned_ts)
            return

    # ---------------------------
    # WAIT_PHONE branch: пользователь ввёл номер телефона
    # ---------------------------
    if state == AuthState.WAIT_PHONE:
        # 1. Проверка кулдауна на повторную отправку
        resend_ts = float(info.get("resend_allowed_at") or 0)
        now_ts = time.time()
        if now_ts < resend_ts:
            wait_sec = max(int(resend_ts - now_ts), 1)
            msg_text = (
                "⏳ <b>Повторная отправка кода временно недоступна.</b>\n\n"
                f"Попробуйте снова через <b>{wait_sec} сек.</b>"
            )
            m = await send_and_log(context.bot, uid, msg_text, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.info("[AUTH] Resend cooldown active for %s, wait=%ds", uid, wait_sec)
            return

        # 2. Проверка формата номера
        phone = text.strip()
        if not re.match(r"^\+\d{9,15}$", phone):
            msg_text = (
                "❌ <b>Неверный формат номера.</b>\n\n"
                "Введите номер в международном формате, например:\n"
                "<code>+71234567890</code> или <code>+380671234567</code>"
            )
            m = await send_and_log(context.bot, uid, msg_text, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.debug("[AUTH] Invalid phone format from %s: %r", uid, phone)
            return

        # 3. Показываем пользователю, что идёт запрос
        wait_msg = (
            "⏳ Запрашиваем код у Telegram...\n\n"
            "Это займёт несколько секунд. Пожалуйста, подождите."
        )
        wait_m = await send_and_log(context.bot, uid, wait_msg, username=uname, parse_mode=ParseMode.HTML)
        try:
            app.auth.track_auth_message(uid, wait_m.message_id)
        except Exception:
            pass

        logger.info("[AUTH] Requesting code for user=%s phone=%s", uid, phone)
        prefix = None
        try:
            code_request, prefix = await _request_telegram_code(
                app,
                uid,
                phone,
                reuse_existing_client=False,
            )

            log_auth_attempt(
                uid, uname, phone, state,
                meta=f"phone_code_hash={code_request.phone_code_hash}",
                result="OK"
            )

            await set_state(
                app.db, uid, AuthState.CODE_SENT,
                phone=phone,
                tmp_prefix=prefix,
                phone_code_hash=code_request.phone_code_hash,
                expires_at=_auth_expires_at(AUTH_CODE_TTL_SEC),
                resend_allowed_at=time.time() + CONFIG.resend_cooldown,
                auth_fail_count=0,
            )
            app.auth.cache_auth_context(
                uid,
                phone=phone,
                phone_code_hash=code_request.phone_code_hash,
                tmp_prefix=prefix,
            )

            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                _build_code_sent_text(),
                message_id=wait_m.message_id,
                reply_markup=get_resend_code_keyboard(uid),
            )

            logger.info("[AUTH] Code request completed successfully for %s", uid)

        except PhoneNumberInvalidError:
            await app.auth.cleanup_tmp(uid)
            log_auth_attempt(uid, uname, phone, state, result="InvalidPhone")
            await set_state(
                app.db,
                uid,
                AuthState.WAIT_PHONE,
                phone=None,
                tmp_prefix=None,
                resend_allowed_at=None,
                auth_fail_count=0,
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                "\u274c <b>\u041d\u043e\u043c\u0435\u0440 \u0442\u0435\u043b\u0435\u0444\u043e\u043d\u0430 \u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u0435\u043d.</b>\n\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0444\u043e\u0440\u043c\u0430\u0442 \u0438 \u043f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0441\u043d\u043e\u0432\u0430.",
                message_id=wait_m.message_id,
            )

        except FloodWaitError as e:
            wait_sec = e.seconds + 10 if hasattr(e, 'seconds') else 90
            await app.auth.cleanup_tmp(uid)
            log_auth_attempt(uid, uname, phone, state, result=f"FloodWait_{wait_sec}")
            await set_state(
                app.db,
                uid,
                AuthState.WAIT_PHONE,
                phone=phone,
                tmp_prefix=None,
                resend_allowed_at=time.time() + wait_sec,
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                f"\u23f3 <b>Telegram \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0438\u043b \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0443 \u043a\u043e\u0434\u043e\u0432.</b>\n\u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0435 \u0447\u0435\u0440\u0435\u0437 <b>{wait_sec} \u0441\u0435\u043a.</b>",
                message_id=wait_m.message_id,
            )

        except SendCodeUnavailableError:
            await app.auth.cleanup_tmp(uid)
            block_sec = CONFIG.sendcode_unavailable_block or 300
            log_auth_attempt(uid, uname, phone, state, result="SendCodeUnavailable")
            await send_critical_alert(
                context.bot,
                app.db,
                error_type="AUTH_CODE_SEND_UNAVAILABLE",
                error_text="SendCodeUnavailableError while requesting Telegram code",
                user_id=uid,
                username=uname,
                context="text_handler.wait_phone.send_code",
                extra={"phone": phone, "block_sec": block_sec},
            )
            await set_state(
                app.db,
                uid,
                AuthState.WAIT_PHONE,
                phone=phone,
                tmp_prefix=None,
                resend_allowed_at=time.time() + block_sec,
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                f"\u26a0\ufe0f <b>\u041e\u0442\u043f\u0440\u0430\u0432\u043a\u0430 \u043a\u043e\u0434\u043e\u0432 \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430.</b>\n\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0447\u0435\u0440\u0435\u0437 <b>{block_sec} \u0441\u0435\u043a.</b>",
                message_id=wait_m.message_id,
            )

        except Exception as e:
            await app.auth.cleanup_tmp(uid)
            log_auth_attempt(uid, uname, phone, state, result="Error")
            logger.exception("[AUTH] Critical error while requesting code for %s: %s", uid, e)
            await send_critical_alert(
                context.bot,
                app.db,
                error_type="AUTH_CODE_REQUEST_FAILED",
                error_text=str(e),
                user_id=uid,
                username=uname,
                context="text_handler.wait_phone.critical",
                extra={"phone": phone},
            )
            await set_state(
                app.db,
                uid,
                AuthState.WAIT_PHONE,
                phone=None,
                tmp_prefix=None,
                resend_allowed_at=None,
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                "\u274c <b>\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u043f\u0440\u043e\u0441\u0438\u0442\u044c \u043a\u043e\u0434.</b>\n\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u043f\u043e\u0437\u0436\u0435 \u0438\u043b\u0438 \u0432\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u043e\u043c\u0435\u0440 \u0435\u0449\u0451 \u0440\u0430\u0437.",
                message_id=wait_m.message_id,
            )

        return
     # ---------------------------
    # CODE_SENT branch: пользователь ввёл код
    # ---------------------------
    elif state == AuthState.CODE_SENT:
        code_raw = text.strip()
        code = code_raw.replace(" ", "")
        
        if not re.fullmatch(r"\d(?: \d){3,5}", code_raw) or not re.fullmatch(r"\d{4,6}", code):
            msg = (
                "❌ <b>Неверный формат кода.</b>\n\n"
                "<b>ВНИМАНИЕ!!!!</b>\n"
                "Код нужно вводить с пробелами между цифрами:\n"
                "<code>1 2 3 4 5</code>"
            )
            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.debug("[AUTH] Invalid code format from %s: %r", uid, code_raw)
            return

        phone = str(info.get("phone") or "").strip()
        tmp_prefix = str(info.get("tmp_prefix") or "").strip()
        phone_code_hash = str(info.get("phone_code_hash") or "").strip()
        expires_at = float(info.get("expires_at") or 0)
        client = app.auth.tmp_clients.get(uid)
        context_shifted = False
        if not phone or not phone_code_hash:
            cached = app.auth.get_cached_auth_context(uid)
            if not phone:
                phone = str(cached.get("phone") or "").strip()
            if not phone_code_hash:
                phone_code_hash = str(cached.get("phone_code_hash") or "").strip()
            if not tmp_prefix:
                tmp_prefix = str(cached.get("tmp_prefix") or "").strip()
        logger.debug(
            "[AUTH] Loaded CODE_SENT state from DB for uid=%s phone=%s hash=%s tmp_prefix=%s",
            uid,
            bool(phone),
            bool(phone_code_hash),
            tmp_prefix or "<none>",
        )
        if expires_at and time.time() > expires_at:
            if phone:
                try:
                    code_request, restored_prefix = await _request_telegram_code(
                        app,
                        uid,
                        phone,
                        reuse_existing_client=True,
                    )
                    await set_state(
                        app.db,
                        uid,
                        AuthState.CODE_SENT,
                        phone=phone,
                        tmp_prefix=restored_prefix,
                        phone_code_hash=code_request.phone_code_hash,
                        expires_at=_auth_expires_at(AUTH_CODE_TTL_SEC),
                        resend_allowed_at=time.time() + CONFIG.resend_cooldown,
                        auth_fail_count=0,
                    )
                    app.auth.cache_auth_context(
                        uid,
                        phone=phone,
                        phone_code_hash=code_request.phone_code_hash,
                        tmp_prefix=restored_prefix,
                    )
                    await send_and_log(
                        context.bot,
                        uid,
                        "⌛ <b>Срок предыдущего кода истёк.</b>\nЯ отправил новый код автоматически. Введите его.",
                        username=uname,
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_resend_code_keyboard(uid),
                    )
                    logger.info("[AUTH] CODE_SENT auto-resend after TTL for uid=%s", uid)
                    return
                except Exception:
                    logger.warning("[AUTH] Failed auto-resend after TTL for uid=%s", uid, exc_info=True)
            await app.auth.cleanup_tmp(uid)
            await set_state(
                app.db,
                uid,
                AuthState.WAIT_PHONE,
                phone=None,
                tmp_prefix=None,
                phone_code_hash=None,
                expires_at=None,
                auth_fail_count=0,
            )
            await send_and_log(
                context.bot,
                uid,
                "⌛ <b>Срок действия кода истёк.</b>\nВведите номер ещё раз, чтобы получить новый код.",
                username=uname,
                parse_mode=ParseMode.HTML,
            )
            logger.info("[AUTH] CODE_SENT expired for uid=%s", uid)
            return

        if not client:
            if tmp_prefix and uid not in app.auth.tmp_prefixes:
                app.auth.tmp_prefixes[uid] = tmp_prefix
            try:
                client, restored_prefix = await app.auth.get_or_create_tmp_client(uid, reuse_existing=True)
                if restored_prefix and restored_prefix != tmp_prefix:
                    context_shifted = True
                    await set_state(app.db, uid, AuthState.CODE_SENT, tmp_prefix=restored_prefix)
                    tmp_prefix = restored_prefix
            except Exception:
                logger.warning("[AUTH] Failed to restore/create tmp client for %s", uid, exc_info=True)
                client = None

        if tmp_prefix and not app.auth.tmp_prefix_has_files(tmp_prefix):
            logger.warning("[AUTH] Missing tmp session artifacts for uid=%s prefix=%s", uid, tmp_prefix)
            context_shifted = True
            client = None

        should_auto_recover_code = bool(phone) and (not phone_code_hash or not client or context_shifted)
        if should_auto_recover_code:
            try:
                code_request, restored_prefix = await _request_telegram_code(
                    app,
                    uid,
                    phone,
                    reuse_existing_client=False,
                )
                await set_state(
                    app.db,
                    uid,
                    AuthState.CODE_SENT,
                    phone=phone,
                    tmp_prefix=restored_prefix,
                    phone_code_hash=code_request.phone_code_hash,
                    expires_at=_auth_expires_at(AUTH_CODE_TTL_SEC),
                    resend_allowed_at=time.time() + CONFIG.resend_cooldown,
                )
                app.auth.cache_auth_context(
                    uid,
                    phone=phone,
                    phone_code_hash=code_request.phone_code_hash,
                    tmp_prefix=restored_prefix,
                )
                logger.info(
                    "[AUTH] Code context recovered for uid=%s tmp_prefix=%s from_db_hash=%s",
                    uid,
                    restored_prefix,
                    bool(phone_code_hash),
                )
                await send_and_log(
                    context.bot,
                    uid,
                    (
                        "⚠️ <b>Сессия ввода кода была обновлена.</b>\n"
                        "Я сразу отправил новый код. Введите его из последнего сообщения Telegram."
                    ),
                    username=uname,
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_resend_code_keyboard(uid),
                )
                log_auth_attempt(uid, uname, phone, state, result="CodeContextRecovered")
                return
            except Exception:
                logger.warning("[AUTH] Failed to auto-recover code entry context for %s", uid, exc_info=True)

        if not phone or not phone_code_hash or not client:
            msg = (
                "\u274c <b>\u0421\u0435\u0441\u0441\u0438\u044f \u0432\u0432\u043e\u0434\u0430 \u043a\u043e\u0434\u0430 \u0438\u0441\u0442\u0435\u043a\u043b\u0430.</b>\n"
                "\u0417\u0430\u043f\u0440\u043e\u0441\u0438\u0442\u0435 \u043d\u043e\u0432\u044b\u0439 \u043a\u043e\u0434 \u0438\u043b\u0438 \u043d\u0430\u0447\u043d\u0438\u0442\u0435 \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0430\u0446\u0438\u044e \u0437\u0430\u043d\u043e\u0432\u043e."
            )
            reply_markup = get_resend_code_keyboard(uid) if phone else None
            await send_and_log(
                context.bot,
                uid,
                msg,
                username=uname,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            logger.info(
                "[AUTH] Code entry unavailable for %s (phone=%s, hash=%s, client=%s, tmp_prefix_exists=%s)",
                uid,
                bool(phone),
                bool(phone_code_hash),
                bool(client),
                bool(tmp_prefix and app.auth.tmp_prefix_has_files(tmp_prefix)),
            )
            await send_critical_alert(
                context.bot,
                app.db,
                error_type="AUTH_CODE_ENTRY_SESSION_EXPIRED",
                error_text="Code entry failed because auth session context is missing",
                user_id=uid,
                username=uname,
                context="text_handler.code_sent.session_missing",
                extra={
                    "has_phone": bool(phone),
                    "has_phone_code_hash": bool(phone_code_hash),
                    "has_tmp_client": bool(client),
                    "auto_recovery_attempted": bool(phone and phone_code_hash),
                },
            )
            if phone:
                await set_state(app.db, uid, AuthState.CODE_SENT, phone=phone)
            else:
                await app.auth.cleanup_tmp(uid)
                await set_state(app.db, uid, AuthState.IDLE)
            return

        logger.info("[AUTH] Пытаемся войти с кодом для %s (phone=%s)", uid, phone)

        try:
            lock = app.auth.get_user_auth_lock(uid)
            async with lock:
                await asyncio.wait_for(
                    client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash),
                    timeout=45
                )

            log_auth_attempt(uid, uname, code_raw, state, meta="success", result="OK")
            logger.info("[AUTH] sign_in УСПЕШНО для %s", uid)

            await app.auth.finalize_session(uid)
            app.auth.clear_cached_auth_context(uid)
            await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=None)

            # Запускаем приветствие
            await start_cmd(update, context)
            return


        except PhoneCodeInvalidError:
            fails = int(info.get("auth_fail_count") or 0) + 1
            log_auth_attempt(uid, uname, code_raw, state, meta=f"fails={fails}", result="InvalidCode")

            if fails >= 5:
                ban_until = time.time() + 300
                await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=ban_until)
                msg = "⛔ <b>Слишком много неверных кодов.</b>\nПовторите попытку через 5 минут."
                reply_markup = None
            else:
                await set_state(app.db, uid, AuthState.CODE_SENT, auth_fail_count=fails)
                msg = "❌ <b>Неверный код.</b>\nПопробуйте снова."
                reply_markup = get_resend_code_keyboard(uid)

            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.warning("[AUTH] Invalid code for %s, fails=%d", uid, fails)
            return

        except PhoneCodeExpiredError:
            fails = int(info.get("auth_fail_count") or 0) + 1
            log_auth_attempt(uid, uname, code_raw, state, meta=f"fails={fails}", result="ExpiredCode")

            if fails >= 5:
                ban_until = time.time() + 300
                await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=ban_until)
                msg = "⛔ <b>Слишком много попыток.</b>\nПовторите через 5 минут."
                reply_markup = None
            else:
                await set_state(app.db, uid, AuthState.CODE_SENT, auth_fail_count=fails)
                msg = "⌛ <b>Код просрочен.</b>\nЗапросите новый код."
                reply_markup = get_resend_code_keyboard(uid)

            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.warning("[AUTH] Code expired for %s", uid)
            return

        except SessionPasswordNeededError:
            log_auth_attempt(uid, uname, "(2FA required)", state, result="Need2FA")
            msg = "🔐 <b>Требуется пароль двухфакторной аутентификации.</b>\nВведите пароль 2FA."
            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            await set_state(
                app.db,
                uid,
                AuthState.WAIT_2FA,
                expires_at=_auth_expires_at(AUTH_2FA_TTL_SEC),
            )
            logger.info("[AUTH] 2FA required for %s", uid)
            return

        except FloodWaitError as e:
            wait_sec = (e.seconds if hasattr(e, 'seconds') else 60) + 10
            log_auth_attempt(uid, uname, code_raw, state, result=f"FloodWait_{wait_sec}")
            await app.auth.cleanup_tmp(uid)
            msg = f"⏳ <b>Telegram просит подождать перед следующей попыткой входа.</b>\nПовторите через <b>{wait_sec} сек.</b>"
            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            await set_state(app.db, uid, AuthState.IDLE, resend_allowed_at=time.time() + wait_sec)
            logger.warning("[AUTH] FloodWait on sign_in for %s: %ds", uid, wait_sec)
            return

        except Exception as e:
            log_auth_attempt(uid, uname, code_raw, state, result="Error")
            fails = int(info.get("auth_fail_count") or 0) + 1
            await send_critical_alert(
                context.bot,
                app.db,
                error_type="AUTH_SIGNIN_FAILED",
                error_text=str(e),
                user_id=uid,
                username=uname,
                context="text_handler.code_sent.sign_in",
                extra={"fails": fails},
            )

            if fails >= 5:
                ban_until = time.time() + 300
                await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=ban_until)
                msg = "⛔ <b>Слишком много ошибок.</b>\nПовторите через 5 минут."
            else:
                await set_state(app.db, uid, AuthState.CODE_SENT, auth_fail_count=fails)
                msg = f"❌ <b>Ошибка входа:</b> {html.escape(str(e))}\nПопробуйте снова."

            await app.auth.cleanup_tmp(uid)
            m = await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            try:
                app.auth.track_auth_message(uid, m.message_id)
            except Exception:
                pass
            logger.exception("[AUTH] sign_in failed for %s: %s", uid, e)
            return

    # ---------------------------
    # WAIT_2FA branch: ввод пароля двухфакторки
    # ---------------------------
    elif state == AuthState.WAIT_2FA:
        password = text.strip()
        client = app.auth.tmp_clients.get(uid)

        if not client:
            msg = "❌ <b>Сессия истекла.</b>\nНачните заново с /start."
            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            logger.info("[AUTH] WAIT_2FA: no client for %s", uid)
            await send_critical_alert(
                context.bot,
                app.db,
                error_type="AUTH_2FA_SESSION_EXPIRED",
                error_text="2FA client missing while waiting for password",
                user_id=uid,
                username=uname,
                context="text_handler.wait_2fa.no_client",
            )
            await set_state(app.db, uid, AuthState.IDLE)
            return

        logger.info("[AUTH] Пытаемся войти с 2FA-паролем для %s", uid)

        try:
            lock = app.auth.get_user_auth_lock(uid)
            async with lock:
                await asyncio.wait_for(
                    client.sign_in(password=password),
                    timeout=45
                )

            log_auth_attempt(uid, uname, "(2FA)", state, result="OK")
            logger.info("[AUTH] 2FA sign_in УСПЕШНО для %s", uid)

            await app.auth.finalize_session(uid)
            app.auth.clear_cached_auth_context(uid)
            await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=None)
            await start_cmd(update, context)
            return

        except Exception as e:
            fails = int(info.get("auth_fail_count") or 0) + 1
            log_auth_attempt(uid, uname, "(2FA)", state, meta=f"fails={fails}", result="Fail")
            await send_critical_alert(
                context.bot,
                app.db,
                error_type="AUTH_2FA_FAILED",
                error_text=str(e),
                user_id=uid,
                username=uname,
                context="text_handler.wait_2fa.sign_in",
                extra={"fails": fails},
            )

            if fails >= 5:
                ban_until = time.time() + 300
                await set_state(app.db, uid, AuthState.IDLE, auth_fail_count=0, banned_until=ban_until)
                msg = "⛔ <b>Слишком много неверных паролей 2FA.</b>\nПопробуйте через 5 минут."
            else:
                await set_state(
                    app.db,
                    uid,
                    AuthState.WAIT_2FA,
                    auth_fail_count=fails,
                    expires_at=_auth_expires_at(AUTH_2FA_TTL_SEC),
                )
                msg = "❌ <b>Неверный пароль 2FA.</b>\nПопробуйте снова."

            await send_and_log(context.bot, uid, msg, username=uname, parse_mode=ParseMode.HTML)
            logger.warning("[AUTH] 2FA failed for %s, fails=%d, error=%s", uid, fails, e)
            return

# Safe helpers for bot UI (unchanged behavior)
async def _safe_delete_message(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.debug("Failed to delete message %s:%s", chat_id, message_id, exc_info=True)


async def _safe_edit_message(bot, chat_id, message_id, text, **kwargs):
    try:
        clean_text = repair_mojibake(text)
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=clean_text, **kwargs)
        return True
    except Exception:
        logger.debug("Failed to edit message %s:%s", chat_id, message_id, exc_info=True)
        return False


async def _send_error_and_cleanup(bot, user_id, text, app: App):
    try:
        await send_and_log(bot, user_id, text)
    except Exception:
        pass
    try:
        await app.auth.cleanup_tmp(user_id)
    except Exception:
        logger.debug("cleanup_tmp failed for %s", user_id, exc_info=True)


async def handle_qr_flow(user_id: int, bot, gen_msg_id: int, app: "App"):
    """
    Full QR flow with clearer user messages and guaranteed cleanup of temporary clients.
    gen_msg_id — message id of "Generating..." message that should be removed/edited.
    """
    create_client_timeout = getattr(app.config, "qr_create_client_timeout", 10)
    qr_login_timeout = getattr(app.config, "qr_login_wait_timeout", app.config.qr_timeout if hasattr(app.config, "qr_timeout") else 60)
    try:
        try:
            client, prefix = await asyncio.wait_for(app.auth.create_tmp_client(user_id), timeout=create_client_timeout)
        except asyncio.TimeoutError:
            logger.warning("Timeout creating tmp client for %s", user_id)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Не удалось создать временный клиент — таймаут.")
            await _send_error_and_cleanup(bot, user_id, "❌ Попробуйте снова позже.", app)
            return
        except Exception as e:
            logger.exception("Failed to create tmp client for %s: %s", user_id, e)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Ошибка при создании временного клиента.")
            await _send_error_and_cleanup(bot, user_id, "❌ Попробуйте снова позже.", app)
            return

        try:
            qr_login = await asyncio.wait_for(client.qr_login(), timeout=create_client_timeout)
        except asyncio.TimeoutError:
            logger.warning("Timeout getting qr_login for %s", user_id)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Не удалось получить данные QR — таймаут.")
            await app.auth.cleanup_tmp(user_id)
            return
        except Exception as e:
            logger.exception("Error getting qr_login for %s: %s", user_id, e)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Ошибка при генерации QR.")
            await app.auth.cleanup_tmp(user_id)
            return

        try:
            qr = qrcode.QRCode(border=1)
            qr.add_data(qr_login.url)
            qr.make(fit=True)
            img_bio = io.BytesIO()
            qr.make_image().save(img_bio, "PNG")
            img_bio.seek(0)
        except Exception as e:
            logger.exception("QR image generation failed for %s: %s", user_id, e)
            await _safe_edit_message(bot, user_id, gen_msg_id, "❌ Ошибка при создании изображения QR.")
            await app.auth.cleanup_tmp(user_id)
            return

        try:
            await _safe_delete_message(bot, user_id, gen_msg_id)
            qr_caption = (
                "📱 Сканируйте этот QR-код в Telegram → Устройства.\n\n"
                "QR используется только для входа в ваш Telegram-аккаунт и привязки его к этому сервису."
            )
            m = await bot.send_photo(chat_id=user_id, photo=img_bio, caption=qr_caption)
            app.auth.track_auth_message(user_id, m.message_id)
        except Exception as e:
            logger.exception("Failed to send QR photo to %s: %s", user_id, e)
            await _send_error_and_cleanup(bot, user_id, "❌ Не удалось отправить QR. Попробуйте снова.", app)
            return

        try:
            await asyncio.wait_for(qr_login.wait(), timeout=qr_login_timeout)
            await app.auth.finalize_session(user_id)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Статистика", callback_data="stats")]])
            active_text = (
                "✅ <b>Сессия активна.</b>\n\n"
                "Ваш аккаунт теперь привязан к сервису — бот работает в фоне и сохраняет удалённые сообщения."
            )
            await send_and_log(bot, user_id, active_text, reply_markup=kb, parse_mode=ParseMode.HTML)
            try:
                await app.auth.cleanup_tmp(user_id)
            except Exception:
                logger.debug("cleanup_tmp after finalize failed for %s", user_id, exc_info=True)
            return
        except asyncio.TimeoutError:
            logger.info("QR auth timeout for %s", user_id)
            try:
                await send_and_log(bot, user_id, "⏳ Время на сканирование истекло. Попробуйте снова.", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            await app.auth.cleanup_tmp(user_id)
            return
        except SessionPasswordNeededError:
            msg_text = "🔐 Для этого аккаунта требуется пароль 2FA. Введите пароль сообщением в ответ на это сообщение."
            m = await send_and_log(bot, user_id, msg_text)
            app.auth.track_auth_message(user_id, m.message_id)
            await set_state(
                app.db,
                user_id,
                AuthState.WAIT_2FA,
                tmp_prefix=prefix,
                expires_at=_auth_expires_at(AUTH_2FA_TTL_SEC),
            )
            return
        except Exception as e:
            logger.exception("Unexpected QR flow error for %s: %s", user_id, e)
            try:
                await send_and_log(bot, user_id, "❌ Ошибка QR авторизации. Попробуйте снова.")
            except Exception:
                pass
            await app.auth.cleanup_tmp(user_id)
            return

    except Exception as e:
        logger.exception("Critical error in handle_qr_flow for %s: %s", user_id, e)
        try:
            await send_and_log(bot, user_id, "❌ Внутренняя ошибка при QR авторизации.")
        except Exception:
            pass
        try:
            await app.auth.cleanup_tmp(user_id)
        except Exception:
            pass


    # Общий обработчик всех callback-запросов
    

    # А функцию переименуй / создай новую примерно так:
async def callback_or_approval_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    uid = query.from_user.id

    # Сначала проверяем админские действия
    if data.startswith("approve_user:") or data.startswith("reject_user:") or data.startswith("deny_user:"):
        await query.answer("Ручное одобрение отключено.", show_alert=False)
        return

    # Всё остальное — обычная авторизация / mute / etc.
    # Здесь копируем текущую логику callback_handler
    app = context.application.bot_data.get("app")
    if not app:
        return

    uname = query.from_user.username
    log_frontend_incoming(uid, uname, text=data, meta="callback")
    await register_and_notify_new_user(update, context, app.db)

    await query.answer()

    if data == "main_menu":
        session_valid = await _is_session_active(app, uid)
        first_name = html_escape(query.from_user.first_name or "друг")
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            _build_command_center_text(first_name, session_valid=session_valid),
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_main_menu_keyboard(session_valid=session_valid),
        )
        return

    if data == "main_help":
        session_valid = await _is_session_active(app, uid)
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            _build_help_text(),
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_main_menu_keyboard(session_valid=session_valid),
        )
        return

    if data in {"stats", "stats_panel"}:
        session_valid = await _is_session_active(app, uid)
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            await _build_stats_text(app, uid),
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_main_menu_keyboard(session_valid=session_valid),
        )
        return

    if data == "unmute_all":
        count = await _clear_muted_chats(app, uid)
        session_valid = await _is_session_active(app, uid)
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            _build_unmute_text(count),
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_main_menu_keyboard(session_valid=session_valid),
        )
        return

    if data == "logout_confirm":
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            (
                "🚪 <b>Отключить текущую сессию?</b>\n\n"
                "SavedBot перестанет слушать аккаунт до повторного подключения. "
                "Уже сохранённые записи останутся в архиве."
            ),
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_logout_confirm_keyboard(),
        )
        return

    if data == "logout_execute":
        await _disconnect_user_session(app, uid)
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            (
                "❎ <b>Текущая сессия отключена</b>\n\n"
                "SavedBot больше не слушает аккаунт. Подключиться заново можно кнопками ниже."
            ),
            message_id=getattr(query.message, "message_id", None),
            reply_markup=build_start_keyboard(),
        )
        return

    if data == "start_advantages":
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            _start_advantages_text(),
            message_id=getattr(query.message, "message_id", None),
            reply_markup=build_start_keyboard(),
        )
        return

    if data == "set_root":
        first_name = html_escape(query.from_user.first_name or "друг")
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            _build_settings_menu_text(first_name),
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_set_root_keyboard(),
        )
        return

    if data == "set_profile":
        profile_text = await _build_set_profile_text(app, query.from_user)
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            profile_text,
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_set_back_keyboard(),
        )
        return

    if data == "set_help":
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            _start_advantages_text(),
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_set_back_keyboard(),
        )
        return

    if data == "set_listen_menu":
        settings = await get_user_chat_type_settings(app.db, uid)
        listen_text = (
            "🎚 <b>Какие чаты слушать и сохранять</b>\n\n"
            "Включите только те источники, которые действительно нужны. "
            "Изменения применяются сразу и влияют на новые события."
        )
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            listen_text,
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_listen_settings_keyboard(settings),
        )
        return

    if data.startswith("set_listen_toggle:"):
        setting_key = str(data.split(":", 1)[1] or "").strip().lower()
        settings = await get_user_chat_type_settings(app.db, uid)
        current_enabled = bool(int(settings.get(setting_key, 0)))
        updated = await set_user_chat_type_setting(app.db, uid, setting_key, not current_enabled)
        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            "🎚 <b>Какие чаты слушать и сохранять</b>\n\nИзменения применены. Можно переключить ещё один тип чатов или вернуться назад.",
            message_id=getattr(query.message, "message_id", None),
            reply_markup=_build_listen_settings_keyboard(updated),
        )
        return

    info = await get_state(app.db, uid)
    banned_until = info.get("banned_until")
    now_ts = time.time()
    if banned_until:
        try:
            banned_ts = float(banned_until)
        except:
            banned_ts = 0
        if banned_ts > now_ts and data in {"auth_phone", "auth_qr", "logout"}:
            wait_sec = max(int(banned_ts - now_ts), 1)
            msg_text = (
                "⛔ <b>Авторизация временно заблокирована.</b>\n\n"
                f"Попробуйте снова через <b>{wait_sec} сек.</b>."
            )
            await send_and_log(context.bot, uid, msg_text, username=uname, parse_mode=ParseMode.HTML)
            return

    if data == "auth_phone":
        await set_state(app.db, uid, AuthState.WAIT_PHONE)
        msg_text = (
            "Введите номер телефона в международном формате:\n"
            "<code>+71234567890</code>\n\n"
            "После этого Telegram пришлёт код."
        )
        m = await send_and_log(
            context.bot,
            uid,
            msg_text,
            username=uname,
            parse_mode=ParseMode.HTML
        )
        try:
            app.auth.track_auth_message(uid, m.message_id)
        except Exception:
            pass
        return

    if data == "auth_qr":
        # Отправляем "Генерируем QR..." и запускаем процесс
        gen_text = "⏳ Генерируем QR-код...\n\nПожалуйста, подождите."
        gen_msg = await send_and_log(
            context.bot,
            uid,
            gen_text,
            username=uname,
            parse_mode=ParseMode.HTML
        )
        try:
            app.auth.track_auth_message(uid, gen_msg.message_id)
        except Exception:
            pass

        # Запускаем QR-поток в фоне (не блокируем обработчик)
        asyncio.create_task(handle_qr_flow(uid, context.bot, gen_msg.message_id, app))
        return

    # Обработчик "Код не пришел?" и повторная отправка кода
    if data.startswith("auth_resend_code:"):
        info = await get_state(app.db, uid)
        state = info.get("state")
        resend_allowed_at = info.get("resend_allowed_at")
        phone = str(info.get("phone") or "").strip()
        expires_at = float(info.get("expires_at") or 0)
        status_message_id = getattr(query.message, "message_id", None)

        help_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f5dd \u0412\u043e\u0439\u0442\u0438 \u043f\u043e QR-\u043a\u043e\u0434\u0443", callback_data="auth_qr")],
            [InlineKeyboardButton("\U0001f4f1 \u0412\u0432\u0435\u0441\u0442\u0438 \u043d\u043e\u043c\u0435\u0440 \u0437\u0430\u043d\u043e\u0432\u043e", callback_data="auth_phone")],
        ])

        if state != AuthState.CODE_SENT or not phone:
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                "\u274c <b>\u0421\u0435\u0441\u0441\u0438\u044f \u0437\u0430\u043f\u0440\u043e\u0441\u0430 \u043a\u043e\u0434\u0430 \u0443\u0441\u0442\u0430\u0440\u0435\u043b\u0430.</b>\n\u0417\u0430\u043f\u0440\u043e\u0441\u0438\u0442\u0435 \u043d\u043e\u0432\u044b\u0439 \u043a\u043e\u0434 \u0438\u043b\u0438 \u0432\u043e\u0439\u0434\u0438\u0442\u0435 \u043f\u043e QR-\u043a\u043e\u0434\u0443.",
                message_id=status_message_id,
                reply_markup=help_kb,
            )
            return

        if expires_at and time.time() > expires_at:
            await app.auth.cleanup_tmp(uid)
            await set_state(
                app.db,
                uid,
                AuthState.WAIT_PHONE,
                phone=None,
                tmp_prefix=None,
                phone_code_hash=None,
                expires_at=None,
                auth_fail_count=0,
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                "⌛ <b>Сессия ввода кода истекла.</b>\nВведите номер заново или войдите по QR-коду.",
                message_id=status_message_id,
                reply_markup=help_kb,
            )
            return

        now_ts = time.time()
        try:
            resend_ts = float(resend_allowed_at or 0)
        except (ValueError, TypeError):
            resend_ts = 0

        if resend_ts > now_ts:
            wait_sec = max(int(resend_ts - now_ts), 1)
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                f"\u23f3 <b>\u041f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435 {wait_sec} \u0441\u0435\u043a.</b> \u043f\u0435\u0440\u0435\u0434 \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u043e\u0439 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u043e\u0439 \u043a\u043e\u0434\u0430.\n\n\u0415\u0441\u043b\u0438 \u043d\u0435 \u0445\u043e\u0442\u0438\u0442\u0435 \u0436\u0434\u0430\u0442\u044c, \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 \u0432\u0445\u043e\u0434 \u043f\u043e QR-\u043a\u043e\u0434\u0443.",
                message_id=status_message_id,
                reply_markup=help_kb,
            )
            return

        await _update_auth_message(
            context.bot,
            app,
            uid,
            uname,
            "\u23f3 \u0417\u0430\u043f\u0440\u0430\u0448\u0438\u0432\u0430\u0435\u043c \u043d\u043e\u0432\u044b\u0439 \u043a\u043e\u0434 \u0443 Telegram...\n\n\u041f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430, \u043f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435.",
            message_id=status_message_id,
        )

        prefix = app.auth.tmp_prefixes.get(uid)
        try:
            code_request, prefix = await _request_telegram_code(
                app,
                uid,
                phone,
                reuse_existing_client=True,
            )

            log_auth_attempt(
                uid,
                uname,
                phone,
                state,
                meta=f"resend=1, phone_code_hash={code_request.phone_code_hash}",
                result="OK",
            )
            await set_state(
                app.db,
                uid,
                AuthState.CODE_SENT,
                phone=phone,
                tmp_prefix=prefix,
                phone_code_hash=code_request.phone_code_hash,
                expires_at=_auth_expires_at(AUTH_CODE_TTL_SEC),
                resend_allowed_at=time.time() + CONFIG.resend_cooldown,
                auth_fail_count=0,
            )
            app.auth.cache_auth_context(
                uid,
                phone=phone,
                phone_code_hash=code_request.phone_code_hash,
                tmp_prefix=prefix,
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                _build_code_sent_text(resent=True),
                message_id=status_message_id,
                reply_markup=get_resend_code_keyboard(uid),
            )
        except PhoneNumberInvalidError:
            await app.auth.cleanup_tmp(uid)
            log_auth_attempt(uid, uname, phone, state, result="ResendInvalidPhone")
            await set_state(
                app.db,
                uid,
                AuthState.WAIT_PHONE,
                phone=None,
                tmp_prefix=None,
                resend_allowed_at=None,
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                "\u274c <b>\u0421\u043e\u0445\u0440\u0430\u043d\u0451\u043d\u043d\u044b\u0439 \u043d\u043e\u043c\u0435\u0440 \u0431\u043e\u043b\u044c\u0448\u0435 \u043d\u0435 \u043f\u0440\u0438\u043d\u0438\u043c\u0430\u0435\u0442\u0441\u044f Telegram.</b>\n\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u043e\u043c\u0435\u0440 \u0437\u0430\u043d\u043e\u0432\u043e \u0438\u043b\u0438 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 QR-\u043a\u043e\u0434.",
                message_id=status_message_id,
                reply_markup=help_kb,
            )
        except FloodWaitError as e:
            wait_sec = (e.seconds if hasattr(e, 'seconds') else 60) + 10
            log_auth_attempt(uid, uname, phone, state, result=f"ResendFloodWait_{wait_sec}")
            await set_state(
                app.db,
                uid,
                AuthState.CODE_SENT,
                phone=phone,
                tmp_prefix=prefix,
                resend_allowed_at=time.time() + wait_sec,
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                f"\u23f3 <b>Telegram \u043f\u043e\u043f\u0440\u043e\u0441\u0438\u043b \u043f\u043e\u0434\u043e\u0436\u0434\u0430\u0442\u044c {wait_sec} \u0441\u0435\u043a.</b> \u043f\u0435\u0440\u0435\u0434 \u043d\u043e\u0432\u043e\u0439 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u043e\u0439 \u043a\u043e\u0434\u0430.",
                message_id=status_message_id,
                reply_markup=help_kb,
            )
        except SendCodeUnavailableError:
            block_sec = CONFIG.sendcode_unavailable_block or 300
            log_auth_attempt(uid, uname, phone, state, result="ResendSendCodeUnavailable")
            await send_critical_alert(
                context.bot,
                app.db,
                error_type="AUTH_RESEND_CODE_UNAVAILABLE",
                error_text="SendCodeUnavailableError while resending code",
                user_id=uid,
                username=uname,
                context="callback.auth_resend_code.send_unavailable",
                extra={"phone": phone, "block_sec": block_sec},
            )
            await set_state(
                app.db,
                uid,
                AuthState.CODE_SENT,
                phone=phone,
                tmp_prefix=prefix,
                resend_allowed_at=time.time() + block_sec,
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                f"\u26a0\ufe0f <b>\u041e\u0442\u043f\u0440\u0430\u0432\u043a\u0430 \u043a\u043e\u0434\u043e\u0432 \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430.</b>\n\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0447\u0435\u0440\u0435\u0437 <b>{block_sec} \u0441\u0435\u043a.</b> \u0438\u043b\u0438 \u0432\u043e\u0439\u0434\u0438\u0442\u0435 \u043f\u043e QR-\u043a\u043e\u0434\u0443.",
                message_id=status_message_id,
                reply_markup=help_kb,
            )
        except Exception as e:
            log_auth_attempt(uid, uname, phone, state, result="ResendError")
            logger.exception("[AUTH] Resend code failed for %s: %s", uid, e)
            await send_critical_alert(
                context.bot,
                app.db,
                error_type="AUTH_RESEND_FAILED",
                error_text=str(e),
                user_id=uid,
                username=uname,
                context="callback.auth_resend_code.error",
                extra={"phone": phone},
            )
            await _update_auth_message(
                context.bot,
                app,
                uid,
                uname,
                "\u274c <b>\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u043e \u0437\u0430\u043f\u0440\u043e\u0441\u0438\u0442\u044c \u043a\u043e\u0434.</b>\n\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0451 \u0440\u0430\u0437 \u0447\u0443\u0442\u044c \u043f\u043e\u0437\u0436\u0435 \u0438\u043b\u0438 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 QR-\u043a\u043e\u0434.",
                message_id=status_message_id,
                reply_markup=help_kb,
            )
        return


    # ────────────────────────────────────────────────
    # Обработчик заглушения чата (mute_chat)
    # ────────────────────────────────────────────────
    if data.startswith("mute_chat:"):
        await query.answer()
        try:
            chat_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.edit_message_text("❌ Ошибка: неверный ID чата")
            return
        
        # Проверяем есть ли уже эта запись
        existing = await app.db.fetchone(
            "SELECT 1 FROM muted_chats WHERE owner_id = ? AND chat_id = ?",
            (uid, chat_id)
        )
        
        if existing:
            await query.answer("✅ Этот чат уже заглушен", show_alert=False)
            return
        
        # Добавляем в заглушенные
        try:
            await app.db.execute(
                "INSERT INTO muted_chats (owner_id, chat_id, muted_at) VALUES (?, ?, ?)",
                (uid, chat_id, datetime.now(timezone.utc).isoformat())
            )
            await app.db.conn.commit()
            await query.edit_message_text(
                query.message.text + "\n\n✅ Чат заглушен. Удалённые сообщения из этого чата больше не будут отправляться."
            )
        except Exception as e:
            logger.exception("Failed to mute chat %s for user %s: %s", chat_id, uid, e)
            await query.edit_message_text("❌ Ошибка при заглушении чата. Попробуйте снова.")
        return

    await _update_auth_message(
        context.bot,
        app,
        uid,
        uname,
        (
            "<b>Меню обновлено.</b>\n\n"
            "Откройте актуальное меню через /start.\n\n"
            f"{_build_marker_line()}"
        ),
        message_id=getattr(query.message, "message_id", None),
        reply_markup=build_start_keyboard(),
    )
    return


# ----------------------------
# Main loop / bootstrap
# ----------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot exception: %s", context.error, exc_info=True)
    app: Optional[App] = context.bot_data.get("app") if hasattr(context, "bot_data") else None
    effective_user = getattr(update, "effective_user", None)
    uid = int(getattr(effective_user, "id", 0) or 0) if effective_user else None
    uname = getattr(effective_user, "username", None) if effective_user else None
    if app is not None:
        try:
            await send_critical_alert(
                context.bot,
                app.db,
                error_type="BOT_HANDLER_UNHANDLED_EXCEPTION",
                error_text=str(context.error),
                user_id=uid,
                username=uname,
                context="handlers.error_handler",
                extra={"update_type": type(update).__name__ if update is not None else "unknown"},
            )
        except Exception:
            logger.debug("Failed to send critical alert from error_handler", exc_info=True)


async def post_init(application: Application):
    """Create App, connect DB, start event workers, restore watchers."""
    app = App(CONFIG, application)
    application.bot_data["app"] = app
    await app.start()
    # Prime bot username cache once on startup.
    try:
        me = await application.native_bot.get_me()
        bot_username = str(getattr(me, "username", "") or "").strip().lstrip("@")
        if bot_username:
            application.bot_data["bot_username"] = bot_username
    except Exception:
        logger.warning("Failed to preload bot username via get_me()", exc_info=True)
        env_username = str(getattr(CONFIG, "bot_username", "") or "").strip().lstrip("@")
        if env_username:
            application.bot_data["bot_username"] = env_username

    # Publish command menu in Telegram UI.
    try:
        await application.native_bot.set_my_commands(
            [
                BotCommand(command="start", description="Запуск и подключение бота"),
                BotCommand(command="set", description="Центр управления"),
                BotCommand(command="profile", description="Профиль"),
                BotCommand(command="stats", description="Статистика архива"),
                BotCommand(command="help", description="Справка по функциям"),
                BotCommand(command="logout", description="Завершить сессию"),
            ]
        )
    except Exception:
        logger.warning("Failed to set bot command menu", exc_info=True)

    logger.info("Restoring sessions...")
    seen_uids = set()
    # legacy sessions in sessions_dir
    for path in glob.glob(os.path.join(CONFIG.sessions_dir, "*.session.zip")):
        try:
            fname = os.path.basename(path)
            uid = int(fname.split(".")[0])
            if uid not in seen_uids:
                seen_uids.add(uid)
        except Exception:
            pass
    # sessions in auth_attempts
    auth_base = os.path.join(CONFIG.logs_dir, AUTH_LOGS_SUBDIR)
    for path in glob.glob(os.path.join(auth_base, "user_*", "*.session.zip")):
        try:
            fname = os.path.basename(path)
            uid = int(fname.split(".")[0])
            if uid not in seen_uids:
                seen_uids.add(uid)
        except Exception:
            pass

    if seen_uids:
        await app.watcher_service.restore_watchers(list(seen_uids))

    logger.info(f"Restored {len(seen_uids)} watchers.")
    daily_task = application.bot_data.get("daily_report_task")
    if daily_task is None or daily_task.done():
        application.bot_data["daily_report_task"] = asyncio.create_task(_daily_report_loop(application))
COMMAND_HANDLERS = {
    "/start": start_cmd,
    "/help": help_cmd,
    "/set": set_cmd,
    "/profile": profile_cmd,
    "/logout": logout_cmd,
    "/stats": stats_cmd,
    "/unmute": unmute_cmd,
    "/cleansessions": cleansessions_cmd,
    "/sessions_health": sessions_health_cmd,
    "/cleardb": cleardb_cmd,
}


def _extract_command(text: str) -> Optional[str]:
    if not text or not text.startswith("/"):
        return None
    command = text.split()[0].strip()
    if "@" in command:
        command = command.split("@", 1)[0]
    return command.lower()


async def _dispatch_message(native_message: AiogramMessage, application: Application) -> None:
    update = Update.from_message(native_message, application.bot)
    context = application.build_context()
    try:
        is_blocked = await access_guard(update, context)
        if is_blocked:
            return

        message_text = native_message.text or ""
        command = _extract_command(message_text)
        if command and command in COMMAND_HANDLERS:
            await COMMAND_HANDLERS[command](update, context)
            return

        if message_text:
            await text_handler(update, context)
    except Exception as exc:
        context.error = exc
        await error_handler(update, context)


async def _dispatch_callback(native_query: AiogramCallbackQuery, application: Application) -> None:
    update = Update.from_callback_query(native_query, application.bot)
    context = application.build_context()
    try:
        await callback_or_approval_handler(update, context)
    except Exception as exc:
        context.error = exc
        await error_handler(update, context)


async def run_bot() -> None:
    application = Application(CONFIG.bot_token)
    dispatcher = Dispatcher()
    router = Router()
    app: Optional[App] = None

    @router.message()
    async def _message_entrypoint(message: AiogramMessage) -> None:
        await _dispatch_message(message, application)

    @router.callback_query()
    async def _callback_entrypoint(query: AiogramCallbackQuery) -> None:
        await _dispatch_callback(query, application)

    dispatcher.include_router(router)

    await post_init(application)
    app = application.bot_data.get("app")

    logger.info("Starting aiogram polling...")

    try:
        try:
            await application.native_bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            logger.debug("delete_webhook before polling failed", exc_info=True)
        await dispatcher.start_polling(
            application.native_bot,
            handle_as_tasks=True,
            allowed_updates=["message", "callback_query"],
        )
    finally:
        for key in ("daily_report_task",):
            task = application.bot_data.get(key)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        if app is not None:
            try:
                await app.stop()
            except Exception:
                logger.exception("Failed to stop runtime app cleanly")
        await application.native_bot.session.close()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
