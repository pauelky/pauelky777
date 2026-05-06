from .shared import *
from .events_core import *
from .state import *

# ----------------------------
# WatcherService & AuthFlow
# ----------------------------

def _is_broadcast_channel(event) -> bool:
    try:
        chat = getattr(event, "chat", None)
        if chat is None:
            return False
        if isinstance(chat, dict):
            chat = type("C", (), chat)()
        return bool(getattr(chat, "broadcast", False) and not getattr(chat, "megagroup", False) and not getattr(chat, "gigagroup", False))
    except Exception:
        return False


def _is_session_terminated_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    session_markers = (
        "sessionrevoked",
        "authkeyduplicated",
        "authkeyunregistered",
        "userdeactivated",
        "userdeactivatedban",
        "unauthorized",
        "session_password_needed",
    )
    if any(marker in name for marker in session_markers):
        return True
    return any(
        marker in text
        for marker in (
            "session revoked",
            "auth key",
            "user deactivated",
            "authorization has been invalidated",
            "logged out",
        )
    )


class WatcherService:
    def __init__(self, storage: Any, event_handler: EventHandler, config: Any, api_id: int, api_hash: str, bot_app: Any):
        self.storage = storage
        self.event_handler = event_handler
        self.config = config
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_app = bot_app
        self.watchers: Dict[int, asyncio.Task] = {}
        self.watched_clients: Dict[int, TelegramClient] = {}
        
        self._story_tasks: Dict[int, asyncio.Task] = {}
        self._dialog_sync_tasks: Dict[int, asyncio.Task] = {}
        self._dialog_avatar_fresh_until: Dict[Tuple[int, int], float] = {}
        self._dialog_avatar_ttl_sec = 6 * 60 * 60.0
        self.seen_story_ids: Dict[int, set] = defaultdict(set)
        self.restart_locks = defaultdict(asyncio.Lock)
        self._restart_failures: Dict[int, int] = {}

    async def _cancel_task(self, task: Optional[asyncio.Task]) -> None:
        if not task:
            return
        current = asyncio.current_task()
        if task is current:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_terminated_session(self, user_id: int, exc: Optional[BaseException] = None) -> None:
        logger.warning(
            "Watcher session terminated for user %s: %s",
            user_id,
            type(exc).__name__ if exc else "unauthorized",
        )
        try:
            await send_critical_alert(
                self.bot_app.bot,
                self.event_handler.db,
                error_type="AUTH_SESSION_TERMINATED",
                error_text=str(exc or "session unauthorized"),
                user_id=user_id,
                context="runtime._handle_terminated_session",
            )
        except Exception:
            logger.debug("Failed to send critical alert for terminated session", exc_info=True)
        try:
            self.storage.delete(user_id)
        except Exception:
            logger.exception("Failed to delete invalid session for user %s", user_id)

        self.seen_story_ids.pop(user_id, None)

        try:
            await set_state(
                self.event_handler.db,
                user_id,
                "IDLE",
                phone=None,
                tmp_prefix=None,
                awaiting_2fa=0,
                auth_fail_count=0,
                banned_until=None,
            )
        except Exception:
            logger.exception("Failed to reset auth state after session termination for user %s", user_id)

        try:
            notify_text = (
                "⚠️ <b>Сессия Telegram завершена.</b>\n\n"
                "Watcher остановлен, потому что этот вход был закрыт в Telegram. "
                "Чтобы бот снова отслеживал сообщения, авторизуйтесь заново."
            )
            await send_and_log(self.bot_app.bot, user_id, notify_text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Failed to notify user about terminated session for user %s", user_id)

    def ensure(self, user_id: int) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._ensure_async(user_id))
        except RuntimeError:
            pass

    async def _ensure_async(self, user_id: int) -> None:
        async with self.restart_locks[user_id]:
            if user_id in self.watchers and not self.watchers[user_id].done():
                return
            self.watchers[user_id] = asyncio.create_task(self._run(user_id))

    async def stop(self, user_id: int) -> None:
        story_task = self._story_tasks.pop(user_id, None)
        await self._cancel_task(story_task)
        dialog_task = self._dialog_sync_tasks.pop(user_id, None)
        await self._cancel_task(dialog_task)

        task = self.watchers.get(user_id)
        current = asyncio.current_task()
        if task and task is not current and not task.done():
            task.cancel()
        client = self.watched_clients.get(user_id)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if task and task is not current:
            await self._cancel_task(task)
        self.watched_clients.pop(user_id, None)
        self.watchers.pop(user_id, None)
        self.seen_story_ids.pop(user_id, None)

    async def stop_all(self) -> None:
        # Gracefully stop all watcher tasks
        tasks = [self.stop(user_id) for user_id in list(self.watchers.keys())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def restore_watchers(self, user_ids: List[int]) -> None:
        # Restore watcher lifecycle
        tasks = [self._ensure_async(uid) for uid in user_ids]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, user_id: int) -> None:
        restore_dir, prefix = self.storage.restore(user_id)
        if not prefix:
            if restore_dir:
                shutil.rmtree(restore_dir, ignore_errors=True)
            return

        run_started_at = time.monotonic()
        client = None
        was_cancelled = False
        session_terminated = False
        crashed_unexpected = False
        try:
            client = TelegramClient(prefix, self.api_id, self.api_hash)
            await client.start()
            try:
                await client.catch_up()
            except Exception:
                logger.exception("catch_up() failed but continuing")

            try:
                me = await client.get_me()
                logger.info("Watcher connected as %s (%s)", getattr(me, "username", None), getattr(me, "id", None))
                dialogs = await client.get_dialogs(limit=5)
                logger.info("Watcher dialogs sample for %s: %s", user_id, [getattr(d, "name", None) for d in dialogs])
            except Exception:
                logger.exception("Failed to inspect dialogs for watcher %s", user_id)

            logger.info("Telethon client started for user %s (prefix=%s)", user_id, prefix)

            if not await client.is_user_authorized():
                logger.warning("User %s session invalid after start()", user_id)
                session_terminated = True
                await self._handle_terminated_session(user_id)
                return

            self.watched_clients[user_id] = client

            @client.on(events.NewMessage(incoming=True))
            async def __debug_incoming(ev):
                try:
                    logger.info(
                        "DEBUG incoming(user=%s): chat_id=%s msg_id=%s text_preview=%s",
                        user_id,
                        getattr(ev, "chat_id", None),
                        getattr(ev, "id", None),
                        (ev.raw_text[:200] if getattr(ev, "raw_text", None) else None),
                    )
                except Exception:
                    pass

            local_sem = asyncio.Semaphore(self.config.max_concurrent)

            def _register_event_handlers(event_filter_func):
                @client.on(events.NewMessage(func=event_filter_func))
                async def _on_new(ev):
                    async with local_sem:
                        try:
                            await self.event_handler.handle_new(user_id, ev)
                        except Exception as exc:
                            logger.exception("NewMessage handler crashed for user %s", user_id)
                            try:
                                await send_critical_alert(
                                    self.bot_app.bot,
                                    self.event_handler.db,
                                    error_type="WATCHER_NEWMESSAGE_HANDLER_CRASH",
                                    error_text=str(exc),
                                    user_id=user_id,
                                    context="runtime._on_new",
                                    extra={
                                        "chat_id": getattr(ev, "chat_id", None),
                                        "msg_id": getattr(ev, "id", None),
                                    },
                                )
                            except Exception:
                                logger.debug("Failed to send critical alert for _on_new", exc_info=True)

                @client.on(events.MessageEdited(func=event_filter_func))
                async def _on_edit(ev):
                    async with local_sem:
                        try:
                            await self.event_handler.handle_edited(user_id, ev)
                        except Exception as exc:
                            logger.exception("MessageEdited handler crashed for user %s", user_id)
                            try:
                                await send_critical_alert(
                                    self.bot_app.bot,
                                    self.event_handler.db,
                                    error_type="WATCHER_EDIT_HANDLER_CRASH",
                                    error_text=str(exc),
                                    user_id=user_id,
                                    context="runtime._on_edit",
                                    extra={
                                        "chat_id": getattr(ev, "chat_id", None),
                                    },
                                )
                            except Exception:
                                logger.debug("Failed to send critical alert for _on_edit", exc_info=True)

                @client.on(events.MessageDeleted(func=event_filter_func))
                async def _on_del(ev):
                    async with local_sem:
                        try:
                            await self.event_handler.handle_deleted(user_id, ev)
                        except Exception as exc:
                            logger.exception("MessageDeleted handler crashed for user %s", user_id)
                            try:
                                await send_critical_alert(
                                    self.bot_app.bot,
                                    self.event_handler.db,
                                    error_type="WATCHER_DELETE_HANDLER_CRASH",
                                    error_text=str(exc),
                                    user_id=user_id,
                                    context="runtime._on_del",
                                )
                            except Exception:
                                logger.debug("Failed to send critical alert for _on_del", exc_info=True)

            _register_event_handlers(lambda ev: not _is_broadcast_channel(ev))
            _register_event_handlers(lambda ev: _is_broadcast_channel(ev))

            logger.info("Watcher event handlers registered for user %s (chats + channels)", user_id)

            if user_id not in self._story_tasks or self._story_tasks[user_id].done():
                self._story_tasks[user_id] = asyncio.create_task(self._story_poller(user_id))
            if user_id not in self._dialog_sync_tasks or self._dialog_sync_tasks[user_id].done():
                self._dialog_sync_tasks[user_id] = asyncio.create_task(self._dialog_sync_loop(user_id))

            await client.run_until_disconnected()

            if self.storage.is_valid(user_id):
                try:
                    authorized_after_disconnect = await client.is_user_authorized()
                except Exception as exc:
                    if _is_session_terminated_error(exc):
                        session_terminated = True
                        await self._handle_terminated_session(user_id, exc)
                        return
                    authorized_after_disconnect = True
                if not authorized_after_disconnect:
                    session_terminated = True
                    await self._handle_terminated_session(user_id)
                    return

        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except Exception as exc:
            if _is_session_terminated_error(exc):
                session_terminated = True
                await self._handle_terminated_session(user_id, exc)
            else:
                crashed_unexpected = True
                logger.exception("Watcher crashed for %s", user_id)
        finally:
            story_task = self._story_tasks.pop(user_id, None)
            await self._cancel_task(story_task)
            dialog_task = self._dialog_sync_tasks.pop(user_id, None)
            await self._cancel_task(dialog_task)
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            if restore_dir:
                shutil.rmtree(restore_dir, ignore_errors=True)
            self.watched_clients.pop(user_id, None)
            self.seen_story_ids.pop(user_id, None)
            if was_cancelled or session_terminated:
                self._restart_failures.pop(user_id, None)
            if not was_cancelled and not session_terminated:
                uptime_sec = max(0.0, time.monotonic() - run_started_at)
                if crashed_unexpected and uptime_sec < 300:
                    failures = int(self._restart_failures.get(user_id, 0) or 0) + 1
                else:
                    failures = 0
                self._restart_failures[user_id] = failures

                base_delay = max(1, int(getattr(self.config, "restart_delay", 4) or 4))
                if failures <= 1:
                    restart_delay = base_delay
                else:
                    restart_delay = min(120, base_delay * (2 ** min(failures - 1, 5)))

                if failures > 0:
                    logger.warning(
                        "Watcher restart backoff for user %s: failures=%s delay=%ss uptime=%.1fs",
                        user_id,
                        failures,
                        restart_delay,
                        uptime_sec,
                    )
                    if failures in {3, 6, 10}:
                        try:
                            await send_critical_alert(
                                self.bot_app.bot,
                                self.event_handler.db,
                                error_type="WATCHER_RESTART_LOOP",
                                error_text=f"Watcher keeps restarting ({failures} times)",
                                user_id=user_id,
                                context="runtime._run.finally",
                                extra={"failures": failures, "restart_delay_sec": restart_delay, "uptime_sec": round(uptime_sec, 2)},
                            )
                        except Exception:
                            logger.debug("Failed to send restart-loop critical alert", exc_info=True)

                await asyncio.sleep(restart_delay)
                if self.storage.is_valid(user_id):
                    await self._ensure_async(user_id)

    def _dialog_avatar_dir(self, user_id: int) -> str:
        avatar_dir = os.path.join(self.config.media_dir, "dialog_avatars", str(user_id))
        os.makedirs(avatar_dir, exist_ok=True)
        return avatar_dir

    def _dialog_avatar_path(self, user_id: int, chat_id: int) -> str:
        return os.path.join(self._dialog_avatar_dir(user_id), f"{int(chat_id)}.jpg")

    async def _sync_dialog_photo(self, user_id: int, client: TelegramClient, entity: Any, chat_id: int) -> str:
        if entity is None:
            return ""

        target_path = self._dialog_avatar_path(user_id, chat_id)
        refresh_key = (int(user_id), int(chat_id))
        has_photo = bool(getattr(entity, "photo", None))

        if not has_photo:
            self._dialog_avatar_fresh_until[refresh_key] = time.monotonic() + self._dialog_avatar_ttl_sec
            if os.path.exists(target_path):
                try:
                    os.remove(target_path)
                except Exception:
                    logger.debug("Dialog avatar cleanup failed for user %s chat %s", user_id, chat_id, exc_info=True)
            return ""

        now_ts = time.monotonic()
        if os.path.exists(target_path) and now_ts < float(self._dialog_avatar_fresh_until.get(refresh_key, 0.0)):
            return target_path

        tmp_prefix = os.path.join(self._dialog_avatar_dir(user_id), f"{int(chat_id)}_{uuid.uuid4().hex}")
        downloaded_path = ""
        try:
            maybe_path = await client.download_profile_photo(entity, file=tmp_prefix, download_big=False)
            if isinstance(maybe_path, str) and maybe_path and os.path.exists(maybe_path):
                downloaded_path = maybe_path
        except Exception:
            logger.debug("Primary dialog avatar download failed for user %s chat %s", user_id, chat_id, exc_info=True)

        if not downloaded_path:
            try:
                photos = await client.get_profile_photos(entity, limit=1)
                if photos:
                    maybe_path = await client.download_media(photos[0], file=tmp_prefix)
                    if isinstance(maybe_path, str) and maybe_path and os.path.exists(maybe_path):
                        downloaded_path = maybe_path
            except Exception:
                logger.debug("Fallback dialog avatar download failed for user %s chat %s", user_id, chat_id, exc_info=True)

        if not downloaded_path:
            return target_path if os.path.exists(target_path) else ""

        normalized_ok = False
        try:
            resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
            with Image.open(downloaded_path) as source:
                image = ImageOps.exif_transpose(source)
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGBA")
                    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
                    background.alpha_composite(image)
                    image = background.convert("RGB")
                elif image.mode == "L":
                    image = image.convert("RGB")
                image = ImageOps.fit(image, (192, 192), method=resample, centering=(0.5, 0.5))
                image.save(target_path, format="JPEG", quality=88, optimize=True)
            normalized_ok = os.path.exists(target_path) and os.path.getsize(target_path) > 256
        except Exception:
            logger.debug("Dialog avatar normalization failed for user %s chat %s", user_id, chat_id, exc_info=True)
            try:
                shutil.copyfile(downloaded_path, target_path)
                normalized_ok = os.path.exists(target_path) and os.path.getsize(target_path) > 256
            except Exception:
                logger.debug("Dialog avatar fallback copy failed for user %s chat %s", user_id, chat_id, exc_info=True)
        finally:
            if downloaded_path and os.path.exists(downloaded_path) and os.path.normcase(downloaded_path) != os.path.normcase(target_path):
                try:
                    os.remove(downloaded_path)
                except Exception:
                    pass

        if normalized_ok:
            self._dialog_avatar_fresh_until[refresh_key] = time.monotonic() + self._dialog_avatar_ttl_sec
            return target_path
        return target_path if os.path.exists(target_path) else ""

    async def _sync_dialog_batch(
        self,
        user_id: int,
        client: TelegramClient,
        dialog: Any,
        *,
        batch_size: int = 120,
        backfill_batches: int = 4,
    ) -> None:
        chat_id = getattr(dialog, "id", None)
        if chat_id is None or not self.event_handler.is_chat_allowed(chat_id):
            return

        entity = getattr(dialog, "entity", None)
        last_message = getattr(dialog, "message", None)
        unread_count = int(getattr(dialog, "unread_count", 0) or 0)
        dialog_photo_path = await self._sync_dialog_photo(user_id, client, entity, chat_id)

        if last_message is not None:
            try:
                await self.event_handler.sync_message_snapshot(
                    user_id,
                    last_message,
                    chat=entity,
                    photo_url=dialog_photo_path,
                    unread_count=unread_count,
                )
            except Exception:
                logger.debug("Dialog snapshot sync failed for user %s chat %s", user_id, chat_id, exc_info=True)
        else:
            try:
                await self.event_handler._upsert_dialog_record(
                    owner_id=user_id,
                    chat_id=chat_id,
                    dialog_type=self.event_handler._dialog_type_from_entity(entity),
                    title=self.event_handler._dialog_title_from_entity(entity, chat_id),
                    username=self.event_handler._dialog_username_from_entity(entity),
                    photo_url=dialog_photo_path,
                    last_message_id=None,
                    last_message_at=None,
                    last_message_preview="",
                    last_sender_id=None,
                    last_sender_label="",
                    unread_count=unread_count,
                )
            except Exception:
                logger.debug("Dialog metadata upsert failed for user %s chat %s", user_id, chat_id, exc_info=True)

        row = await self.event_handler.db.fetchone(
            """
            SELECT oldest_synced_msg_id, newest_synced_msg_id, history_complete
            FROM chat_sync_state
            WHERE owner_id = ? AND chat_id = ?
            """,
            (user_id, chat_id),
        )
        oldest_synced = int(row[0]) if row and row[0] is not None else None
        newest_synced = int(row[1]) if row and row[1] is not None else None
        history_complete = bool(row[2]) if row and row[2] is not None else False

        discovered_ids: List[int] = []

        if newest_synced:
            newer_messages = [
                msg
                async for msg in client.iter_messages(entity, min_id=int(newest_synced), reverse=True, limit=batch_size)
            ]
        else:
            newer_messages = [
                msg
                async for msg in client.iter_messages(entity, limit=batch_size)
            ]
            newer_messages.reverse()

        for msg in newer_messages:
            if getattr(msg, "id", None) is None:
                continue
            await self.event_handler.sync_message_snapshot(
                user_id,
                msg,
                chat=entity,
                photo_url=dialog_photo_path,
                unread_count=unread_count,
            )
            discovered_ids.append(int(msg.id))

        if not history_complete:
            oldest_cursor = oldest_synced
            if oldest_cursor is None and discovered_ids:
                oldest_cursor = min(discovered_ids)
            if oldest_cursor is not None:
                for _ in range(max(1, int(backfill_batches or 1))):
                    older_messages = [
                        msg
                        async for msg in client.iter_messages(entity, offset_id=int(oldest_cursor), limit=batch_size)
                    ]
                    older_messages.reverse()
                    if not older_messages:
                        history_complete = True
                        break
                    batch_ids: List[int] = []
                    for msg in older_messages:
                        if getattr(msg, "id", None) is None:
                            continue
                        await self.event_handler.sync_message_snapshot(
                            user_id,
                            msg,
                            chat=entity,
                            photo_url=dialog_photo_path,
                            unread_count=unread_count,
                        )
                        msg_id = int(msg.id)
                        discovered_ids.append(msg_id)
                        batch_ids.append(msg_id)
                    if not batch_ids:
                        history_complete = True
                        break
                    oldest_cursor = min(batch_ids)
                    await asyncio.sleep(0.05)
            elif last_message is None:
                history_complete = True

        if last_message is not None and getattr(last_message, "id", None) is not None:
            discovered_ids.append(int(last_message.id))

        if discovered_ids:
            oldest_value = min(discovered_ids + ([oldest_synced] if oldest_synced is not None else []))
            newest_value = max(discovered_ids + ([newest_synced] if newest_synced is not None else []))
        else:
            oldest_value = oldest_synced
            newest_value = newest_synced

        preview_text = ""
        sender_label = ""
        last_sender_id = None
        last_message_at = None
        if last_message is not None:
            preview_text = repair_mojibake(
                getattr(last_message, "raw_text", None)
                or getattr(last_message, "message", None)
                or ""
            ) or detect_content_type(last_message)
            last_message_at = (
                getattr(last_message, "date", None).isoformat()
                if getattr(last_message, "date", None) is not None
                else None
            )
            try:
                last_sender = getattr(last_message, "sender", None) or await last_message.get_sender()
            except Exception:
                last_sender = None
            last_sender_id, _, last_sender_label = self.event_handler._sender_snapshot(last_sender, message=last_message)
            if getattr(last_message, "out", False):
                last_sender_label = "Вы"

        await self.event_handler._upsert_dialog_record(
            owner_id=user_id,
            chat_id=chat_id,
            dialog_type=self.event_handler._dialog_type_from_entity(entity),
            title=self.event_handler._dialog_title_from_entity(entity, chat_id),
            username=self.event_handler._dialog_username_from_entity(entity),
            photo_url=dialog_photo_path,
            last_message_id=getattr(last_message, "id", None) if last_message is not None else newest_value,
            last_message_at=last_message_at,
            last_message_preview=preview_text,
            last_sender_id=last_sender_id,
            last_sender_label=last_sender_label,
            unread_count=unread_count,
            oldest_synced_msg_id=oldest_value,
            newest_synced_msg_id=newest_value,
            history_complete=history_complete,
            sync_error="",
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        next_sync_after = (
            datetime.now(timezone.utc) + timedelta(minutes=12 if history_complete else 1)
        ).isoformat()
        await self.event_handler._upsert_sync_state(
            owner_id=user_id,
            chat_id=chat_id,
            dialog_type=self.event_handler._dialog_type_from_entity(entity),
            sync_state="complete" if history_complete else "active",
            sync_priority=self.event_handler._sync_priority_for_dialog(
                self.event_handler._dialog_type_from_entity(entity),
                history_complete,
            ),
            oldest_synced_msg_id=oldest_value,
            newest_synced_msg_id=newest_value,
            history_complete=history_complete,
            backfill_passes_delta=max(0, int(backfill_batches or 0)),
            last_realtime_sync_at=now_iso,
            last_backfill_at=now_iso,
            next_sync_after=next_sync_after,
            last_error="",
        )

    async def _dialog_sync_loop(self, user_id: int) -> None:
        logger.info("[DIALOG SYNC] started for user %s", user_id)
        while True:
            client = self.watched_clients.get(user_id)
            if client is None or not self.storage.is_valid(user_id):
                logger.info("[DIALOG SYNC] stopping for user %s: watcher unavailable", user_id)
                return

            try:
                state_rows = await self.event_handler.db.fetchall(
                    """
                    SELECT chat_id, sync_priority, history_complete, next_sync_after, backfill_passes
                    FROM chat_sync_state
                    WHERE owner_id = ?
                    """,
                    (user_id,),
                )
                sync_state_map: Dict[int, Dict[str, Any]] = {}
                for row in state_rows or []:
                    try:
                        sync_state_map[int(row[0])] = {
                            "priority": int(row[1] or 100),
                            "history_complete": bool(row[2]),
                            "next_sync_after": str(row[3] or "").strip(),
                            "backfill_passes": int(row[4] or 0),
                        }
                    except Exception:
                        continue

                dialogs = [
                    dialog
                    async for dialog in client.iter_dialogs(archived=None, ignore_migrated=True)
                ]
                now_dt = datetime.now(timezone.utc)

                def _dialog_sort_key(dialog: Any) -> Tuple[int, int, int, float]:
                    chat_id = getattr(dialog, "id", None)
                    state = sync_state_map.get(int(chat_id)) if chat_id is not None else None
                    history_complete = bool(state.get("history_complete")) if state else False
                    priority = int(state.get("priority", 100 if history_complete else 25)) if state else 25
                    next_sync_after = str(state.get("next_sync_after") or "").strip() if state else ""
                    defer_flag = 0
                    if history_complete and next_sync_after:
                        try:
                            next_dt = datetime.fromisoformat(next_sync_after.replace("Z", "+00:00"))
                            if next_dt.tzinfo is None:
                                next_dt = next_dt.replace(tzinfo=timezone.utc)
                            if next_dt > now_dt:
                                defer_flag = 1
                        except Exception:
                            defer_flag = 0
                    last_message = getattr(dialog, "message", None)
                    message_dt = getattr(last_message, "date", None)
                    if message_dt is None:
                        sort_ts = 0.0
                    else:
                        if message_dt.tzinfo is None:
                            message_dt = message_dt.replace(tzinfo=timezone.utc)
                        sort_ts = -message_dt.timestamp()
                    return (defer_flag, priority, 1 if history_complete else 0, sort_ts)

                dialogs.sort(key=_dialog_sort_key)
                for dialog in dialogs:
                    if not self.storage.is_valid(user_id):
                        return
                    dialog_state = sync_state_map.get(int(getattr(dialog, "id", 0) or 0), {})
                    next_sync_after = str(dialog_state.get("next_sync_after") or "").strip()
                    if dialog_state.get("history_complete") and next_sync_after:
                        try:
                            next_dt = datetime.fromisoformat(next_sync_after.replace("Z", "+00:00"))
                            if next_dt.tzinfo is None:
                                next_dt = next_dt.replace(tzinfo=timezone.utc)
                            if next_dt > now_dt:
                                continue
                        except Exception:
                            pass
                    history_complete = bool(dialog_state.get("history_complete"))
                    backfill_batches = 1 if history_complete else 6
                    batch_size = 90 if history_complete else 140
                    try:
                        await self._sync_dialog_batch(
                            user_id,
                            client,
                            dialog,
                            batch_size=batch_size,
                            backfill_batches=backfill_batches,
                        )
                    except FloodWaitError as exc:
                        wait_for = int(getattr(exc, "seconds", 5) or 5)
                        logger.warning("[DIALOG SYNC] flood wait for user %s: %ss", user_id, wait_for)
                        await self.event_handler._upsert_sync_state(
                            owner_id=user_id,
                            chat_id=getattr(dialog, "id", None),
                            sync_state="paused",
                            next_sync_after=(datetime.now(timezone.utc) + timedelta(seconds=wait_for + 1)).isoformat(),
                            last_error=f"Flood wait {wait_for}s",
                            error_delta=1,
                        )
                        await asyncio.sleep(wait_for + 1)
                    except Exception:
                        await self.event_handler._upsert_sync_state(
                            owner_id=user_id,
                            chat_id=getattr(dialog, "id", None),
                            sync_state="error",
                            next_sync_after=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
                            last_error="dialog sync failed",
                            error_delta=1,
                        )
                        logger.exception("[DIALOG SYNC] dialog sync failed for user %s dialog %s", user_id, getattr(dialog, "id", None))
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_session_terminated_error(exc) or not self.storage.is_valid(user_id):
                    logger.info("[DIALOG SYNC] stopping for user %s: %s", user_id, type(exc).__name__)
                    return
                logger.exception("[DIALOG SYNC] loop failed for user %s", user_id)

            await asyncio.sleep(10)

    async def _story_poller(self, user_id: int):
        """Периодически проверяет появление новых историй у доступных диалогов."""
        client = self.watched_clients.get(user_id)
        if not client:
            return

        logger.info(f"[STORY POLLER] started polling stories for user {user_id}")

        while True:
            try:
                result = await client(functions.stories.GetAllStoriesRequest(hidden=False))

                for peer_stories in getattr(result, 'stories', []):
                    peer_id = utils.get_peer_id(peer_stories.peer)
                    for story in getattr(peer_stories, 'stories', []):
                        story_id = story.id
                        if story_id in self.seen_story_ids[user_id]:
                            continue

                        self.seen_story_ids[user_id].add(story_id)
                        await self._process_new_story(user_id, story, peer_id, client)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if _is_session_terminated_error(e) or not self.storage.is_valid(user_id):
                    logger.info("[STORY POLLER] stopping for user %s: %s", user_id, type(e).__name__)
                    break
                logger.warning(f"[STORY POLLER] polling failed for user {user_id}: {e}")

            await asyncio.sleep(60)

    async def _process_new_story(self, owner_id: int, story, peer_id: int, client):
        """Обработка одной новой сторис из поллинга с правильной дедупликацией"""
        try:
            sender = await client.get_entity(peer_id)
        except Exception as e:
            logger.warning(f"[STORY] Не удалось получить отправителя {peer_id}: {e}")
            sender = None
        
        sender_name = utils.get_display_name(sender) if sender else f"User {peer_id}"
        sender_username = getattr(sender, 'username', None)
        sender_id = getattr(sender, 'id', None) if sender else peer_id

        media_path = None
        if story.media and CONFIG.download_media:
            try:
                ext = ".jpg" if isinstance(story.media, types.Photo) else ".mp4"
                ts = int(time.time() * 1000)
                fname = f"story_{owner_id}_{story.id}_{ts}{ext}"
                path = os.path.join(CONFIG.media_dir, fname)

                await asyncio.wait_for(client.download_media(story.media, file=path), timeout=25.0)

                if os.path.exists(path) and os.path.getsize(path) > 500:
                    media_path = path
                    logger.debug(f"[STORY] Скачана сторис из поллинга → {path}")
            except asyncio.TimeoutError:
                logger.warning(f"[STORY] Таймаут скачивания из поллинга для {owner_id}/{story.id}")
            except Exception as e:
                logger.warning(f"[STORY] Скачивание из поллинга не удалось: {e}")

        now_iso = datetime.now(timezone.utc).isoformat()
        
        try:
            # Проверяем, не сохраняли ли мы эту сторис уже
            existing = await self.event_handler.db.fetchone(
                "SELECT id FROM stories WHERE owner_id = ? AND story_id = ?",
                (owner_id, story.id)
            )
            
            if existing:
                logger.debug(f"[STORY] Сторис {story.id} уже сохранена, пропускаем")
                return
            
            # Вставляем новую
            await self.event_handler.db.execute("""
                INSERT INTO stories 
                (owner_id, peer_id, story_id, sender_id, sender_name, sender_username, caption, media_path, posted_at, added_at, content_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                owner_id, peer_id, story.id, sender_id, sender_name,
                sender_username,
                getattr(story, 'caption', None) or "", 
                media_path,
                story.date.isoformat() if hasattr(story, 'date') else now_iso,
                now_iso,
                "📷 Story" if media_path and media_path.endswith(('.jpg','.png')) else "🎥 Story Video"
            ))
            await self.event_handler.db.conn.commit()
            logger.info(f"[STORY] Сохранена сторис {story.id} от {sender_name}")
        except Exception as e:
            logger.error(f"[STORY] Ошибка сохранения в БД: {e}")
            return

        payload = {
            "event_type": "story",
            "story_id": story.id,
            "text": getattr(story, 'caption', "") or "",
            "media_path": media_path,
            "sender_username": sender_name,
            "sender_id": sender_id,
            "chat_title": f"Сторис от {sender_name}",
            "message_date": now_iso,
            "content_type": "📖 Story",
        }
        
        try:
            await self.event_handler.aggregator.add_event(owner_id, payload)
            logger.info(f"[STORY] Сторис {story.id} отправлена пользователю")
        except Exception as e:
            logger.error(f"[STORY] Ошибка отправки сторис {story.id}: {e}")


class AuthFlow:
    def __init__(self, storage: Any, db: Any, watcher_service: WatcherService, config: Any, api_id: int, api_hash: str, bot_app: Any):
        self.storage = storage
        self.db = db
        self.watcher_service = watcher_service
        self.config = config
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_app = bot_app
        self.tmp_clients: Dict[int, TelegramClient] = {}
        self.tmp_prefixes: Dict[int, str] = {}
        self.tmp_auth_context: Dict[int, Dict[str, Any]] = {}
        self.auth_service_messages: Dict[int, List[int]] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_interval_sec = 60 * 60
        self._tmp_ttl_sec = 20 * 60
        self._auth_locks: Dict[int, asyncio.Lock] = {}

    def track_auth_message(self, user_id: int, msg_id: int) -> None:
        self.auth_service_messages.setdefault(user_id, []).append(msg_id)

    async def cleanup_auth_ui(self, user_id: int) -> None:
        msgs = self.auth_service_messages.pop(user_id, [])
        for mid in msgs:
            try:
                await self.bot_app.bot.delete_message(chat_id=user_id, message_id=mid)
            except Exception:
                pass

    def get_user_auth_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._auth_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._auth_locks[user_id] = lock
        return lock

    async def create_tmp_client(self, user_id: int) -> Tuple[TelegramClient, str]:
        """
        РЎРѕР·РґР°С‘С‚ РІСЂРµРјРµРЅРЅС‹Р№ РєР»РёРµРЅС‚ РўРћР›Р¬РљРћ РґР»СЏ РѕС‚РїСЂР°РІРєРё РєРѕРґР° Рё sign_in.
        РСЃРїРѕР»СЊР·СѓРµРј connect() РІРјРµСЃС‚Рѕ start(), С‡С‚РѕР±С‹ РёР·Р±РµР¶Р°С‚СЊ РёРЅС‚РµСЂР°РєС‚РёРІРЅРѕРіРѕ РІРІРѕРґР°.
        """
        if user_id in self.tmp_clients or user_id in self.tmp_prefixes:
            await self.cleanup_tmp(user_id)
        await self.purge_user_tmp_files(user_id)

        prefix = os.path.join(self.config.sessions_dir, f"tmp_{user_id}_{uuid.uuid4().hex}")

        client = TelegramClient(
            prefix,
            self.api_id,
            self.api_hash,
            connection_retries=3,
            retry_delay=2,
            request_retries=3,
            flood_sleep_threshold=60,
            timeout=20,
        )

        logger.info("[AUTH] Creating temporary client for uid=%s, prefix=%s", user_id, prefix)

        try:
            # Подключаемся без авторизации и без интерактивного ввода
            await client.connect()

            # is_connected() почти всегда синхронный → без await
            if not client.is_connected():
                raise ConnectionError("Не удалось установить соединение с Telegram")

            # is_user_authorized() в новых версиях может быть async, в старых — sync
            # Пробуем безопасно
            try:
                authorized = await client.is_user_authorized()
            except TypeError:  # если вдруг не корутина
                authorized = client.is_user_authorized()

            logger.debug("[AUTH] Tmp client for %s: connected=%s, authorized=%s",
                         user_id, client.is_connected(), authorized)

            self.tmp_clients[user_id] = client
            self.tmp_prefixes[user_id] = prefix

            logger.info("[AUTH] Temporary client готов (non-interactive) для uid=%s", user_id)
            return client, prefix

        except Exception as e:
            logger.exception("[AUTH] Ошибка при создании/подключении tmp клиента для %s: %s", user_id, e)
            # Безопасная очистка
            if 'client' in locals():
                try:
                    await client.disconnect()
                except Exception:
                    pass
            raise


    async def get_or_create_tmp_client(self, user_id: int, *, reuse_existing: bool = True) -> Tuple[TelegramClient, str]:
        if reuse_existing:
            client = self.tmp_clients.get(user_id)
            prefix = self.tmp_prefixes.get(user_id)
            if client and prefix:
                try:
                    if not client.is_connected():
                        await client.connect()
                    if client.is_connected():
                        return client, prefix
                except Exception:
                    logger.warning("[AUTH] Existing tmp client unusable for uid=%s, recreating", user_id, exc_info=True)
                    await self.cleanup_tmp(user_id)
            elif prefix:
                restored_client = TelegramClient(
                    prefix,
                    self.api_id,
                    self.api_hash,
                    connection_retries=3,
                    retry_delay=2,
                    request_retries=3,
                    flood_sleep_threshold=60,
                    timeout=20,
                )
                try:
                    await restored_client.connect()
                    if restored_client.is_connected():
                        self.tmp_clients[user_id] = restored_client
                        logger.info("[AUTH] Restored tmp client from prefix for uid=%s", user_id)
                        return restored_client, prefix
                    await restored_client.disconnect()
                except Exception:
                    logger.warning("[AUTH] Failed to restore tmp client for uid=%s, recreating", user_id, exc_info=True)
                    try:
                        await restored_client.disconnect()
                    except Exception:
                        pass

        return await self.create_tmp_client(user_id)

    def tmp_prefix_has_files(self, prefix: str) -> bool:
        if not prefix:
            return False
        try:
            candidates = [prefix, os.path.abspath(prefix)]
            for item in candidates:
                if any(Path(p).exists() for p in glob.glob(item + "*")):
                    return True
            return False
        except Exception:
            return False

    def cache_auth_context(
        self,
        user_id: int,
        *,
        phone: Optional[str],
        phone_code_hash: Optional[str],
        tmp_prefix: Optional[str],
    ) -> None:
        self.tmp_auth_context[user_id] = {
            "phone": str(phone or "").strip(),
            "phone_code_hash": str(phone_code_hash or "").strip(),
            "tmp_prefix": str(tmp_prefix or "").strip(),
            "created_at": time.time(),
        }
        logger.debug(
            "[AUTH] Cached auth context uid=%s phone=%s hash=%s prefix=%s",
            user_id,
            bool(phone),
            bool(phone_code_hash),
            tmp_prefix or "<none>",
        )

    def get_cached_auth_context(self, user_id: int) -> Dict[str, Any]:
        return dict(self.tmp_auth_context.get(user_id) or {})

    def clear_cached_auth_context(self, user_id: int) -> None:
        self.tmp_auth_context.pop(user_id, None)

    async def purge_user_tmp_files(self, user_id: int, *, keep_prefix: Optional[str] = None) -> int:
        removed = 0
        mask = os.path.join(self.config.sessions_dir, f"tmp_{user_id}_*")
        for p in glob.glob(mask):
            if keep_prefix and (p == keep_prefix or p.startswith(keep_prefix)):
                continue
            try:
                os.remove(p)
                removed += 1
                logger.debug("[AUTH] Removed stale tmp session file for uid=%s: %s", user_id, p)
            except FileNotFoundError:
                continue
            except Exception:
                logger.warning("[AUTH] Failed to remove stale tmp session file for uid=%s: %s", user_id, p, exc_info=True)
        if removed:
            logger.info("[AUTH] Purged stale tmp artifacts for uid=%s, removed=%s", user_id, removed)
        return removed

    async def _cleanup_orphaned_tmp_files(self) -> int:
        removed = 0
        now_ts = time.time()
        known_prefixes = set(self.tmp_prefixes.values())
        try:
            rows = await self.db.fetchall(
                "SELECT user_id, state, tmp_prefix, updated_at FROM auth_state WHERE tmp_prefix IS NOT NULL AND tmp_prefix != ''",
                (),
            )
        except Exception:
            logger.warning("[AUTH] Failed reading auth_state while scanning tmp sessions", exc_info=True)
            rows = []
        active_prefixes: set[str] = set()
        for row in rows:
            state = str(row[1] or "")
            prefix = str(row[2] or "").strip()
            updated_raw = str(row[3] or "")
            if not prefix:
                continue
            is_fresh = False
            try:
                is_fresh = (now_ts - datetime.fromisoformat(updated_raw).timestamp()) <= self._tmp_ttl_sec
            except Exception:
                pass
            if state in {AuthState.CODE_SENT, AuthState.WAIT_2FA} and is_fresh:
                active_prefixes.add(prefix)

        for path in glob.glob(os.path.join(self.config.sessions_dir, "tmp_*")):
            candidate = path[:-8] if path.endswith(".session") else path
            if candidate in known_prefixes or candidate in active_prefixes:
                continue
            stale_by_mtime = False
            try:
                stale_by_mtime = (now_ts - os.path.getmtime(path)) > self._tmp_ttl_sec
            except Exception:
                stale_by_mtime = True
            if not stale_by_mtime:
                continue
            try:
                os.remove(path)
                removed += 1
                logger.info("[AUTH] Hourly cleaner removed orphan tmp session: %s", path)
            except FileNotFoundError:
                continue
            except Exception:
                logger.warning("[AUTH] Hourly cleaner failed to remove tmp file: %s", path, exc_info=True)
        return removed

    async def _reset_stale_auth_states(self) -> int:
        reset_count = 0
        now_ts = time.time()
        try:
            rows = await self.db.fetchall(
                "SELECT user_id, state, tmp_prefix, updated_at, expires_at FROM auth_state WHERE state IN (?, ?)",
                (AuthState.CODE_SENT, AuthState.WAIT_2FA),
            )
        except Exception:
            logger.warning("[AUTH] Failed reading stale auth states", exc_info=True)
            return 0

        for row in rows:
            user_id = int(row[0])
            state = str(row[1] or "")
            tmp_prefix = str(row[2] or "").strip()
            updated_raw = str(row[3] or "")
            expires_at_raw = row[4]
            age_sec = None
            try:
                age_sec = now_ts - datetime.fromisoformat(updated_raw).timestamp()
            except Exception:
                age_sec = self._tmp_ttl_sec + 1
            has_tmp_files = bool(tmp_prefix and self.tmp_prefix_has_files(tmp_prefix))
            try:
                expires_at = float(expires_at_raw or 0)
            except Exception:
                expires_at = 0.0
            is_expired = bool(expires_at and now_ts > expires_at)
            if (not is_expired) and age_sec <= self._tmp_ttl_sec:
                continue
            await self.cleanup_tmp(user_id)
            await set_state(
                self.db,
                user_id,
                AuthState.WAIT_PHONE,
                tmp_prefix=None,
                phone_code_hash=None,
                expires_at=None,
                auth_fail_count=0,
            )
            reset_count += 1
            logger.info(
                "[AUTH] Hourly cleaner reset stale auth state uid=%s state=%s age=%s has_tmp=%s expired=%s",
                user_id,
                state,
                int(age_sec or 0),
                has_tmp_files,
                is_expired,
            )
        return reset_count

    async def _cleanup_invalid_persistent_sessions(self) -> int:
        removed = 0
        checked = 0
        candidates = set(glob.glob(os.path.join(self.config.sessions_dir, "*.session.zip")))
        candidates.update(
            glob.glob(
                os.path.join(self.config.logs_dir, AUTH_LOGS_SUBDIR, "user_*", "*.session.zip")
            )
        )
        for path in sorted(candidates):
            checked += 1
            is_valid = await self.storage.is_zip_session_valid(Path(path))
            if is_valid:
                continue
            try:
                os.remove(path)
                removed += 1
                logger.warning("[AUTH] Hourly cleaner removed invalid session zip: %s", path)
            except Exception:
                logger.warning("[AUTH] Failed to remove invalid session zip: %s", path, exc_info=True)
        logger.info("[AUTH] Hourly cleaner checked persistent sessions: checked=%s removed=%s", checked, removed)
        return removed

    async def session_housekeeping_once(self, *, trigger: str) -> None:
        logger.info("[AUTH] Session housekeeping started (trigger=%s)", trigger)
        reset_states = await self._reset_stale_auth_states()
        removed_tmp = await self._cleanup_orphaned_tmp_files()
        removed_persistent = await self._cleanup_invalid_persistent_sessions()
        logger.info(
            "[AUTH] Session housekeeping finished (trigger=%s, reset_states=%s, removed_tmp=%s, removed_persistent=%s)",
            trigger,
            reset_states,
            removed_tmp,
            removed_persistent,
        )

    async def get_session_health_snapshot(self) -> Dict[str, int]:
        tmp_files = glob.glob(os.path.join(self.config.sessions_dir, "tmp_*"))
        zip_main = glob.glob(os.path.join(self.config.sessions_dir, "*.session.zip"))
        zip_logs = glob.glob(os.path.join(self.config.logs_dir, AUTH_LOGS_SUBDIR, "user_*", "*.session.zip"))
        try:
            stale_rows = await self.db.fetchall(
                "SELECT COUNT(1) FROM auth_state WHERE state IN (?, ?)",
                (AuthState.CODE_SENT, AuthState.WAIT_2FA),
            )
            stale_states = int(stale_rows[0][0]) if stale_rows else 0
        except Exception:
            stale_states = 0
        return {
            "tmp_files": len(tmp_files),
            "session_zip_main": len(zip_main),
            "session_zip_logs": len(zip_logs),
            "auth_states_pending": stale_states,
            "active_tmp_clients": len(self.tmp_clients),
        }

    async def _session_housekeeping_loop(self) -> None:
        while True:
            try:
                await self.session_housekeeping_once(trigger="hourly")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[AUTH] Session housekeeping loop failed")
            await asyncio.sleep(self._cleanup_interval_sec)

    def start_housekeeping(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._session_housekeeping_loop())
        logger.info("[AUTH] Session housekeeping loop started")

    async def stop_housekeeping(self) -> None:
        if not self._cleanup_task:
            return
        task = self._cleanup_task
        self._cleanup_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def finalize_session(self, user_id: int) -> None:
        prefix = self.tmp_prefixes.get(user_id)
        if not prefix:
            logger.warning("[AUTH] finalize_session: нет prefix для uid=%s", user_id)
            return

        client = self.tmp_clients.get(user_id)
        is_authorized = False

        try:
            if client:
                # Безопасная проверка авторизации (может быть sync или async)
                try:
                    is_authorized = await client.is_user_authorized()
                except TypeError:
                    is_authorized = client.is_user_authorized()
        except Exception:
            logger.exception("[AUTH] Ошибка проверки авторизации tmp клиента uid=%s", user_id)

        # Если не авторизован — финальная проверка через start()
        if not is_authorized:
            logger.info("[AUTH] Tmp client не авторизован, пробуем start() для проверки uid=%s", user_id)
            try:
                tmp_client = TelegramClient(prefix, self.api_id, self.api_hash)
                await tmp_client.start()
                try:
                    is_authorized = await tmp_client.is_user_authorized()
                except TypeError:
                    is_authorized = tmp_client.is_user_authorized()
                await tmp_client.disconnect()
            except Exception as e:
                logger.exception("[AUTH] Финальная проверка start() провалилась для uid=%s: %s", user_id, e)
                is_authorized = False

        if not is_authorized:
            logger.warning("[AUTH] Сессия не авторизована после всех попыток uid=%s", user_id)
            await self.cleanup_tmp(user_id)
            asyncio.create_task(self.cleanup_auth_ui(user_id))
            return

        try:
            await self.storage.save(user_id, prefix)
            log_auth_attempt(
                user_id=user_id,
                username=None,
                text=str(self.storage._save_zip_path(user_id)),
                state="SESSION_SAVED",
                meta="path",
                result="OK"
            )
            logger.info("[AUTH] Сессия успешно сохранена для uid=%s", user_id)
        except Exception:
            logger.exception("[AUTH] Ошибка сохранения сессии для uid=%s", user_id)

        finally:
            # Чистим временные данные
            client = self.tmp_clients.pop(user_id, None)
            if client:
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception:
                    pass

            if prefix:
                for p in glob.glob(prefix + "*"):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

            self.tmp_prefixes.pop(user_id, None)

        # Запускаем watcher
        try:
            await self.watcher_service._ensure_async(user_id)
            logger.info("[AUTH] Watcher запущен для uid=%s", user_id)
        except Exception:
            logger.exception("[AUTH] Ошибка запуска watcher после авторизации uid=%s", user_id)

        asyncio.create_task(self.cleanup_auth_ui(user_id))

    async def cleanup_tmp(self, user_id: int) -> None:
        client = self.tmp_clients.pop(user_id, None)
        prefix = self.tmp_prefixes.pop(user_id, None)
        self.clear_cached_auth_context(user_id)

        if client:
            try:
                # is_connected() обычно синхронный
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

        if prefix:
            for p in glob.glob(prefix + "*"):
                try:
                    os.remove(p)
                except Exception:
                    pass

        logger.debug("[AUTH] Временные данные очищены для uid=%s", user_id)

# ----------------------------
# Orchestrator App (rewritten)
# ----------------------------
class App:
    """
    Central application object: wires together DB, session storage, aggregator,
    event handler, watcher service and auth flow.
    """
    def __init__(self, config: Config, bot_application: Any):
        self.config = config
        self.bot_app = bot_application

        # core services
        self.db = Database(config.db_path, config)
        self.storage = SessionStorage(config.sessions_dir, config.api_id, config.api_hash, logs_dir=config.logs_dir)

        # Stable sequential delivery for delete/edit bursts.
        self.aggregator = DeleteAggregator(bot_application, self.db, workers=1)

        # event handler (without watcher_service first)
        self.event_handler = EventHandler(self.db, self.aggregator, config)
        self.watcher_service = WatcherService(self.storage, self.event_handler, config, config.api_id, config.api_hash, bot_application)
        
        # Now inject watcher_service into event_handler
        self.event_handler.watcher_service = self.watcher_service
        self.aggregator.watcher_service = self.watcher_service

        # auth/session flow
        self.auth = AuthFlow(self.storage, self.db, self.watcher_service, config, config.api_id, config.api_hash, bot_application)

    async def start(self) -> None:
        """Connect DB and start background workers. Watchers are restored separately in post_init."""
        try:
            await self.db.connect()
        except Exception:
            logger.exception("App.start: failed to connect DB")
            raise

        # start event handler / aggregator workers
        try:
            await self.event_handler.start_workers()
        except Exception:
            logger.exception("App.start: failed to start event handler workers")
            raise

        logger.info("App started: DB connected and workers running")
        try:
            await self.auth.session_housekeeping_once(trigger="startup")
        except Exception:
            logger.exception("App.start: failed initial auth session housekeeping")
        self.auth.start_housekeeping()

    async def stop(self) -> None:
        """Gracefully stop watchers, background workers and database resources."""
        try:
            await self.auth.stop_housekeeping()
        except Exception:
            logger.exception("App.stop: failed to stop auth session housekeeping")
        try:
            await self.watcher_service.stop_all()
        except Exception:
            logger.exception("App.stop: failed to stop watcher service")

        try:
            await self.event_handler.stop_workers()
        except Exception:
            logger.exception("App.stop: failed to stop event workers")

        try:
            await self.db.close()
        except Exception:
            logger.exception("App.stop: failed to close database")
