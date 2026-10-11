from __future__ import annotations

import html
import logging
import re

from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE = 4096

_RE_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_RE_STRIKE = re.compile(r"~~(.+?)~~")
_RE_ITALIC = re.compile(
    r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|(?<!_)_(?!_)(.+?)(?<!_)_(?!_)"
)
_RE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_RE_UL_ITEM = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_RE_TABLE_SEP = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _format_inline(text: str) -> str:
    """Apply inline markdown → Telegram HTML. Escapes plain parts once."""
    if not text:
        return ""

    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\x00PH{len(placeholders) - 1}\x00"

    def restore(value: str) -> str:
        for idx, ph in enumerate(placeholders):
            value = value.replace(f"\x00PH{idx}\x00", ph)
        return value

    def esc_keep(value: str) -> str:
        parts = re.split(r"(\x00PH\d+\x00)", value)
        return "".join(
            part if part.startswith("\x00PH") and part.endswith("\x00") else _esc(part)
            for part in parts
        )

    work = _RE_INLINE_CODE.sub(
        lambda m: stash(f"<code>{_esc(m.group(1))}</code>"), text
    )
    work = _RE_LINK.sub(
        lambda m: stash(
            f'<a href="{html.escape(m.group(2), quote=True)}">{esc_keep(m.group(1))}</a>'
        ),
        work,
    )
    work = _RE_BOLD.sub(
        lambda m: stash(
            f"<b>{esc_keep(m.group(1) if m.group(1) is not None else m.group(2))}</b>"
        ),
        work,
    )
    work = _RE_STRIKE.sub(
        lambda m: stash(f"<s>{esc_keep(m.group(1))}</s>"),
        work,
    )
    work = _RE_ITALIC.sub(
        lambda m: stash(
            f"<i>{esc_keep(m.group(1) if m.group(1) is not None else m.group(2))}</i>"
        ),
        work,
    )
    work = esc_keep(work)
    return restore(work)


def _split_table_row(line: str) -> list[str]:
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [cell.strip().replace("\\|", "|") for cell in raw.split("|")]


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|") or stripped.count("|") < 2:
        return False
    if _RE_TABLE_SEP.match(stripped):
        return True
    return len(_split_table_row(stripped)) >= 2


def _render_table_row(header: list[str], cells: list[str]) -> str:
    width = max(len(header), len(cells))
    padded = cells + [""] * (width - len(cells))
    formatted = [_format_inline(cell) for cell in padded[:width]]
    if width == 2:
        return f"<b>{formatted[0]}</b> — {formatted[1]}"
    if width == 3:
        title = f"<b>{formatted[0]}</b>"
        if formatted[1]:
            title += f" · {formatted[1]}"
        if formatted[2]:
            return f"{title}\n{formatted[2]}"
        return title
    labels = header + [""] * (width - len(header))
    lines: list[str] = []
    for label, value in zip(labels, formatted):
        if not value:
            continue
        if label:
            lines.append(f"<b>{_format_inline(label)}:</b> {value}")
        else:
            lines.append(value)
    return "\n".join(lines)


def _render_table(lines: list[str]) -> str | None:
    """Turn a Markdown pipe table into Telegram blocks. None if it is not a table."""
    if not any(_RE_TABLE_SEP.match(line.strip()) for line in lines):
        return None
    rows: list[list[str]] = []
    for line in lines:
        if _RE_TABLE_SEP.match(line.strip()):
            continue
        cells = _split_table_row(line)
        if len(cells) < 2:
            return None
        rows.append(cells)
    if len(rows) < 2:
        return None
    header, data = rows[0], rows[1:]
    blocks = [_render_table_row(header, row) for row in data]
    blocks = [block for block in blocks if block.strip()]
    if not blocks:
        return None
    return "\n\n".join(blocks)


