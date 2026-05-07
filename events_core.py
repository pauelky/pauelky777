from .shared import *
import sqlite3
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageStat


def _short_text(value: Any, max_len: int) -> str:
    text = repair_mojibake(value or "").replace("\x00", "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "..."


def _is_minor_edit(original_text: Optional[str], new_text: Optional[str]) -> bool:
    """Simple guard to avoid notifying on trivial edit noise."""
    if not original_text or not new_text:
        return False
    a = str(original_text or "").strip()
    b = str(new_text or "").strip()
    return a == b


class DeleteAggregator:
    """
    Aggregator that forwards saved/edited/deleted message payloads to bot owners.
    """

    def __init__(self, bot_app, db, *, workers: int = 2, watcher_service: Any = None):
        self.bot = bot_app.bot
        self.db = db
        self.watcher_service = watcher_service
        self.send_queue: asyncio.Queue = asyncio.Queue()
        self.workers = max(1, int(workers))
        self._workers_tasks: List[asyncio.Task] = []
        self._stopping = False
        self._avatar_cache: Dict[Tuple[int, str], Tuple[float, Optional[Tuple[bytes, str]]]] = {}
        self._avatar_cache_ttl = 15 * 60.0

    async def start_workers(self) -> None:
        if self._workers_tasks:
            return
        self._stopping = False
        for _ in range(self.workers):
            t = asyncio.create_task(self._send_worker())
            self._workers_tasks.append(t)
        try:
            logger.info("DeleteAggregator: started %d workers", len(self._workers_tasks))
        except Exception:
            pass

    async def stop_workers(self) -> None:
        self._stopping = True
        for t in list(self._workers_tasks):
            t.cancel()
        for t in list(self._workers_tasks):
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                try:
                    logger.exception("DeleteAggregator worker shutdown error")
                except Exception:
                    pass
        self._workers_tasks = []
        try:
            logger.info("DeleteAggregator: stopped workers")
        except Exception:
            pass

    async def add_event(self, owner_id: int, payload: Dict[str, Any]) -> None:
        # Guard: only queue events for known bot users (by owner_id).
        if not owner_id or owner_id <= 0:
            logger.debug("DeleteAggregator.add_event: invalid owner_id=%s", owner_id)
            return
        row = await self.db.fetchone("SELECT 1 FROM bot_users WHERE user_id = ?", (owner_id,))
        if not row:
            logger.warning("DeleteAggregator.add_event: skipping unknown owner_id=%s", owner_id)
            return
        await self.send_queue.put((owner_id, payload))

    async def _send_worker(self) -> None:
        while not self._stopping:
            try:
                owner_id, payload = await self.send_queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._send_single_with_retry(owner_id, payload)
            except Exception:
                try:
                    logger.exception("DeleteAggregator worker failed to send payload")
                except Exception:
                    pass
            finally:
                try:
                    # Keep delivery smooth and prevent message bursts on mass deletions.
                    if not self._stopping:
                        await asyncio.sleep(1.0)
                except Exception:
                    pass
                try:
                    self.send_queue.task_done()
                except Exception:
                    pass
        while not self.send_queue.empty():
            try:
                self.send_queue.get_nowait()
                self.send_queue.task_done()
            except Exception:
                break

    async def _send_single_with_retry(self, owner_id: int, payload: Dict[str, Any], *, max_attempts: int = 6):
        """
        Send message with retry logic and pre-flight checks.
        Pre-flight checks ensure we don't waste attempts on impossible cases.
        """
        # ─── PRE-FLIGHT CHECKS (before any retry attempt) ───
        
        # 1. Check if user exists and is banned, strong owner filter.
        try:
            user_row = await self.db.fetchone(
                "SELECT banned FROM bot_users WHERE user_id=?",
                (owner_id,)
            )
            if not user_row:
                logger.warning("DeleteAggregator: owner_id %s not found in bot_users, skipping send", owner_id)
                return
            if user_row[0]:  # User is banned
                logger.debug("User %s is banned, skipping send", owner_id)
                return
        except Exception:
            logger.debug("Could not check bot_users status for user %s", owner_id)
            # Continue anyway (should not happen, but we have fallback below)
        
        # 2. Check if chat is muted (before sending anything)
        chat_id = payload.get("chat_id")
        if chat_id:
            try:
                is_muted = await self.db.fetchone(
                    "SELECT 1 FROM muted_chats WHERE owner_id=? AND chat_id=?",
                    (owner_id, chat_id)
                )
                if is_muted:
                    logger.debug("Chat %s is muted for user %s, skipping send", chat_id, owner_id)
                    return
            except Exception:
                logger.debug("Could not check mute status for user %s / chat %s", owner_id, chat_id)
        
        # ─── RETRY LOOP ───
        attempt = 0
        base_delay = 0.6
        while True:
            attempt += 1
            try:
                return await self._send_single(owner_id, payload)
            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 1.0)) + 0.3
                try:
                    logger.warning("DeleteAggregator: RetryAfter, sleeping %.1fs (attempt %d)", wait, attempt)
                except Exception:
                    pass
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError) as e:
                if attempt >= max_attempts:
                    try:
                        logger.exception("DeleteAggregator: Network error and max attempts reached")
                    except Exception:
                        pass
                    raise
                sleep_for = min(base_delay * (2 ** (attempt - 1)), 10.0)
                await asyncio.sleep(sleep_for)
            except Exception:
                if attempt >= max_attempts:
                    try:
                        logger.exception("DeleteAggregator: Unexpected error and max attempts reached")
                    except Exception:
                        pass
                    raise
                sleep_for = min(base_delay * (2 ** (attempt - 1)), 10.0)
                await asyncio.sleep(sleep_for)

    async def _read_file_bytes(self, path: str) -> bytes:
        loop = asyncio.get_running_loop()
        def _read():
            with open(path, "rb") as f:
                return f.read()
        return await loop.run_in_executor(None, _read)

    async def _resolve_sender_entity(self, client: Any, sender_id: Optional[int], sender_username: str) -> Optional[Any]:
        candidates: List[Any] = []
        if sender_id:
            try:
                sender_id_int = int(sender_id)
                candidates.extend([sender_id_int, types.PeerUser(sender_id_int)])
            except Exception:
                pass
        if sender_username:
            username = sender_username.strip().lstrip("@")
            if username:
                candidates.extend([username, f"@{username}"])

        seen: set = set()
        for candidate in candidates:
            marker = repr(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            try:
                entity = await asyncio.wait_for(client.get_entity(candidate), timeout=8.0)
                if entity is not None:
                    return entity
            except Exception:
                continue
        return None

    def _load_avatar_image(self, avatar_bytes: bytes) -> Optional[Image.Image]:
        try:
            with Image.open(io.BytesIO(avatar_bytes)) as src:
                img = ImageOps.exif_transpose(src)
                has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
                if has_alpha:
                    rgba = img.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    img = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    img = img.convert("RGB")
                return img.copy()
        except Exception as e:
            logger.debug("DeleteAggregator: avatar decode failed: %s", e)
            return None

    def _serialize_avatar_image(self, avatar_img: Image.Image) -> bytes:
        out = io.BytesIO()
        avatar_img.save(out, format="JPEG", quality=92, optimize=True)
        return out.getvalue()

    def _avatar_looks_invalid(self, avatar_img: Image.Image) -> bool:
        probe = avatar_img.convert("RGB").resize((48, 48), Image.Resampling.BILINEAR)
        stat = ImageStat.Stat(probe)
        mean = sum(stat.mean) / max(1, len(stat.mean))
        stddev = sum(stat.stddev) / max(1, len(stat.stddev))
        extrema = probe.getextrema()
        dynamic_range = sum((mx - mn) for mn, mx in extrema)

        if mean <= 6 and stddev <= 3:
            return True
        if dynamic_range <= 18 and mean <= 45:
            return True
        return False

    def _get_card_font(self, size: int, *, bold: bool = False):
        candidates: List[str] = []
        if os.name == "nt":
            candidates.extend([
                r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
                r"C:\Windows\Fonts\seguisb.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
                r"C:\Windows\Fonts\tahomabd.ttf" if bold else r"C:\Windows\Fonts\tahoma.ttf",
            ])
        candidates.extend([
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        ])
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _build_placeholder_avatar(self, size: int, avatar_label: str) -> Image.Image:
        label = (avatar_label or "").strip()
        parts = [p for p in re.split(r"[^A-Za-zА-Яа-яЁё0-9]+", label) if p]
        initials = "".join(part[0] for part in parts[:2]).upper()
        if not initials:
            initials = "?"

        palette = [
            ((83, 109, 254), (121, 134, 255)),
            ((16, 185, 129), (52, 211, 153)),
            ((245, 158, 11), (251, 191, 36)),
            ((239, 68, 68), (248, 113, 113)),
            ((14, 165, 233), (56, 189, 248)),
        ]
        palette_idx = sum(ord(ch) for ch in label) % len(palette)
        top_color, bottom_color = palette[palette_idx]

        img = Image.new("RGB", (size, size), top_color)
        draw = ImageDraw.Draw(img)
        for y in range(size):
            ratio = y / max(1, size - 1)
            color = tuple(
                int(top_color[i] * (1.0 - ratio) + bottom_color[i] * ratio)
                for i in range(3)
            )
            draw.line((0, y, size, y), fill=color)

        halo_pad = max(4, size // 14)
        draw.ellipse(
            (halo_pad, halo_pad, size - halo_pad, size - halo_pad),
            outline=(255, 255, 255),
            width=max(3, size // 42),
        )

        font = self._get_card_font(max(24, int(size * 0.42)), bold=True)
        bbox = draw.textbbox((0, 0), initials, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        text_x = (size - text_w) / 2 - bbox[0]
        text_y = (size - text_h) / 2 - bbox[1]
        draw.text((text_x, text_y), initials, font=font, fill=(255, 255, 255))
        return img

    def _render_meta_card(self, avatar_bytes: Optional[bytes], avatar_label: str, title: str, lines: List[str]) -> Optional[bytes]:
        try:
            title_font = self._get_card_font(44, bold=True)
            line_font = self._get_card_font(28, bold=False)
            width = 1080
            outer_pad = 36
            inner_pad = 42
            avatar_size = 168
            gap = 28

            avatar_img: Optional[Image.Image] = None
            if avatar_bytes:
                avatar_img = self._load_avatar_image(avatar_bytes)
                if avatar_img is not None and self._avatar_looks_invalid(avatar_img):
                    logger.warning("DeleteAggregator: suspicious avatar detected for %s, using placeholder", avatar_label)
                    avatar_img = None

            if avatar_img is None:
                avatar_img = self._build_placeholder_avatar(avatar_size, avatar_label)
            else:
                avatar_img = ImageOps.fit(avatar_img, (avatar_size, avatar_size), method=Image.Resampling.LANCZOS)

            mask = Image.new("L", (avatar_size, avatar_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)

            canvas = Image.new("RGB", (width, 1200), (245, 247, 250))
            draw = ImageDraw.Draw(canvas)
            x0 = outer_pad
            y0 = outer_pad
            x1 = width - outer_pad
            text_x = x0 + inner_pad + avatar_size + gap
            content_width = x1 - inner_pad - text_x

            def wrap_text(value: str, font) -> List[str]:
                value = (value or "").strip()
                if not value:
                    return []
                words = value.split()
                if not words:
                    return [value]
                lines_out: List[str] = []
                current = words[0]
                for word in words[1:]:
                    probe = f"{current} {word}"
                    if draw.textlength(probe, font=font) <= content_width:
                        current = probe
                    else:
                        lines_out.append(current)
                        current = word
                lines_out.append(current)
                normalized: List[str] = []
                for item in lines_out:
                    if draw.textlength(item, font=font) <= content_width:
                        normalized.append(item)
                        continue
                    chunk = ""
                    for char in item:
                        probe = chunk + char
                        if chunk and draw.textlength(probe, font=font) > content_width:
                            normalized.append(chunk)
                            chunk = char
                        else:
                            chunk = probe
                    if chunk:
                        normalized.append(chunk)
                return normalized

            title_lines = wrap_text(title, title_font)
            body_lines: List[str] = []
            for line in lines:
                body_lines.extend(wrap_text(line, line_font))

            line_height = 40
            title_height = 58
            content_height = inner_pad * 2
            content_height += max(avatar_size, len(title_lines) * title_height + 18 + len(body_lines) * line_height)
            card_height = content_height + outer_pad * 2

            canvas = Image.new("RGB", (width, card_height), (245, 247, 250))
            draw = ImageDraw.Draw(canvas)
            draw.rounded_rectangle((x0, y0, x1, card_height - outer_pad), radius=34, fill=(255, 255, 255), outline=(225, 229, 236), width=2)

            avatar_x = x0 + inner_pad
            avatar_y = y0 + inner_pad
            canvas.paste(avatar_img, (avatar_x, avatar_y), mask)
            draw.ellipse((avatar_x, avatar_y, avatar_x + avatar_size - 1, avatar_y + avatar_size - 1), outline=(222, 226, 232), width=3)

            current_y = y0 + inner_pad
            for idx, line in enumerate(title_lines):
                draw.text((text_x, current_y), line, font=title_font, fill=(18, 23, 33))
                current_y += title_height
                if idx == len(title_lines) - 1:
                    current_y += 8

            for line in body_lines:
                draw.text((text_x, current_y), line, font=line_font, fill=(70, 78, 92))
                current_y += line_height

            out = io.BytesIO()
            canvas.save(out, format="JPEG", quality=93, optimize=True)
            return out.getvalue()
        except Exception as e:
            logger.debug("DeleteAggregator: meta card render failed: %s", e)
            return None

    async def _get_sender_avatar(self, owner_id: int, sender_id: Optional[int], sender_username: str) -> Optional[Tuple[bytes, str]]:
        cache_key_base = str(sender_id) if sender_id else sender_username.strip().lstrip("@").lower()
        if not cache_key_base:
            return None

        now_ts = time.monotonic()
        cache_key = (owner_id, cache_key_base)
        cached = self._avatar_cache.get(cache_key)
        if cached and cached[0] > now_ts:
            return cached[1]

        client = getattr(self.watcher_service, "watched_clients", {}).get(owner_id) if self.watcher_service else None
        if not client:
            self._avatar_cache[cache_key] = (now_ts + 30.0, None)
            return None

        entity = await self._resolve_sender_entity(client, sender_id, sender_username)
        if entity is None:
            self._avatar_cache[cache_key] = (now_ts + 60.0, None)
            return None

        avatar_bytes: Optional[bytes] = None
        try:
            photos = await asyncio.wait_for(client.get_profile_photos(entity, limit=1), timeout=12.0)
            photo = photos[0] if photos else None
            if photo is not None:
                for thumb in (0, None):
                    try:
                        kwargs = {"thumb": thumb} if thumb is not None else {}
                        candidate = await asyncio.wait_for(
                            client.download_media(photo, file=bytes, **kwargs),
                            timeout=12.0,
                        )
                        candidate_img = self._load_avatar_image(candidate) if candidate else None
                        if candidate_img is not None and not self._avatar_looks_invalid(candidate_img):
                            avatar_bytes = self._serialize_avatar_image(candidate_img)
                            break
                    except Exception:
                        continue

            if not avatar_bytes:
                for download_big in (False, True):
                    try:
                        candidate = await asyncio.wait_for(
                            client.download_profile_photo(entity, file=bytes, download_big=download_big),
                            timeout=12.0,
                        )
                        candidate_img = self._load_avatar_image(candidate) if candidate else None
                        if candidate_img is not None and not self._avatar_looks_invalid(candidate_img):
                            avatar_bytes = self._serialize_avatar_image(candidate_img)
                            break
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(
                "DeleteAggregator: could not download avatar for owner=%s sender=%s/%s: %s",
                owner_id,
                sender_id,
                sender_username,
                e,
            )

        if not avatar_bytes:
            self._avatar_cache[cache_key] = (now_ts + 5 * 60.0, None)
            return None

        filename_suffix = str(sender_id or cache_key_base).replace("@", "").replace("/", "_")
        result = (avatar_bytes, f"avatar_{filename_suffix}.jpg")
        self._avatar_cache[cache_key] = (now_ts + self._avatar_cache_ttl, result)
        return result

    async def _send_meta_message(
        self,
        owner_id: int,
        meta_text: str,
        meta_markup: Optional[InlineKeyboardMarkup],
        *,
        sender_id: Optional[int],
        sender_username: str,
        avatar_label: str,
        card_title: str,
        card_lines: List[str],
    ) -> Any:
        avatar = await self._get_sender_avatar(owner_id, sender_id, sender_username)
        avatar_bytes = avatar[0] if avatar else None
        avatar_name = avatar[1] if avatar else "avatar_placeholder.jpg"
        card_bytes = self._render_meta_card(avatar_bytes, avatar_label, card_title, card_lines)
        if card_bytes:
            bio = io.BytesIO(card_bytes)
            bio.name = f"card_{avatar_name}"
            bio.seek(0)
            try:
                return await self.bot.send_photo(
                    chat_id=owner_id,
                    photo=bio,
                    reply_markup=meta_markup,
                )
            finally:
                try:
                    bio.close()
                except Exception:
                    pass

        return await self.bot.send_message(
            chat_id=owner_id,
            text=meta_text,
            parse_mode=ParseMode.HTML,
            reply_markup=meta_markup,
        )

    async def _send_single(self, owner_id: int, item: Dict[str, Any]) -> Optional[Any]:
        media_path: Optional[str] = item.get("media_path")
        text: str = repair_mojibake(item.get("text") or "")[:50000]
        original_text: str = repair_mojibake(item.get("original_text") or "")[:50000]
        edit_count: int = int(item.get("edit_count") or 0)
        event_type: str = str(item.get("event_type") or "deleted").strip().lower()
        sender_id: Optional[int] = item.get("sender_id")

        sender_username_raw = repair_mojibake(str(item.get("sender_username") or "")).strip().lstrip("@")
        sender_username = (
            sender_username_raw if re.fullmatch(r"[A-Za-z0-9_]{3,}", sender_username_raw or "") else ""
        )
        sender = sender_username_raw
        if not sender:
            sender = "ID " + str(sender_id or "")
        chat = repair_mojibake(item.get("chat_title") or "—")
        chat_id = item.get("chat_id")
        chat_username_raw = str(item.get("chat_username") or "").strip().lstrip("@")
        chat_username = chat_username_raw if re.fullmatch(r"[A-Za-z0-9_]{3,}", chat_username_raw or "") else ""
        content_type = repair_mojibake(str(item.get("content_type") or ""))

        ts_del = format_human_timestamp(item.get("deleted_at"), CONFIG.tz_name)
        ts_edit = format_human_timestamp(item.get("edited_at"), CONFIG.tz_name) if item.get("edited_at") else None
        ts_orig = format_human_timestamp(item.get("message_date"), CONFIG.tz_name)

        sender_h = html.escape(str(sender or ""))
        chat_h = html.escape(str(chat or ""))
        text_h = html.escape(text)
        original_h = html.escape(original_text)

        if event_type == "edited":
            title = "✏️ <b>Сообщение изменено</b>"
        elif event_type == "disappearing":
            title = "👻 <b>Исчезающее сообщение</b>"
        elif event_type == "story":
            title = "📸 <b>История сохранена</b>"
        else:
            title = "🗑️ <b>Удалённое сообщение</b>"

        content_type_display = content_type or guess_content_type_from_path(media_path) or "сообщение"
        content_type_h = html.escape(str(content_type_display))

        sender_label = f"@{sender_h}" if sender_h and not str(sender).startswith("ID ") else (sender_h or "неизвестно")
        sender_label_plain = f"@{sender}" if sender and not str(sender).startswith("ID ") else (sender or "неизвестно")
        card_title = html.unescape(re.sub(r"<[^>]+>", "", title))
        card_lines: List[str] = [
            f"От: {sender_label_plain}",
            f"Чат: {chat}",
            f"Время события: {ts_orig}",
            f"Тип: {content_type_display}",
        ]
        meta_lines: List[str] = [
            title,
            "",
            f"👤 <b>От:</b> {sender_label}",
            f"💬 <b>Чат:</b> {chat_h}",
            f"🕒 <b>Время события:</b> {ts_orig}",
            f"📄 <b>Тип:</b> {content_type_h}",
        ]

        if event_type == "deleted":
            meta_lines.append(f"❌ <b>Удалено:</b> {ts_del}")
            card_lines.append(f"Удалено: {ts_del}")
        elif event_type == "edited":
            meta_lines.append(f"✏️ <b>Изменено:</b> {ts_edit or ts_orig}")
            card_lines.append(f"Изменено: {ts_edit or ts_orig}")
            if edit_count > 0:
                meta_lines.append(f"🔁 <b>Изменений:</b> {edit_count}")
                card_lines.append(f"Изменений: {edit_count}")

        views = int(item.get("views") or 0)
        reactions_display = format_reactions_display(item.get("reactions", "{}"))
        if event_type == "deleted" and views > 0:
            meta_lines.append(f"👁️ <b>Просмотров:</b> {views}")
            card_lines.append(f"Просмотров: {views}")
        if event_type == "deleted" and reactions_display:
            meta_lines.append(f"❤️ <b>Реакции:</b> {reactions_display}")
            card_lines.append(f"Реакции: {reactions_display}")
        footer_map = {
            "deleted": "удаленное сообщение",
            "edited": "отредактированное сообщение",
            "disappearing": "исчезающее сообщение",
        }
        footer_text = footer_map.get(event_type, "сообщение")
        meta_lines.extend(["", f"<i>{footer_text}</i>"])

        meta_text = "\n".join(meta_lines)

        buttons: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        target_username = sender_username if event_type in {"deleted", "edited"} and sender_username else chat_username
        if target_username:
            row.append(InlineKeyboardButton("↗️ Перейти в чат", url=f"https://t.me/{target_username}"))
        if event_type in {"deleted", "edited"} and chat_id is not None:
            try:
                row.append(InlineKeyboardButton("🔇 Заглушить чат", callback_data=f"mute_chat:{int(chat_id)}"))
            except Exception:
                pass
        if row:
            buttons.append(row)

        meta_markup = InlineKeyboardMarkup(buttons) if buttons else None

        try:
            log_outgoing_message(owner_id, meta_text)
        except Exception:
            pass

        last_message: Optional[Any] = await self._send_meta_message(
            owner_id,
            meta_text,
            meta_markup,
            sender_id=sender_id,
            sender_username=sender_username_raw,
            avatar_label=sender_label_plain,
            card_title=card_title,
            card_lines=card_lines,
        )

        # Сначала отправляем описание/текст, затем файл.
        details_parts: List[str] = []
        if event_type == "edited" and edit_count > 0 and original_text and original_text != text:
            details_parts.append("📝 <b>Что изменилось</b>")
            details_parts.append("")
            details_parts.append(f"<b>Было:</b>\n{original_h}")
            details_parts.append("")
            details_parts.append(f"<b>Стало:</b>\n{text_h}")
        elif text.strip():
            details_parts.append("📝 <b>Содержимое</b>")
            details_parts.append("")
            details_parts.append(text_h)

        details_text = "\n".join(details_parts).strip()

        if details_text:
            await asyncio.sleep(0.15)
            if len(details_text) <= 3800:
                last_message = await self.bot.send_message(
                    chat_id=owner_id,
                    text=details_text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                compact = details_text[:3797] + "..."
                await self.bot.send_message(chat_id=owner_id, text=compact, parse_mode=ParseMode.HTML)
                loop = asyncio.get_running_loop()
                full_bytes = await loop.run_in_executor(None, lambda: (text or "").encode("utf-8"))
                bio = io.BytesIO(full_bytes)
                bio.name = f"message_{item.get('msg_id', 'unknown')}.txt"
                bio.seek(0)
                try:
                    last_message = await self.bot.send_document(chat_id=owner_id, document=bio, filename=bio.name)
                finally:
                    try:
                        bio.close()
                    except Exception:
                        pass

        if media_path and os.path.exists(media_path):
            await asyncio.sleep(0.15)
            ext = os.path.splitext(media_path)[1].lower()
            ctype = (content_type or "").lower()
            bio: Optional[io.BytesIO] = None
            try:
                data = await self._read_file_bytes(media_path)
                bio = io.BytesIO(data)
                bio.name = os.path.basename(media_path)
                bio.seek(0)

                if "video_note" in ctype or "кружочек" in ctype:
                    last_message = await self.bot.send_video_note(chat_id=owner_id, video_note=bio)
                elif ext in (".mp4", ".mov", ".mkv", ".webm") or "video" in ctype:
                    last_message = await self.bot.send_video(chat_id=owner_id, video=bio, supports_streaming=True)
                elif ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif") or "photo" in ctype or "image" in ctype:
                    last_message = await self.bot.send_photo(chat_id=owner_id, photo=bio)
                elif ext in (".ogg", ".oga") or "voice" in ctype:
                    last_message = await self.bot.send_voice(chat_id=owner_id, voice=bio)
                elif ext in (".mp3", ".wav", ".m4a") or "audio" in ctype:
                    last_message = await self.bot.send_audio(chat_id=owner_id, audio=bio)
                else:
                    last_message = await self.bot.send_document(chat_id=owner_id, document=bio)
            except Exception:
                try:
                    logger.exception("Failed to send media to owner %s from %s", owner_id, media_path)
                    await self.bot.send_message(
                        chat_id=owner_id,
                        text="⚠️ Файл не удалось отправить автоматически."
                    )
                except Exception:
                    pass
            finally:
                if bio:
                    try:
                        bio.close()
                    except Exception:
                        pass

        if last_message is None:
            return await self.bot.send_message(chat_id=owner_id, text="ℹ️ (без контента)")
        return last_message

class EventHandler:
    def __init__(self, db: Any, aggregator: DeleteAggregator, config: Any, watcher_service: Any = None):
        self.db = db
        self.aggregator = aggregator
        self.config = config
        self.watcher_service = watcher_service
        self.disappearing_queue: asyncio.Queue[Tuple[int, int, Dict[str, Any]]] = asyncio.Queue()
        self._disappearing_task: Optional[asyncio.Task] = None
        self.delete_queue: asyncio.Queue[Tuple[int, events.MessageDeleted]] = asyncio.Queue()
        self._delete_task: Optional[asyncio.Task] = None

        self._muted_chat_cache: Dict[Tuple[int, int], Tuple[float, bool]] = {}
        self._muted_cache_ttl = 60.0  # seconds
        self._listen_settings_cache: Dict[int, Tuple[float, Dict[str, int]]] = {}
        self._listen_cache_ttl = 45.0

    async def start_workers(self) -> None:
        await self.aggregator.start_workers()
        if not self._disappearing_task or self._disappearing_task.done():
            self._disappearing_task = asyncio.create_task(self._disappearing_worker())
        if not self._delete_task or self._delete_task.done():
            self._delete_task = asyncio.create_task(self._delete_worker())

    async def stop_workers(self) -> None:
        for task in (self._disappearing_task, self._delete_task):
            if task:
                task.cancel()
        self._disappearing_task = self._delete_task = None
        await self.aggregator.stop_workers()

    def is_chat_allowed(self, chat_id: Optional[int]) -> bool:
        if chat_id is None:
            return False
        allowed = self.config.allowed_chat_ids_set
        if allowed is None:
            return True
        return chat_id in allowed

    def _dialog_kind_from_entity(self, entity: Any, sender: Any = None) -> str:
        if entity is None:
            return "unknown"
        if isinstance(entity, types.User):
            if bool(getattr(entity, "bot", False)) or bool(getattr(sender, "bot", False)):
                return "bot"
            return "private"
        if isinstance(entity, types.Channel):
            if getattr(entity, "broadcast", False):
                return "channel"
            if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
                return "supergroup"
            return "group"
        if isinstance(entity, (types.Chat, types.ChatForbidden)):
            return "group"
        return "unknown"

    def _listen_key_for_kind(self, dialog_kind: str) -> Optional[str]:
        kind = str(dialog_kind or "").strip().lower()
        if kind == "private":
            return "allow_private"
        if kind == "group":
            return "allow_groups"
        if kind == "supergroup":
            return "allow_supergroups"
        if kind == "channel":
            return "allow_channels"
        if kind == "bot":
            return "allow_bots"
        return None

    async def _is_dialog_kind_allowed(self, owner_id: int, dialog_kind: str) -> bool:
        now_ts = time.monotonic()
        cached = self._listen_settings_cache.get(int(owner_id))
        if cached and cached[0] > now_ts:
            settings = dict(cached[1])
        else:
            settings = await get_user_chat_type_settings(self.db, int(owner_id))
            self._listen_settings_cache[int(owner_id)] = (now_ts + self._listen_cache_ttl, dict(settings))

        listen_key = self._listen_key_for_kind(dialog_kind)
        if not listen_key:
            return True
        return bool(int(settings.get(listen_key, CHAT_LISTEN_DEFAULTS.get(listen_key, 0))))

    def _dialog_type_from_entity(self, entity: Any) -> str:
        if entity is None:
            return "unknown"
        if isinstance(entity, types.User):
            return "private"
        if isinstance(entity, types.Channel):
            if getattr(entity, "broadcast", False):
                return "channel"
            if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
                return "supergroup"
            return "group"
        if isinstance(entity, (types.Chat, types.ChatForbidden)):
            return "group"
        return "unknown"

    def _dialog_title_from_entity(self, entity: Any, chat_id: Optional[int]) -> str:
        if entity is None:
            return f"Диалог {chat_id}" if chat_id is not None else "Диалог"

        if isinstance(entity, types.User):
            first_name = repair_mojibake(getattr(entity, "first_name", "") or "").strip()
            last_name = repair_mojibake(getattr(entity, "last_name", "") or "").strip()
            display = " ".join(part for part in (first_name, last_name) if part).strip()
            if display:
                return display
            username = repair_mojibake(getattr(entity, "username", "") or "").strip()
            if username:
                return f"@{username}"
            entity_id = getattr(entity, "id", None)
            return f"Пользователь {entity_id}" if entity_id else "Личный чат"

        for attr in ("title", "first_name"):
            value = repair_mojibake(getattr(entity, attr, "") or "").strip()
            if value:
                return value

        username = repair_mojibake(getattr(entity, "username", "") or "").strip()
        if username:
            return f"@{username}"
        return f"Диалог {chat_id}" if chat_id is not None else "Диалог"

    def _dialog_username_from_entity(self, entity: Any) -> str:
        username = repair_mojibake(getattr(entity, "username", "") or "").strip().lstrip("@")
        return re.sub(r"[^0-9A-Za-z_]", "", username)

    def _sender_snapshot(self, sender: Any, *, message: Any = None) -> Tuple[Optional[int], str, str]:
        outgoing = bool(getattr(message, "out", False))
        sender_id = getattr(sender, "id", None)
        sender_username = repair_mojibake(getattr(sender, "username", "") or "").strip().lstrip("@")
        sender_username = re.sub(r"[^0-9A-Za-z_]", "", sender_username)

        display_name = repair_mojibake(get_safe_sender_name(sender)).strip()
        if outgoing and not display_name:
            display_name = "Вы"
        if not display_name:
            if sender_username:
                display_name = f"@{sender_username}"
            elif sender_id is not None:
                display_name = f"ID {sender_id}"
            else:
                display_name = "Неизвестно"
        return sender_id, sender_username, display_name

    def _is_access_denied_error(self, exc: Exception) -> bool:
        name = type(exc).__name__
        if name in {"ChannelPrivateError", "ChatAdminRequiredError", "UserBannedInChannelError"}:
            return True
        text = str(exc).lower()
        return ("private" in text and "permission" in text) or ("banned" in text and "channel" in text)

    async def _safe_get_sender(self, message_or_event: Any, *, owner_id: Optional[int] = None, context: str = "") -> Any:
        sender = getattr(message_or_event, "sender", None)
        if sender is not None:
            return sender
        if not hasattr(message_or_event, "get_sender"):
            return None
        try:
            return await message_or_event.get_sender()
        except Exception as exc:
            if self._is_access_denied_error(exc):
                logger.debug(
                    "Skipping get_sender due access error owner=%s context=%s: %s",
                    owner_id, context or "-", type(exc).__name__,
                )
                return None
            logger.debug(
                "get_sender failed owner=%s context=%s: %s",
                owner_id, context or "-", type(exc).__name__,
            )
            return None

    async def _safe_get_chat(self, message_or_event: Any, *, owner_id: Optional[int] = None, context: str = "") -> Any:
        chat = getattr(message_or_event, "chat", None)
        if chat is not None:
            return chat
        if not hasattr(message_or_event, "get_chat"):
            return None
        try:
            return await message_or_event.get_chat()
        except Exception as exc:
            if self._is_access_denied_error(exc):
                logger.debug(
                    "Skipping get_chat due access error owner=%s context=%s: %s",
                    owner_id, context or "-", type(exc).__name__,
                )
                return None
            logger.debug(
                "get_chat failed owner=%s context=%s: %s",
                owner_id, context or "-", type(exc).__name__,
            )
            return None

    def _reply_to_msg_id(self, message: Any) -> Optional[int]:
        reply_header = getattr(message, "reply_to", None)
        if not reply_header:
            return None
        return getattr(reply_header, "reply_to_msg_id", None) or getattr(reply_header, "reply_to_top_id", None)

    def _utcnow_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _sync_priority_for_dialog(self, dialog_type: Optional[str], history_complete: Optional[bool]) -> int:
        base = 100 if history_complete else 25
        kind = str(dialog_type or "").strip().lower()
        if kind == "private":
            return base - 12
        if kind == "group":
            return base - 4
        return base

    def _local_hour_from_iso(self, value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.astimezone(self.config.tz).hour)
        except Exception:
            return None

    def _risk_signal_spec(self, signal_type: str) -> Tuple[str, float, str]:
        mapping = {
            "message_delete": ("high", 2.6, "Удаление сообщения"),
            "message_edit": ("medium", 1.6, "Изменение сообщения"),
            "disappearing_media": ("high", 3.0, "Исчезающее медиа"),
            "night_activity": ("low", 0.9, "Ночная активность"),
            "delete_burst": ("high", 4.2, "Всплеск удалений"),
            "edit_burst": ("medium", 2.8, "Всплеск правок"),
        }
        return mapping.get(signal_type, ("info", 0.5, signal_type.replace("_", " ").strip()))

    async def _upsert_sync_state(
        self,
        *,
        owner_id: int,
        chat_id: Optional[int],
        dialog_type: Optional[str] = None,
        sync_state: Optional[str] = None,
        sync_priority: Optional[int] = None,
        oldest_synced_msg_id: Optional[int] = None,
        newest_synced_msg_id: Optional[int] = None,
        history_complete: Optional[bool] = None,
        backfill_passes_delta: int = 0,
        last_realtime_sync_at: Optional[str] = None,
        last_backfill_at: Optional[str] = None,
        next_sync_after: Optional[str] = None,
        last_error: Optional[str] = None,
        error_delta: int = 0,
    ) -> None:
        if chat_id is None:
            return
        now_iso = self._utcnow_iso()
        priority_value = sync_priority
        if priority_value is None:
            priority_value = self._sync_priority_for_dialog(dialog_type, history_complete)

        try:
            await self.db.execute(
                """
                INSERT INTO chat_sync_state (
                    owner_id, chat_id, sync_state, sync_priority,
                    oldest_synced_msg_id, newest_synced_msg_id, history_complete,
                    backfill_passes, last_realtime_sync_at, last_backfill_at, next_sync_after,
                    error_count, last_error, last_error_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                    sync_state = COALESCE(excluded.sync_state, chat_sync_state.sync_state),
                    sync_priority = COALESCE(excluded.sync_priority, chat_sync_state.sync_priority),
                    oldest_synced_msg_id = CASE
                        WHEN chat_sync_state.oldest_synced_msg_id IS NULL THEN excluded.oldest_synced_msg_id
                        WHEN excluded.oldest_synced_msg_id IS NULL THEN chat_sync_state.oldest_synced_msg_id
                        WHEN excluded.oldest_synced_msg_id < chat_sync_state.oldest_synced_msg_id THEN excluded.oldest_synced_msg_id
                        ELSE chat_sync_state.oldest_synced_msg_id
                    END,
                    newest_synced_msg_id = CASE
                        WHEN chat_sync_state.newest_synced_msg_id IS NULL THEN excluded.newest_synced_msg_id
                        WHEN excluded.newest_synced_msg_id IS NULL THEN chat_sync_state.newest_synced_msg_id
                        WHEN excluded.newest_synced_msg_id > chat_sync_state.newest_synced_msg_id THEN excluded.newest_synced_msg_id
                        ELSE chat_sync_state.newest_synced_msg_id
                    END,
                    history_complete = COALESCE(excluded.history_complete, chat_sync_state.history_complete),
                    backfill_passes = chat_sync_state.backfill_passes + COALESCE(excluded.backfill_passes, 0),
                    last_realtime_sync_at = COALESCE(excluded.last_realtime_sync_at, chat_sync_state.last_realtime_sync_at),
                    last_backfill_at = COALESCE(excluded.last_backfill_at, chat_sync_state.last_backfill_at),
                    next_sync_after = COALESCE(excluded.next_sync_after, chat_sync_state.next_sync_after),
                    error_count = chat_sync_state.error_count + COALESCE(excluded.error_count, 0),
                    last_error = CASE
                        WHEN excluded.last_error IS NOT NULL THEN excluded.last_error
                        ELSE chat_sync_state.last_error
                    END,
                    last_error_at = CASE
                        WHEN excluded.last_error IS NOT NULL THEN COALESCE(excluded.last_error_at, excluded.updated_at)
                        ELSE chat_sync_state.last_error_at
                    END,
                    updated_at = COALESCE(excluded.updated_at, chat_sync_state.updated_at)
                """,
                (
                    owner_id,
                    chat_id,
                    sync_state or "active",
                    priority_value,
                    oldest_synced_msg_id,
                    newest_synced_msg_id,
                    None if history_complete is None else int(bool(history_complete)),
                    int(backfill_passes_delta or 0),
                    last_realtime_sync_at,
                    last_backfill_at,
                    next_sync_after,
                    int(error_delta or 0),
                    repair_mojibake(last_error or "").strip() if last_error is not None else None,
                    now_iso if last_error is not None else None,
                    now_iso,
                    now_iso,
                ),
            )
        except sqlite3.OperationalError as e:
            # GRACEFUL DEGRADATION: Log warning but don't crash if database is locked
            if 'locked' in str(e).lower():
                logger.warning(
                    "Failed to update chat_sync_state (database locked): "
                    "owner_id=%s, chat_id=%s",
                    owner_id, chat_id
                )
            else:
                raise
        except Exception:
            logger.exception(
                "Failed to upsert chat_sync_state: owner_id=%s, chat_id=%s",
                owner_id, chat_id
            )

    async def _bump_risk_profile(
        self,
        *,
        owner_id: int,
        profile_kind: str,
        profile_id: Optional[int],
        signal_type: str,
        score: float,
        event_at: str,
    ) -> None:
        if profile_id is None:
            return
        delete_inc = 1 if signal_type in {"message_delete", "delete_burst"} else 0
        edit_inc = 1 if signal_type in {"message_edit", "edit_burst"} else 0
        disappearing_inc = 1 if signal_type == "disappearing_media" else 0
        night_inc = 1 if signal_type == "night_activity" else 0
        burst_inc = 1 if signal_type in {"delete_burst", "edit_burst"} else 0
        summary = (
            f"{'Чат' if profile_kind == 'chat' else 'Пользователь'} {profile_id}: "
            f"{signal_type.replace('_', ' ')} ({event_at[:16]})"
        )
        now_iso = self._utcnow_iso()
        await self.db.execute(
            """
            INSERT INTO risk_profiles (
                owner_id, profile_kind, profile_id, risk_score,
                delete_count, edit_count, disappearing_count, night_count, burst_count,
                last_signal_type, last_event_at, summary, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, profile_kind, profile_id) DO UPDATE SET
                risk_score = risk_profiles.risk_score + COALESCE(excluded.risk_score, 0),
                delete_count = risk_profiles.delete_count + COALESCE(excluded.delete_count, 0),
                edit_count = risk_profiles.edit_count + COALESCE(excluded.edit_count, 0),
                disappearing_count = risk_profiles.disappearing_count + COALESCE(excluded.disappearing_count, 0),
                night_count = risk_profiles.night_count + COALESCE(excluded.night_count, 0),
                burst_count = risk_profiles.burst_count + COALESCE(excluded.burst_count, 0),
                last_signal_type = COALESCE(excluded.last_signal_type, risk_profiles.last_signal_type),
                last_event_at = COALESCE(excluded.last_event_at, risk_profiles.last_event_at),
                summary = COALESCE(excluded.summary, risk_profiles.summary),
                updated_at = COALESCE(excluded.updated_at, risk_profiles.updated_at)
            """,
            (
                owner_id,
                profile_kind,
                int(profile_id),
                float(score or 0.0),
                delete_inc,
                edit_inc,
                disappearing_inc,
                night_inc,
                burst_inc,
                signal_type,
                event_at,
                summary,
                now_iso,
                now_iso,
            ),
        )

    async def _record_risk_event(
        self,
        *,
        owner_id: int,
        chat_id: Optional[int],
        sender_id: Optional[int],
        msg_id: Optional[int],
        signal_type: str,
        event_at: Optional[str] = None,
        detail: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        dedupe_key: Optional[str] = None,
        severity: Optional[str] = None,
        score: Optional[float] = None,
        title: Optional[str] = None,
    ) -> bool:
        if chat_id is None and sender_id is None:
            return False
        event_at_iso = event_at or self._utcnow_iso()
        default_severity, default_score, default_title = self._risk_signal_spec(signal_type)
        event_severity = severity or default_severity
        event_score = float(default_score if score is None else score)
        event_title = title or default_title
        dedupe_value = dedupe_key or f"{signal_type}:{chat_id or 0}:{sender_id or 0}:{msg_id or event_at_iso}"
        existing = await self.db.fetchone(
            "SELECT id FROM risk_events WHERE owner_id = ? AND dedupe_key = ? LIMIT 1",
            (owner_id, dedupe_value),
        )
        if existing:
            return False
        detail_value = repair_mojibake(detail or "").strip()
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        now_iso = self._utcnow_iso()
        await self.db.execute(
            """
            INSERT INTO risk_events (
                owner_id, chat_id, sender_id, msg_id, signal_type,
                severity, score, title, detail, meta_json, event_at, dedupe_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                chat_id,
                sender_id,
                msg_id,
                signal_type,
                event_severity,
                event_score,
                event_title,
                detail_value,
                meta_json,
                event_at_iso,
                dedupe_value,
                now_iso,
            ),
        )
        await self._bump_risk_profile(
            owner_id=owner_id,
            profile_kind="chat",
            profile_id=chat_id,
            signal_type=signal_type,
            score=event_score,
            event_at=event_at_iso,
        )
        await self._bump_risk_profile(
            owner_id=owner_id,
            profile_kind="sender",
            profile_id=sender_id,
            signal_type=signal_type,
            score=event_score,
            event_at=event_at_iso,
        )
        return True

    async def _emit_burst_signal_if_needed(
        self,
        *,
        owner_id: int,
        chat_id: Optional[int],
        sender_id: Optional[int],
        signal_type: str,
        event_at: str,
    ) -> None:
        if chat_id is None or signal_type not in {"message_delete", "message_edit"}:
            return
        threshold = 5 if signal_type == "message_delete" else 7
        burst_signal = "delete_burst" if signal_type == "message_delete" else "edit_burst"
        try:
            event_dt = datetime.fromisoformat(str(event_at).replace("Z", "+00:00"))
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=timezone.utc)
        except Exception:
            event_dt = datetime.now(timezone.utc)
        window_start = (event_dt - timedelta(hours=1)).isoformat()
        row = await self.db.fetchone(
            """
            SELECT COUNT(*)
            FROM risk_events
            WHERE owner_id = ? AND chat_id = ? AND signal_type = ? AND event_at >= ?
            """,
            (owner_id, chat_id, signal_type, window_start),
        )
        count_in_window = int(row[0] or 0) if row else 0
        if count_in_window < threshold:
            return
        bucket = event_dt.astimezone(self.config.tz).strftime("%Y%m%d%H")
        await self._record_risk_event(
            owner_id=owner_id,
            chat_id=chat_id,
            sender_id=sender_id,
            msg_id=None,
            signal_type=burst_signal,
            event_at=event_dt.isoformat(),
            detail=f"За последний час в чате зафиксировано {count_in_window} событий типа {signal_type}.",
            meta={"count_last_hour": count_in_window, "source_signal": signal_type},
            dedupe_key=f"{burst_signal}:{chat_id}:{bucket}",
        )


    async def _upsert_dialog_record(
        self,
        *,
        owner_id: int,
        chat_id: Optional[int],
        dialog_type: str,
        title: str,
        username: str,
        last_message_id: Optional[int],
        last_message_at: Optional[str],
        last_message_preview: str,
        last_sender_id: Optional[int],
        last_sender_label: str,
        photo_url: Optional[str] = None,
        unread_count: int = 0,
        oldest_synced_msg_id: Optional[int] = None,
        newest_synced_msg_id: Optional[int] = None,
        history_complete: Optional[bool] = None,
        sync_error: Optional[str] = None,
    ) -> None:
        if chat_id is None:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        preview = _short_text(repair_mojibake(last_message_preview or "").replace("\x00", "").strip(), 180)
        await self.db.execute(
            """
            INSERT INTO chat_dialogs (
                owner_id, chat_id, dialog_type, title, username, photo_url,
                last_message_id, last_message_at, last_message_preview,
                last_sender_id, last_sender_label, unread_count,
                oldest_synced_msg_id, newest_synced_msg_id, history_complete,
                sync_error, last_sync_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                dialog_type = COALESCE(excluded.dialog_type, chat_dialogs.dialog_type),
                title = COALESCE(NULLIF(excluded.title, ''), chat_dialogs.title),
                username = COALESCE(NULLIF(excluded.username, ''), chat_dialogs.username),
                photo_url = CASE
                    WHEN excluded.photo_url IS NOT NULL THEN NULLIF(excluded.photo_url, '')
                    ELSE chat_dialogs.photo_url
                END,
                last_message_id = CASE
                    WHEN excluded.last_message_id IS NOT NULL
                         AND COALESCE(chat_dialogs.last_message_id, 0) <= excluded.last_message_id
                    THEN excluded.last_message_id
                    ELSE chat_dialogs.last_message_id
                END,
                last_message_at = CASE
                    WHEN excluded.last_message_at IS NOT NULL
                         AND COALESCE(chat_dialogs.last_message_at, '') <= excluded.last_message_at
                    THEN excluded.last_message_at
                    ELSE chat_dialogs.last_message_at
                END,
                last_message_preview = CASE
                    WHEN excluded.last_message_at IS NOT NULL
                         AND COALESCE(chat_dialogs.last_message_at, '') <= excluded.last_message_at
                    THEN excluded.last_message_preview
                    ELSE chat_dialogs.last_message_preview
                END,
                last_sender_id = CASE
                    WHEN excluded.last_message_at IS NOT NULL
                         AND COALESCE(chat_dialogs.last_message_at, '') <= excluded.last_message_at
                    THEN excluded.last_sender_id
                    ELSE chat_dialogs.last_sender_id
                END,
                last_sender_label = CASE
                    WHEN excluded.last_message_at IS NOT NULL
                         AND COALESCE(chat_dialogs.last_message_at, '') <= excluded.last_message_at
                    THEN excluded.last_sender_label
                    ELSE chat_dialogs.last_sender_label
                END,
                unread_count = CASE
                    WHEN excluded.unread_count > 0 THEN excluded.unread_count
                    ELSE chat_dialogs.unread_count
                END,
                oldest_synced_msg_id = CASE
                    WHEN chat_dialogs.oldest_synced_msg_id IS NULL THEN excluded.oldest_synced_msg_id
                    WHEN excluded.oldest_synced_msg_id IS NULL THEN chat_dialogs.oldest_synced_msg_id
                    WHEN excluded.oldest_synced_msg_id < chat_dialogs.oldest_synced_msg_id THEN excluded.oldest_synced_msg_id
                    ELSE chat_dialogs.oldest_synced_msg_id
                END,
                newest_synced_msg_id = CASE
                    WHEN chat_dialogs.newest_synced_msg_id IS NULL THEN excluded.newest_synced_msg_id
                    WHEN excluded.newest_synced_msg_id IS NULL THEN chat_dialogs.newest_synced_msg_id
                    WHEN excluded.newest_synced_msg_id > chat_dialogs.newest_synced_msg_id THEN excluded.newest_synced_msg_id
                    ELSE chat_dialogs.newest_synced_msg_id
                END,
                history_complete = COALESCE(excluded.history_complete, chat_dialogs.history_complete),
                sync_error = CASE
                    WHEN excluded.sync_error IS NOT NULL THEN excluded.sync_error
                    ELSE chat_dialogs.sync_error
                END,
                last_sync_at = COALESCE(excluded.last_sync_at, chat_dialogs.last_sync_at),
                updated_at = COALESCE(excluded.updated_at, chat_dialogs.updated_at)
            """,
            (
                owner_id,
                chat_id,
                dialog_type or "unknown",
                title or "Диалог",
                username or "",
                (photo_url or "").strip(),
                last_message_id,
                last_message_at,
                preview,
                last_sender_id,
                repair_mojibake(last_sender_label or "").strip(),
                int(unread_count or 0),
                oldest_synced_msg_id,
                newest_synced_msg_id,
                None if history_complete is None else int(bool(history_complete)),
                sync_error,
                now_iso,
                now_iso,
                now_iso,
            ),
        )

    async def is_muted_chat(self, owner_id: int, chat_id: int) -> bool:
        key = (owner_id, chat_id)
        now_ts = time.monotonic()
        if key in self._muted_chat_cache:
            expires_at, value = self._muted_chat_cache[key]
            if now_ts < expires_at:
                return value

        # try memcached first when available
        if MEMCACHED_AVAILABLE and MC_CLIENT is not None:
            mc_key = f"muted:{owner_id}:{chat_id}"
            try:
                cached = MC_CLIENT.get(mc_key)
                if cached is not None:
                    self._muted_chat_cache[key] = (now_ts + self._muted_cache_ttl, cached)
                    return bool(cached)
            except Exception:
                pass

        row = await self.db.fetchone("SELECT 1 FROM muted_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        result = bool(row)
        self._muted_chat_cache[key] = (now_ts + self._muted_cache_ttl, result)

        if MEMCACHED_AVAILABLE and MC_CLIENT is not None:
            try:
                MC_CLIENT.set(mc_key, result, time=int(self._muted_cache_ttl))
            except Exception:
                pass

        return result

    async def _upsert_thread_message(
        self,
        *,
        owner_id: int,
        chat_id: Optional[int],
        msg_id: Optional[int],
        chat_title: Optional[str],
        chat_username: Optional[str],
        sender_id: Optional[int],
        sender_username: Optional[str],
        sender_display_name: Optional[str] = None,
        sender_handle: Optional[str] = None,
        is_outgoing: bool = False,
        reply_to_msg_id: Optional[int] = None,
        dialog_type: Optional[str] = None,
        content_type: Optional[str] = None,
        text: Optional[str] = None,
        original_text: Optional[str] = None,
        media_path: Optional[str] = None,
        status: str = "active",
        edit_count: int = 0,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        deleted_at: Optional[str] = None,
        views: int = 0,
        reactions: str = "{}",
    ) -> None:
        if chat_id is None or msg_id is None:
            return

        text_value = text or ""
        original_value = original_text if original_text is not None else text_value

        await self.db.execute(
            """
            INSERT INTO chat_thread_messages (
                owner_id, chat_id, chat_title, chat_username, msg_id,
                sender_id, sender_username, sender_display_name, sender_handle, is_outgoing, reply_to_msg_id, dialog_type,
                content_type, text, original_text,
                media_path, status, edit_count, created_at, updated_at, deleted_at,
                views, reactions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, chat_id, msg_id) DO UPDATE SET
                chat_title = COALESCE(excluded.chat_title, chat_thread_messages.chat_title),
                chat_username = COALESCE(excluded.chat_username, chat_thread_messages.chat_username),
                sender_id = COALESCE(excluded.sender_id, chat_thread_messages.sender_id),
                sender_username = COALESCE(excluded.sender_username, chat_thread_messages.sender_username),
                sender_display_name = COALESCE(NULLIF(excluded.sender_display_name, ''), chat_thread_messages.sender_display_name),
                sender_handle = COALESCE(NULLIF(excluded.sender_handle, ''), chat_thread_messages.sender_handle),
                is_outgoing = CASE
                    WHEN excluded.is_outgoing IS NOT NULL THEN excluded.is_outgoing
                    ELSE chat_thread_messages.is_outgoing
                END,
                reply_to_msg_id = COALESCE(excluded.reply_to_msg_id, chat_thread_messages.reply_to_msg_id),
                dialog_type = COALESCE(NULLIF(excluded.dialog_type, ''), chat_thread_messages.dialog_type),
                content_type = COALESCE(excluded.content_type, chat_thread_messages.content_type),
                text = COALESCE(excluded.text, chat_thread_messages.text),
                original_text = COALESCE(chat_thread_messages.original_text, excluded.original_text, excluded.text, ''),
                media_path = CASE
                    WHEN excluded.media_path IS NOT NULL AND TRIM(excluded.media_path) <> '' THEN excluded.media_path
                    ELSE chat_thread_messages.media_path
                END,
                status = COALESCE(excluded.status, chat_thread_messages.status),
                edit_count = CASE
                    WHEN excluded.edit_count > chat_thread_messages.edit_count THEN excluded.edit_count
                    ELSE chat_thread_messages.edit_count
                END,
                created_at = COALESCE(chat_thread_messages.created_at, excluded.created_at),
                updated_at = COALESCE(excluded.updated_at, chat_thread_messages.updated_at),
                deleted_at = CASE
                    WHEN excluded.deleted_at IS NOT NULL AND TRIM(excluded.deleted_at) <> '' THEN excluded.deleted_at
                    ELSE chat_thread_messages.deleted_at
                END,
                views = COALESCE(excluded.views, chat_thread_messages.views),
                reactions = COALESCE(excluded.reactions, chat_thread_messages.reactions)
            """,
            (
                owner_id,
                chat_id,
                chat_title,
                chat_username,
                msg_id,
                sender_id,
                sender_username,
                sender_display_name,
                sender_handle,
                1 if is_outgoing else 0,
                reply_to_msg_id,
                dialog_type,
                content_type,
                text_value,
                original_value,
                media_path,
                status,
                int(edit_count or 0),
                created_at,
                updated_at,
                deleted_at,
                int(views or 0),
                reactions or "{}",
            ),
        )

    async def sync_message_snapshot(
        self,
        owner_id: int,
        message: Any,
        *,
        chat: Any = None,
        media_path: Optional[str] = None,
        photo_url: Optional[str] = None,
        unread_count: int = 0,
        history_complete: Optional[bool] = None,
        sync_error: Optional[str] = None,
    ) -> bool:
        msg = getattr(message, "message", None) or message
        msg_id = getattr(msg, "id", None)
        if not msg_id:
            return False

        chat = chat or getattr(msg, "chat", None)
        if chat is None and hasattr(msg, "get_chat"):
            try:
                chat = await msg.get_chat()
            except Exception:
                chat = None

        chat_id = getattr(msg, "chat_id", None)
        if chat_id is None and chat is not None:
            try:
                chat_id = utils.get_peer_id(chat)
            except Exception:
                chat_id = getattr(chat, "id", None)
        if chat_id is None or not self.is_chat_allowed(chat_id):
            return False

        sender = getattr(msg, "sender", None)
        if sender is None and hasattr(msg, "get_sender"):
            try:
                sender = await msg.get_sender()
            except Exception:
                sender = None

        sender_id, sender_username, sender_display_name = self._sender_snapshot(sender, message=msg)
        chat_title = self._dialog_title_from_entity(chat, chat_id)
        chat_username = self._dialog_username_from_entity(chat)
        dialog_type = self._dialog_type_from_entity(chat)
        content_type = detect_content_type(msg)
        text_value = repair_mojibake(getattr(msg, "raw_text", None) or getattr(msg, "message", None) or getattr(msg, "text", None) or "")
        created_at = getattr(msg, "date", None)
        edit_date = getattr(msg, "edit_date", None)
        created_at_iso = created_at.isoformat() if created_at else datetime.now(timezone.utc).isoformat()
        updated_at_iso = (edit_date or created_at or datetime.now(timezone.utc)).isoformat()
        status = "edited" if edit_date else "active"
        views = int(getattr(msg, "views", None) or 0)
        reactions = extract_reactions_json(msg)

        await self._upsert_thread_message(
            owner_id=owner_id,
            chat_id=chat_id,
            msg_id=msg_id,
            chat_title=chat_title,
            chat_username=chat_username,
            sender_id=sender_id,
            sender_username=sender_username,
            sender_display_name=sender_display_name,
            sender_handle=f"@{sender_username}" if sender_username else "",
            is_outgoing=bool(getattr(msg, "out", False)),
            reply_to_msg_id=self._reply_to_msg_id(msg),
            dialog_type=dialog_type,
            content_type=content_type,
            text=text_value,
            original_text=text_value,
            media_path=media_path,
            status=status,
            edit_count=1 if edit_date else 0,
            created_at=created_at_iso,
            updated_at=updated_at_iso,
            deleted_at=None,
            views=views,
            reactions=reactions or "{}",
        )
        await self._upsert_dialog_record(
            owner_id=owner_id,
            chat_id=chat_id,
            dialog_type=dialog_type,
            title=chat_title,
            username=chat_username,
            photo_url=photo_url,
            last_message_id=msg_id,
            last_message_at=created_at_iso,
            last_message_preview=text_value or content_type,
            last_sender_id=sender_id,
            last_sender_label="Вы" if getattr(msg, "out", False) else sender_display_name,
            unread_count=unread_count,
            oldest_synced_msg_id=msg_id,
            newest_synced_msg_id=msg_id,
            history_complete=history_complete,
            sync_error=sync_error,
        )
        await self._upsert_sync_state(
            owner_id=owner_id,
            chat_id=chat_id,
            dialog_type=dialog_type,
            sync_state="complete" if history_complete else "active",
            oldest_synced_msg_id=msg_id,
            newest_synced_msg_id=msg_id,
            history_complete=history_complete,
            last_realtime_sync_at=updated_at_iso,
            last_error=sync_error,
            error_delta=1 if sync_error else 0,
        )
        return True

    async def _add_thread_revision(
        self,
        *,
        owner_id: int,
        chat_id: Optional[int],
        msg_id: Optional[int],
        event_type: str,
        text: Optional[str],
        previous_text: Optional[str],
        created_at: Optional[str],
    ) -> None:
        if chat_id is None or msg_id is None:
            return
        await self.db.execute(
            """
            INSERT INTO chat_thread_revisions (
                owner_id, chat_id, msg_id, event_type, text, previous_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                chat_id,
                msg_id,
                event_type,
                text or "",
                previous_text or "",
                created_at or datetime.now(timezone.utc).isoformat(),
            ),
        )

    async def _mark_thread_deleted_fallback(
        self,
        *,
        owner_id: int,
        msg_id: int,
        chat_id: Optional[int],
        deleted_at: str,
    ) -> bool:
        if chat_id is not None:
            await self.db.execute(
                """
                UPDATE chat_thread_messages
                SET status='deleted', deleted_at=?, updated_at=?
                WHERE owner_id=? AND msg_id=? AND chat_id=?
                """,
                (deleted_at, deleted_at, owner_id, msg_id, chat_id),
            )
            row = await self.db.fetchone(
                "SELECT id, sender_id FROM chat_thread_messages WHERE owner_id=? AND msg_id=? AND chat_id=? LIMIT 1",
                (owner_id, msg_id, chat_id),
            )
            if row:
                await self._add_thread_revision(
                    owner_id=owner_id,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    event_type="deleted",
                    text=None,
                    previous_text=None,
                    created_at=deleted_at,
                )
                await self._record_risk_event(
                    owner_id=owner_id,
                    chat_id=chat_id,
                    sender_id=row[1] if len(row) > 1 else None,
                    msg_id=msg_id,
                    signal_type="message_delete",
                    event_at=deleted_at,
                    detail=f"Сообщение было удалено и помечено из полного архива для чата {chat_id}.",
                    meta={"chat_id": chat_id, "fallback": True},
                    dedupe_key=f"message_delete:{chat_id}:{msg_id}",
                )
                return True

        rows = await self.db.fetchall(
            "SELECT chat_id, sender_id FROM chat_thread_messages WHERE owner_id=? AND msg_id=? ORDER BY id DESC",
            (owner_id, msg_id),
        )
        if rows:
            chat_ids = {int(row[0]) for row in rows if row[0] is not None}
            if len(chat_ids) != 1:
                logger.warning(
                    "Skip delete fallback without chat_id: owner=%s msg_id=%s matched_chats=%s",
                    owner_id,
                    msg_id,
                    sorted(chat_ids),
                )
                return False
            thread_chat_id = next(iter(chat_ids))
            sender_id = rows[0][1] if len(rows[0]) > 1 else None
            await self.db.execute(
                """
                UPDATE chat_thread_messages
                SET status='deleted', deleted_at=?, updated_at=?
                WHERE owner_id=? AND msg_id=? AND chat_id=?
                """,
                (deleted_at, deleted_at, owner_id, msg_id, thread_chat_id),
            )
            await self._add_thread_revision(
                owner_id=owner_id,
                chat_id=thread_chat_id,
                msg_id=msg_id,
                event_type="deleted",
                text=None,
                previous_text=None,
                created_at=deleted_at,
            )
            await self._record_risk_event(
                owner_id=owner_id,
                chat_id=thread_chat_id,
                sender_id=sender_id,
                msg_id=msg_id,
                signal_type="message_delete",
                event_at=deleted_at,
                detail=f"Сообщение было удалено и помечено из полного архива для чата {thread_chat_id}.",
                meta={"chat_id": thread_chat_id, "fallback": True},
                dedupe_key=f"message_delete:{thread_chat_id}:{msg_id}",
            )
            return True
        return False

    async def _disappearing_worker(self) -> None:
        while True:
            try:
                owner_id, msg_id, payload = await self.disappearing_queue.get()
                await asyncio.sleep(2.8)
                row = await self.db.fetchone("""
                    SELECT media_path, already_forwarded, text
                    FROM pending
                    WHERE owner_id = ? AND msg_id = ?
                """, (owner_id, msg_id))
                if not row:
                    archived = await self.db.fetchone(
                        "SELECT 1 FROM deleted_messages WHERE owner_id = ? AND msg_id = ? LIMIT 1",
                        (owner_id, msg_id),
                    )
                    if not archived:
                        fallback_media = payload.get("media_path")
                        payload["media_path"] = fallback_media if fallback_media and os.path.exists(fallback_media) else None
                        payload["event_type"] = "disappearing"
                        await self.aggregator.add_event(owner_id, payload)
                        logger.info("Disappearing fallback sent from queue payload: %d/%d", owner_id, msg_id)
                    else:
                        logger.debug("Disappearing record already archived: %d/%d", owner_id, msg_id)
                    continue
                media_path, already_forwarded, text = row
                if already_forwarded:
                    logger.debug("Already forwarded: %d/%d", owner_id, msg_id)
                    continue
                payload.update({
                    "text": text or payload.get("text", ""),
                    "media_path": media_path if media_path and os.path.exists(media_path) else None,
                })
                await self.aggregator.add_event(owner_id, payload)
                await self.db.execute("UPDATE pending SET already_forwarded = 1 WHERE owner_id = ? AND msg_id = ?", (owner_id, msg_id))
                logger.info("Disappearing message sent after delay: %d/%d", owner_id, msg_id)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Disappearing worker error")
            finally:
                try:
                    self.disappearing_queue.task_done()
                except Exception:
                    pass

    async def handle_new(self, owner_id: int, event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        if chat_id is None or not self.is_chat_allowed(chat_id):
            return
        if await self.db.fetchone("SELECT 1 FROM muted_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)):
            return
        chat = await self._safe_get_chat(event, owner_id=owner_id, context="handle_new")
        sender = await self._safe_get_sender(event, owner_id=owner_id, context="handle_new")
        dialog_kind = self._dialog_kind_from_entity(chat, sender)
        if not await self._is_dialog_kind_allowed(owner_id, dialog_kind):
            return
        text = event.raw_text or ""
        msg_id = event.id
        msg_date_iso = event.date.isoformat() if event.date else datetime.now(timezone.utc).isoformat()
        chat_title = self._dialog_title_from_entity(chat, chat_id)
        chat_username = self._dialog_username_from_entity(chat)
        dialog_type = "bot" if dialog_kind == "bot" else self._dialog_type_from_entity(chat)
        sender_id, sender_username, sender_display_name = self._sender_snapshot(sender, message=getattr(event, "message", None) or event)
        sender_name = sender_display_name
        content_type_value = detect_content_type(event)
        is_disappearing = bool(
            (getattr(event.message, "ttl_period", None) if hasattr(event, "message") else None) or
            (getattr(event.media, "ttl_seconds", None) if event.media else None)
        )
        thread_exists = await self.db.fetchone(
            "SELECT id FROM chat_thread_messages WHERE owner_id=? AND chat_id=? AND msg_id=? LIMIT 1",
            (owner_id, chat_id, msg_id),
        )
        # ==================== РЎРўРћР РРЎ: MessageMediaStory (СЂРµРїРѕСЃС‚/РѕС‚РїСЂР°РІРєР° РІ С‡Р°С‚) ====================
        media = getattr(event, 'media', None) or (getattr(event.message, 'media', None) if hasattr(event, 'message') else None)
        if media and isinstance(media, types.MessageMediaStory):
            story_media = media

            try:
                sender = await event.get_sender()
                sender_name = get_safe_sender_name(sender)
                sender_username = getattr(sender, 'username', None)
                sender_id = getattr(sender, 'id', None) if sender else None
            except Exception as e:
                logger.warning(f"[STORY] Ошибка получения отправителя: {e}")
                sender_name = "Unknown"
                sender_username = None
                sender_id = None

            media_path = None
            if CONFIG.download_media:
                try:
                    ext = ".jpg" if "photo" in str(story_media) else ".mp4"
                    ts = int(time.time() * 1000)
                    fname = f"story_{owner_id}_{story_media.id or ts}_{ts}{ext}"
                    path = os.path.join(self.config.media_dir, fname)

                    await asyncio.wait_for(event.download_media(file=path), timeout=20.0)

                    if os.path.exists(path) and os.path.getsize(path) > 300:
                        media_path = path
                        logger.info(f"[STORY] Скачана сторис (репост) → {path}")
                    else:
                        if os.path.exists(path):
                            os.unlink(path)
                except asyncio.TimeoutError:
                    logger.warning(f"[STORY] Таймаут скачивания репост-сторис owner={owner_id}")
                except Exception as exc:
                    logger.warning(f"[STORY] Не скачалась репост-сторис owner={owner_id}: {exc}")

            now_iso = datetime.now(timezone.utc).isoformat()
            
            try:
                # Проверяем дедупликацию по story_id
                existing = await self.db.fetchone(
                    "SELECT id FROM stories WHERE owner_id = ? AND story_id = ?",
                    (owner_id, story_media.id)
                )
                
                if not existing:
                    await self.db.execute("""
                        INSERT INTO stories 
                        (owner_id, peer_id, story_id, sender_id, sender_username, sender_name,
                         caption, media_path, posted_at, added_at, content_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        owner_id,
                        story_media.user_id,
                        story_media.id,
                        sender_id,
                        sender_username,
                        sender_name,
                        getattr(story_media, 'caption', None) or "",
                        media_path,
                        now_iso,
                        now_iso,
                        "📷 Story" if media_path and media_path.lower().endswith(('.jpg', '.png')) else "🎥 Story Video"
                    ))
                    logger.info(f"[STORY] Сохранена репост-сторис {story_media.id}")
                else:
                    logger.debug(f"[STORY] Сторис {story_media.id} уже сохранена (дедуп), пропускаем")
            except Exception as e:
                logger.error(f"[STORY] Ошибка сохранения репост-сторис: {e}")

            # Отправляем владельцу
            payload = {
                "event_type": "story",
                "story_id": story_media.id,
                "text": getattr(story_media, 'caption', "") or "",
                "media_path": media_path,
                "sender_username": sender_name,
                "sender_id": sender_id,
                "chat_title": f"Сторис от {sender_name} (репост)",
                "message_date": now_iso,
                "content_type": "📖 Story",
            }
            await self.aggregator.add_event(owner_id, payload)
            logger.info(f"[STORY] Сторис {story_media.id} отправлена пользователю")
            return  # не обрабатываем дальше как обычное сообщение
        # =====================================================================================
        media_path = None
        if event.media and self.config.download_media:
            try:
                ext = detect_media_ext(event) or ".bin"
                ts = int(time.time() * 1000)
                fname = f"d_{owner_id}_{msg_id}_{ts}{ext}"
                path = os.path.join(self.config.media_dir, fname)
                if is_disappearing:
                    logger.info("Priority download disappearing media → %d/%d", owner_id, msg_id)
                    await asyncio.wait_for(event.download_media(file=path), timeout=16.0)
                else:
                    await event.download_media(file=path)
                if os.path.exists(path) and os.path.getsize(path) > 400:
                    media_path = path
                else:
                    if os.path.exists(path):
                        os.unlink(path)
                    logger.warning("Invalid/empty media file: %s", path)
            except asyncio.TimeoutError:
                logger.warning("Timeout downloading disappearing media %d/%d", owner_id, msg_id)
            except Exception as e:
                logger.warning("Media download failed %d/%d: %s", owner_id, msg_id, str(e))

        views = 0
        reactions_json = '{}'
        try:
            msg_obj = getattr(event, 'message', None) or event
            extracted_views = getattr(msg_obj, 'views', None)
            views = extracted_views if extracted_views is not None else 0
            reactions_json = extract_reactions_json(msg_obj)
            logger.debug("New message stats for owner=%s msg=%s: views=%s reactions=%s", owner_id, msg_id, views, reactions_json)
        except Exception as e:
            logger.debug("Failed to extract initial views/reactions for pending: %s", e)

        await self.db.execute("""
            INSERT OR IGNORE INTO pending (
                owner_id, chat_id, chat_title, chat_username, msg_id,
                text, original_text, edit_count, last_edited_at, media_path, content_type,
                sender_id, sender_username, message_date, added_at,
                is_disappearing, already_forwarded, views, reactions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            owner_id, chat_id, chat_title, chat_username,
            msg_id, text, text, 0, None, media_path, content_type_value,
            sender_id, sender_name,
            msg_date_iso, datetime.now(timezone.utc).isoformat(),
            1 if is_disappearing else 0, 0,
            views, reactions_json
        ))

        # Try to get richer stats from watcher in case initial event lacked reactions/views
        if self.watcher_service and chat_id is not None:
            try:
                watcher_client = self.watcher_service.watched_clients.get(owner_id)
                if watcher_client:
                    incoming_msg = await asyncio.wait_for(
                        watcher_client.get_messages(chat_id, ids=msg_id, important=True, limit=1),
                        timeout=6.0
                    )
                    if isinstance(incoming_msg, list):
                        incoming_msg = incoming_msg[0] if incoming_msg else None
                    if incoming_msg:
                        api_views = getattr(incoming_msg, 'views', None)
                        api_reactions = extract_reactions_json(incoming_msg)

                        if api_views is not None and api_views != views:
                            views = api_views
                        if api_reactions and api_reactions != '{}' and api_reactions != reactions_json:
                            reactions_json = api_reactions

                        await self.db.execute(
                            "UPDATE pending SET views=?, reactions=? WHERE owner_id=? AND msg_id=?",
                            (views, reactions_json, owner_id, msg_id)
                        )
                        logger.debug(
                            "Watcher sync for owner=%s msg=%s → views=%s reactions=%s",
                            owner_id, msg_id, views, reactions_json,
                        )
            except Exception as e:
                logger.debug("Watcher sync failed for owner=%s msg=%s: %s", owner_id, msg_id, e)

        now_iso = datetime.now(timezone.utc).isoformat()
        await self._upsert_thread_message(
            owner_id=owner_id,
            chat_id=chat_id,
            msg_id=msg_id,
            chat_title=chat_title,
            chat_username=chat_username,
            sender_id=sender_id,
            sender_username=sender_username,
            sender_display_name=sender_display_name,
            sender_handle=f"@{sender_username}" if sender_username else "",
            is_outgoing=bool(getattr(getattr(event, "message", None) or event, "out", False)),
            reply_to_msg_id=self._reply_to_msg_id(getattr(event, "message", None) or event),
            dialog_type=dialog_type,
            content_type=content_type_value,
            text=text,
            original_text=text,
            media_path=media_path,
            status="active",
            edit_count=0,
            created_at=msg_date_iso,
            updated_at=now_iso,
            deleted_at=None,
            views=views,
            reactions=reactions_json,
        )
        await self._upsert_dialog_record(
            owner_id=owner_id,
            chat_id=chat_id,
            dialog_type=dialog_type,
            title=chat_title,
            username=chat_username,
            last_message_id=msg_id,
            last_message_at=msg_date_iso,
            last_message_preview=text or content_type_value,
            last_sender_id=sender_id,
            last_sender_label="Вы" if bool(getattr(getattr(event, "message", None) or event, "out", False)) else sender_display_name,
            newest_synced_msg_id=msg_id,
        )
        await self._upsert_sync_state(
            owner_id=owner_id,
            chat_id=chat_id,
            dialog_type=dialog_type,
            sync_state="active",
            oldest_synced_msg_id=msg_id,
            newest_synced_msg_id=msg_id,
            last_realtime_sync_at=now_iso,
        )
        if not thread_exists:
            await self._add_thread_revision(
                owner_id=owner_id,
                chat_id=chat_id,
                msg_id=msg_id,
                event_type="created",
                text=text,
                previous_text=None,
                created_at=msg_date_iso,
            )

        local_hour = self._local_hour_from_iso(msg_date_iso)
        is_outgoing = bool(getattr(getattr(event, "message", None) or event, "out", False))
        if local_hour is not None and 0 <= local_hour < 6 and not is_outgoing:
            await self._record_risk_event(
                owner_id=owner_id,
                chat_id=chat_id,
                sender_id=sender_id,
                msg_id=msg_id,
                signal_type="night_activity",
                event_at=msg_date_iso,
                detail=f"{chat_title}: сообщение пришло в {local_hour:02d}:00 по локальному времени.",
                meta={"chat_title": chat_title, "content_type": content_type_value},
            )

        if is_disappearing:
            await self._record_risk_event(
                owner_id=owner_id,
                chat_id=chat_id,
                sender_id=sender_id,
                msg_id=msg_id,
                signal_type="disappearing_media",
                event_at=msg_date_iso,
                detail=f"{chat_title}: обнаружено исчезающее сообщение.",
                meta={"chat_title": chat_title, "content_type": content_type_value},
            )
            payload = {
                "msg_id": msg_id,
                "text": text,
                "original_text": text,
                "edit_count": 0,
                "last_edited_at": None,
                "media_path": media_path,
                "sender_username": sender_name,
                "sender_id": sender_id,
                "chat_title": chat_title,
                "chat_id": chat_id,
                "chat_username": chat_username,
                "message_date": msg_date_iso,
                "content_type": content_type_value,
                "event_type": "disappearing",
            }
            await self.disappearing_queue.put((owner_id, msg_id, payload))
            logger.debug("Disappearing enqueued for delayed processing: %d/%d", owner_id, msg_id)

    async def handle_edited(self, owner_id: int, event: events.MessageEdited.Event) -> None:
        chat_id = event.chat_id
        if not self.is_chat_allowed(chat_id):
            return
        if await self.is_muted_chat(owner_id, chat_id):
            return
        chat_for_filter = await self._safe_get_chat(event, owner_id=owner_id, context="handle_edited_filter")
        sender_for_filter = await self._safe_get_sender(event, owner_id=owner_id, context="handle_edited_filter")
        dialog_kind = self._dialog_kind_from_entity(chat_for_filter, sender_for_filter)
        if not await self._is_dialog_kind_allowed(owner_id, dialog_kind):
            return
        msg_id = event.id
        if not msg_id:
            return
        new_text = event.raw_text or ""
        edited_at_iso = (event.edit_date or datetime.now(timezone.utc)).isoformat()

        row = await self.db.fetchone("""
            SELECT id, text, original_text, edit_count, chat_title, media_path,
                   sender_id, sender_username, message_date, chat_id, chat_username
            FROM pending WHERE owner_id = ? AND msg_id = ?
        """, (owner_id, msg_id))

        if not row:
            chat = chat_for_filter or await self._safe_get_chat(event, owner_id=owner_id, context="handle_edited_missing_row")
            await self.sync_message_snapshot(owner_id, event, chat=chat)
            return

        row_id, old_text, original_text, edit_count, chat_title, media_path, \
        sender_id, sender_username, message_date, chat_id, chat_username = row

        old_text = old_text or ""
        if original_text is None:
            original_text = old_text

        if new_text == old_text:
            return

        new_edit_count = int(edit_count or 0) + 1

        chat = chat_for_filter or await self._safe_get_chat(event, owner_id=owner_id, context="handle_edited")
        dialog_type = "bot" if dialog_kind == "bot" else self._dialog_type_from_entity(chat)
        event_sender = sender_for_filter or await self._safe_get_sender(event, owner_id=owner_id, context="handle_edited")
        _, current_sender_username, current_sender_display_name = self._sender_snapshot(event_sender, message=getattr(event, "message", None) or event)
        sender_username = current_sender_username or (sender_username or "")
        sender_display_name = current_sender_display_name or (sender_username or "")

        msg_obj = getattr(event, 'message', None) or event
        current_views = getattr(msg_obj, 'views', None)
        current_reactions = extract_reactions_json(msg_obj)

        # Keep existing rows if we can't parse current values.
        row_views, row_reactions = await self.db.fetchone(
            "SELECT views, reactions FROM pending WHERE id = ?", (row_id,)
        ) or (None, None)

        if current_views is None:
            current_views = int(row_views or 0)
        if not current_reactions or current_reactions == '{}':
            current_reactions = row_reactions or '{}'

        await self.db.execute("""
            UPDATE pending
            SET text = ?, original_text = ?, edit_count = ?, last_edited_at = ?, views = ?, reactions = ?
            WHERE id = ?
        """, (
            new_text,
            original_text,
            new_edit_count,
            edited_at_iso,
            current_views,
            current_reactions,
            row_id
        ))

        thread_content_type = guess_content_type_from_path(media_path)
        await self._upsert_thread_message(
            owner_id=owner_id,
            chat_id=chat_id,
            msg_id=msg_id,
            chat_title=chat_title,
            chat_username=chat_username,
            sender_id=sender_id,
            sender_username=sender_username,
            sender_display_name=sender_display_name,
            sender_handle=f"@{sender_username}" if sender_username else "",
            is_outgoing=bool(getattr(msg_obj, "out", False)),
            reply_to_msg_id=self._reply_to_msg_id(msg_obj),
            dialog_type=dialog_type,
            content_type=thread_content_type,
            text=new_text,
            original_text=original_text,
            media_path=media_path,
            status="edited",
            edit_count=new_edit_count,
            created_at=message_date,
            updated_at=edited_at_iso,
            deleted_at=None,
            views=int(current_views or 0),
            reactions=current_reactions or "{}",
        )
        await self._upsert_dialog_record(
            owner_id=owner_id,
            chat_id=chat_id,
            dialog_type=dialog_type,
            title=chat_title or self._dialog_title_from_entity(chat, chat_id),
            username=chat_username or self._dialog_username_from_entity(chat),
            last_message_id=msg_id,
            last_message_at=edited_at_iso,
            last_message_preview=new_text or thread_content_type,
            last_sender_id=sender_id,
            last_sender_label="Вы" if bool(getattr(msg_obj, "out", False)) else sender_display_name,
            newest_synced_msg_id=msg_id,
        )
        await self._upsert_sync_state(
            owner_id=owner_id,
            chat_id=chat_id,
            dialog_type=dialog_type,
            sync_state="active",
            oldest_synced_msg_id=msg_id,
            newest_synced_msg_id=msg_id,
            last_realtime_sync_at=edited_at_iso,
        )
        await self._add_thread_revision(
            owner_id=owner_id,
            chat_id=chat_id,
            msg_id=msg_id,
            event_type="edited",
            text=new_text,
            previous_text=old_text,
            created_at=edited_at_iso,
        )
        recorded = await self._record_risk_event(
            owner_id=owner_id,
            chat_id=chat_id,
            sender_id=sender_id,
            msg_id=msg_id,
            signal_type="message_edit",
            event_at=edited_at_iso,
            detail=f"{chat_title}: сообщение было отредактировано.",
            meta={"chat_title": chat_title, "before": old_text[:200], "after": new_text[:200]},
            dedupe_key=f"message_edit:{chat_id}:{msg_id}:{edited_at_iso}",
        )
        if recorded:
            await self._emit_burst_signal_if_needed(
                owner_id=owner_id,
                chat_id=chat_id,
                sender_id=sender_id,
                signal_type="message_edit",
                event_at=edited_at_iso,
            )

        if _is_minor_edit(original_text, new_text):
            return

        content_type = thread_content_type

        payload = {
            "msg_id": msg_id,
            "text": new_text,
            "original_text": original_text,
            "edit_count": new_edit_count,
            "last_edited_at": edited_at_iso,
            "media_path": media_path,
            "sender_username": sender_username,
            "sender_id": sender_id,
            "chat_title": chat_title,
            "chat_id": chat_id,
            "message_date": message_date,
            "edited_at": edited_at_iso,
            "content_type": content_type,
            "event_type": "edited",
        }

        try:
            log_owner_event(owner_id, event_kind="message_edited", data={
                "message_id": msg_id, "chat_id": chat_id, "chat_title": chat_title,
                "sender_id": sender_id, "sender_username": sender_username,
                "content_type": content_type, "before_text": original_text,
                "after_text": new_text, "edit_count": new_edit_count,
                "edited_at": edited_at_iso, "message_date": message_date,
                "media_path": media_path,
            })
        except Exception:
            logger.exception("Failed to log edited event")

        await self.aggregator.add_event(owner_id, payload)

    async def handle_deleted(self, owner_id: int, event: events.MessageDeleted.Event) -> None:
        await self.delete_queue.put((owner_id, event))

    async def _delete_worker(self) -> None:
        while True:
            try:
                owner_id, event = await self.delete_queue.get()
                await self._process_delete(owner_id, event)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Delete worker crashed")
            finally:
                try:
                    self.delete_queue.task_done()
                except Exception:
                    pass

    async def _process_delete(self, owner_id: int, event: events.MessageDeleted.Event) -> None:
        """
        Надёжное перемещение удалённых сообщений из pending в deleted_messages.
        - Обработка ошибок на каждом этапе
        - Дедупликация (не вставляем дубликаты)
        - Atomicity: либо полностью успех, либо откат
        - Полное логирование каждого действия
        """
        event_chat_id = getattr(event, "chat_id", None)
        
        # Проверка разрешённых чатов
        if event_chat_id is not None and not self.is_chat_allowed(event_chat_id):
            return
            
        deleted_ids = set(event.deleted_ids or [])
        if not deleted_ids:
            return

        # Попытка получить реальный chat_id из события (важно для каналов)
        final_chat_id = event_chat_id
        try:
            if hasattr(event, "get_chat"):
                chat = await event.get_chat()
                final_chat_id = getattr(chat, "id", None) or final_chat_id
        except Exception as e:
            logger.debug("Failed to get_chat in _process_delete for owner=%s: %s", owner_id, e)

        # Owner guard: only process if owner still exists in bot_users (сессия активна)
        valid_owner = await self.db.fetchone("SELECT 1 FROM bot_users WHERE user_id=?", (owner_id,))
        if not valid_owner:
            logger.warning("Delete processing skip: owner %s not found in bot_users", owner_id)
            return

        # Финальная проверка muted chats (используем finalized chat_id)
        if final_chat_id and await self.is_muted_chat(owner_id, final_chat_id):
            logger.debug("Chat %s is muted for owner %s, skipping delete processing", final_chat_id, owner_id)
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        processed_count = 0
        skipped_ids = []
        error_ids = []

        for msg_id in deleted_ids:
            try:
                # === STEP 1: Найти запись в pending ===
                row = None
                pending_lookup_ambiguous = False
                for attempt in range(1, 26):
                    try:
                        base_query = """
                            SELECT id, chat_title, text, original_text, edit_count, last_edited_at,
                                   media_path, sender_id, sender_username, message_date, chat_id,
                                   chat_username, COALESCE(already_forwarded, 0),
                                   COALESCE(views, 0), COALESCE(reactions, '{}'),
                                   COALESCE(is_disappearing, 0), COALESCE(content_type, '')
                            FROM pending WHERE owner_id = ? AND msg_id = ?
                        """
                        if final_chat_id is not None:
                            row = await self.db.fetchone(
                                base_query + " AND chat_id = ? ORDER BY id DESC LIMIT 1",
                                (owner_id, msg_id, final_chat_id),
                            )
                        else:
                            pending_rows = await self.db.fetchall(
                                base_query + " ORDER BY id DESC",
                                (owner_id, msg_id),
                            )
                            chat_ids = {int(candidate[10]) for candidate in pending_rows if candidate[10] is not None}
                            if len(pending_rows) == 1 or len(chat_ids) == 1:
                                row = pending_rows[0] if pending_rows else None
                            elif pending_rows:
                                logger.warning(
                                    "Skip pending delete without chat_id: owner=%s msg_id=%s matched_chats=%s",
                                    owner_id,
                                    msg_id,
                                    sorted(chat_ids),
                                )
                                pending_lookup_ambiguous = True
                                row = None
                        if row:
                            break
                        if pending_lookup_ambiguous:
                            break
                    except Exception as e:
                        logger.debug("Attempt %d to fetch pending row failed: %s", attempt, e)
                    if attempt < 25:
                        await asyncio.sleep(0.07)

                if not row:
                    if pending_lookup_ambiguous:
                        skipped_ids.append(msg_id)
                        continue
                    logger.debug("Message %d not found in pending for owner %s (normal for external deletes)",
                                msg_id, owner_id)
                    fallback_marked = await self._mark_thread_deleted_fallback(
                        owner_id=owner_id,
                        msg_id=msg_id,
                        chat_id=final_chat_id,
                        deleted_at=now_iso,
                    )
                    if fallback_marked:
                        processed_count += 1
                    else:
                        skipped_ids.append(msg_id)
                    continue

                (row_id, chat_title, text, original_text, edit_count, last_edited_at,
                 media_path, sender_id, sender_username, message_date, db_chat_id,
                 chat_username, already_forwarded, row_views, row_reactions,
                 is_disappearing_row, pending_content_type) = row

                logger.debug("delete_fetch owner=%s msg=%s row_views=%s row_reactions=%s", owner_id, msg_id, row_views, row_reactions)

                # Нормализуем текст
                text = text or ""
                original_text = original_text or text
                edit_count = int(edit_count or 0)
                row_views = int(row_views or 0)
                row_reactions = row_reactions or '{}'                
                # РСЃРїРѕР»СЊР·СѓРµРј chat_id РёР· Р±Р°Р·С‹ (РЅР°РёР±РѕР»РµРµ РЅР°РґС‘Р¶РЅС‹Р№ РёСЃС‚РѕС‡РЅРёРє РґР»СЏ СЌС‚РѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ)
                final_msg_chat_id = db_chat_id or final_chat_id or event_chat_id
                if final_msg_chat_id is None:
                    logger.warning("Skip delete without resolved chat_id: owner=%s msg_id=%s", owner_id, msg_id)
                    skipped_ids.append(msg_id)
                    continue

                # === STEP 2: Проверить дедупликацию ===
                existing = await self.db.fetchone(
                    "SELECT id FROM deleted_messages WHERE owner_id=? AND chat_id=? AND msg_id=?",
                    (owner_id, final_msg_chat_id, msg_id)
                )
                if existing:
                    logger.debug("Message %d already in deleted_messages for owner %s, cleaning up pending",
                                owner_id, msg_id)
                    await self._upsert_thread_message(
                        owner_id=owner_id,
                        chat_id=final_msg_chat_id,
                        msg_id=msg_id,
                        chat_title=chat_title,
                        chat_username=chat_username,
                        sender_id=sender_id,
                        sender_username=sender_username,
                        content_type=guess_content_type_from_path(media_path),
                        text=text,
                        original_text=original_text,
                        media_path=media_path,
                        status="deleted",
                        edit_count=edit_count,
                        created_at=message_date or now_iso,
                        updated_at=now_iso,
                        deleted_at=now_iso,
                        views=int(row_views or 0),
                        reactions=row_reactions or "{}",
                    )
                    try:
                        await self.db.execute("DELETE FROM pending WHERE id=?", (row_id,))
                    except Exception as e:
                        logger.warning("Failed to delete pending row %d after dedup check: %s", row_id, e)
                    skipped_ids.append(msg_id)
                    continue

                # === STEP 3: Определить тип контента ===
                content_type = pending_content_type or guess_content_type_from_path(media_path)

                # === STEP 3.5: Получить views и reactions ===
                views = row_views if 'row_views' in locals() else 0
                reactions_json = row_reactions if 'row_reactions' in locals() else '{}'
                try:
                    # Пробуем получить последнее состояние сообщения из Telegram
                    if self.watcher_service:
                        client = self.watcher_service.watched_clients.get(owner_id)
                        if client:
                            try:
                                messages = await asyncio.wait_for(
                                    client.get_messages(final_msg_chat_id, ids=msg_id, important=True, limit=1),
                                    timeout=8.0
                                )
                                # get_messages может вернуть Message, список Message, или None
                                msg = None
                                if isinstance(messages, list):
                                    msg = messages[0] if messages else None
                                elif messages is not None:
                                    msg = messages

                                if msg:
                                    tmp_views = getattr(msg, 'views', None)
                                    if tmp_views is not None:
                                        views = tmp_views or 0

                                    tmp_reactions = extract_reactions_json(msg)
                                    if tmp_reactions and tmp_reactions != '{}':
                                        reactions_json = tmp_reactions

                                    # Если не удалось получить реакции, пробуем вызвать to_dict
                                    if reactions_json == '{}':
                                        try:
                                            msg_dict = msg.to_dict() if hasattr(msg, 'to_dict') else {}
                                            if isinstance(msg_dict, dict):
                                                raw_reactions = msg_dict.get('reactions') or msg_dict.get('reactions_data')
                                                if raw_reactions:
                                                    tmp_reactions2 = extract_reactions_json(raw_reactions)
                                                    if tmp_reactions2 and tmp_reactions2 != '{}':
                                                        reactions_json = tmp_reactions2
                                                        logger.debug("Fallback to msg_dict reactions for msg %d: %s", msg_id, reactions_json)
                                        except Exception:
                                            pass

                                    logger.debug("Retrieved msg views=%s, reactions=%s for msg %d (fallback row views=%s, reactions=%s)",
                                                 views, reactions_json, msg_id, row_views, row_reactions)
                            except asyncio.TimeoutError:
                                logger.debug("Timeout getting message state for views/reactions: %d/%d", owner_id, msg_id)
                            except Exception as e:
                                logger.debug("Failed to get message state for views/reactions: %s", e)
                except Exception as e:
                    logger.debug("Error in views/reactions extraction block: %s", e)

                await self._upsert_thread_message(
                    owner_id=owner_id,
                    chat_id=final_msg_chat_id,
                    msg_id=msg_id,
                    chat_title=chat_title,
                    chat_username=chat_username,
                    sender_id=sender_id,
                    sender_username=sender_username,
                    content_type=content_type,
                    text=text,
                    original_text=original_text,
                    media_path=media_path,
                    status="deleted",
                    edit_count=edit_count,
                    created_at=message_date or now_iso,
                    updated_at=now_iso,
                    deleted_at=now_iso,
                    views=views,
                    reactions=reactions_json,
                )
                await self._add_thread_revision(
                    owner_id=owner_id,
                    chat_id=final_msg_chat_id,
                    msg_id=msg_id,
                    event_type="deleted",
                    text=text,
                    previous_text=None,
                    created_at=now_iso,
                )
                await self._upsert_sync_state(
                    owner_id=owner_id,
                    chat_id=final_msg_chat_id,
                    sync_state="active",
                    newest_synced_msg_id=msg_id,
                    oldest_synced_msg_id=msg_id,
                    last_realtime_sync_at=now_iso,
                )

                # === STEP 4: INSERT в deleted_messages (атомарно) ===
                try:
                    await self.db.execute("""
                        INSERT INTO deleted_messages (
                            owner_id, chat_id, chat_title, chat_username, msg_id,
                            sender_id, sender_username, content_type, text_preview,
                            text_full, original_text_preview, original_text_full,
                            edit_count, last_edited_at, media_path,
                            original_timestamp, saved_at, views, reactions
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        owner_id, final_msg_chat_id, chat_title, chat_username, msg_id,
                        sender_id, sender_username, content_type,
                        text[:50] if text else "",
                        text or "",
                        original_text[:50] if original_text else "",
                        original_text or "",
                        edit_count, last_edited_at,
                        media_path, message_date, now_iso, views, reactions_json
                    ))
                    logger.debug("Inserted deleted_message %d/%d into DB", owner_id, msg_id)
                except Exception as e:
                    logger.error("CRITICAL: Failed to INSERT deleted_message %d for owner %s: %s",
                                msg_id, owner_id, e)
                    error_ids.append(msg_id)
                    continue  # Не удаляем из pending, чтобы не потерять

                # === STEP 5: DELETE из pending (атомарно) ===
                try:
                    await self.db.execute("DELETE FROM pending WHERE id=?", (row_id,))
                    logger.debug("Deleted pending row %d for message %d", row_id, msg_id)
                except Exception as e:
                    logger.error("CRITICAL: Failed to DELETE pending row %d (msg %d): %s",
                                row_id, msg_id, e)
                    # Запись уже в deleted_messages, но осталась в pending
                    # Это не критично, но логируем для отслеживания

                # === STEP 6: Логирование события ===
                try:
                    log_owner_event(owner_id, event_kind="message_deleted", data={
                        "message_id": msg_id, 
                        "chat_id": final_msg_chat_id, 
                        "chat_title": chat_title,
                        "sender_id": sender_id, 
                        "sender_username": sender_username,
                        "content_type": content_type, 
                        "text": text, 
                        "original_text": original_text,
                        "edit_count": edit_count, 
                        "last_edited_at": last_edited_at,
                        "media_path": media_path, 
                        "message_date": message_date,
                        "deleted_at": now_iso,
                    })
                except Exception as e:
                    logger.debug("Failed to log delete event for %d: %s", msg_id, e)

                recorded = await self._record_risk_event(
                    owner_id=owner_id,
                    chat_id=final_msg_chat_id,
                    sender_id=sender_id,
                    msg_id=msg_id,
                    signal_type="message_delete",
                    event_at=now_iso,
                    detail=f"{chat_title}: сообщение было удалено.",
                    meta={"chat_title": chat_title, "content_type": content_type, "edit_count": edit_count},
                    dedupe_key=f"message_delete:{final_msg_chat_id}:{msg_id}",
                )
                if recorded:
                    await self._emit_burst_signal_if_needed(
                        owner_id=owner_id,
                        chat_id=final_msg_chat_id,
                        sender_id=sender_id,
                        signal_type="message_delete",
                        event_at=now_iso,
                    )

                # === STEP 7: Отправить владельцу (если не отправляли ранее) ===
                if not already_forwarded:
                    event_type_to_send = "disappearing" if int(is_disappearing_row or 0) == 1 else "deleted"
                    payload = {
                        "msg_id": msg_id,
                        "text": text,
                        "original_text": original_text,
                        "edit_count": edit_count,
                        "last_edited_at": last_edited_at,
                        "media_path": media_path,
                        "sender_username": sender_username,
                        "sender_id": sender_id,
                        "chat_title": chat_title,
                        "chat_id": final_msg_chat_id,
                        "message_date": message_date,
                        "deleted_at": now_iso,
                        "content_type": content_type,
                        "event_type": event_type_to_send,
                        "views": views,
                        "reactions": reactions_json,
                    }
                    try:
                        await self.aggregator.add_event(owner_id, payload)
                        logger.debug("Queued delete event for forwarding: %d/%d", owner_id, msg_id)
                    except Exception as e:
                        logger.error("Failed to queue delete event for owner %s msg %d: %s",
                                    owner_id, msg_id, e)

                # === STEP 8: Очистить старые записи ===
                try:
                    await self.db.clean_old_records(owner_id)
                except Exception as e:
                    logger.debug("Failed to clean old records for owner %s: %s", owner_id, e)

                processed_count += 1

            except Exception as e:
                logger.exception("Unhandled error processing delete msg_id=%d owner=%s",
                                msg_id, owner_id)
                error_ids.append(msg_id)

        # === Финальное логирование ===
        if processed_count > 0 or skipped_ids or error_ids:
            logger.info(
                "Delete batch processed: owner=%s processed=%d skipped=%d errors=%d (ids=%s)",
                owner_id, processed_count, len(skipped_ids), len(error_ids),
                error_ids[:5] if error_ids else "none"
            )


async def check_user_allowed(db: Database, user_id: int) -> bool:
    row = await db.fetchone(
        "SELECT status FROM access_requests WHERE user_id=?",
        (user_id,)
    )

    if not row:
        return False

    return bool(row[0])

async def is_user_fully_approved(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if user_id in CONFIG.admin_ids:
        return True

    app = context.application.bot_data.get("app")
    if not app:
        return False

    row = await app.db.fetchone(
        "SELECT approved FROM users WHERE user_id = ?",
        (user_id,)
    )

    return bool(row and row[0] == 1)

async def send_admin_approval_request(db: Database, bot, user_id: int, username: Optional[str]):

    now = datetime.now(timezone.utc).isoformat()

    try:
        await db.execute(
            """
            UPDATE bot_users
            SET requested_at=?
            WHERE user_id=?
            """,
            (now, user_id)
        )
    except Exception as e:
        logger.exception("Failed to update bot_users.requested_at for %s: %s", user_id, e)

    try:
        await db.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, last_name, first_seen_at, requested_at, approved)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (user_id, username or None, None, None, now, now)
        )
    except Exception as e:
        logger.exception("Failed to insert/update users for %s: %s", user_id, e)

    if not CONFIG.admin_ids:
        return

    text = (
        f"👤 Новый пользователь хочет доступ\n\n"
        f"ID: {user_id}\n"
        f"Username: {username}\n\n"
        f"Разрешить доступ?"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Разрешить", callback_data=f"approve_user:{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"deny_user:{user_id}")
    ]])

    for admin in CONFIG.admin_ids:
        try:
            await bot.send_message(
                admin,
                text,
                reply_markup=keyboard
            )
        except Exception:
            logger.exception("Failed notify admin")
