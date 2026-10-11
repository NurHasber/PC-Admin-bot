from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ChatAction, ChatMemberStatus, ChatType, ContentType
from aiogram.filters import BaseFilter, CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, TelegramObject

from .chat_allowlist import ChatAllowlistStore, extract_invite_link
from .chat_history import ChatHistoryStore
from .chat_sessions import chat_key, private_key
from .config import Settings
from .cursor_bridge import CursorBridge
from .file_buffer import FileBuffer
from .history_intent import detect_history_need, split_quoted_tag
from .history_scope import (
    PendingHistoryAsk,
    month_clarification,
    month_needs_choice,
    normalize_scope_text,
    parse_period,
    parse_scope_answer,
    scope_question,
    start_ack,
)
from .local_panel import LocalStatusPanel
from .mentions import extract_shared_prompt, is_shared_chat
from .outbox import MAX_DOWNLOAD_BYTES
from .pending_jobs import PendingJob
from .safety_gate import SafetyGate
from .telegram_format import send_formatted, send_saved_files
from .telegram_ui import (
    BTN_CHAT_AGENT,
    BTN_CHATS,
    BTN_MODEL,
    BTN_NEW_TOPIC,
    BTN_TOPICS,
    CB_CHAT_AGENT_PREFIX,
    CB_CHAT_AGENT_PRIVATE,
    CB_CHAT_FORGET_PREFIX,
    CB_DELIVERY_CHAT,
    CB_DELIVERY_DM,
    CB_LEAVE_ASK_PREFIX,
    CB_LEAVE_CANCEL,
    CB_LEAVE_OK_PREFIX,
    CB_MODEL_PREFIX,
    CB_TOPIC_PREFIX,
    CONTROL_BUTTON_TEXTS,
    chat_agents_inline_keyboard,
    delivery_inline_keyboard,
    leave_confirm_keyboard,
    main_reply_keyboard,
    membership_chats_keyboard,
    models_inline_keyboard,
    topics_inline_keyboard,
)

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], None]

SHARED_CHAT_SYSTEM_PREFIX = (
    "You are answering in a shared Telegram {chat_kind} titled \"{title}\". "
    "Everyone in that chat can see your reply. "
    "When the request contains [quoted_message_id=N], the [chat_messages] block "
    "includes that message as a #N line — answer from that text (a link in the "
    "line is the message; a webpage preview picture is not a separate file). "
    "When the request asks to find something in the chat, [chat_messages] is the "
    "full set the user confirmed: every match, or the whole chat, optionally "
    "only inside the period they named. Nothing is omitted for length. "
    "Use those lines and do not say where they came from. If a requested #id is "
    "absent from the block, that message is unavailable — say so in one sentence "
    "and do not invent its text. "
    "Do not reveal secrets, API keys, passwords, tokens, .env contents, personal data, "
    "or credentials. If the request would expose secrets/credentials, give a short "
    "redacted answer or ask to continue in a private DM — never dump secrets into "
    "the shared chat. That DM redirect is ONLY for secrets/credentials. "
    "CRITICAL — never mention in this shared chat any internal bot plumbing: "
    "SQLite, local database, chat history storage/logging, how messages are stored "
    "or processed on the PC, Cursor SDK, env files, allowlist, or similar "
    "implementation details. Speak like a normal assistant. "
    "NEVER suggest moving to DM for chat-member research, \"who wrote what\", "
    "message analysis, history lookup, or similar. End with the public answer only; "
    "no meta-offers about private follow-up except for secrets/credentials. "
    "The reply is shown in Telegram. Use **bold**, _italic_, and lists. "
    "Never use a Markdown pipe table or an ASCII grid — Telegram does not render them. "
    "For several dated notes, write one block per note: first line **date · who**, "
    "then the point, with a blank line between notes."
)

DM_CHAT_AGENT_SYSTEM_PREFIX = (
    "You are the Cursor agent bound to Telegram {chat_kind} \"{title}\". "
    "The admin is talking to you in a private DM right now — keep replies in this DM. "
    "This session shares context with @mentions of you and Replies to your "
    "messages in that chat. "
    "A [chat_messages] block, when present, is the quoted message and/or the "
    "chat lines this turn needs. Use those lines; never describe how they were "
    "obtained. If a requested #id is absent, that message is unavailable. "
    "Do not assume your text will be posted to the group unless the admin asks you "
    "to draft something for the group. "
    "When drafting text that might be posted to the shared chat, never include "
    "internal details (SQLite, local DB, history logging, Cursor SDK, env, allowlist) "
    "and never add offers to continue chat-history analysis in DM. "
    "Format for Telegram: **bold**, _italic_, lists. "
    "Never use a Markdown pipe table or an ASCII grid. "
    "For several dated notes, write one block per note: **date · who**, then the point."
)

_IN_CHAT_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.RESTRICTED,
    ChatMemberStatus.CREATOR,
}


class AdminFilter(BaseFilter):
    def __init__(self, admin_id: int) -> None:
        self.admin_id = admin_id

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id == self.admin_id)


class _GroupMentionLogMiddleware(BaseMiddleware):
    """Log every group/channel message so silent drops are visible in the log."""

    def __init__(self, admin_id: int, bot_username: str) -> None:
        self._admin_id = admin_id
        self._bot_username = bot_username

    async def __call__(self, handler, event: Message, data):
        if is_shared_chat(event):
            text = event.text or event.caption or ""
            uid = event.from_user.id if event.from_user else None
            logger.info(
                "Shared update chat=%s type=%s from=%s admin=%s text=%r",
                event.chat.id,
                event.chat.type,
                uid,
                uid == self._admin_id,
                text[:160],
            )
        return await handler(event, data)