def markdown_to_telegram_html(text: str) -> str:
    """Convert common agent Markdown to Telegram HTML (no unsupported tags)."""
    if not text:
        return ""

    fences: list[str] = []

    def stash_fence(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip()
        code = match.group(2).rstrip("\n")
        table = _render_table(code.split("\n"))
        if table is not None:
            fences.append(table)
            return f"\x00FENCE{len(fences) - 1}\x00"
        body = _esc(code)
        # Telegram supports optional language class on <code> inside <pre>.
        if lang and re.fullmatch(r"[A-Za-z0-9_+#-]+", lang):
            block = f'<pre><code class="language-{lang}">{body}</code></pre>'
        else:
            block = f"<pre>{body}</pre>"
        fences.append(block)
        return f"\x00FENCE{len(fences) - 1}\x00"

    work = _RE_FENCE.sub(stash_fence, text.replace("\r\n", "\n").replace("\r", "\n"))

    lines = work.split("\n")
    out: list[str] = []
    quote_buf: list[str] = []
    table_buf: list[str] = []

    def flush_quote() -> None:
        nonlocal quote_buf
        if not quote_buf:
            return
        # Real newlines inside <blockquote> — Telegram does NOT support <br/>.
        inner = "\n".join(_format_inline(q) for q in quote_buf)
        out.append(f"<blockquote>{inner}</blockquote>")
        quote_buf = []

    def flush_table() -> None:
        nonlocal table_buf
        if not table_buf:
            return
        rendered = _render_table(table_buf)
        if rendered is None:
            out.extend(_format_inline(row) for row in table_buf)
        else:
            out.append(rendered)
        table_buf = []

    for line in lines:
        if line.startswith("\x00FENCE") and line.endswith("\x00"):
            flush_quote()
            flush_table()
            out.append(line)
            continue

        if _is_table_line(line):
            flush_quote()
            table_buf.append(line)
            continue
        flush_table()

        if line.startswith(">"):
            quote_buf.append(line.lstrip(">").lstrip())
            continue

        flush_quote()

        heading = _RE_HEADING.match(line)
        if heading:
            out.append(f"<b>{_format_inline(heading.group(2).strip())}</b>")
            continue

        ul = _RE_UL_ITEM.match(line)
        if ul:
            out.append(f"{ul.group(1)}• {_format_inline(ul.group(2))}")
            continue

        out.append(_format_inline(line))

    flush_quote()
    flush_table()

    result = "\n".join(out)
    for idx, block in enumerate(fences):
        result = result.replace(f"\x00FENCE{idx}\x00", block)

    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def chunk_telegram_html(text: str, size: int = TELEGRAM_MAX_MESSAGE) -> list[str]:
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= size:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


def strip_html_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def answer_formatted(message, text: str) -> None:
    """Send agent reply using Telegram HTML; fall back to plain on parse errors."""
    await send_formatted(
        message.bot,
        message.chat.id,
        text,
        reply_to_message_id=message.message_id,
    )


async def send_formatted(
    bot,
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: int | None = None,
) -> None:
    """Send HTML-formatted text to an arbitrary chat."""
    from aiogram.exceptions import TelegramBadRequest

    html_text = markdown_to_telegram_html(text)
    parts = chunk_telegram_html(html_text)
    if not parts:
        await bot.send_message(chat_id, "(empty)")
        return

    for index, part in enumerate(parts):
        kwargs: dict = {"parse_mode": "HTML"}
        if index == 0 and reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        try:
            await bot.send_message(chat_id, part, **kwargs)
        except TelegramBadRequest as exc:
            logger.warning(
                "Telegram HTML rejected (%s); falling back to plain. Preview: %r",
                exc,
                part[:240],
            )
            plain = strip_html_tags(part)
            for plain_part in chunk_telegram_html(plain or text):
                plain_kwargs: dict = {}
                if index == 0 and reply_to_message_id is not None:
                    plain_kwargs["reply_to_message_id"] = reply_to_message_id
                    reply_to_message_id = None
                await bot.send_message(chat_id, plain_part, **plain_kwargs)


async def send_saved_files(
    bot,
    chat_id: int,
    paths: list,
    *,
    reply_to_message_id: int | None = None,
) -> list:
    """Send image files as photos and everything else as documents.

    Returns paths that were sent successfully.
    """
    from .outbox import PHOTO_EXTENSIONS, mark_sent

    sent = []
    for index, path in enumerate(paths):
        if not path.is_file():
            continue
        kwargs: dict = {}
        if index == 0 and reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        payload = FSInputFile(path, filename=path.name)
        try:
            if path.suffix.lower() in PHOTO_EXTENSIONS:
                await bot.send_photo(chat_id, payload, **kwargs)
            else:
                await bot.send_document(chat_id, payload, **kwargs)
        except Exception:
            logger.exception("Failed to send file %s to chat %s", path.name, chat_id)
            try:
                await bot.send_message(
                    chat_id,
                    f"Не удалось отправить файл {path.name}.",
                )
            except Exception:
                logger.exception("Failed to report file send error")
            continue
        mark_sent(path)
        sent.append(path)
    return sent
