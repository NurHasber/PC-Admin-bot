from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def resolve_silent_bridge_command() -> list[str]:
    """Prefer node.exe + bridge.js over cursor-sdk-bridge.cmd (avoids cmd.exe)."""
    from cursor_sdk._vendor import resolve_bridge_path

    launcher = Path(resolve_bridge_path())
    if launcher.suffix.lower() == ".cmd":
        node = launcher.parent / "node.exe"
        script = launcher.parent.parent / "dist" / "bin" / "cursor-sdk-bridge.js"
        if node.is_file() and script.is_file():
            return [str(node), str(script)]
    return [str(launcher)]


@asynccontextmanager
async def silent_asyncio_subprocesses() -> AsyncIterator[None]:
    """On Windows, hide consoles for asyncio child processes (e.g. node bridge)."""
    if sys.platform != "win32":
        yield
        return

    original = asyncio.create_subprocess_exec

    async def _wrapped(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        flags = int(kwargs.get("creationflags", 0) or 0)  # type: ignore[arg-type]
        kwargs["creationflags"] = flags | _CREATE_NO_WINDOW
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    asyncio.create_subprocess_exec = _wrapped  # type: ignore[assignment]
    try:
        yield
    finally:
        asyncio.create_subprocess_exec = original  # type: ignore[assignment]
