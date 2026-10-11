from __future__ import annotations

import logging
import webbrowser
from collections.abc import Callable
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

StatusGetter = Callable[[], str]
QuitCallback = Callable[[], None]
UrlGetter = Callable[[], str]


def load_or_create_icon(icon_path: Path) -> Image.Image:
    if icon_path.is_file():
        return Image.open(icon_path)

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=(32, 120, 72, 255))
    draw.rectangle((20, 18, 44, 46), fill=(240, 240, 240, 255))
    draw.rectangle((26, 24, 38, 30), fill=(32, 120, 72, 255))
    return image


class TrayApp:
    def __init__(
        self,
        icon_path: Path,
        get_status: StatusGetter,
        on_quit: QuitCallback,
        get_panel_url: UrlGetter | None = None,
    ) -> None:
        self._get_status = get_status
        self._on_quit = on_quit
        self._get_panel_url = get_panel_url or (lambda: "http://127.0.0.1:8787/")
        self._status = "Starting…"
        self._icon = pystray.Icon(
            name="pc_admin_bot",
            title="HesBi Telegram Bot",
            icon=load_or_create_icon(icon_path),
            menu=pystray.Menu(
                pystray.MenuItem(
                    lambda _item: f"Status: {self._status}",
                    None,
                    enabled=False,
                ),
                pystray.MenuItem("Open status page", self._open_panel),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit, default=True),
            ),
        )

    def set_status(self, status: str) -> None:
        self._status = status
        title = f"HesBi Telegram Bot - {status}"
        try:
            self._icon.title = title
            if self._icon.visible:
                self._icon.update_menu()
        except Exception:
            logger.debug("Tray status update skipped", exc_info=True)

    def _open_panel(
        self, _icon: pystray.Icon | None = None, _item: object | None = None
    ) -> None:
        url = self._get_panel_url()
        try:
            webbrowser.open(url)
        except Exception:
            logger.exception("Failed to open status page %s", url)

    def _quit(
        self, _icon: pystray.Icon | None = None, _item: object | None = None
    ) -> None:
        logger.info("Quit requested from tray")
        # Never block the tray/UI thread here — waiting on asyncio deadlocks Quit.
        try:
            self._on_quit()
        except Exception:
            logger.exception("on_quit failed")
        try:
            self._icon.visible = False
        except Exception:
            logger.debug("Hide tray icon failed", exc_info=True)
        try:
            self._icon.stop()
        except Exception:
            logger.exception("icon.stop failed")

    def run_blocking(self) -> None:
        """Must run on the main thread on Windows."""
        self._icon.run()

    def stop(self) -> None:
        try:
            self._icon.visible = False
        except Exception:
            pass
        try:
            self._icon.stop()
        except Exception:
            logger.debug("Tray stop ignored", exc_info=True)
