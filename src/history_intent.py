"""Decide which stored chat messages a prompt needs.

A reply or an explicit message id always loads that one message.
A question about the chat loads either text matches or a recent window —
never the whole log on an ordinary question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .history_scope import strip_period_phrases

# Needs a scan of stored chat messages (still not attached unless this hits).
_HISTORY_RE = re.compile(
    r"истори[юияе]|"
    r"кто\s+(?:писал|сказал|говорил|пишет)|"
    r"что\s+(?:писали|говорил|обсужда|писалось)|"
    r"в\s+нашем\s+чате|"
    r"предыдущ\w*\s+сообщени|"
    r"раньше\s+(?:в\s+чате|писал)|"
    r"найди\s+(?:в\s+чате|сообщен)|"
    r"переписк|"
    r"что\s+обсужда|"
    r"chat history|"
    r"who\s+(?:wrote|said)|"
    r"earlier messages",
    re.IGNORECASE,
)

_EXPLICIT_ID_RE = re.compile(
    r"(?:message_id|quoted_message_id)\s*=\s*(\d{1,12})|"
    r"#(\d{2,12})\b|"
    r"сообщени[еяю]\s+#?(\d{2,12})",
    re.IGNORECASE,
)

_USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")

_FILE_RE = re.compile(
    r"файл|документ|фото|картин|изображен|вложен|скрин|"
    r"pdf|docx|xlsx|pptx|таблиц|"
    r"\bтз\b|attachment|the file|this file|attached",
    re.IGNORECASE,
)

_QUOTE_TAG_RE = re.compile(r"\[quoted_message_id=(\d+)\]")

# Words that name the task, not the thing to find in the chat.
_SEARCH_STOP = {
    "это", "этот", "эта", "эти", "этом", "эту", "этого", "этому",
    "что", "как", "кто", "где", "когда", "куда", "или", "для", "про",
    "чат", "чате", "чата", "чату", "нашем", "нашего", "нашей", "наш",
    "сообщение", "сообщения", "сообщении", "сообщений", "сообщениях",
    "написал", "написала", "написали", "писал", "писала", "писали",
    "сказал", "сказала", "говорил", "говорила", "говорили",
    "обсуждали", "обсуждал", "обсуждала", "обсуждалось",
    "найди", "найти", "поищи", "поиск", "покажи", "показать",
    "пожалуйста", "можно", "просто", "только", "бот", "бота",
    "the", "and", "for", "who", "what", "chat", "message", "messages",
    "find", "search", "wrote", "said", "about",
}

_FOCUS_RE = re.compile(
    r"[«\"]([^»\"]{2,80})[»\"]"
    r"|(?:про|about)\s+([^\n]{2,80})$"
    r"|(?:найди|поищи|покажи|find)\s+(?:в\s+чате\s+|сообщен\w*\s+)?([^\n]{2,80})",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё\-]{3,}")


@dataclass
class HistoryNeed:
    """What to read. Empty means: do not touch stored messages."""

    message_ids: set[int] = field(default_factory=set)
    scan_recent: bool = False
    search_query: str = ""
    usernames: set[str] = field(default_factory=set)
    wants_files: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.message_ids and not self.scan_recent


def _content_tokens(text: str, *, limit: int) -> list[str]:
    kept: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        word = raw.casefold().replace("ё", "е")
        if word in _SEARCH_STOP:
            continue
        if word.startswith(("сообщен", "написа", "говори", "обсужда")):
            continue
        kept.append(raw)
        if len(kept) >= limit:
            break
    return kept


def search_phrase(text: str) -> str:
    """Words to look up in chat text. Empty means: no specific subject."""
    raw = strip_period_phrases(text or "")
    raw = _USERNAME_RE.sub(" ", raw)
    match = _FOCUS_RE.search(raw)
    if match:
        focused = next(group for group in match.groups() if group)
        tokens = _content_tokens(focused, limit=6)
        if tokens:
            return " ".join(tokens)
        return ""
    tokens = _content_tokens(raw, limit=5)
    if len(tokens) > 4:
        return ""
    return " ".join(tokens)


def split_quoted_tag(prompt: str) -> tuple[str, int | None]:
    """Return (user text, quoted message id) without the id tag line."""
    match = _QUOTE_TAG_RE.search(prompt or "")
    quoted_id = int(match.group(1)) if match else None
    text = _QUOTE_TAG_RE.sub("", prompt or "")
    text = "\n".join(line for line in text.splitlines() if line.strip())
    return text.strip(), quoted_id


def detect_history_need(
    user_text: str,
    *,
    quoted_message_id: int | None = None,
    bot_username: str | None = None,
) -> HistoryNeed:
    """
    Inspect the user's words only (not the quoted body).

    - A reply id, or an id written in the text, always loads that message.
    - A chat-history question loads text matches when it names a subject,
      otherwise a recent window.
    - @names narrow a recent window; they do not by themselves trigger one.
    """
    text = user_text or ""
    need = HistoryNeed()
    bot = (bot_username or "").lstrip("@").lower()

    if quoted_message_id is not None:
        need.message_ids.add(int(quoted_message_id))

    for groups in _EXPLICIT_ID_RE.findall(text):
        for raw in groups:
            if raw:
                need.message_ids.add(int(raw))

    if _HISTORY_RE.search(text):
        need.scan_recent = True
        need.search_query = search_phrase(text)
        for name in _USERNAME_RE.findall(text):
            if name.lower() != bot:
                need.usernames.add(name.lower())

    if _FILE_RE.search(text):
        need.wants_files = True

    return need
