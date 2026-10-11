from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram.enums import MessageEntityType
from aiogram.types import Message, MessageEntity

from .config import ROOT_DIR

logger = logging.getLogger(__name__)

DB_FILE = ROOT_DIR / "chat_history.sqlite"


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _local_today_start() -> tuple[int, float]:
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), start.timestamp()


def _local_date_start(value: str) -> int | None:
    try:
        day = datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    start = day.replace(tzinfo=_local_tz())
    return int(start.timestamp())


def _local_date_end(value: str) -> int | None:
    start = _local_date_start(value)
    if start is None:
        return None
    return start + int(timedelta(days=1).total_seconds())


def _like_pattern(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _sender_key_label(
    user_id: object, username: object, display: object
) -> tuple[str, str]:
    uname = str(username or "").strip()
    name = str(display or "").strip()
    if user_id is not None:
        key = f"id:{int(user_id)}"
    else:
        key = f"anon:{uname.lower()}|{name.lower()}"
    bits = []
    if uname:
        bits.append(f"@{uname}")
    if name and name.lower() != uname.lower():
        bits.append(name)
    label = " · ".join(bits) if bits else (key if user_id is None else str(user_id))
    return key, label


def _sender_clause(sender: str | None) -> tuple[str, list[object]]:
    raw = (sender or "").strip()
    if not raw:
        return "", []
    if raw.startswith("id:"):
        try:
            user_id = int(raw[3:])
        except ValueError:
            return "", []
        return "from_user_id=?", [user_id]
    if raw.startswith("anon:"):
        rest = raw[5:]
        username, _, display = rest.partition("|")
        return (
            "from_user_id IS NULL AND lower(COALESCE(from_username, ''))=? "
            "AND lower(COALESCE(from_display_name, ''))=?",
            [username, display],
        )
    return "", []


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    date_unix INTEGER NOT NULL,
    date_iso TEXT NOT NULL,
    from_user_id INTEGER,
    from_username TEXT,
    from_display_name TEXT,
    from_is_bot INTEGER NOT NULL DEFAULT 0,
    text TEXT,
    caption TEXT,
    entities_json TEXT,
    caption_entities_json TEXT,
    formatting_markdown TEXT,
    content_type TEXT,
    has_photo INTEGER NOT NULL DEFAULT 0,
    has_document INTEGER NOT NULL DEFAULT 0,
    has_video INTEGER NOT NULL DEFAULT 0,
    has_animation INTEGER NOT NULL DEFAULT 0,
    has_audio INTEGER NOT NULL DEFAULT 0,
    has_voice INTEGER NOT NULL DEFAULT 0,
    has_video_note INTEGER NOT NULL DEFAULT 0,
    has_sticker INTEGER NOT NULL DEFAULT 0,
    has_media INTEGER NOT NULL DEFAULT 0,
    media_file_name TEXT,
    media_mime_type TEXT,
    reply_to_message_id INTEGER,
    message_thread_id INTEGER,
    is_edited INTEGER NOT NULL DEFAULT 0,
    edited_unix INTEGER,
    chat_title TEXT,
    raw_json TEXT,
    stored_at REAL NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_date
    ON messages (chat_id, date_unix);
CREATE INDEX IF NOT EXISTS idx_messages_from
    ON messages (chat_id, from_user_id);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    session_key TEXT,
    model TEXT,
    status TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    usage_known INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created
    ON agent_runs (created_at);

CREATE TABLE IF NOT EXISTS reply_latency (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at REAL NOT NULL,
    response_ms INTEGER NOT NULL,
    chat_id INTEGER,
    session_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_reply_latency_sent
    ON reply_latency (sent_at);

CREATE TABLE IF NOT EXISTS buffer_counters (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def _display_name(message: Message) -> str | None:
    user = message.from_user
    if user is None:
        if message.sender_chat is not None:
            return message.sender_chat.title
        return None
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or user.username or str(user.id)


def _entities_to_json(entities: list[MessageEntity] | None) -> str | None:
    if not entities:
        return None
    rows: list[dict[str, Any]] = []
    for ent in entities:
        item: dict[str, Any] = {
            "type": str(ent.type),
            "offset": ent.offset,
            "length": ent.length,
        }
        if ent.url:
            item["url"] = ent.url
        if ent.language:
            item["language"] = ent.language
        if ent.custom_emoji_id:
            item["custom_emoji_id"] = ent.custom_emoji_id
        if ent.user is not None:
            item["user_id"] = ent.user.id
            item["username"] = ent.user.username
        rows.append(item)
    return json.dumps(rows, ensure_ascii=False)


def _apply_md_markers(text: str, entities: list[MessageEntity] | None) -> str:
    """Best-effort Markdown from Telegram entities (for search/display)."""
    if not text or not entities:
        return text or ""
    # Telegram entity offsets are UTF-16 code units.
    utf16 = text.encode("utf-16-le")

    def slice_utf16(start: int, length: int) -> str:
        a = start * 2
        b = (start + length) * 2
        return utf16[a:b].decode("utf-16-le")

    parts: list[str] = []
    cursor = 0
    forward = sorted(entities, key=lambda e: (e.offset, -e.length))
    used_until = 0
    total = len(utf16) // 2
    for ent in forward:
        if ent.offset < used_until:
            continue
        if ent.offset > cursor:
            parts.append(slice_utf16(cursor, ent.offset - cursor))
        try:
            frag = slice_utf16(ent.offset, ent.length)
        except Exception:
            continue
        t = ent.type
        if t == MessageEntityType.BOLD:
            parts.append(f"**{frag}**")
        elif t == MessageEntityType.ITALIC:
            parts.append(f"_{frag}_")
        elif t == MessageEntityType.UNDERLINE:
            parts.append(f"__{frag}__")
        elif t == MessageEntityType.STRIKETHROUGH:
            parts.append(f"~~{frag}~~")
        elif t == MessageEntityType.CODE:
            parts.append(f"`{frag}`")
        elif t == MessageEntityType.PRE:
            lang = ent.language or ""
            parts.append(f"```{lang}\n{frag}\n```")
        elif t == MessageEntityType.TEXT_LINK and ent.url:
            parts.append(f"[{frag}]({ent.url})")
        elif t == MessageEntityType.TEXT_MENTION and ent.user:
            parts.append(f"[{frag}](tg://user?id={ent.user.id})")
        else:
            parts.append(frag)
        cursor = ent.offset + ent.length
        used_until = cursor
    if cursor < total:
        parts.append(slice_utf16(cursor, total - cursor))
    return "".join(parts) or text


def _media_flags(message: Message) -> dict[str, Any]:
    has_photo = bool(message.photo)
    has_document = message.document is not None
    has_video = message.video is not None
    has_animation = message.animation is not None
    has_audio = message.audio is not None
    has_voice = message.voice is not None
    has_video_note = message.video_note is not None
    has_sticker = message.sticker is not None
    has_media = any(
        (
            has_photo,
            has_document,
            has_video,
            has_animation,
            has_audio,
            has_voice,
            has_video_note,
            has_sticker,
        )
    )
    file_name = None
    mime = None
    if message.document is not None:
        file_name = message.document.file_name
        mime = message.document.mime_type
    elif message.video is not None:
        file_name = message.video.file_name
        mime = message.video.mime_type
    elif message.audio is not None:
        file_name = message.audio.file_name
        mime = message.audio.mime_type
    elif message.animation is not None:
        file_name = message.animation.file_name
        mime = message.animation.mime_type
    return {
        "has_photo": int(has_photo),
        "has_document": int(has_document),
        "has_video": int(has_video),
        "has_animation": int(has_animation),
        "has_audio": int(has_audio),
        "has_voice": int(has_voice),
        "has_video_note": int(has_video_note),
        "has_sticker": int(has_sticker),
        "has_media": int(has_media),
        "media_file_name": file_name,
        "media_mime_type": mime,
    }


def _content_type(message: Message) -> str:
    if message.photo:
        return "photo"
    if message.document:
        return "document"
    if message.video:
        return "video"
    if message.animation:
        return "animation"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.sticker:
        return "sticker"
    if message.poll:
        return "poll"
    if message.location:
        return "location"
    if message.contact:
        return "contact"
    if message.new_chat_members:
        return "new_chat_members"
    if message.left_chat_member:
        return "left_chat_member"
    if message.text:
        return "text"
    if message.caption:
        return "caption"
    return "other"


def _safe_raw_json(message: Message) -> str | None:
    try:
        # aiogram v3 models: model_dump
        data = message.model_dump(mode="json", exclude_none=True)
        # Strip bulky file bytes references — keep metadata only.
        for key in ("photo",):
            if key in data and isinstance(data[key], list):
                data[key] = [
                    {k: v for k, v in item.items() if k != "file_path"}
                    if isinstance(item, dict)
                    else item
                    for item in data[key]
                ]
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        logger.debug("raw_json dump failed", exc_info=True)
        return None


class ChatHistoryStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or DB_FILE
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.create_function(
            "ulower",
            1,
            lambda value: "" if value is None else str(value).casefold(),
            deterministic=True,
        )
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._ensure_local_path_column()
        logger.info("Chat history DB ready: %s", self._path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _ensure_local_path_column(self) -> None:
        cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(messages)")
        }
        if "local_path" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN local_path TEXT")

    def set_local_path(self, chat_id: int, message_id: int, path: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE messages SET local_path=?
                WHERE chat_id=? AND message_id=?
                """,
                (path, chat_id, message_id),
            )

    def clear_local_path(self, chat_id: int, message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE messages SET local_path=NULL
                WHERE chat_id=? AND message_id=?
                """,
                (chat_id, message_id),
            )

    def get_local_path(self, chat_id: int, message_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT local_path FROM messages
                WHERE chat_id=? AND message_id=?
                """,
                (chat_id, message_id),
            ).fetchone()
        if row is None or not row[0]:
            return None
        return str(row[0])

    def buffered_files_for(
        self,
        chat_id: int,
        *,
        message_ids: set[int] | None = None,
        recent_limit: int = 0,
    ) -> list[dict[str, object]]:
        ids = sorted({int(i) for i in (message_ids or set()) if int(i) > 0})
        rows: list[dict[str, object]] = []
        with self._lock:
            if ids:
                placeholders = ",".join("?" * len(ids))
                cur = self._conn.execute(
                    f"""
                    SELECT message_id, local_path, media_file_name, date_iso
                    FROM messages
                    WHERE chat_id=? AND message_id IN ({placeholders})
                      AND local_path IS NOT NULL AND local_path != ''
                    """,
                    (chat_id, *ids),
                )
                cols = [d[0] for d in cur.description]
                rows.extend(dict(zip(cols, row)) for row in cur.fetchall())
            if recent_limit > 0:
                cur = self._conn.execute(
                    """
                    SELECT message_id, local_path, media_file_name, date_iso
                    FROM messages
                    WHERE chat_id=? AND local_path IS NOT NULL AND local_path != ''
                    ORDER BY date_unix DESC, message_id DESC
                    LIMIT ?
                    """,
                    (chat_id, max(1, min(int(recent_limit), 20))),
                )
                cols = [d[0] for d in cur.description]
                rows.extend(dict(zip(cols, row)) for row in cur.fetchall())
        seen: set[int] = set()
        unique: list[dict[str, object]] = []
        for row in rows:
            mid = int(row["message_id"])
            if mid in seen:
                continue
            seen.add(mid)
            unique.append(row)
        return unique

    def add_counter(self, key: str, delta: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO buffer_counters (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
                """,
                (key, int(delta)),
            )

    def get_counter(self, key: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM buffer_counters WHERE key=?",
                (key,),
            ).fetchone()
        return int(row[0]) if row else 0

    def store_message(self, message: Message, *, edited: bool = False) -> bool:
        """Upsert one Telegram message. Returns True if stored."""
        chat = message.chat
        if chat is None:
            return False
        user = message.from_user
        text = message.text
        caption = message.caption
        entities = list(message.entities or [])
        cap_entities = list(message.caption_entities or [])
        body_for_md = text or caption or ""
        ents_for_md = entities or cap_entities
        formatting = _apply_md_markers(body_for_md, ents_for_md) if body_for_md else None

        date_dt = message.date
        if date_dt is None:
            date_dt = datetime.now(timezone.utc)
        if date_dt.tzinfo is None:
            date_dt = date_dt.replace(tzinfo=timezone.utc)
        date_unix = int(date_dt.timestamp())
        date_iso = date_dt.astimezone(timezone.utc).isoformat()

        edited_unix = None
        if edited and message.edit_date is not None:
            ed = message.edit_date
            if isinstance(ed, int):
                edited_unix = ed
            else:
                if ed.tzinfo is None:
                    ed = ed.replace(tzinfo=timezone.utc)
                edited_unix = int(ed.timestamp())

        media = _media_flags(message)
        reply_id = (
            message.reply_to_message.message_id
            if message.reply_to_message is not None
            else None
        )

        row = {
            "chat_id": chat.id,
            "message_id": message.message_id,
            "date_unix": date_unix,
            "date_iso": date_iso,
            "from_user_id": user.id if user else (
                message.sender_chat.id if message.sender_chat else None
            ),
            "from_username": (user.username if user else None)
            or (
                message.sender_chat.username
                if message.sender_chat is not None
                else None
            ),
            "from_display_name": _display_name(message),
            "from_is_bot": int(bool(user and user.is_bot)),
            "text": text,
            "caption": caption,
            "entities_json": _entities_to_json(entities),
            "caption_entities_json": _entities_to_json(cap_entities),
            "formatting_markdown": formatting,
            "content_type": _content_type(message),
            **media,
            "reply_to_message_id": reply_id,
            "message_thread_id": message.message_thread_id,
            "is_edited": int(edited),
            "edited_unix": edited_unix,
            "chat_title": chat.title,
            "raw_json": _safe_raw_json(message),
            "stored_at": datetime.now(timezone.utc).timestamp(),
        }

        sql = """
        INSERT INTO messages (
            chat_id, message_id, date_unix, date_iso,
            from_user_id, from_username, from_display_name, from_is_bot,
            text, caption, entities_json, caption_entities_json, formatting_markdown,
            content_type,
            has_photo, has_document, has_video, has_animation, has_audio,
            has_voice, has_video_note, has_sticker, has_media,
            media_file_name, media_mime_type,
            reply_to_message_id, message_thread_id,
            is_edited, edited_unix, chat_title, raw_json, stored_at
        ) VALUES (
            :chat_id, :message_id, :date_unix, :date_iso,
            :from_user_id, :from_username, :from_display_name, :from_is_bot,
            :text, :caption, :entities_json, :caption_entities_json, :formatting_markdown,
            :content_type,
            :has_photo, :has_document, :has_video, :has_animation, :has_audio,
            :has_voice, :has_video_note, :has_sticker, :has_media,
            :media_file_name, :media_mime_type,
            :reply_to_message_id, :message_thread_id,
            :is_edited, :edited_unix, :chat_title, :raw_json, :stored_at
        )
        ON CONFLICT(chat_id, message_id) DO UPDATE SET
            text=excluded.text,
            caption=excluded.caption,
            entities_json=excluded.entities_json,
            caption_entities_json=excluded.caption_entities_json,
            formatting_markdown=excluded.formatting_markdown,
            content_type=excluded.content_type,
            has_photo=excluded.has_photo,
            has_document=excluded.has_document,
            has_video=excluded.has_video,
            has_animation=excluded.has_animation,
            has_audio=excluded.has_audio,
            has_voice=excluded.has_voice,
            has_video_note=excluded.has_video_note,
            has_sticker=excluded.has_sticker,
            has_media=excluded.has_media,
            media_file_name=excluded.media_file_name,
            media_mime_type=excluded.media_mime_type,
            is_edited=CASE WHEN excluded.is_edited=1 THEN 1 ELSE messages.is_edited END,
            edited_unix=COALESCE(excluded.edited_unix, messages.edited_unix),
            chat_title=COALESCE(excluded.chat_title, messages.chat_title),
            raw_json=excluded.raw_json,
            stored_at=excluded.stored_at
        """
        try:
            with self._lock:
                self._conn.execute(sql, row)
            return True
        except Exception:
            logger.exception(
                "Failed to store message chat_id=%s message_id=%s",
                chat.id,
                message.message_id,
            )
            return False

    def count_for_chat(self, chat_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id=?",
                (chat_id,),
            )
            return int(cur.fetchone()[0])

    def counts_by_chat(self) -> dict[int, int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT chat_id, COUNT(*) FROM messages GROUP BY chat_id"
            )
            return {int(row[0]): int(row[1]) for row in cur.fetchall()}

    def recent_for_chat(self, chat_id: int, *, limit: int = 8) -> list[dict[str, object]]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT message_id, date_iso, from_username, from_display_name,
                       text, caption, content_type, has_media, is_edited
                FROM messages
                WHERE chat_id=?
                ORDER BY date_unix DESC, message_id DESC
                LIMIT ?
                """,
                (chat_id, max(1, min(limit, 50))),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_message(
        self, chat_id: int, message_id: int
    ) -> dict[str, object] | None:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT message_id, date_unix, date_iso,
                       from_username, from_display_name, from_is_bot,
                       text, caption, formatting_markdown, content_type,
                       has_media, media_file_name, reply_to_message_id, is_edited
                FROM messages
                WHERE chat_id=? AND message_id=?
                """,
                (chat_id, message_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    def rows_by_ids(
        self, chat_id: int, message_ids: set[int]
    ) -> list[dict[str, object]]:
        ids = sorted({int(i) for i in message_ids if int(i) > 0})
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"""
                SELECT message_id, date_unix, date_iso,
                       from_username, from_display_name, from_is_bot,
                       text, caption, formatting_markdown, content_type,
                       has_media, media_file_name, reply_to_message_id, is_edited
                FROM messages
                WHERE chat_id=? AND message_id IN ({placeholders})
                """,
                (chat_id, *ids),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def recent_matching(
        self,
        chat_id: int,
        *,
        limit: int,
        usernames: set[str] | None = None,
        exclude_message_ids: set[int] | None = None,
    ) -> list[dict[str, object]]:
        """Newest-first rows for an on-demand history scan. Chronological order."""
        cap = max(1, min(int(limit), 500))
        names = sorted({n.lower().lstrip("@") for n in (usernames or set()) if n})
        excl = sorted({int(i) for i in (exclude_message_ids or set())})
        where = ["chat_id=?"]
        params: list[object] = [chat_id]
        if names:
            where.append(
                f"lower(COALESCE(from_username, '')) IN ({','.join('?' * len(names))})"
            )
            params.extend(names)
        if excl:
            where.append(
                f"message_id NOT IN ({','.join('?' * len(excl))})"
            )
            params.extend(excl)
        params.append(cap)
        sql = f"""
            SELECT message_id, date_unix, date_iso,
                   from_username, from_display_name, from_is_bot,
                   text, caption, formatting_markdown, content_type,
                   has_media, media_file_name, reply_to_message_id, is_edited
            FROM messages
            WHERE {' AND '.join(where)}
            ORDER BY date_unix DESC, message_id DESC
            LIMIT ?
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        rows.reverse()
        return rows

    def search_text(
        self,
        chat_id: int,
        query: str,
        *,
        usernames: set[str] | None = None,
        exclude_message_ids: set[int] | None = None,
        start_unix: int | None = None,
        end_unix: int | None = None,
    ) -> list[dict[str, object]]:
        """Every message whose text contains every word of `query`. Chronological."""
        tokens = [part.casefold() for part in (query or "").split() if part.strip()]
        if not tokens:
            return []
        return self._fetch_messages(
            chat_id,
            tokens=tokens,
            usernames=usernames,
            exclude_message_ids=exclude_message_ids,
            start_unix=start_unix,
            end_unix=end_unix,
        )

    def all_messages(
        self,
        chat_id: int,
        *,
        usernames: set[str] | None = None,
        exclude_message_ids: set[int] | None = None,
        start_unix: int | None = None,
        end_unix: int | None = None,
    ) -> list[dict[str, object]]:
        """Every stored message in the chat, optionally inside a date range."""
        return self._fetch_messages(
            chat_id,
            tokens=[],
            usernames=usernames,
            exclude_message_ids=exclude_message_ids,
            start_unix=start_unix,
            end_unix=end_unix,
        )

    def _fetch_messages(
        self,
        chat_id: int,
        *,
        tokens: list[str],
        usernames: set[str] | None,
        exclude_message_ids: set[int] | None,
        start_unix: int | None,
        end_unix: int | None,
    ) -> list[dict[str, object]]:
        names = sorted({n.lower().lstrip("@") for n in (usernames or set()) if n})
        excl = sorted({int(i) for i in (exclude_message_ids or set())})
        where = ["chat_id=?"]
        params: list[object] = [chat_id]
        if names:
            where.append(
                f"lower(COALESCE(from_username, '')) IN ({','.join('?' * len(names))})"
            )
            params.extend(names)
        blob = (
            "ulower(COALESCE(text,'') || ' ' || COALESCE(caption,'') || ' ' "
            "|| COALESCE(formatting_markdown,''))"
        )
        for token in tokens:
            escaped = (
                token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            where.append(f"{blob} LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if start_unix is not None:
            where.append("date_unix>=?")
            params.append(int(start_unix))
        if end_unix is not None:
            where.append("date_unix<?")
            params.append(int(end_unix))
        if excl:
            where.append(f"message_id NOT IN ({','.join('?' * len(excl))})")
            params.extend(excl)
        sql = f"""
            SELECT message_id, date_unix, date_iso,
                   from_username, from_display_name, from_is_bot,
                   text, caption, formatting_markdown, content_type,
                   has_media, media_file_name, reply_to_message_id, is_edited
            FROM messages
            WHERE {' AND '.join(where)}
            ORDER BY date_unix ASC, message_id ASC
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def context_rows_for_chat(
        self,
        chat_id: int,
        *,
        limit: int = 120,
        exclude_message_ids: set[int] | None = None,
        ensure_message_ids: set[int] | None = None,
    ) -> list[dict[str, object]]:
        """Newest-first fetch, then chronological; optionally pin specific ids."""
        cap = max(1, min(int(limit), 500))
        excl = exclude_message_ids or set()
        ensure = ensure_message_ids or set()
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT message_id, date_unix, date_iso,
                       from_username, from_display_name, from_is_bot,
                       text, caption, formatting_markdown, content_type,
                       has_media, media_file_name, reply_to_message_id, is_edited
                FROM messages
                WHERE chat_id=?
                ORDER BY date_unix DESC, message_id DESC
                LIMIT ?
                """,
                (chat_id, cap),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

            by_id = {int(r["message_id"]): r for r in rows}
            for mid in ensure:
                if mid in by_id or mid in excl:
                    continue
                pinned = self.get_message(chat_id, mid)
                if pinned is not None:
                    by_id[mid] = pinned

        ordered = sorted(
            by_id.values(),
            key=lambda r: (int(r["date_unix"] or 0), int(r["message_id"] or 0)),
        )
        if excl:
            ordered = [r for r in ordered if int(r["message_id"]) not in excl]
        return ordered

    def format_context_block(
        self,
        rows: list[dict[str, object]],
        *,
        max_chars: int = 24000,
    ) -> str:
        """Compact chronological transcript for agent prompts."""
        if not rows:
            return ""
        lines: list[str] = []
        for row in rows:
            mid = int(row["message_id"])
            who = (row.get("from_display_name") or "").strip()
            uname = (row.get("from_username") or "").strip()
            if uname:
                author = f"@{uname}" + (f" ({who})" if who and who != uname else "")
            elif who:
                author = who
            else:
                author = "unknown"
            if int(row.get("from_is_bot") or 0):
                author += " [bot]"

            body = (
                (row.get("formatting_markdown") or "").strip()
                or (row.get("text") or "").strip()
                or (row.get("caption") or "").strip()
            )
            extras: list[str] = []
            if int(row.get("has_media") or 0):
                media_name = (row.get("media_file_name") or "").strip()
                ctype = (row.get("content_type") or "media").strip()
                extras.append(
                    f"[media:{ctype}"
                    + (f" {media_name}" if media_name else "")
                    + "]"
                )
            if int(row.get("is_edited") or 0):
                extras.append("[edited]")
            reply_to = row.get("reply_to_message_id")
            if reply_to is not None:
                extras.append(f"[reply_to={int(reply_to)}]")

            stamp = (row.get("date_iso") or "")[:19]
            head = f"#{mid} {stamp} {author}"
            if extras:
                head += " " + " ".join(extras)
            if body:
                lines.append(f"{head}: {body}")
            else:
                lines.append(head)

        header = "[chat_messages]\n"
        if max_chars <= 0:
            return header + "\n".join(lines)
        # Prefer newest messages when over budget: drop from the top (oldest).
        budget = max(500, int(max_chars) - len(header))
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            add = len(line) + (1 if kept else 0)
            if used + add > budget and kept:
                break
            kept.append(line)
            used += add
        kept.reverse()
        if len(kept) < len(lines):
            omitted = len(lines) - len(kept)
            kept.insert(0, f"… ({omitted} older messages omitted)")
        return header + "\n".join(kept)

    def total_count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM messages")
            return int(cur.fetchone()[0])

    def record_agent_run(self, row: dict[str, Any]) -> None:
        payload = {
            "created_at": float(row.get("created_at") or datetime.now(timezone.utc).timestamp()),
            "session_key": str(row.get("session_key") or ""),
            "model": str(row.get("model") or ""),
            "status": str(row.get("status") or ""),
            "duration_ms": int(row.get("duration_ms") or 0),
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
            "reasoning_tokens": int(row.get("reasoning_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
            "usage_known": int(row.get("usage_known") or 0),
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agent_runs (
                    created_at, session_key, model, status, duration_ms,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_write_tokens, reasoning_tokens, total_tokens, usage_known
                ) VALUES (
                    :created_at, :session_key, :model, :status, :duration_ms,
                    :input_tokens, :output_tokens, :cache_read_tokens,
                    :cache_write_tokens, :reasoning_tokens, :total_tokens, :usage_known
                )
                """,
                payload,
            )

    def record_reply_latency(
        self,
        response_ms: int,
        *,
        chat_id: int | None = None,
        session_key: str = "",
    ) -> None:
        """Wall time from request handling start until Telegram accepted the reply."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reply_latency (sent_at, response_ms, chat_id, session_key)
                VALUES (?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).timestamp(),
                    max(0, int(response_ms)),
                    chat_id,
                    session_key or "",
                ),
            )

    def _reply_speed_unlocked(self, since: float) -> dict[str, Any]:
        def agg(where: str, params: tuple[object, ...]) -> dict[str, int | None]:
            row = self._conn.execute(
                f"""
                SELECT COUNT(*), AVG(response_ms), MIN(response_ms), MAX(response_ms)
                FROM reply_latency
                {where}
                """,
                params,
            ).fetchone()
            count = int(row[0] or 0) if row else 0
            if not count:
                return {"count": 0, "avg_ms": None, "min_ms": None, "max_ms": None}
            return {
                "count": count,
                "avg_ms": int(row[1] or 0),
                "min_ms": int(row[2] or 0),
                "max_ms": int(row[3] or 0),
            }

        last = self._conn.execute(
            """
            SELECT response_ms, sent_at, chat_id, session_key
            FROM reply_latency
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        last_reply = None
        if last is not None:
            sent = float(last[1] or 0)
            last_reply = {
                "response_ms": int(last[0] or 0),
                "at": datetime.fromtimestamp(sent).astimezone().isoformat(timespec="seconds"),
                "chat_id": last[2],
                "session_key": last[3] or "",
            }
        return {
            "today": agg("WHERE sent_at>=?", (since,)),
            "all": agg("", ()),
            "last": last_reply,
        }

    def usage_summary(self) -> dict[str, Any]:
        start_unix, start_real = _local_today_start()
        empty = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "runs": 0,
            "known_runs": 0,
        }
        with self._lock:
            all_row = self._usage_agg()
            today_row = self._usage_agg(since=start_real)
            last = self._conn.execute(
                """
                SELECT created_at, model, status, duration_ms, total_tokens,
                       input_tokens, output_tokens, usage_known, session_key
                FROM agent_runs ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            by_model_cur = self._conn.execute(
                """
                SELECT model,
                       COUNT(*) AS runs,
                       SUM(total_tokens) AS total_tokens,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens
                FROM agent_runs
                GROUP BY model
                ORDER BY SUM(total_tokens) DESC, COUNT(*) DESC
                LIMIT 8
                """
            )
            by_model = [
                {
                    "model": row[0] or "—",
                    "runs": int(row[1] or 0),
                    "total_tokens": int(row[2] or 0),
                    "input_tokens": int(row[3] or 0),
                    "output_tokens": int(row[4] or 0),
                }
                for row in by_model_cur.fetchall()
            ]
            messages_today = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE date_unix>=?",
                    (start_unix,),
                ).fetchone()[0]
            )
            senders = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT from_user_id, lower(COALESCE(from_username, '')),
                               lower(COALESCE(from_display_name, ''))
                        FROM messages
                        GROUP BY 1, 2, 3
                    )
                    """
                ).fetchone()[0]
            )
            reply_speed = self._reply_speed_unlocked(start_real)
        last_run = None
        if last is not None:
            created = float(last[0] or 0)
            last_run = {
                "at": datetime.fromtimestamp(created).astimezone().isoformat(timespec="seconds"),
                "model": last[1] or "",
                "status": last[2] or "",
                "duration_ms": int(last[3] or 0),
                "total_tokens": int(last[4] or 0),
                "input_tokens": int(last[5] or 0),
                "output_tokens": int(last[6] or 0),
                "usage_known": bool(last[7]),
                "session_key": last[8] or "",
            }
        return {
            "tokens_all": all_row or empty,
            "tokens_today": today_row or empty,
            "by_model": by_model,
            "last_run": last_run,
            "messages_today": messages_today,
            "senders_total": senders,
            "reply_speed": reply_speed,
        }

    def _usage_agg(self, *, since: float | None = None) -> dict[str, int]:
        where = ""
        params: tuple[object, ...] = ()
        if since is not None:
            where = "WHERE created_at>=?"
            params = (since,)
        row = self._conn.execute(
            f"""
            SELECT COUNT(*),
                   SUM(input_tokens), SUM(output_tokens),
                   SUM(cache_read_tokens), SUM(cache_write_tokens),
                   SUM(reasoning_tokens), SUM(total_tokens),
                   SUM(usage_known)
            FROM agent_runs
            {where}
            """,
            params,
        ).fetchone()
        if row is None:
            return {
                "runs": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "known_runs": 0,
            }
        return {
            "runs": int(row[0] or 0),
            "input_tokens": int(row[1] or 0),
            "output_tokens": int(row[2] or 0),
            "cache_read_tokens": int(row[3] or 0),
            "cache_write_tokens": int(row[4] or 0),
            "reasoning_tokens": int(row[5] or 0),
            "total_tokens": int(row[6] or 0),
            "known_runs": int(row[7] or 0),
        }

    def query_messages(
        self,
        *,
        chat_id: int | None = None,
        sender: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        text_query: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        page_size = max(10, min(int(page_size or 25), 100))
        page = max(1, int(page or 1))
        where: list[str] = []
        params: list[object] = []
        if chat_id is not None:
            where.append("chat_id=?")
            params.append(int(chat_id))
        sender_sql, sender_params = _sender_clause(sender)
        if sender_sql:
            where.append(sender_sql)
            params.extend(sender_params)
        if date_from:
            start = _local_date_start(date_from)
            if start is not None:
                where.append("date_unix>=?")
                params.append(start)
        if date_to:
            end = _local_date_end(date_to)
            if end is not None:
                where.append("date_unix<?")
                params.append(end)
        q = (text_query or "").strip()
        if q:
            like = _like_pattern(q)
            where.append(
                "(COALESCE(text,'') LIKE ? ESCAPE '\\' "
                "OR COALESCE(caption,'') LIKE ? ESCAPE '\\' "
                "OR COALESCE(formatting_markdown,'') LIKE ? ESCAPE '\\')"
            )
            params.extend((like, like, like))
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM messages {clause}",
                    params,
                ).fetchone()[0]
            )
            pages = max(1, (total + page_size - 1) // page_size) if total else 1
            if page > pages:
                page = pages
            offset = (page - 1) * page_size
            cur = self._conn.execute(
                f"""
                SELECT chat_id, chat_title, message_id, date_iso, date_unix,
                       from_user_id, from_username, from_display_name,
                       content_type, has_media, is_edited,
                       substr(COALESCE(text, caption, ''), 1, 400) AS body
                FROM messages
                {clause}
                ORDER BY date_unix DESC, message_id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, page_size, offset),
            )
            cols = [d[0] for d in cur.description]
            items = [dict(zip(cols, row)) for row in cur.fetchall()]
            facets = self._facets_unlocked(chat_id)
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "items": items,
            "facets": facets,
        }

    def _facets_unlocked(self, chat_id: int | None) -> dict[str, Any]:
        chats = []
        for row in self._conn.execute(
            """
            SELECT chat_id, MAX(COALESCE(chat_title, '')), COUNT(*)
            FROM messages
            GROUP BY chat_id
            ORDER BY COUNT(*) DESC, chat_id
            """
        ):
            title = (row[1] or "").strip() or f"chat {row[0]}"
            chats.append(
                {"chat_id": int(row[0]), "title": title, "count": int(row[2])}
            )
        sender_where = ""
        sender_params: tuple[object, ...] = ()
        if chat_id is not None:
            sender_where = "WHERE chat_id=?"
            sender_params = (int(chat_id),)
        senders = []
        for row in self._conn.execute(
            f"""
            SELECT from_user_id,
                   MAX(from_username),
                   MAX(from_display_name),
                   COUNT(*)
            FROM messages
            {sender_where}
            GROUP BY from_user_id,
                     lower(COALESCE(from_username, '')),
                     lower(COALESCE(from_display_name, ''))
            ORDER BY COUNT(*) DESC
            LIMIT 300
            """,
            sender_params,
        ):
            user_id, username, display, count = row
            key, label = _sender_key_label(user_id, username, display)
            senders.append({"key": key, "label": label, "count": int(count or 0)})
        return {"chats": chats, "senders": senders}
