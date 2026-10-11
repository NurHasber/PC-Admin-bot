from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT_DIR

logger = logging.getLogger(__name__)

CHAT_SESSIONS_FILE = ROOT_DIR / "chat_sessions.json"


@dataclass
class ChatSession:
    """One Cursor agent session per Telegram chat (group/channel/private-root)."""

    key: str
    title: str
    agent_id: str
    created_at: float
    updated_at: float

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> ChatSession:
        return cls(
            key=str(data["key"]),
            title=str(data.get("title") or data["key"]),
            agent_id=str(data.get("agent_id") or ""),
            created_at=float(data.get("created_at") or 0),
            updated_at=float(data.get("updated_at") or 0),
        )


def private_key() -> str:
    return "private"


def chat_key(chat_id: int) -> str:
    return f"chat:{chat_id}"


class ChatSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._load()

    def _load(self) -> None:
        if not CHAT_SESSIONS_FILE.is_file():
            return
        try:
            raw = json.loads(CHAT_SESSIONS_FILE.read_text(encoding="utf-8"))
            items = raw.get("sessions", {})
            if isinstance(items, dict):
                for key, value in items.items():
                    if isinstance(value, dict):
                        self._sessions[str(key)] = ChatSession.from_json(value)
        except Exception:
            logger.exception("Failed to read chat_sessions.json")

    def save(self) -> None:
        payload = {
            "sessions": {k: v.to_json() for k, v in self._sessions.items()},
        }
        CHAT_SESSIONS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_or_create(self, key: str, title: str) -> ChatSession:
        session = self._sessions.get(key)
        if session is not None:
            if title and session.title != title:
                session.title = title
                session.updated_at = time.time()
                self.save()
            return session
        now = time.time()
        session = ChatSession(
            key=key,
            title=title or key,
            agent_id="",
            created_at=now,
            updated_at=now,
        )
        self._sessions[key] = session
        self.save()
        return session

    def set_agent_id(self, key: str, agent_id: str) -> None:
        session = self._sessions.get(key)
        if session is None:
            return
        session.agent_id = agent_id
        session.updated_at = time.time()
        self.save()

    def touch(self, key: str) -> None:
        session = self._sessions.get(key)
        if session is None:
            return
        session.updated_at = time.time()
        self.save()

    def get(self, key: str) -> ChatSession | None:
        return self._sessions.get(key)

    def list_chat_sessions(self) -> list[ChatSession]:
        """Group/channel sessions only (keys chat:*), newest first."""
        items = [s for s in self._sessions.values() if s.key.startswith("chat:")]
        items.sort(key=lambda s: s.updated_at, reverse=True)
        return items

    def parse_chat_id(self, key: str) -> int | None:
        if not key.startswith("chat:"):
            return None
        try:
            return int(key.removeprefix("chat:"))
        except ValueError:
            return None

