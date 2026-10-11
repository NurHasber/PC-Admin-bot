from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PendingJob:
    """In-memory sensitive / group confirmation job for the admin DM."""

    prompt: str
    session_key: str
    source_chat_id: int
    source_message_id: int | None
    shared_chat: bool
    chat_title: str
    stage: str = "password"  # password | delivery
    agent_reply: str | None = None
    deliverable_paths: list[str] = field(default_factory=list)
    started_at: float = 0.0
