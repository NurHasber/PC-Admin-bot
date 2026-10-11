"""Files the agent may hand back to Telegram, and files the admin attached."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .config import ROOT_DIR

logger = logging.getLogger(__name__)

OUTBOX_DIR = ROOT_DIR / "agent_outbox"
SENT_DIR = OUTBOX_DIR / "_sent"
INBOX_DIR = ROOT_DIR / "agent_inbox"

# Stay under Telegram Bot API sendDocument limit (50 MB).
MAX_SEND_BYTES = 45 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DOCUMENT_EXTENSIONS = {
    ".docx",
    ".doc",
    ".pdf",
    ".xlsx",
    ".xls",
    ".csv",
    ".txt",
    ".md",
    ".pptx",
    ".zip",
    ".gif",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}

_SAFE_NAME = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)


def ensure_outbox() -> Path:
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTBOX_DIR


def deliverable_instructions() -> str:
    """Short rules appended to every agent prompt."""
    root = ensure_outbox()
    py = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    return (
        "Files and images for Telegram: if the reply should include a document "
        f"or a picture, save the finished file(s) only in this directory: {root}\n"
        "Extensions: .docx .pdf .xlsx .csv .pptx .txt .md .zip .png .jpg .jpeg .webp .gif. "
        f"Word: use \"{py}\" with python-docx (from docx import Document) and save the .docx there. "
        "Images must be real image files, not a description.\n"
        "The app attaches new files from that directory to the Telegram reply. "
        "Do not mention the directory, Python, or that a file was saved on disk.\n"
        "Lines under [attached_local_files] or [local_files] are local paths you can open "
        "(including a Google Drive folder if it is synced on this PC). If a source file "
        "cannot be opened, say so — do not invent its contents. Never copy secrets, env "
        "files, or databases into the deliverable directory."
    )


def snapshot_outbox(root: Path | None = None) -> dict[str, tuple[int, int]]:
    """Map relative path -> (mtime_ns, size) for sendable files."""
    base = root or ensure_outbox()
    found: dict[str, tuple[int, int]] = {}
    if not base.is_dir():
        return found
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if _is_ignored(base, path):
            continue
        if path.suffix.lower() not in DOCUMENT_EXTENSIONS:
            continue
        try:
            rel = path.relative_to(base).as_posix()
            stat = path.stat()
        except OSError:
            continue
        found[rel] = (stat.st_mtime_ns, stat.st_size)
    return found


def collect_new_deliverables(
    before: dict[str, tuple[int, int]],
    root: Path | None = None,
) -> list[Path]:
    """Files created or rewritten since snapshot_outbox()."""
    base = root or OUTBOX_DIR
    after = snapshot_outbox(base)
    changed: list[Path] = []
    for rel, meta in after.items():
        if before.get(rel) == meta:
            continue
        path = (base / rel).resolve()
        if not _is_inside(base, path):
            continue
        if path.stat().st_size > MAX_SEND_BYTES:
            logger.warning("Skip oversized deliverable %s", path.name)
            continue
        if path.stat().st_size <= 0:
            continue
        changed.append(path)
    changed.sort(key=lambda p: p.stat().st_mtime_ns)
    return changed


def mark_sent(path: Path) -> None:
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    dest = SENT_DIR / f"{int(time.time())}_{path.name}"
    try:
        path.replace(dest)
    except OSError:
        logger.exception("Could not archive sent file %s", path)


def inbox_path(chat_id: int, message_id: int, filename: str) -> Path:
    folder = INBOX_DIR / str(chat_id)
    folder.mkdir(parents=True, exist_ok=True)
    safe = _SAFE_NAME.sub("_", Path(filename).name).strip("._") or "file"
    safe = safe[:120]
    return folder / f"{message_id}_{safe}"


def _is_ignored(base: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(base)
    except ValueError:
        return True
    return any(part.startswith("_") or part.startswith(".") for part in rel.parts)


def _is_inside(base: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True