class TelegramBotService:
    def __init__(
        self,
        settings: Settings,
        bridge: CursorBridge,
        on_status: StatusCallback | None = None,
        allowlist: ChatAllowlistStore | None = None,
        history: ChatHistoryStore | None = None,
    ) -> None:
        self._settings = settings
        self._bridge = bridge
        self._on_status = on_status or (lambda _s: None)
        self._allowlist = allowlist or ChatAllowlistStore()
        self._history = history or ChatHistoryStore()
        self._gate = SafetyGate(
            password=settings.confirm_password,
            elevation_seconds=settings.confirm_elevation_seconds,
        )
        self._bot = Bot(token=settings.telegram_bot_token)
        self._dp = Dispatcher()
        self._bot_username = ""
        self._bot_id: int | None = None
        self._bot_display_name = ""
        self._admin_username = ""
        self._admin_display_name = ""
        self._started_at = time.time()
        self._bridge.set_usage_hook(self._history.record_agent_run)
        self._files = FileBuffer(max_bytes=settings.file_buffer_max_bytes)
        self._pending_job: PendingJob | None = None
        self._history_pending: dict[int, PendingHistoryAsk] = {}
        # Per-chat activity for the localhost status panel (tray stays global).
        self._chat_activity: dict[int, dict[str, object]] = {}
        self._global_status = "Starting…"
        self._panel: LocalStatusPanel | None = None
        self._group_log_mw = _GroupMentionLogMiddleware(
            settings.telegram_admin_user_id,
            "",
        )
        # outer = runs for every message update, even if no handler matches
        self._dp.message.outer_middleware(self._group_log_mw)
        self._register_handlers()

    def _set_status(self, status: str) -> None:
        self._global_status = status
        self._on_status(status)

    def panel_url(self) -> str:
        if self._panel is not None:
            return self._panel.url
        return (
            f"http://{self._settings.status_panel_host}:"
            f"{self._settings.status_panel_port}/"
        )

    def _touch_chat_activity(
        self,
        chat_id: int,
        *,
        activity: str,
        detail: str = "",
    ) -> None:
        self._chat_activity[chat_id] = {
            "activity": activity,
            "detail": detail[:200],
            "updated_at": time.time(),
        }

    def status_snapshot(self) -> dict:
        """JSON-serializable snapshot for the local status panel."""
        counts = self._history.counts_by_chat()
        chats_out: list[dict] = []
        ids = set(self._allowlist.allowed_ids()) | set(counts.keys())
        for chat_id in sorted(ids):
            key = chat_key(chat_id)
            session = self._bridge.chats.get(key)
            title = session.title if session else f"chat {chat_id}"
            act = self._chat_activity.get(chat_id) or {}
            chats_out.append(
                {
                    "chat_id": chat_id,
                    "title": title,
                    "agent_id": (session.agent_id if session else "") or "",
                    "history_count": counts.get(chat_id, 0),
                    "activity": act.get("activity") or "idle",
                    "last_detail": act.get("detail") or "",
                }
            )

        if self._gate.locked:
            gate_label = "LOCKED"
        elif self._gate.awaiting_password:
            gate_label = "awaiting password"
        elif self._gate.is_elevated():
            left = self._gate.elevation_remaining_seconds()
            gate_label = f"elevated ~{left // 60}m {left % 60}s"
        elif self._gate.enabled:
            gate_label = "on (password for risky)"
        else:
            gate_label = "off"

        return {
            "global_status": self._global_status,
            "model": self._bridge.selected_model,
            "dm_session_label": self._dm_context_label(),
            "dm_session_key": self._dm_session_key(),
            "bridge_ready": self._bridge.ready,
            "bridge_last_error": self._bridge.last_error,
            "gate_label": gate_label,
            "gate_locked": self._gate.locked,
            "gate_elevated": self._gate.is_elevated(),
            "history_total": self._history.total_count(),
            "chats": chats_out,
            "bot_username": self._bot_username,
            "bot_id": self._bot_id,
            "bot_display_name": self._bot_display_name,
            "admin_user_id": self._settings.telegram_admin_user_id,
            "admin_username": self._admin_username,
            "admin_display_name": self._admin_display_name,
            "uptime_seconds": max(0, int(time.time() - self._started_at)),
            "usage": self._history.usage_summary(),
            "file_buffer": self._file_buffer_snapshot(),
        }

    async def _load_admin_profile(self) -> None:
        try:
            chat = await self._bot.get_chat(self._settings.telegram_admin_user_id)
        except Exception:
            logger.info("Could not load admin Telegram profile")
            return
        self._admin_username = (getattr(chat, "username", None) or "").strip()
        parts = [
            (getattr(chat, "first_name", None) or "").strip(),
            (getattr(chat, "last_name", None) or "").strip(),
        ]
        self._admin_display_name = " ".join(p for p in parts if p).strip()

    def history_page(
        self,
        *,
        chat_id: int | None = None,
        sender: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        text_query: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict:
        return self._history.query_messages(
            chat_id=chat_id,
            sender=sender,
            date_from=date_from,
            date_to=date_to,
            text_query=text_query,
            page=page,
            page_size=page_size,
        )

    async def _delete_user_message(self, message: Message) -> None:
        """Remove sensitive user input (e.g. password) from chat history."""
        try:
            await message.delete()
        except Exception:
            logger.warning("Could not delete user message chat_id=%s", message.chat.id)

    async def _maybe_refresh_reply_keyboard(self, message: Message) -> None:
        """Telegram keeps the old reply keyboard until we send a new one."""
        if is_shared_chat(message):
            return
        if not self._bridge.store.needs_reply_keyboard_refresh():
            return
        await message.answer(
            "Меню обновлено:\n"
            "Модель · Темы · Новая тема · Чат-агент · Чаты",
            reply_markup=main_reply_keyboard(),
        )
        self._bridge.store.mark_reply_keyboard_current()

    def _dm_session_key(self) -> str:
        key = self._bridge.store.state.dm_session_key or private_key()
        if key != private_key() and not key.startswith("chat:"):
            return private_key()
        return key

    def _dm_context_label(self) -> str:
        key = self._dm_session_key()
        if key == private_key():
            topic = self._bridge.store.state.active_topic()
            return topic.title if topic else "?"
        session = self._bridge.chats.get(key)
        if session is not None:
            return f"чат «{session.title}»"
        return key

    def _status_line(self) -> str:
        if self._gate.locked:
            return "Locked"
        return f"Running · {self._bridge.selected_model} · {self._dm_context_label()}"

    def _clear_pending_job(self) -> None:
        self._pending_job = None

    def _file_buffer_snapshot(self) -> dict[str, object]:
        stats = self._files.stats()
        return {
            **stats,
            "skipped_too_large": self._history.get_counter("skipped_too_large"),
            "skipped_no_room": self._history.get_counter("skipped_no_room"),
            "evicted_files": self._history.get_counter("evicted_files"),
        }

    async def _maybe_store_history(self, message: Message, *, edited: bool = False) -> None:
        if not is_shared_chat(message):
            return
        if not self._allowlist.is_allowed(message.chat.id):
            return
        replied = message.reply_to_message
        if replied is not None and (
            replied.chat is None or replied.chat.id == message.chat.id
        ):
            self._history.store_message(replied)
            await self._buffer_message_media(replied)
        ok = self._history.store_message(message, edited=edited)
        if ok:
            await self._buffer_message_media(message)
            preview = (message.text or message.caption or "")[:80]
            who = ""
            if message.from_user and message.from_user.username:
                who = f"@{message.from_user.username} "
            elif message.from_user:
                who = f"id:{message.from_user.id} "
            self._touch_chat_activity(
                message.chat.id,
                activity="edited" if edited else "msg",
                detail=f"{who}{preview}",
            )
            logger.debug(
                "History stored chat=%s msg=%s edited=%s",
                message.chat.id,
                message.message_id,
                edited,
            )

    def _shared_system_prefix(self, *, title: str, kind: str = "group") -> str:
        return SHARED_CHAT_SYSTEM_PREFIX.format(chat_kind=kind, title=title)

    def _dm_chat_agent_prefix(self, *, title: str, kind: str = "group") -> str:
        return DM_CHAT_AGENT_SYSTEM_PREFIX.format(chat_kind=kind, title=title)

    def _attach_history_if_needed(
        self,
        prompt: str,
        *,
        chat_id: int,
        exclude_message_id: int | None = None,
    ) -> str:
        """Attach one quoted message. A chat-wide pull waits for confirmation."""
        user_text, quoted_id = split_quoted_tag(prompt)
        need = detect_history_need(
            user_text,
            quoted_message_id=quoted_id,
            bot_username=self._bot_username,
        )
        if need.scan_recent or not need.message_ids:
            return prompt
        block = self._history_rows_block(
            chat_id,
            message_ids=need.message_ids,
            exclude_message_ids={exclude_message_id} if exclude_message_id else set(),
        )
        if not block:
            return prompt
        return f"{block}\n\n---\n{prompt}"

    async def _notify_admin_dm(self, text: str, **kwargs) -> bool:
        try:
            await self._bot.send_message(
                self._settings.telegram_admin_user_id,
                text,
                **kwargs,
            )
            return True
        except Exception:
            logger.exception("Failed to DM admin")
            return False

    async def _safe_chat_notice(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        try:
            kwargs: dict = {}
            if reply_to_message_id is not None:
                kwargs["reply_to_message_id"] = reply_to_message_id
            await self._bot.send_message(chat_id, text, **kwargs)
        except Exception:
            logger.info(
                "Could not post notice to chat_id=%s (missing rights?)", chat_id
            )

    def _media_download_target(
        self, message: Message
    ) -> tuple[object, str, int | None] | None:
        mid = message.message_id
        if message.document is not None:
            doc = message.document
            return doc, doc.file_name or f"file_{mid}", doc.file_size
        if message.photo:
            photo = message.photo[-1]
            return photo, f"photo_{mid}.jpg", photo.file_size
        if message.video is not None:
            video = message.video
            return video, video.file_name or f"video_{mid}.mp4", video.file_size
        if message.animation is not None:
            anim = message.animation
            return anim, anim.file_name or f"animation_{mid}.mp4", anim.file_size
        if message.audio is not None:
            audio = message.audio
            return audio, audio.file_name or f"audio_{mid}.mp3", audio.file_size
        if message.voice is not None:
            return message.voice, f"voice_{mid}.ogg", message.voice.file_size
        if message.video_note is not None:
            return message.video_note, f"video_note_{mid}.mp4", message.video_note.file_size
        return None

    async def _buffer_message_media(self, message: Message) -> Path | None:
        """Download one chat file into the bounded buffer. Returns the local path."""
        if message.chat is None:
            return None
        chat_id = message.chat.id
        existing = self._history.get_local_path(chat_id, message.message_id)
        if existing and Path(existing).is_file():
            return Path(existing)
        cached = self._files.find(chat_id, message.message_id)
        if cached is not None and cached.is_file():
            self._history.set_local_path(chat_id, message.message_id, str(cached))
            return cached
        target = self._media_download_target(message)
        if target is None:
            return None
        file_obj, name, size = target
        if size and size > MAX_DOWNLOAD_BYTES:
            self._history.add_counter("skipped_too_large", 1)
            logger.info(
                "Skip file buffer chat=%s msg=%s name=%s size=%s (telegram limit)",
                chat_id,
                message.message_id,
                name,
                size,
            )
            return None
        if not self._files.make_room(size or 0, on_evict=self._note_evicted):
            self._history.add_counter("skipped_no_room", 1)
            logger.info("File buffer full, cannot fit %s", name)
            return None
        dest = self._files.destination(chat_id, message.message_id, name)
        try:
            await self._bot.download(file_obj, destination=dest)
        except Exception:
            logger.exception("Failed to buffer file %s", name)
            if dest.exists():
                dest.unlink(missing_ok=True)
            return None
        if not dest.is_file():
            return None
        if not self._files.make_room(0, protect=dest, on_evict=self._note_evicted):
            dest.unlink(missing_ok=True)
            self._history.add_counter("skipped_no_room", 1)
            return None
        self._history.set_local_path(chat_id, message.message_id, str(dest))
        return dest

    def _note_evicted(self, path: Path) -> None:
        self._history.add_counter("evicted_files", 1)
        ids = self._files.parse_ids(path)
        if ids is not None:
            self._history.clear_local_path(ids[0], ids[1])

    async def _with_local_files(self, message: Message, prompt: str) -> str:
        """Ensure this message's file is in the buffer and pass its path to the agent."""
        path = await self._buffer_message_media(message)
        if path is None:
            target = self._media_download_target(message)
            if target is not None:
                _obj, name, size = target
                if size and size > MAX_DOWNLOAD_BYTES:
                    note = f"(file {name} is too large to download)"
                    block = f"[attached_local_files]\n{note}\n[/attached_local_files]"
                    return f"{prompt.rstrip()}\n\n{block}" if prompt.strip() else block
            return prompt
        block = f"[attached_local_files]\n{path}\n[/attached_local_files]"
        return f"{prompt.rstrip()}\n\n{block}" if prompt.strip() else block

    def _attach_buffered_files(self, prompt: str, chat_id: int) -> str:
        """Give the agent paths of files already buffered for this chat."""
        user_text, quoted_id = split_quoted_tag(prompt)
        need = detect_history_need(
            user_text,
            quoted_message_id=quoted_id,
            bot_username=self._bot_username,
        )
        ids: set[int] = set(need.message_ids)
        if quoted_id is not None:
            ids.add(quoted_id)
        if not ids and not need.wants_files:
            return prompt
        rows = self._history.buffered_files_for(
            chat_id,
            message_ids=ids or None,
            recent_limit=8 if need.wants_files else 0,
        )
        lines: list[str] = []
        for row in rows:
            raw = str(row.get("local_path") or "")
            if not raw or not Path(raw).is_file():
                continue
            lines.append(f"#{int(row['message_id'])} {raw}")
        if not lines:
            return prompt
        block = "[local_files]\n" + "\n".join(lines) + "\n[/local_files]"
        return f"{prompt.rstrip()}\n\n{block}"

    async def _send_agent_result(
        self,
        bot,
        chat_id: int,
        reply: str | None,
        files: list[Path],
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        text = (reply or "").strip()
        has_text = bool(text) and text != "(empty agent reply)"
        if files:
            try:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
            except Exception:
                logger.debug("upload action failed", exc_info=True)
        file_reply = reply_to_message_id
        if has_text:
            await send_formatted(
                bot,
                chat_id,
                text,
                reply_to_message_id=reply_to_message_id,
            )
            file_reply = None
        elif not files:
            await send_formatted(
                bot,
                chat_id,
                "(empty)",
                reply_to_message_id=reply_to_message_id,
            )
            return
        if files:
            await send_saved_files(
                bot,
                chat_id,
                files,
                reply_to_message_id=file_reply,
            )

    async def _forward_to_agent(
        self,
        message: Message,
        text: str,
        *,
        session_key: str,
        chat_title: str | None = None,
        system_prefix: str | None = None,
        reply_in_chat_id: int | None = None,
        reply_to_message_id: int | None = None,
        ack: str | None = None,
    ) -> str | None:
        target_chat = reply_in_chat_id or message.chat.id
        scope_chat = message.chat.id if is_shared_chat(message) else target_chat
        started = time.time()
        if ack is None:
            ack = "Понял, делаю."
        if ack:
            try:
                await message.reply(ack)
            except Exception:
                logger.debug("start acknowledgement failed", exc_info=True)
        await message.bot.send_chat_action(target_chat, ChatAction.TYPING)
        self._set_status("Busy (agent…)")
        self._touch_chat_activity(
            scope_chat, activity="busy", detail=text[:120]
        )
        try:
            if not self._bridge.ready:
                await self._bridge.start()
            reply = await self._bridge.ask(
                text,
                session_key=session_key,
                chat_title=chat_title,
                system_prefix=system_prefix,
            )
            await self._send_agent_result(
                message.bot,
                target_chat,
                reply,
                list(self._bridge.last_deliverables),
                reply_to_message_id=reply_to_message_id,
            )
            self._history.record_reply_latency(
                int((time.time() - started) * 1000),
                chat_id=target_chat,
                session_key=session_key,
            )
            self._touch_chat_activity(
                scope_chat, activity="idle", detail="ok"
            )
            self._set_status(self._status_line())
            return reply
        except Exception as exc:
            logger.exception("Failed to handle admin message")
            self._touch_chat_activity(
                scope_chat, activity="error", detail=str(exc)[:160]
            )
            self._set_status("Error (Cursor)")
            err = f"Agent error: {exc}"
            try:
                if reply_in_chat_id is not None:
                    await self._bot.send_message(reply_in_chat_id, err)
                else:
                    await message.answer(err)
            except Exception:
                logger.exception("Failed to report agent error")
            return None

    async def _ask_delivery_choice(self, job: PendingJob) -> None:
        job.stage = "delivery"
        title = job.chat_title or str(job.source_chat_id)
        sent = await self._notify_admin_dm(
            f"Ответ агента готов (чат «{title}»).\n"
            "Куда отправить?",
            reply_markup=delivery_inline_keyboard(),
        )
        if not sent:
            logger.error("Delivery choice DM failed; dropping pending reply")
            self._clear_pending_job()

    async def _run_shared_sensitive_agent(self, job: PendingJob) -> None:
        self._set_status("Busy (agent…)")
        if not job.started_at:
            job.started_at = time.time()
        self._touch_chat_activity(
            job.source_chat_id, activity="busy", detail=job.prompt[:120]
        )
        try:
            if not self._bridge.ready:
                await self._bridge.start()
            # Prompt already includes history when the job was created.
            reply = await self._bridge.ask(
                job.prompt,
                session_key=job.session_key,
                chat_title=job.chat_title,
                system_prefix=self._shared_system_prefix(
                    title=job.chat_title or job.session_key,
                    kind="group/channel",
                ),
            )
            job.agent_reply = reply
            job.deliverable_paths = [str(p) for p in self._bridge.last_deliverables]
            self._touch_chat_activity(
                job.source_chat_id, activity="delivery", detail="awaiting choice"
            )
            self._set_status(self._status_line())
            await self._ask_delivery_choice(job)
        except Exception as exc:
            logger.exception("Shared sensitive agent run failed")
            self._touch_chat_activity(
                job.source_chat_id, activity="error", detail=str(exc)[:160]
            )
            self._set_status("Error (Cursor)")
            self._clear_pending_job()
            await self._notify_admin_dm(f"Agent error: {exc}")

    async def _begin_shared_sensitive(
        self,
        message: Message,
        prompt: str,
        *,
        session_key: str,
        chat_title: str,
    ) -> None:
        job = PendingJob(
            prompt=prompt,
            session_key=session_key,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            shared_chat=True,
            chat_title=chat_title,
            stage="password",
            started_at=time.time(),
        )
        self._pending_job = job

        await self._safe_chat_notice(
            message.chat.id,
            "Нужно подтверждение в личке с ботом.",
            reply_to_message_id=message.message_id,
        )

        if self._gate.locked:
            await self._notify_admin_dm(self._gate.locked_message())
            self._clear_pending_job()
            self._set_status("Locked")
            return

        if self._gate.is_elevated() or not self._gate.enabled:
            await self._notify_admin_dm(
                f"Чувствительный запрос из «{chat_title}». "
                "Запускаю агента… затем выберете, куда отправить ответ."
            )
            await self._run_shared_sensitive_agent(job)
            return

        challenge = self._gate.begin_challenge(prompt)
        dm_ok = await self._notify_admin_dm(
            f"Чувствительный запрос из «{chat_title}».\n\n{challenge}"
        )
        if not dm_ok:
            await self._safe_chat_notice(
                message.chat.id,
                "Не удалось написать в личку. Открой бота в DM (/start) и повтори.",
                reply_to_message_id=message.message_id,
            )
            self._gate.cancel_challenge()
            self._clear_pending_job()
            return
        self._set_status("Awaiting password")

    async def _handle_invite_link_dm(self, message: Message, text: str) -> bool:
        link = extract_invite_link(text, bot_username=self._bot_username)
        if link is None:
            return False
        pending = self._allowlist.arm_pending(link)
        left_min = max(1, int((pending.expires_at - time.time()) / 60))
        await message.answer(
            "Telegram не даёт боту самому вступить по invite-ссылке.\n\n"
            f"Ссылка принята (ожидание ~{left_min} мин):\n{pending.link}\n\n"
            "Открой её и добавь меня в чат сам. "
            "Останусь только если добавишь ты; если добавит кто-то другой — сразу выйду.\n"
            "После вступления чат появится в «Чат-агент»."
        )
        return True

    _HISTORY_SCOPE_TTL = 30 * 60

    def _history_rows_block(
        self,
        chat_id: int,
        *,
        message_ids: set[int],
        exclude_message_ids: set[int] | None = None,
    ) -> str:
        excl = {int(i) for i in (exclude_message_ids or set()) if int(i) > 0}
        ids = {int(i) for i in message_ids if int(i) > 0 and int(i) not in excl}
        if not ids:
            return ""
        try:
            by_id: dict[int, dict[str, object]] = {}
            for row in self._history.rows_by_ids(chat_id, ids):
                by_id[int(row["message_id"])] = row
            parents: set[int] = set()
            for row in list(by_id.values()):
                parent = row.get("reply_to_message_id")
                if parent is None:
                    continue
                parent_id = int(parent)
                if parent_id not in by_id and parent_id not in excl:
                    parents.add(parent_id)
            if parents:
                for row in self._history.rows_by_ids(chat_id, parents):
                    by_id[int(row["message_id"])] = row
            rows = sorted(
                by_id.values(),
                key=lambda r: (int(r["date_unix"] or 0), int(r["message_id"] or 0)),
            )
            return self._history.format_context_block(rows, max_chars=0)
        except Exception:
            logger.exception("Failed to load quoted messages chat_id=%s", chat_id)
            return ""

    def _confirmed_history_block(
        self,
        pending: PendingHistoryAsk,
        scope,
        exclude_message_ids: set[int],
    ) -> str:
        try:
            by_id: dict[int, dict[str, object]] = {}
            if pending.message_ids:
                seed = {
                    int(i)
                    for i in pending.message_ids
                    if int(i) not in exclude_message_ids
                }
                for row in self._history.rows_by_ids(pending.history_chat_id, seed):
                    by_id[int(row["message_id"])] = row
                parents: set[int] = set()
                for row in list(by_id.values()):
                    parent = row.get("reply_to_message_id")
                    if parent is None:
                        continue
                    parent_id = int(parent)
                    if parent_id not in by_id and parent_id not in exclude_message_ids:
                        parents.add(parent_id)
                if parents:
                    for row in self._history.rows_by_ids(
                        pending.history_chat_id, parents
                    ):
                        by_id[int(row["message_id"])] = row
            names = set(pending.usernames) or None
            if pending.kind == "search":
                found = self._history.search_text(
                    pending.history_chat_id,
                    pending.search_query,
                    usernames=names,
                    exclude_message_ids=exclude_message_ids,
                    start_unix=scope.start_unix,
                    end_unix=scope.end_unix,
                )
            else:
                found = self._history.all_messages(
                    pending.history_chat_id,
                    usernames=names,
                    exclude_message_ids=exclude_message_ids,
                    start_unix=scope.start_unix,
                    end_unix=scope.end_unix,
                )
            for row in found:
                by_id[int(row["message_id"])] = row
            rows = sorted(
                by_id.values(),
                key=lambda r: (int(r["date_unix"] or 0), int(r["message_id"] or 0)),
            )
            block = self._history.format_context_block(rows, max_chars=0)
            if not block:
                block = "[chat_messages]\n(no messages matched)\n"
            logger.info(
                "History confirmed chat_id=%s kind=%s rows=%s",
                pending.history_chat_id,
                pending.kind,
                len(rows),
            )
            return block
        except Exception:
            logger.exception(
                "Failed to load confirmed history chat_id=%s",
                pending.history_chat_id,
            )
            return ""

    async def _ask_history_scope(
        self,
        message: Message,
        prompt: str,
        *,
        history_chat_id: int,
        shared: bool,
        session_key: str,
        chat_title: str,
        system_prefix: str,
    ) -> bool:
        user_text, quoted_id = split_quoted_tag(prompt)
        need = detect_history_need(
            user_text,
            quoted_message_id=quoted_id,
            bot_username=self._bot_username,
        )
        if not need.scan_recent:
            return False
        kind = "search" if need.search_query else "transcript"
        proposed = parse_period(user_text)
        pending = PendingHistoryAsk(
            prompt=prompt,
            history_chat_id=history_chat_id,
            source_message_id=message.message_id,
            kind=kind,
            shared=shared,
            session_key=session_key,
            chat_title=chat_title,
            system_prefix=system_prefix,
            search_query=need.search_query,
            usernames=tuple(sorted(need.usernames)),
            message_ids=tuple(sorted(need.message_ids)),
            proposed=proposed,
            reply_to_message_id=message.message_id,
            asked_at=time.time(),
        )
        if proposed is not None and not proposed.unbounded:
            await self._begin_confirmed_history(message, pending, proposed)
            return True
        if month_needs_choice(user_text):
            question = month_clarification()
        else:
            question = scope_question(kind, need.search_query, proposed)
        sent = await message.reply(question)
        pending.question_message_id = sent.message_id
        self._history_pending[message.chat.id] = pending
        logger.info(
            "History scope asked chat_id=%s kind=%s query=%r",
            history_chat_id,
            kind,
            need.search_query,
        )
        return True

    async def _consume_history_scope(
        self,
        message: Message,
        prompt: str,
        *,
        shared: bool,
    ) -> bool:
        pending = self._history_pending.get(message.chat.id)
        if pending is None:
            return False
        if time.time() - pending.asked_at > self._HISTORY_SCOPE_TTL:
            self._history_pending.pop(message.chat.id, None)
            return False
        user_text, _quoted = split_quoted_tag(prompt)
        if normalize_scope_text(user_text) in {"отмена", "cancel", "/cancel"}:
            self._history_pending.pop(message.chat.id, None)
            await message.reply("Хорошо, этот запрос не выполняю.")
            return True
        scope = parse_scope_answer(user_text, proposed=pending.proposed)
        replied = (
            message.reply_to_message is not None
            and message.reply_to_message.message_id == pending.question_message_id
        )
        if scope is None:
            if replied and len(user_text) <= 80:
                if month_needs_choice(user_text):
                    await message.reply(month_clarification())
                else:
                    await message.reply(
                        "Не разобрал период. Напиши «все», «последний» "
                        "(от сегодня на месяц назад), «текущий» "
                        "или даты, например «с 1 по 10 сентября». "
                        "Отмена — «отмена»."
                    )
                return True
            self._history_pending.pop(message.chat.id, None)
            return False
        await self._begin_confirmed_history(message, pending, scope)
        return True

    async def _begin_confirmed_history(
        self,
        message: Message,
        pending: PendingHistoryAsk,
        scope,
    ) -> None:
        self._history_pending.pop(message.chat.id, None)
        exclude = {pending.source_message_id, message.message_id}
        block = self._confirmed_history_block(pending, scope, exclude)
        text = f"{block}\n\n---\n{pending.prompt}" if block else pending.prompt
        text = self._attach_buffered_files(text, pending.history_chat_id)
        await self._dispatch_confirmed(
            message,
            pending,
            text,
            ack=start_ack(pending.kind, pending.search_query, scope),
        )

    async def _dispatch_confirmed(
        self,
        message: Message,
        pending: PendingHistoryAsk,
        text: str,
        *,
        ack: str,
    ) -> None:
        if pending.shared:
            if self._gate.looks_sensitive(pending.prompt):
                await self._begin_shared_sensitive(
                    message,
                    text,
                    session_key=pending.session_key,
                    chat_title=pending.chat_title,
                )
                return
            await self._forward_to_agent(
                message,
                text,
                session_key=pending.session_key,
                chat_title=pending.chat_title,
                system_prefix=pending.system_prefix,
                reply_to_message_id=pending.reply_to_message_id,
                ack=ack,
            )
            return
        if self._gate.is_suspicious(pending.prompt):
            self._pending_job = PendingJob(
                prompt=text,
                session_key=pending.session_key,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                shared_chat=False,
                chat_title=pending.chat_title,
                stage="password",
            )
            await message.answer(self._gate.begin_challenge(pending.prompt))
            self._set_status("Awaiting password")
            return
        await self._forward_to_agent(
            message,
            text,
            session_key=pending.session_key,
            chat_title=pending.chat_title or None,
            system_prefix=pending.system_prefix or None,
            reply_to_message_id=pending.reply_to_message_id,
            ack=ack,
        )

    async def _handle_admin_prompt(
        self,
        message: Message,
        prompt: str,
        *,
        shared: bool,
    ) -> None:
        if self._gate.locked:
            if shared:
                await self._notify_admin_dm(self._gate.locked_message())
            else:
                await message.answer(self._gate.locked_message())
            self._set_status("Locked")
            return

        if await self._consume_history_scope(message, prompt, shared=shared):
            return

        if shared:
            chat_title = (message.chat.title or "").strip() or f"chat {message.chat.id}"
            session = chat_key(message.chat.id)
            prefix = self._shared_system_prefix(
                title=chat_title,
                kind=(
                    "channel"
                    if message.chat.type == ChatType.CHANNEL
                    else "group"
                ),
            )
            if await self._ask_history_scope(
                message,
                prompt,
                history_chat_id=message.chat.id,
                shared=True,
                session_key=session,
                chat_title=chat_title,
                system_prefix=prefix,
            ):
                return
            enriched = self._attach_buffered_files(
                self._attach_history_if_needed(
                    prompt,
                    chat_id=message.chat.id,
                    exclude_message_id=message.message_id,
                ),
                message.chat.id,
            )
            if self._gate.looks_sensitive(prompt):
                await self._begin_shared_sensitive(
                    message,
                    enriched,
                    session_key=session,
                    chat_title=chat_title,
                )
                return
            await self._forward_to_agent(
                message,
                enriched,
                session_key=session,
                chat_title=chat_title,
                system_prefix=prefix,
                reply_to_message_id=message.message_id,
            )
            return

        # Private DM — route to selected topic or chat agent.
        session = self._dm_session_key()
        if session.startswith("chat:"):
            chat_session = self._bridge.chats.get(session)
            chat_title = (
                chat_session.title if chat_session else session
            )
            try:
                bound_chat_id = int(session.split(":", 1)[1])
            except (IndexError, ValueError):
                bound_chat_id = None
            dm_prompt = prompt
            if bound_chat_id is not None:
                prefix = self._dm_chat_agent_prefix(title=chat_title)
                if await self._ask_history_scope(
                    message,
                    prompt,
                    history_chat_id=bound_chat_id,
                    shared=False,
                    session_key=session,
                    chat_title=chat_title,
                    system_prefix=prefix,
                ):
                    return
                dm_prompt = self._attach_buffered_files(
                    self._attach_history_if_needed(
                        prompt,
                        chat_id=bound_chat_id,
                    ),
                    bound_chat_id,
                )
            if self._gate.is_suspicious(prompt):
                self._pending_job = PendingJob(
                    prompt=dm_prompt,
                    session_key=session,
                    source_chat_id=message.chat.id,
                    source_message_id=message.message_id,
                    shared_chat=False,
                    chat_title=chat_title,
                    stage="password",
                )
                await message.answer(self._gate.begin_challenge(prompt))
                self._set_status("Awaiting password")
                return
            await self._forward_to_agent(
                message,
                dm_prompt,
                session_key=session,
                chat_title=chat_title,
                system_prefix=self._dm_chat_agent_prefix(title=chat_title),
            )
            return

        if self._gate.is_suspicious(prompt):
            self._pending_job = PendingJob(
                prompt=prompt,
                session_key=session,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                shared_chat=False,
                chat_title=None,
                stage="password",
            )
            await message.answer(self._gate.begin_challenge(prompt))
            self._set_status("Awaiting password")
            return
        await self._forward_to_agent(
            message,
            prompt,
            session_key=session,
        )

    async def _chat_title(self, chat_id: int) -> str:
        session = self._bridge.chats.get(chat_key(chat_id))
        if session is not None and session.title:
            cached = session.title
        else:
            cached = f"chat {chat_id}"
        try:
            chat = await self._bot.get_chat(chat_id)
            title = (chat.title or "").strip()
            if title:
                self._bridge.chats.get_or_create(chat_key(chat_id), title)
                return title
        except Exception:
            logger.debug("get_chat failed for %s", chat_id, exc_info=True)
        return cached

    def _forget_chat(self, chat_id: int) -> None:
        self._allowlist.revoke(chat_id)
        key = chat_key(chat_id)
        if self._bridge.store.state.dm_session_key == key:
            self._bridge.store.set_dm_session_key(private_key())

    async def _collect_membership_rows(self) -> list[tuple[int, str, bool]]:
        ids = set(self._allowlist.allowed_ids())
        for session in self._bridge.chats.list_chat_sessions():
            cid = self._bridge.chats.parse_chat_id(session.key)
            if cid is not None:
                ids.add(cid)
        rows: list[tuple[int, str, bool]] = []
        for chat_id in sorted(ids):
            title = f"chat {chat_id}"
            session = self._bridge.chats.get(chat_key(chat_id))
            if session is not None and session.title:
                title = session.title
            is_member = False
            try:
                chat = await self._bot.get_chat(chat_id)
                title = (chat.title or "").strip() or title
                # If getChat works, bot can still see the chat (usually still a member).
                me = await self._bot.get_chat_member(chat_id, self._bot_id or 0)
                status = me.status
                is_member = status in _IN_CHAT_STATUSES
                if title:
                    self._bridge.chats.get_or_create(chat_key(chat_id), title)
            except Exception:
                is_member = False
            rows.append((chat_id, title, is_member))
        return rows

    async def _send_membership_list(
        self,
        message: Message,
        *,
        edit: bool = False,
    ) -> None:
        rows = await self._collect_membership_rows()
        if not rows:
            text = (
                "Бот ни в одном групповом чате не состоит "
                "(и allowlist пуст).\n"
                "Пришли invite-ссылку сюда и добавь меня сам."
            )
            if edit:
                await message.edit_text(text)
            else:
                await message.answer(text)
            return
        kb = membership_chats_keyboard(rows)
        text = (
            "Чаты бота (из allowlist / известных сессий).\n"
            "«Выйти» — leave + снять allowlist. "
            "«Убрать» — запись без членства.\n"
            f"Всего: {len(rows)}."
        )
        if edit:
            await message.edit_text(text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)

    def _rights_note_for_member(self, chat, member) -> str:
        """Human-readable note about whether the bot can talk in this chat."""
        is_channel = chat.type == ChatType.CHANNEL
        status = member.status

        if status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            if is_channel:
                can_post = getattr(member, "can_post_messages", None)
                if can_post is False:
                    return (
                        "\n\n⚠️ Канал: я админ, но без права «публиковать сообщения» — "
                        "писать в канал не смогу. Включи Post messages."
                    )
                return "\n\nПрава: админ канала с публикацией — ок."
            return "\n\nПрава: админ группы — ок."

        if is_channel:
            return (
                "\n\n⚠️ Канал: без прав админа (с Post messages) я не смогу писать. "
                "Сделай меня администратором канала."
            )

        if status == ChatMemberStatus.RESTRICTED:
            can_send = bool(getattr(member, "can_send_messages", False))
            if not can_send:
                return (
                    "\n\n⚠️ Группа: мне запрещено отправлять сообщения (restricted). "
                    "Разреши писать или дай админку — иначе на @mention не отвечу."
                )

        # Ordinary member: admin is NOT required for @mention in groups.
        return (
            "\n\nПрава: обычный участник (не админ).\n"
            "В группах админка не обязательна: при privacy mode я вижу твои "
            "@mention и могу отвечать, если участникам/ботам разрешено писать.\n"
            "Если в чате писать могут только админы — выдай админку "
            "(или право писать сообщения)."
        )

    async def _handle_my_chat_member(self, event: ChatMemberUpdated) -> None:
        chat = event.chat
        if chat.type == ChatType.PRIVATE:
            return

        old_status = event.old_chat_member.status
        new_status = event.new_chat_member.status
        was_in = old_status in _IN_CHAT_STATUSES
        now_in = new_status in _IN_CHAT_STATUSES
        title = (chat.title or "").strip() or f"chat {chat.id}"
        actor = event.from_user
        actor_id = actor.id if actor else None
        admin_id = self._settings.telegram_admin_user_id

        if now_in and not was_in:
            allowed = self._allowlist.is_allowed(chat.id) or actor_id == admin_id
            if not allowed:
                logger.warning(
                    "Leaving unauthorized chat_id=%s title=%r adder=%s",
                    chat.id,
                    title,
                    actor_id,
                )
                try:
                    await self._bot.leave_chat(chat.id)
                except Exception:
                    logger.exception("leave_chat failed for %s", chat.id)
                await self._notify_admin_dm(
                    f"Меня добавили в «{title}» (id `{chat.id}`), "
                    f"но не ты — вышел.\n"
                    "Чтобы подключить чат: пришли invite-ссылку сюда, "
                    "затем добавь меня сам."
                )
                return

            self._allowlist.allow(chat.id)
            self._allowlist.consume_pending_if_valid()
            self._bridge.chats.get_or_create(chat_key(chat.id), title)
            who = "ты" if actor_id == admin_id else "allowlist"
            rights = self._rights_note_for_member(chat, event.new_chat_member)
            await self._notify_admin_dm(
                f"Подключён к «{title}» (id `{chat.id}`, {who}).\n"
                "В чате — твой @mention или Reply на моё сообщение. "
                "В личке можно выбрать этот агент в «Чат-агент».\n"
                "История Telegram через API недоступна задним числом; "
                "новые сообщения пишутся в локальную БД; агент — по "
                "@mention / Reply."
                f"{rights}"
            )
            self._set_status(self._status_line())
            return

        if was_in and now_in and old_status != new_status:
            # e.g. promoted to admin after join
            rights = self._rights_note_for_member(chat, event.new_chat_member)
            await self._notify_admin_dm(
                f"Права в «{title}» изменились "
                f"({old_status} → {new_status}).{rights}"
            )
            return

        if was_in and not now_in:
            logger.info("Removed from chat_id=%s title=%r", chat.id, title)
            await self._notify_admin_dm(
                f"Меня удалили / я вышел из «{title}» (id `{chat.id}`). "
                "Allowlist сохранён — можешь добавить снова сам."
            )

    def _register_handlers(self) -> None:
        admin = AdminFilter(self._settings.telegram_admin_user_id)
        nick = self._settings.admin_nick

        @self._dp.my_chat_member()
        async def my_chat_member_handler(event: ChatMemberUpdated) -> None:
            await self._handle_my_chat_member(event)

        @self._dp.message(CommandStart(), admin)
        async def start_handler(message: Message) -> None:
            if is_shared_chat(message):
                return
            if self._gate.locked:
                await message.answer(self._gate.locked_message())
                return
            ctx = self._dm_context_label()
            dm_key = self._dm_session_key()
            mode = (
                f"Чат-агент: {ctx}"
                if dm_key.startswith("chat:")
                else f"Личная тема: {ctx}"
            )
            if self._gate.enabled:
                mins = max(1, self._gate.elevation_seconds // 60)
                elev = ""
                if self._gate.is_elevated():
                    left = self._gate.elevation_remaining_seconds()
                    elev = f" Повышенный доступ ещё ~{left // 60}м {left % 60}с."
                gate_note = (
                    f"Защита паролем: включена "
                    f"(опасные запросы → пароль → {mins} мин; "
                    f"смена темы сбрасывает).{elev}"
                )
            else:
                gate_note = (
                    "Защита паролем: выключена — задайте CONFIRM_PASSWORD в env.env."
                )
            await message.answer(
                f"ok, {nick}\n"
                f"{mode}\n"
                f"Модель: {self._bridge.selected_model}\n"
                f"{gate_note}\n\n"
                "Кнопки: модель, темы, новая тема, чат-агент, чаты.\n"
                "Чтобы добавить бота в группу: пришли invite-ссылку сюда "
                "и добавь меня сам (бот сам по ссылке не входит).\n"
                "В группах — твой @mention или Reply на моё сообщение; "
                "чужие добавления → выход.",
                reply_markup=main_reply_keyboard(),
            )
            self._bridge.store.mark_reply_keyboard_current()

        @self._dp.message(admin, F.text.in_(CONTROL_BUTTON_TEXTS))
        async def control_button_handler(message: Message) -> None:
            if is_shared_chat(message):
                return
            if self._gate.locked:
                await message.answer(self._gate.locked_message())
                return
            if self._gate.awaiting_password:
                await message.answer(
                    "Сейчас ожидается пароль (или /cancel). Кнопки временно недоступны."
                )
                return
            await self._maybe_refresh_reply_keyboard(message)
            text = message.text or ""
            if text == BTN_MODEL:
                await _send_model_picker(message, self._bridge)
            elif text == BTN_CHAT_AGENT:
                await _send_chat_agent_picker(message, self._bridge)
            elif text == BTN_CHATS:
                await self._send_membership_list(message)
            elif text in {BTN_TOPICS, BTN_NEW_TOPIC}:
                if not self._bridge.store.state.dm_is_private():
                    await message.answer(
                        "Сейчас выбран чат-агент. Сначала «Чат-агент» → "
                        "«Личные темы», потом темы снова доступны."
                    )
                    return
                if text == BTN_TOPICS:
                    await _send_topic_picker(message, self._bridge)
                else:
                    await _create_new_topic(
                        message, self._bridge, self._set_status, self._gate
                    )

        @self._dp.callback_query(admin, F.data.startswith(CB_MODEL_PREFIX))
        async def model_callback(query: CallbackQuery) -> None:
            if self._gate.locked:
                await query.answer("Заблокировано", show_alert=True)
                return
            model_id = (query.data or "")[len(CB_MODEL_PREFIX) :]
            if not model_id:
                await query.answer("Пустой id модели")
                return
            try:
                if not self._bridge.ready:
                    await self._bridge.start()
                await self._bridge.set_model(model_id)
                self._set_status(self._status_line())
                await query.answer(f"Модель: {model_id}")
                if query.message:
                    await query.message.edit_text(
                        f"Модель для следующих сообщений: `{model_id}`\n"
                        "(контекст темы/чат-агента сохраняется)",
                        parse_mode="Markdown",
                    )
            except Exception as exc:
                logger.exception("Model selection failed")
                await query.answer("Ошибка", show_alert=True)
                if query.message:
                    await query.message.answer(f"Не удалось сменить модель: {exc}")

        @self._dp.callback_query(admin, F.data.startswith(CB_TOPIC_PREFIX))
        async def topic_callback(query: CallbackQuery) -> None:
            if self._gate.locked:
                await query.answer("Заблокировано", show_alert=True)
                return
            if not self._bridge.store.state.dm_is_private():
                await query.answer("Сначала вернись в «Личные темы»", show_alert=True)
                return
            payload = (query.data or "")[len(CB_TOPIC_PREFIX) :]
            try:
                if not self._bridge.ready:
                    await self._bridge.start()
                if payload == "__new__":
                    topic = await self._bridge.new_topic()
                    revoke_note = self._gate.revoke_elevation("новая тема")
                    self._set_status(self._status_line())
                    await query.answer(f"Тема: {topic.title}")
                    if query.message:
                        text = (
                            f"Новая тема «{topic.title}». Контекст пустой.\n"
                            f"Модель: `{self._bridge.selected_model}`"
                        )
                        if revoke_note:
                            text += f"\n\n{revoke_note}"
                        await query.message.edit_text(text, parse_mode="Markdown")
                    return
                topic = await self._bridge.switch_topic(payload)
                revoke_note = self._gate.revoke_elevation("смена темы")
                self._set_status(self._status_line())
                await query.answer(f"▶ {topic.title}")
                if query.message:
                    await _send_topic_picker(query.message, self._bridge, edit=True)
                if revoke_note and query.message:
                    await query.message.answer(revoke_note)
            except Exception as exc:
                logger.exception("Topic switch failed")
                await query.answer("Ошибка", show_alert=True)
                if query.message:
                    await query.message.answer(f"Тема: {exc}")

        @self._dp.callback_query(admin, F.data.startswith(CB_CHAT_AGENT_PREFIX))
        async def chat_agent_callback(query: CallbackQuery) -> None:
            if self._gate.locked:
                await query.answer("Заблокировано", show_alert=True)
                return
            data = query.data or ""
            if data == CB_CHAT_AGENT_PRIVATE:
                self._bridge.store.set_dm_session_key(private_key())
                self._set_status(self._status_line())
                await query.answer("Личные темы")
                if query.message:
                    await query.message.edit_text(
                        "DM снова на личные темы. "
                        "«Темы» / «Новая тема» доступны."
                    )
                return
            session_key = data[len(CB_CHAT_AGENT_PREFIX) :]
            if not session_key.startswith("chat:"):
                await query.answer("Неизвестный чат", show_alert=True)
                return
            session = self._bridge.chats.get(session_key)
            if session is None:
                await query.answer("Чат не найден", show_alert=True)
                return
            self._bridge.store.set_dm_session_key(session_key)
            self._set_status(self._status_line())
            await query.answer(f"Чат: {session.title}"[:200])
            if query.message:
                await query.message.edit_text(
                    f"DM подключён к чат-агенту «{session.title}».\n"
                    "Сообщения здесь идут в тот же контекст, что и @mention в группе.\n"
                    "Ответы — только в личку. "
                    "Вернуться: «Чат-агент» → «Личные темы»."
                )

        @self._dp.callback_query(admin, F.data.startswith(CB_LEAVE_ASK_PREFIX))
        async def leave_ask_callback(query: CallbackQuery) -> None:
            if self._gate.locked:
                await query.answer("Заблокировано", show_alert=True)
                return
            raw = (query.data or "")[len(CB_LEAVE_ASK_PREFIX) :]
            try:
                chat_id = int(raw)
            except ValueError:
                await query.answer("Некорректный id", show_alert=True)
                return
            title = await self._chat_title(chat_id)
            await query.answer()
            if query.message:
                await query.message.edit_text(
                    f"Выйти из «{title}» (`{chat_id}`)?\n"
                    "Allowlist тоже снимется — чтобы вернуться, "
                    "снова пришли ссылку и добавь меня сам.",
                    reply_markup=leave_confirm_keyboard(chat_id),
                    parse_mode="Markdown",
                )

        @self._dp.callback_query(admin, F.data == CB_LEAVE_CANCEL)
        async def leave_cancel_callback(query: CallbackQuery) -> None:
            await query.answer("Отменено")
            if query.message:
                await self._send_membership_list(query.message, edit=True)

        @self._dp.callback_query(admin, F.data.startswith(CB_LEAVE_OK_PREFIX))
        async def leave_ok_callback(query: CallbackQuery) -> None:
            if self._gate.locked:
                await query.answer("Заблокировано", show_alert=True)
                return
            raw = (query.data or "")[len(CB_LEAVE_OK_PREFIX) :]
            try:
                chat_id = int(raw)
            except ValueError:
                await query.answer("Некорректный id", show_alert=True)
                return
            title = await self._chat_title(chat_id)
            try:
                await self._bot.leave_chat(chat_id)
            except Exception as exc:
                logger.exception("leave_chat failed for %s", chat_id)
                await query.answer("Не удалось выйти", show_alert=True)
                if query.message:
                    await query.message.answer(
                        f"Не удалось выйти из «{title}»: {exc}"
                    )
                return
            self._forget_chat(chat_id)
            await query.answer("Вышел")
            if query.message:
                await query.message.edit_text(
                    f"Вышел из «{title}» (`{chat_id}`). "
                    "Чат убран из allowlist."
                )
            self._set_status(self._status_line())

        @self._dp.callback_query(admin, F.data.startswith(CB_CHAT_FORGET_PREFIX))
        async def chat_forget_callback(query: CallbackQuery) -> None:
            if self._gate.locked:
                await query.answer("Заблокировано", show_alert=True)
                return
            raw = (query.data or "")[len(CB_CHAT_FORGET_PREFIX) :]
            try:
                chat_id = int(raw)
            except ValueError:
                await query.answer("Некорректный id", show_alert=True)
                return
            title = await self._chat_title(chat_id)
            self._forget_chat(chat_id)
            await query.answer("Убрано")
            if query.message:
                await query.message.edit_text(
                    f"«{title}» (`{chat_id}`) убран из списка "
                    "(бота там уже не было)."
                )
            self._set_status(self._status_line())

        @self._dp.callback_query(
            admin, F.data.in_({CB_DELIVERY_DM, CB_DELIVERY_CHAT})
        )
        async def delivery_callback(query: CallbackQuery) -> None:
            job = self._pending_job
            if job is None or job.stage != "delivery" or (
                not job.agent_reply and not job.deliverable_paths
            ):
                await query.answer("Нет ожидающего ответа", show_alert=True)
                return
            to_chat = (query.data or "") == CB_DELIVERY_CHAT
            reply_text = job.agent_reply or ""
            files = [Path(p) for p in job.deliverable_paths]
            source_chat_id = job.source_chat_id
            source_message_id = job.source_message_id
            title = job.chat_title
            started_at = job.started_at
            session_key = job.session_key
            self._clear_pending_job()

            target = (
                source_chat_id
                if to_chat
                else self._settings.telegram_admin_user_id
            )
            try:
                await self._send_agent_result(
                    self._bot,
                    target,
                    reply_text,
                    files,
                    reply_to_message_id=source_message_id if to_chat else None,
                )
                if started_at:
                    self._history.record_reply_latency(
                        int((time.time() - started_at) * 1000),
                        chat_id=target,
                        session_key=session_key,
                    )
                if to_chat:
                    await query.answer("Отправлено в общий чат")
                    if query.message:
                        await query.message.edit_text(
                            f"Ответ отправлен в «{title}»."
                        )
                else:
                    await query.answer("Только в личку")
                    if query.message:
                        await query.message.edit_text("Ответ только в личке.")
            except Exception as exc:
                logger.exception("Delivery failed")
                await query.answer("Не удалось отправить", show_alert=True)
                await self._notify_admin_dm(f"Не удалось отправить ответ: {exc}")

        async def _shared_or_private_text(message: Message) -> None:
            """Admin text: private = full UI; shared = @mention or Reply to bot."""
            shared = is_shared_chat(message)
            text = (message.text or message.caption or "").strip()
            has_file = message.document is not None or bool(message.photo)
            if not text and not shared and not has_file:
                return
            if not text and has_file and not shared:
                text = "Посмотри приложенный файл."

            if shared:
                if not self._allowlist.is_allowed(message.chat.id):
                    logger.info(
                        "Ignore shared chat_id=%s (not allowlisted) text=%r",
                        message.chat.id,
                        text[:80],
                    )
                    return
                prompt = extract_shared_prompt(
                    message,
                    self._bot_username,
                    bot_id=self._bot_id,
                )
                if prompt is None:
                    logger.debug(
                        "Shared msg without @mention/reply-to-bot chat_id=%s",
                        message.chat.id,
                    )
                    return
                prompt = await self._with_local_files(message, prompt)
                if not prompt.strip():
                    await self._safe_chat_notice(
                        message.chat.id,
                        "Нужен текст: либо после @бота, либо в ответе (Reply) на моё сообщение.",
                        reply_to_message_id=message.message_id,
                    )
                    return
                logger.info(
                    "Group bot request chat_id=%s prompt=%r",
                    message.chat.id,
                    prompt[:160],
                )
                await self._handle_admin_prompt(message, prompt, shared=True)
                return

            # Private DM
            if self._gate.locked:
                await message.answer(self._gate.locked_message())
                self._set_status("Locked")
                return

            await self._maybe_refresh_reply_keyboard(message)

            if text.lower() in {"/cancel", "cancel"}:
                had_history = self._history_pending.pop(message.chat.id, None)
                if self._gate.awaiting_password:
                    await message.answer(self._gate.cancel_challenge())
                    self._clear_pending_job()
                    self._set_status(self._status_line())
                elif had_history is not None:
                    await message.answer("Хорошо, этот запрос не выполняю.")
                return

            if self._gate.awaiting_password:
                await self._delete_user_message(message)
                reply, pending = self._gate.submit_password(text)
                await message.answer(reply)
                if self._gate.locked:
                    self._clear_pending_job()
                    self._set_status("Locked")
                    return
                if not pending:
                    return
                job = self._pending_job
                if job is not None and job.shared_chat:
                    await self._run_shared_sensitive_agent(job)
                    return
                session = (
                    job.session_key
                    if job is not None
                    else self._dm_session_key()
                )
                title = job.chat_title if job is not None else None
                self._clear_pending_job()
                system_prefix = None
                chat_title = None
                if session.startswith("chat:"):
                    chat_title = title or session
                    system_prefix = self._dm_chat_agent_prefix(title=chat_title)
                await self._forward_to_agent(
                    message,
                    pending,
                    session_key=session,
                    chat_title=chat_title,
                    system_prefix=system_prefix,
                )
                return

            if text.startswith("/"):
                return

            if await self._handle_invite_link_dm(message, text):
                return

            text = await self._with_local_files(message, text)
            await self._handle_admin_prompt(message, text, shared=False)

        @self._dp.message(
            F.chat.type.in_(
                {
                    ChatType.GROUP,
                    ChatType.SUPERGROUP,
                    "group",
                    "supergroup",
                }
            )
        )
        async def group_message(message: Message) -> None:
            """All group messages: history for everyone; agent only admin @mention."""
            await self._maybe_store_history(message)
            user = message.from_user
            if user is None or user.id != self._settings.telegram_admin_user_id:
                return
            await _shared_or_private_text(message)

        @self._dp.edited_message(
            F.chat.type.in_(
                {
                    ChatType.GROUP,
                    ChatType.SUPERGROUP,
                    "group",
                    "supergroup",
                }
            )
        )
        async def group_edited_message(message: Message) -> None:
            await self._maybe_store_history(message, edited=True)

        @self._dp.message(admin, F.text)
        async def private_text(message: Message) -> None:
            if is_shared_chat(message):
                await self._maybe_store_history(message)
                await _shared_or_private_text(message)
                return
            await _shared_or_private_text(message)

        @self._dp.message(admin, F.caption)
        async def private_caption(message: Message) -> None:
            if is_shared_chat(message):
                await self._maybe_store_history(message)
            await _shared_or_private_text(message)

        @self._dp.message(
            admin,
            F.chat.type == ChatType.PRIVATE,
            F.content_type.in_({ContentType.DOCUMENT, ContentType.PHOTO}),
        )
        async def private_media(message: Message) -> None:
            if message.caption:
                return
            await _shared_or_private_text(message)

        @self._dp.channel_post()
        async def channel_post_handler(message: Message) -> None:
            await self._maybe_store_history(message)
            user = message.from_user
            if user is None or user.id != self._settings.telegram_admin_user_id:
                return
            await _shared_or_private_text(message)

        @self._dp.edited_channel_post()
        async def edited_channel_post_handler(message: Message) -> None:
            await self._maybe_store_history(message, edited=True)

        @self._dp.message(~admin, F.chat.type == ChatType.PRIVATE)
        async def denied_handler(message: Message) -> None:
            await message.answer("access denied")

    async def run(self) -> None:
        self._set_status("Starting…")
        try:
            me = await self._bot.get_me()
            self._bot_username = me.username or ""
            self._bot_id = me.id
            self._bot_display_name = (me.first_name or "").strip()
            self._group_log_mw._bot_username = self._bot_username
            logger.info("Bot identity @%s id=%s", self._bot_username, self._bot_id)
            await self._load_admin_profile()
            if not me.can_read_all_group_messages:
                logger.warning(
                    "Bot privacy mode ON (can_read_all_group_messages=false). "
                    "Mentions should still work; if groups stay silent, "
                    "disable privacy via BotFather → /setprivacy → Disable."
                )
        except Exception:
            logger.exception("get_me failed")
        try:
            await self._bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            logger.exception("delete_webhook failed")
        try:
            await self._bridge.start()
            self._set_status(self._status_line())
        except Exception as exc:
            logger.error("Cursor agent unavailable at startup: %s", exc)
            self._set_status("Error (Cursor)")
        logger.info(
            "Telegram polling started (admin=%s id=%s)",
            self._settings.admin_nick,
            self._settings.telegram_admin_user_id,
        )
        self._panel = LocalStatusPanel(
            self.status_snapshot,
            self.history_page,
            host=self._settings.status_panel_host,
            port=self._settings.status_panel_upstream_port,
            public_port=self._settings.status_panel_port,
        )
        try:
            await self._panel.start()
        except OSError as exc:
            logger.error(
                "Status panel failed to bind %s:%s (%s). Public page is %s:%s.",
                self._settings.status_panel_host,
                self._settings.status_panel_upstream_port,
                exc,
                self._settings.status_panel_host,
                self._settings.status_panel_port,
            )
            self._panel = None
        try:
            await self._dp.start_polling(
                self._bot,
                allowed_updates=[
                    "message",
                    "edited_message",
                    "callback_query",
                    "channel_post",
                    "edited_channel_post",
                    "my_chat_member",
                ],
                handle_as_tasks=True,
            )
        finally:
            self._set_status("Stopping…")
            if self._panel is not None:
                try:
                    await self._panel.stop()
                except Exception:
                    logger.debug("panel.stop ignored", exc_info=True)
                self._panel = None
            try:
                await asyncio.wait_for(self._bridge.stop(), timeout=5)
            except Exception:
                logger.exception("bridge.stop in run() finally failed/timed out")
            try:
                await asyncio.wait_for(self._bot.session.close(), timeout=3)
            except Exception:
                logger.exception("bot session close failed/timed out")
            try:
                self._history.close()
            except Exception:
                logger.debug("history.close ignored", exc_info=True)
            self._set_status("Stopped")

    async def stop(self) -> None:
        if self._panel is not None:
            try:
                await self._panel.stop()
            except Exception:
                logger.debug("panel.stop in stop() ignored", exc_info=True)
            self._panel = None
        try:
            await asyncio.wait_for(self._dp.stop_polling(), timeout=5)
        except TimeoutError:
            logger.warning("stop_polling timed out")
        except RuntimeError:
            logger.info("stop_polling skipped (not running)")


async def _send_model_picker(message: Message, bridge: CursorBridge) -> None:
    try:
        if not bridge.ready:
            await bridge.start()
        models = await bridge.list_models()
        if not models:
            await message.answer("Список моделей пуст.")
            return
        kb = models_inline_keyboard(models, bridge.selected_model)
        await message.answer(
            f"Текущая модель: `{bridge.selected_model}`\nВыберите модель:",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("list_models failed")
        await message.answer(f"Не удалось загрузить модели: {exc}")


async def _send_topic_picker(
    message: Message,
    bridge: CursorBridge,
    *,
    edit: bool = False,
) -> None:
    topics = await bridge.list_topics()
    active_id = bridge.store.state.active_topic_id
    rows = [(t.id, t.title, t.id == active_id) for t in topics]
    kb = topics_inline_keyboard(rows)
    text = (
        "Темы (▶ — активная). Переключение подгружает сохранённый контекст агента.\n"
        f"Модель сейчас: `{bridge.selected_model}`"
    )
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")


async def _send_chat_agent_picker(message: Message, bridge: CursorBridge) -> None:
    sessions = bridge.chats.list_chat_sessions()
    active = bridge.store.state.dm_session_key or private_key()
    private_active = active == private_key() or not active.startswith("chat:")
    if not sessions:
        await message.answer(
            "Пока нет чат-агентов.\n"
            "Пришли invite-ссылку сюда, открой её и добавь бота сам — "
            "после этого чат появится в списке."
        )
        return
    rows = [
        (s.key, s.title, s.key == active and not private_active) for s in sessions
    ]
    kb = chat_agents_inline_keyboard(rows, private_active=private_active)
    await message.answer(
        "Выбери чат-агента для общения в личке "
        "(тот же контекст, что и @mention в группе).\n"
        "«Личные темы» — обычный DM с темами.",
        reply_markup=kb,
    )


async def _create_new_topic(
    message: Message,
    bridge: CursorBridge,
    set_status: StatusCallback,
    gate: SafetyGate,
) -> None:
    try:
        if not bridge.ready:
            await bridge.start()
        topic = await bridge.new_topic()
        revoke_note = gate.revoke_elevation("новая тема")
        set_status(f"Running · {bridge.selected_model} · {topic.title}")
        text = (
            f"Новая тема «{topic.title}». Старые темы остаются в списке «📂 Темы».\n"
            f"Модель: `{bridge.selected_model}`"
        )
        if revoke_note:
            text += f"\n\n{revoke_note}"
        await message.answer(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("new_topic failed")
        await message.answer(f"Не удалось создать тему: {exc}")
