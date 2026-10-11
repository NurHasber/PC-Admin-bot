from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

BTN_MODEL = "🤖 Модель"
BTN_TOPICS = "📂 Темы"
BTN_NEW_TOPIC = "➕ Новая тема"
BTN_CHAT_AGENT = "💬 Чат-агент"
BTN_CHATS = "📋 Чаты"

CONTROL_BUTTON_TEXTS = frozenset(
    {BTN_MODEL, BTN_TOPICS, BTN_NEW_TOPIC, BTN_CHAT_AGENT, BTN_CHATS}
)

CB_MODEL_PREFIX = "md:"
CB_TOPIC_PREFIX = "tp:"
CB_TOPIC_NEW = "tp:__new__"
CB_DELIVERY_DM = "dl:dm"
CB_DELIVERY_CHAT = "dl:chat"
CB_CHAT_AGENT_PREFIX = "ca:"
CB_CHAT_AGENT_PRIVATE = "ca:private"
CB_LEAVE_ASK_PREFIX = "lva:"
CB_LEAVE_OK_PREFIX = "lvok:"
CB_LEAVE_CANCEL = "lvcancel"
CB_CHAT_FORGET_PREFIX = "rm:"

TELEGRAM_CALLBACK_DATA_MAX = 64


def delivery_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Только в личку",
                    callback_data=CB_DELIVERY_DM,
                ),
                InlineKeyboardButton(
                    text="В общий чат",
                    callback_data=CB_DELIVERY_CHAT,
                ),
            ],
        ]
    )


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_MODEL),
                KeyboardButton(text=BTN_TOPICS),
            ],
            [
                KeyboardButton(text=BTN_NEW_TOPIC),
                KeyboardButton(text=BTN_CHAT_AGENT),
            ],
            [
                KeyboardButton(text=BTN_CHATS),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def models_inline_keyboard(
    models: list[tuple[str, str]],
    selected_model_id: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for model_id, label in models:
        mark = "✓ " if model_id == selected_model_id else ""
        text = f"{mark}{label}"[:64]
        callback = f"{CB_MODEL_PREFIX}{model_id}"
        if len(callback.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_MAX:
            continue
        rows.append([InlineKeyboardButton(text=text, callback_data=callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topics_inline_keyboard(
    topics: list[tuple[str, str, bool]],
) -> InlineKeyboardMarkup:
    """Each item: (topic_id, title, is_active)."""
    rows: list[list[InlineKeyboardButton]] = []
    for topic_id, title, is_active in topics:
        prefix = "▶ " if is_active else ""
        text = f"{prefix}{title}"[:64]
        callback = f"{CB_TOPIC_PREFIX}{topic_id}"
        rows.append([InlineKeyboardButton(text=text, callback_data=callback)])
    rows.append(
        [InlineKeyboardButton(text="➕ Новая тема", callback_data=CB_TOPIC_NEW)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chat_agents_inline_keyboard(
    chats: list[tuple[str, str, bool]],
    *,
    private_active: bool,
) -> InlineKeyboardMarkup:
    """Each item: (session_key, title, is_active)."""
    rows: list[list[InlineKeyboardButton]] = []
    private_mark = "▶ " if private_active else ""
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{private_mark}Личные темы",
                callback_data=CB_CHAT_AGENT_PRIVATE,
            )
        ]
    )
    for session_key, title, is_active in chats:
        callback = f"{CB_CHAT_AGENT_PREFIX}{session_key}"
        if len(callback.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_MAX:
            continue
        prefix = "▶ " if is_active else ""
        text = f"{prefix}{title}"[:64]
        rows.append([InlineKeyboardButton(text=text, callback_data=callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def membership_chats_keyboard(
    rows_data: list[tuple[int, str, bool]],
) -> InlineKeyboardMarkup:
    """Each item: (chat_id, title, is_member). Leave or forget."""
    rows: list[list[InlineKeyboardButton]] = []
    for chat_id, title, is_member in rows_data:
        label = (title or str(chat_id))[:40]
        if is_member:
            action = InlineKeyboardButton(
                text="Выйти",
                callback_data=f"{CB_LEAVE_ASK_PREFIX}{chat_id}",
            )
            name_btn = InlineKeyboardButton(
                text=label,
                callback_data=f"{CB_LEAVE_ASK_PREFIX}{chat_id}",
            )
        else:
            action = InlineKeyboardButton(
                text="Убрать",
                callback_data=f"{CB_CHAT_FORGET_PREFIX}{chat_id}",
            )
            name_btn = InlineKeyboardButton(
                text=f"{label} (нет)",
                callback_data=f"{CB_CHAT_FORGET_PREFIX}{chat_id}",
            )
        rows.append([name_btn, action])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def leave_confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, выйти",
                    callback_data=f"{CB_LEAVE_OK_PREFIX}{chat_id}",
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=CB_LEAVE_CANCEL,
                ),
            ],
        ]
    )
