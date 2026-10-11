from __future__ import annotations

import re

from aiogram.enums import ChatType, MessageEntityType
from aiogram.types import Message


def is_shared_chat(message: Message) -> bool:
    chat_type = message.chat.type
    return chat_type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
        ChatType.CHANNEL,
    }


def message_mentions_bot(
    message: Message,
    bot_username: str,
    *,
    bot_id: int | None = None,
) -> bool:
    """True only when @bot_username is present as a mention entity or plain text."""
    if not bot_username and bot_id is None:
        return False
    text = message.text or message.caption or ""
    if not text:
        return False
    needle = f"@{bot_username}".lower() if bot_username else ""
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for ent in entities:
        if ent.type == MessageEntityType.MENTION and needle:
            frag = text[ent.offset : ent.offset + ent.length]
            if frag.lower() == needle:
                return True
        if ent.type == MessageEntityType.TEXT_MENTION and ent.user:
            if bot_id is not None and ent.user.id == bot_id:
                return True
            uname = (ent.user.username or "").lower()
            if bot_username and uname == bot_username.lower():
                return True
    if needle and needle in text.lower():
        return True
    return False


def message_is_reply_to_bot(message: Message, bot_id: int | None) -> bool:
    """True when this message is a Telegram Reply to one of the bot's messages."""
    if bot_id is None:
        return False
    replied = message.reply_to_message
    if replied is None:
        return False
    user = replied.from_user
    return bool(user and user.id == bot_id)


def strip_bot_mention(text: str, bot_username: str) -> str:
    if not text or not bot_username:
        return (text or "").strip()
    cleaned = re.sub(
        rf"@{re.escape(bot_username)}\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(cleaned.split()).strip()


def extract_shared_prompt(
    message: Message,
    bot_username: str,
    *,
    bot_id: int | None = None,
) -> str | None:
    """
    Prompt for shared-chat agent, or None if not addressed to the bot.

    Triggers:
    - @bot mention
    - Reply to a message from the bot

    A Reply adds quoted_message_id. The stored message for that id is
    attached before the agent runs.
    """
    mentioned = message_mentions_bot(message, bot_username, bot_id=bot_id)
    reply_to_bot = message_is_reply_to_bot(message, bot_id)
    if not mentioned and not reply_to_bot:
        return None

    raw = message.text or message.caption or ""
    user_text = (
        strip_bot_mention(raw, bot_username) if mentioned else raw.strip()
    )
    if not user_text and message.reply_to_message is None:
        return ""

    parts: list[str] = []
    replied = message.reply_to_message
    if replied is not None:
        parts.append(f"[quoted_message_id={replied.message_id}]")
    if user_text:
        parts.append(user_text)
    return "\n".join(parts)


# Backwards-compatible alias used by older call sites / tests.
def extract_mention_prompt(
    message: Message,
    bot_username: str,
    *,
    bot_id: int | None = None,
) -> str | None:
    return extract_shared_prompt(message, bot_username, bot_id=bot_id)
