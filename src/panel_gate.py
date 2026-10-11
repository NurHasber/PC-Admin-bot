"""Local page on the public panel port.

Stays up when the bot is stopped, like an offline interstitial: a launch
button, then a poll, then the live status page once the app answers.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from .config import ROOT_DIR, panel_addresses

logger = logging.getLogger(__name__)

_FAVICON = ROOT_DIR / "assets" / "icon.ico"
_LAUNCH_COOLDOWN_S = 20
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}

_OFFLINE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HesBi Telegram Bot — не запущен</title>
  <link rel="icon" href="/favicon.ico" type="image/x-icon" />
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #12141a;
      color: #e8eaef;
      font: 16px/1.45 "Segoe UI", system-ui, sans-serif;
    }
    main { text-align: center; max-width: 420px; padding: 24px; }
    img {
      width: 96px;
      height: 96px;
      border-radius: 24px;
      background: #1b1f29;
      padding: 16px;
    }
    h1 { font-size: 22px; font-weight: 600; margin: 20px 0 8px; }
    p { margin: 0; color: #9aa3b5; }
    button {
      margin-top: 22px;
      border: 0;
      border-radius: 999px;
      padding: 10px 22px;
      font: inherit;
      font-weight: 600;
      background: #e8eaef;
      color: #12141a;
      cursor: pointer;
    }
    button:disabled { opacity: 0.55; cursor: default; }
    #note { min-height: 1.45em; margin-top: 14px; font-size: 14px; }
  </style>
</head>
<body>
  <main>
    <img src="/favicon.ico" alt="" />
    <h1>Приложение не запущено</h1>
    <p>Панель откроется сама, когда бот заработает.</p>
    <button id="launch" type="button">Запустить приложение</button>
    <p id="note"></p>
  </main>
  <script>
    const btn = document.getElementById("launch");
    const note = document.getElementById("note");
    let launching = false;
    let startedAt = 0;

    async function gate() {
      const response = await fetch("/api/gate", { cache: "no-store" });
      if (!response.ok) return { online: false };
      return response.json();
    }

    async function poll() {
      try {
        const data = await gate();
        if (data.online) {
          note.textContent = "Приложение запущено. Открываю панель…";
          location.reload();
          return;
        }
      } catch (err) {}
      if (!launching) return;
      if (Date.now() - startedAt > 45000) {
        launching = false;
        btn.disabled = false;
        note.textContent = "Приложение не ответило. Если оно уже в трее, открой панель оттуда.";
        return;
      }
      setTimeout(poll, 700);
    }

    btn.addEventListener("click", async () => {
      if (launching) return;
      launching = true;
      startedAt = Date.now();
      btn.disabled = true;
      note.textContent = "Запускаю…";
      try {
        const response = await fetch("/api/launch", { method: "POST" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "не удалось запустить");
        note.textContent = data.already
          ? "Уже запускается. Жду ответ…"
          : "Жду, пока приложение поднимется…";
      } catch (err) {
        launching = false;
        btn.disabled = false;
        note.textContent = "Не удалось запустить. " + (err && err.message ? err.message : "");
        return;
      }
      poll();
    });

    setInterval(async () => {
      try {
        const data = await gate();
        if (data.online) location.reload();
      } catch (err) {}
    }, 2000);
  </script>
</body>
</html>
"""


def _no_window_flags() -> int:
    if sys.platform != "win32":
        return 0
    detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return detached | new_group | no_window


def _hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    if sys.platform != "win32":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


