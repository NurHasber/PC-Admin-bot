from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT_DIR, Settings

logger = logging.getLogger(__name__)

STATE_FILE = ROOT_DIR / "bot_state.json"


@dataclass
class TopicRecord:
    id: str
    title: str
    agent_id: str
    created_at: float
    updated_at: float

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> TopicRecord:
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            agent_id=str(data.get("agent_id") or ""),
            created_at=float(data.get("created_at") or 0),
            updated_at=float(data.get("updated_at") or 0),
        )


# Bump when reply keyboard layout changes so DM gets a one-shot refresh.
REPLY_KEYBOARD_VERSION = 2


@dataclass
class BotState:
    selected_model: str
    active_topic_id: str
    topics: list[TopicRecord]
    # Where private-DM texts go: "private" (topics) or "chat:{telegram_chat_id}".
    dm_session_key: str = "private"
    reply_keyboard_version: int = 0

    def topic_by_id(self, topic_id: str) -> TopicRecord | None:
        for topic in self.topics:
            if topic.id == topic_id:
                return topic
        return None

    def active_topic(self) -> TopicRecord | None:
        return self.topic_by_id(self.active_topic_id)

    def dm_is_private(self) -> bool:
        return self.dm_session_key == "private" or not self.dm_session_key.startswith(
            "chat:"
        )


class BotStateStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.state = self._load_or_create()

    def _load_or_create(self) -> BotState:
        if STATE_FILE.is_file():
            try:
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                topics = [
                    TopicRecord.from_json(item)
                    for item in raw.get("topics", [])
                    if isinstance(item, dict)
                ]
                active_id = str(raw.get("active_topic_id") or "")
                model = str(raw.get("selected_model") or self._settings.cursor_model)
                dm_key = str(raw.get("dm_session_key") or "private")
                if dm_key != "private" and not dm_key.startswith("chat:"):
                    dm_key = "private"
                try:
                    kb_ver = int(raw.get("reply_keyboard_version") or 0)
                except (TypeError, ValueError):
                    kb_ver = 0
                if topics and active_id and any(t.id == active_id for t in topics):
                    return BotState(
                        selected_model=model,
                        active_topic_id=active_id,
                        topics=topics,
                        dm_session_key=dm_key,
                        reply_keyboard_version=kb_ver,
                    )
                logger.warning("bot_state.json invalid; recreating default state")
            except Exception:
                logger.exception("Failed to read bot_state.json; recreating default state")

        now = time.time()
        default = TopicRecord(
            id=uuid.uuid4().hex[:8],
            title="Основная",
            agent_id="",
            created_at=now,
            updated_at=now,
        )
        state = BotState(
            selected_model=self._settings.cursor_model,
            active_topic_id=default.id,
            topics=[default],
        )
        self._write(state)
        return state

    def save(self) -> None:
        self._write(self.state)

    def _write(self, state: BotState) -> None:
        payload = {
            "selected_model": state.selected_model,
            "active_topic_id": state.active_topic_id,
            "topics": [topic.to_json() for topic in state.topics],
            "dm_session_key": state.dm_session_key or "private",
            "reply_keyboard_version": int(state.reply_keyboard_version or 0),
        }
        STATE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def set_model(self, model_id: str) -> None:
        self.state.selected_model = model_id
        self.save()

    def set_dm_session_key(self, session_key: str) -> None:
        key = (session_key or "private").strip()
        if key != "private" and not key.startswith("chat:"):
            key = "private"
        self.state.dm_session_key = key
        self.save()

    def needs_reply_keyboard_refresh(self) -> bool:
        return int(self.state.reply_keyboard_version or 0) < REPLY_KEYBOARD_VERSION

    def mark_reply_keyboard_current(self) -> None:
        self.state.reply_keyboard_version = REPLY_KEYBOARD_VERSION
        self.save()

    def touch_topic(self, topic_id: str) -> None:
        topic = self.state.topic_by_id(topic_id)
        if topic is None:
            return
        topic.updated_at = time.time()
        self.save()

    def add_topic(self, title: str) -> TopicRecord:
        now = time.time()
        topic = TopicRecord(
            id=uuid.uuid4().hex[:8],
            title=title,
            agent_id="",
            created_at=now,
            updated_at=now,
        )
        self.state.topics.insert(0, topic)
        self.state.active_topic_id = topic.id
        self.save()
        return topic

    def activate_topic(self, topic_id: str) -> TopicRecord | None:
        topic = self.state.topic_by_id(topic_id)
        if topic is None:
            return None
        self.state.active_topic_id = topic.id
        topic.updated_at = time.time()
        self.save()
        return topic

    def set_topic_agent_id(self, topic_id: str, agent_id: str) -> None:
        topic = self.state.topic_by_id(topic_id)
        if topic is None:
            return
        topic.agent_id = agent_id
        topic.updated_at = time.time()
        self.save()

    def rename_topic_if_default(self, topic_id: str, message_preview: str) -> None:
        topic = self.state.topic_by_id(topic_id)
        if topic is None:
            return
        if not topic.title.startswith("Новая тема") and topic.title != "Основная":
            return
        preview = " ".join(message_preview.split())
        if len(preview) < 4:
            return
        topic.title = preview[:48] + ("…" if len(preview) > 48 else "")
        topic.updated_at = time.time()
        self.save()

    def next_default_topic_title(self) -> str:
        n = sum(1 for t in self.state.topics if t.title.startswith("Новая тема"))
        return f"Новая тема {n + 1}"
