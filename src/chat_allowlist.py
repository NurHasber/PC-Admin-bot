from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT_DIR

logger = logging.getLogger(__name__)

ALLOWLIST_FILE = ROOT_DIR / "chat_allowlist.json"
PENDING_TTL_SECONDS = 30 * 60

# Private invite links and public t.me/<name> (not deep links with ?start=).
_INVITE_LINK_RE = re.compile(
    r"(?i)\b(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:\+|joinchat/)([A-Za-z0-9_-]+)"
)
_PUBLIC_CHAT_LINK_RE = re.compile(
    r"(?i)\b(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"([A-Za-z][A-Za-z0-9_]{3,})"
    r"(?:/[0-9]+)?(?!\S)"
)


@dataclass
class PendingInvite:
    link: str
    expires_at: float

    def to_json(self) -> dict[str, object]:
        return {"link": self.link, "expires_at": self.expires_at}

    @classmethod
    def from_json(cls, data: dict[str, object]) -> PendingInvite | None:
        link = str(data.get("link") or "").strip()
        try:
            expires_at = float(data.get("expires_at") or 0)
        except (TypeError, ValueError):
            return None
        if not link or expires_at <= 0:
            return None
        return cls(link=link, expires_at=expires_at)


def extract_invite_link(text: str, *, bot_username: str = "") -> str | None:
    """Return first invite/public chat link found in text, or None."""
    if not text:
        return None
    m = _INVITE_LINK_RE.search(text)
    if m:
        return m.group(0).split("?")[0].rstrip(").,;\"'")
    for m in _PUBLIC_CHAT_LINK_RE.finditer(text):
        name = m.group(1)
        if bot_username and name.lower() == bot_username.lower():
            continue
        # Skip common bot deep-link style noise handled elsewhere.
        if name.lower() in {"share", "proxy", "socks", "iv", "addstickers"}:
            continue
        return m.group(0).split("?")[0].rstrip(").,;\"'")
    return None


class ChatAllowlistStore:
    def __init__(self) -> None:
        self._allowed: set[int] = set()
        self._pending: PendingInvite | None = None
        self._load()

    def _load(self) -> None:
        if not ALLOWLIST_FILE.is_file():
            return
        try:
            raw = json.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))
            ids = raw.get("allowed_chat_ids", [])
            if isinstance(ids, list):
                for item in ids:
                    try:
                        self._allowed.add(int(item))
                    except (TypeError, ValueError):
                        continue
            pending_raw = raw.get("pending_invite")
            if isinstance(pending_raw, dict):
                pending = PendingInvite.from_json(pending_raw)
                if pending and pending.expires_at > time.time():
                    self._pending = pending
        except Exception:
            logger.exception("Failed to read chat_allowlist.json")

    def save(self) -> None:
        payload: dict[str, object] = {
            "allowed_chat_ids": sorted(self._allowed),
        }
        if self._pending is not None and self._pending.expires_at > time.time():
            payload["pending_invite"] = self._pending.to_json()
        ALLOWLIST_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self._allowed

    def allowed_ids(self) -> set[int]:
        return set(self._allowed)

    def allow(self, chat_id: int) -> None:
        if chat_id in self._allowed:
            return
        self._allowed.add(chat_id)
        self.save()
        logger.info("Allowlisted chat_id=%s", chat_id)

    def revoke(self, chat_id: int) -> bool:
        if chat_id not in self._allowed:
            return False
        self._allowed.discard(chat_id)
        self.save()
        logger.info("Revoked allowlist chat_id=%s", chat_id)
        return True

    def arm_pending(self, link: str, *, ttl_seconds: int = PENDING_TTL_SECONDS) -> PendingInvite:
        pending = PendingInvite(
            link=link.strip(),
            expires_at=time.time() + max(60, ttl_seconds),
        )
        self._pending = pending
        self.save()
        logger.info("Armed pending invite until %s link=%s", pending.expires_at, link)
        return pending

    def pending(self) -> PendingInvite | None:
        if self._pending is None:
            return None
        if self._pending.expires_at <= time.time():
            self._pending = None
            self.save()
            return None
        return self._pending

    def consume_pending_if_valid(self) -> PendingInvite | None:
        pending = self.pending()
        if pending is None:
            return None
        self._pending = None
        self.save()
        return pending

    def clear_pending(self) -> None:
        if self._pending is None:
            return
        self._pending = None
        self.save()