class PanelGate:
    def __init__(self, host: str, public_port: int, upstream_port: int) -> None:
        self.host = host
        self.public_port = public_port
        self.upstream_port = upstream_port
        self._session: ClientSession | None = None
        self._last_launch = 0.0

    def build_app(self) -> web.Application:
        app = web.Application()
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        app.router.add_get("/api/gate", self._handle_gate)
        app.router.add_post("/api/launch", self._handle_launch)
        app.router.add_get("/favicon.ico", self._handle_favicon)
        app.router.add_route("*", "/", self._handle_proxy)
        app.router.add_route("*", "/{tail:.*}", self._handle_proxy)
        return app

    async def _on_startup(self, _app: web.Application) -> None:
        self._session = ClientSession(
            timeout=ClientTimeout(total=60),
            trust_env=False,
        )

    async def _on_cleanup(self, _app: web.Application) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _upstream(self, rel_url: str) -> str:
        return f"http://{self.host}:{self.upstream_port}{rel_url}"

    async def online(self) -> bool:
        if self._session is None:
            return False
        try:
            async with self._session.get(
                self._upstream("/api/status"),
                timeout=ClientTimeout(total=0.8),
                allow_redirects=False,
            ) as response:
                return response.status == 200
        except Exception:
            return False

    async def _handle_gate(self, _request: web.Request) -> web.Response:
        body = {"online": await self.online()}
        return web.json_response(body, headers={"Cache-Control": "no-store"})

    async def _handle_launch(self, _request: web.Request) -> web.Response:
        if await self.online():
            return web.json_response({"ok": True, "already": True, "online": True})
        now = time.time()
        if now - self._last_launch < _LAUNCH_COOLDOWN_S:
            return web.json_response({"ok": True, "already": True, "online": False})
        launcher = ROOT_DIR / "scripts" / "run.vbs"
        if not launcher.is_file():
            return web.json_response(
                {"ok": False, "error": "не найден scripts\\run.vbs"},
                status=500,
            )
        self._last_launch = now
        try:
            subprocess.Popen(
                ["wscript.exe", "//nologo", str(launcher)],
                cwd=str(ROOT_DIR),
                startupinfo=_hidden_startupinfo(),
                creationflags=_no_window_flags(),
            )
        except Exception as exc:
            self._last_launch = 0.0
            logger.exception("Failed to launch bot")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        logger.info("Launch requested from offline page")
        return web.json_response({"ok": True, "already": False, "online": False})

    async def _handle_favicon(self, _request: web.Request) -> web.Response:
        if not _FAVICON.is_file():
            raise web.HTTPNotFound()
        return web.Response(body=_FAVICON.read_bytes(), content_type="image/x-icon")

    async def _handle_proxy(self, request: web.Request) -> web.StreamResponse:
        if self._session is None:
            return self._offline_or_status(request)
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _HOP_HEADERS and key.lower() != "host"
        }
        try:
            async with self._session.request(
                request.method,
                self._upstream(str(request.rel_url)),
                headers=headers,
                data=await request.read(),
                allow_redirects=False,
            ) as upstream:
                out_headers = {
                    key: value
                    for key, value in upstream.headers.items()
                    if key.lower() not in _HOP_HEADERS
                }
                return web.Response(
                    status=upstream.status,
                    body=await upstream.read(),
                    headers=out_headers,
                )
        except Exception:
            return self._offline_or_status(request)

    def _offline_or_status(self, request: web.Request) -> web.Response:
        if request.method == "GET" and request.path in {"/", ""}:
            return web.Response(
                text=_OFFLINE_HTML,
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {"online": False, "error": "app is not running"},
            status=503,
            headers={"Cache-Control": "no-store"},
        )


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def ensure_running() -> None:
    """Start the gate if nothing is answering on the public panel port."""
    host, public, upstream = panel_addresses()
    if _port_open(host, public):
        return
    pythonw = ROOT_DIR / ".venv" / "Scripts" / "pythonw.exe"
    exe = str(pythonw if pythonw.is_file() else Path(sys.executable))
    subprocess.Popen(
        [exe, "-m", "src.panel_gate"],
        cwd=str(ROOT_DIR),
        startupinfo=_hidden_startupinfo(),
        creationflags=_no_window_flags(),
    )
    logger.info("Started panel gate on %s:%s (upstream %s)", host, public, upstream)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(ROOT_DIR / "pc_admin_bot.log", encoding="utf-8")],
    )
    host, public, upstream = panel_addresses()
    gate = PanelGate(host, public, upstream)
    logger.info(
        "Panel gate listening on http://%s:%s/ (upstream %s)",
        host,
        public,
        upstream,
    )
    try:
        web.run_app(
            gate.build_app(),
            host=host,
            port=public,
            print=None,
            access_log=None,
        )
    except OSError:
        logger.info("Panel gate port %s already in use; exiting", public)


if __name__ == "__main__":
    main()
