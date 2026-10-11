from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / "env.env"


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_admin_user_id: int
    cursor_api_key: str
    cursor_model: str
    cursor_cwd: Path
    confirm_password: str
    confirm_elevation_seconds: int
    admin_nick: str = "admin"
    # Localhost status panel (Project 5 uses 8765 — do not collide).
    status_panel_host: str = "127.0.0.1"
    status_panel_port: int = 8787
    # Bot panel binds here. The public port is held by the offline gate.
    status_panel_upstream_port: int = 8788
    # On-demand history lookup caps (not attached to every prompt).
    history_context_messages: int = 120
    history_context_max_chars: int = 24000
    file_buffer_max_bytes: int = 512 * 1024 * 1024


def panel_addresses() -> tuple[str, int, int]:
    """Public host, public port, and the port the live panel binds.

    Does not read bot secrets. The public port stays open when the app is
    stopped so the offline page can launch it.
    """
    host = "127.0.0.1"
    public = 8787
    if ENV_FILE.is_file():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key == "STATUS_PANEL_HOST" and value:
                host = value
            elif key == "STATUS_PANEL_PORT" and value:
                public = int(value)
    if not (1 <= public <= 65535):
        public = 8787
    upstream = public + 1 if public < 65535 else public - 1
    return host, public, upstream


def load_settings() -> Settings:
    if not ENV_FILE.is_file():
        raise FileNotFoundError(f"Missing config file: {ENV_FILE}")

    load_dotenv(ENV_FILE, override=True)

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    admin_raw = (os.getenv("TELEGRAM_ADMIN_USER_ID") or "").strip()
    api_key = (os.getenv("CURSOR_API_KEY") or "").strip()
    model = (os.getenv("CURSOR_MODEL") or "composer-2.5").strip()
    cwd_raw = (os.getenv("CURSOR_CWD") or str(ROOT_DIR)).strip()
    confirm_password = (os.getenv("CONFIRM_PASSWORD") or "").strip()
    elev_raw = (os.getenv("CONFIRM_ELEVATION_MINUTES") or "10").strip()
    try:
        elev_minutes = max(1, int(elev_raw))
    except ValueError as exc:
        raise ValueError("CONFIRM_ELEVATION_MINUTES must be an integer") from exc

    panel_host = (os.getenv("STATUS_PANEL_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    panel_port_raw = (os.getenv("STATUS_PANEL_PORT") or "8787").strip()
    try:
        panel_port = int(panel_port_raw)
    except ValueError as exc:
        raise ValueError("STATUS_PANEL_PORT must be an integer") from exc
    if not (1 <= panel_port <= 65535):
        raise ValueError("STATUS_PANEL_PORT out of range")
    upstream_port = panel_port + 1 if panel_port < 65535 else panel_port - 1

    hist_limit_raw = (os.getenv("HISTORY_CONTEXT_MESSAGES") or "120").strip()
    hist_chars_raw = (os.getenv("HISTORY_CONTEXT_MAX_CHARS") or "24000").strip()
    try:
        history_context_messages = max(0, min(500, int(hist_limit_raw)))
    except ValueError as exc:
        raise ValueError("HISTORY_CONTEXT_MESSAGES must be an integer") from exc
    try:
        history_context_max_chars = max(0, min(200_000, int(hist_chars_raw)))
    except ValueError as exc:
        raise ValueError("HISTORY_CONTEXT_MAX_CHARS must be an integer") from exc

    buffer_mb_raw = (os.getenv("FILE_BUFFER_MAX_MB") or "512").strip()
    try:
        file_buffer_max_mb = max(1, min(100_000, int(buffer_mb_raw)))
    except ValueError as exc:
        raise ValueError("FILE_BUFFER_MAX_MB must be an integer") from exc

    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("TELEGRAM_ADMIN_USER_ID", admin_raw),
            ("CURSOR_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Missing required keys in env.env: {', '.join(missing)}")

    try:
        admin_id = int(admin_raw)
    except ValueError as exc:
        raise ValueError("TELEGRAM_ADMIN_USER_ID must be an integer") from exc

    cwd = Path(cwd_raw).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"CURSOR_CWD is not a directory: {cwd}")

    return Settings(
        telegram_bot_token=token,
        telegram_admin_user_id=admin_id,
        cursor_api_key=api_key,
        cursor_model=model,
        cursor_cwd=cwd,
        confirm_password=confirm_password,
        confirm_elevation_seconds=elev_minutes * 60,
        status_panel_host=panel_host,
        status_panel_port=panel_port,
        status_panel_upstream_port=upstream_port,
        history_context_messages=history_context_messages,
        history_context_max_chars=history_context_max_chars,
        file_buffer_max_bytes=file_buffer_max_mb * 1024 * 1024,
    )
