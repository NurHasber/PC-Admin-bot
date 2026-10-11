from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import sys
import threading

from .bot_state import BotStateStore
from .chat_allowlist import ChatAllowlistStore
from .chat_history import ChatHistoryStore
from .chat_sessions import ChatSessionStore
from .config import ROOT_DIR, load_settings
from .panel_gate import ensure_running
from .cursor_bridge import CursorBridge
from .single_instance import notify_already_running, release, try_acquire
from .telegram_bot import TelegramBotService
from .tray_app import TrayApp


LOG_FILE = ROOT_DIR / "pc_admin_bot.log"


def _setup_logging() -> None:
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
    # pythonw has no console; attaching stdout would be useless / may be None.
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
    )


def main() -> int:
    # Windows spawn can re-enter -m src.main in a child; never start a second bot.
    if multiprocessing.current_process().name != "MainProcess":
        return 0

    if not try_acquire():
        notify_already_running()
        return 2

    _setup_logging()
    logger = logging.getLogger("pc_admin_bot")
    logger.info("Single-instance acquired pid=%s", os.getpid())
    try:
        ensure_running()
    except Exception:
        logger.exception("Panel gate did not start")

    try:
        settings = load_settings()
    except Exception as exc:
        logger.error("Config error: %s", exc)
        tray = TrayApp(
            icon_path=ROOT_DIR / "assets" / "icon.ico",
            get_status=lambda: "Config error",
            on_quit=lambda: None,
        )
        tray.set_status(f"Config error: {exc}")
        try:
            tray.run_blocking()
            return 1
        finally:
            release()

    loop = asyncio.new_event_loop()
    store = BotStateStore(settings)
    chat_sessions = ChatSessionStore()
    allowlist = ChatAllowlistStore()
    history = ChatHistoryStore()
    bridge = CursorBridge(settings, store, chat_sessions)
    status_box = {"value": "Starting…"}
    bot_holder: dict[str, TelegramBotService | None] = {"bot": None}
    stop_scheduled = threading.Event()

    def set_status(status: str) -> None:
        status_box["value"] = status
        tray.set_status(status)

    async def _shutdown_bot() -> None:
        bot = bot_holder["bot"]
        if bot is None:
            return
        try:
            await asyncio.wait_for(bot.stop(), timeout=5)
        except Exception:
            logger.exception("bot.stop failed or timed out")
        try:
            await asyncio.wait_for(bridge.stop(), timeout=5)
        except Exception:
            logger.exception("bridge.stop failed or timed out")

    def request_stop() -> None:
        """Non-blocking: schedule shutdown on the asyncio thread."""
        if stop_scheduled.is_set():
            return
        stop_scheduled.set()
        set_status("Stopping…")
        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_shutdown_bot(), loop)
            else:
                logger.info("Event loop not running; nothing to stop async")
        except Exception:
            logger.exception("Failed to schedule shutdown")

    bot = TelegramBotService(
        settings,
        bridge,
        on_status=set_status,
        allowlist=allowlist,
        history=history,
    )
    bot_holder["bot"] = bot

    tray = TrayApp(
        icon_path=ROOT_DIR / "assets" / "icon.ico",
        get_status=lambda: status_box["value"],
        on_quit=request_stop,
        get_panel_url=bot.panel_url,
    )

    def run_async() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(bot.run())
        except Exception:
            logger.exception("Bot loop crashed")
            set_status("Error")
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                logger.debug("Async cleanup ignored", exc_info=True)
            try:
                loop.close()
            except Exception:
                pass

    worker = threading.Thread(
        target=run_async, name="pc-admin-bot-async", daemon=True
    )
    worker.start()

    logger.info("PC Admin Bot starting (cwd=%s)", settings.cursor_cwd)
    try:
        tray.run_blocking()
    finally:
        request_stop()
        worker.join(timeout=8)
        if worker.is_alive():
            logger.warning("Async worker still alive after Quit; forcing exit")
        release()
        logger.info("PC Admin Bot exited")
        # Guarantee process death: hung bridge/httpx must not leave a zombie tray.
        os._exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
