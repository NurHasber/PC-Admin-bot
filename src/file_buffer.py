"""Bounded on-disk cache of chat files for the agent to open."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from .config import ROOT_DIR

logger = logging.getLogger(__name__)

BUFFER_DIR = ROOT_DIR / "file_buffer"
_SAFE_NAME = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)


class FileBuffer:
    def __init__(self, root: Path | None = None, *, max_bytes: int) -> None:
        self.root = root or BUFFER_DIR
        self.max_bytes = max(1, int(max_bytes))
        self.root.mkdir(parents=True, exist_ok=True)

    def stats(self) -> dict[str, int | float]:
        files = self._files()
        used = sum(size for _path, size, _mtime in files)
        limit = self.max_bytes
        percent = (used / limit * 100.0) if limit else 0.0
        return {
            "used_bytes": used,
            "limit_bytes": limit,
            "files": len(files),
            "percent": round(min(percent, 100.0), 1),
        }

    def destination(self, chat_id: int, message_id: int, filename: str) -> Path:
        folder = self.root / str(int(chat_id))
        folder.mkdir(parents=True, exist_ok=True)
        safe = _SAFE_NAME.sub("_", Path(filename).name).strip("._") or "file"
        return folder / f"{int(message_id)}_{safe[:100]}"

    def find(self, chat_id: int, message_id: int) -> Path | None:
        folder = self.root / str(int(chat_id))
        if not folder.is_dir():
            return None
        prefix = f"{int(message_id)}_"
        for path in folder.iterdir():
            if path.is_file() and path.name.startswith(prefix):
                return path
        return None

    def make_room(
        self,
        extra_bytes: int,
        *,
        protect: Path | None = None,
        on_evict: Callable[[Path], None] | None = None,
    ) -> bool:
        """Drop oldest files until `extra_bytes` fit. Never deletes `protect`."""
        extra = max(0, int(extra_bytes))
        if extra > self.max_bytes:
            return False
        guard = 0
        while self._used() + extra > self.max_bytes:
            guard += 1
            if guard > 10000:
                return False
            oldest = self._oldest(protect)
            if oldest is None:
                return False
            if on_evict is not None:
                on_evict(oldest)
            self._delete(oldest)
        return True

    def parse_ids(self, path: Path) -> tuple[int, int] | None:
        try:
            chat_id = int(path.parent.name)
        except ValueError:
            return None
        head, sep, _rest = path.name.partition("_")
        if not sep:
            return None
        try:
            return chat_id, int(head)
        except ValueError:
            return None

    def _files(self) -> list[tuple[Path, int, int]]:
        found: list[tuple[Path, int, int]] = []
        if not self.root.is_dir():
            return found
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((path, stat.st_size, stat.st_mtime_ns))
        return found

    def _used(self) -> int:
        return sum(size for _path, size, _mtime in self._files())

    def _oldest(self, protect: Path | None) -> Path | None:
        protect_resolved = protect.resolve() if protect is not None else None
        oldest: tuple[int, Path] | None = None
        for path, _size, mtime in self._files():
            if protect_resolved is not None and path.resolve() == protect_resolved:
                continue
            if oldest is None or mtime < oldest[0]:
                oldest = (mtime, path)
        return oldest[1] if oldest else None

    def _delete(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            logger.exception("Failed to evict buffered file %s", path)
