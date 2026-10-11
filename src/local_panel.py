from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from aiohttp import web

from .config import ROOT_DIR
from .file_buffer import BUFFER_DIR

logger = logging.getLogger(__name__)

_FAVICON_PATH = ROOT_DIR / "assets" / "icon.ico"

# Project 5 panel uses 8765 — keep this bot on a separate localhost port.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

SnapshotFn = Callable[[], dict[str, Any]]
HistoryFn = Callable[..., dict[str, Any]]


_INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HesBi Telegram Bot — status</title>
  <link rel="icon" href="/favicon.ico" type="image/x-icon" />
  <style>
    :root {
      --bg: #12141a;
      --card: #1b1f29;
      --text: #e8eaef;
      --muted: #9aa3b5;
      --accent: #6cb6ff;
      --ok: #6bcf8e;
      --warn: #e0b35a;
      --bad: #e06c75;
      --line: #2a3140;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 28px 32px 64px;
    }
    h1 { font-size: 1.35rem; font-weight: 650; margin: 0; letter-spacing: -0.02em; }
    h2 { font-size: 0.95rem; margin: 36px 0 0; color: var(--muted); font-weight: 600; }
    p { margin: 0; }
    .sub { color: var(--muted); margin: 8px 0 20px; }
    .lead {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      margin: 8px 0 14px;
      max-width: 72ch;
    }
    .lead a {
      color: var(--accent);
      text-underline-offset: 2px;
    }
    .lead a:hover { color: #9dceff; }
    .grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
    .grid + .grid,
    .grid + .card,
    h2 + .grid,
    h2 + .card { margin-top: 12px; }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px 16px;
    }
    .label { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
    .value {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 15px;
      line-height: 1.4;
      word-break: break-word;
    }
    .ok { color: var(--ok); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    table { width: 100%; border-collapse: collapse; }
    th, td {
      text-align: left;
      padding: 10px 16px 10px 0;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th:last-child, td:last-child { padding-right: 0; }
    tr:last-child td { border-bottom: 0; }
    th { color: var(--muted); font-weight: 600; font-size: 12px; padding-top: 0; }
    td .muted { display: block; margin-top: 4px; }
    code { color: var(--accent); font-size: 12px; }
    .mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
    .muted { color: var(--muted); }
    .pill {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      background: #243044;
      font-size: 12px;
    }
    .copy-row { display: flex; flex-wrap: wrap; gap: 8px; }
    .copy-btn {
      background: #12141a;
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 6px 10px;
      cursor: pointer;
      font: inherit;
      text-align: left;
    }
    .copy-btn:hover { border-color: var(--accent); color: var(--accent); }
    .hint { color: var(--muted); font-size: 12px; line-height: 1.4; margin-top: 10px; }
    .value .hint { margin-top: 0; }
    .filters {
      display: flex;
      flex-wrap: wrap;
      gap: 12px 14px;
      align-items: end;
      margin: 0 0 16px;
    }
    .filters label {
      display: flex;
      flex-direction: column;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
    }
    .filters input, .filters select, .filters button {
      background: #12141a;
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 6px 8px;
      font: inherit;
    }
    .filters button { cursor: pointer; }
    .filters button:hover { border-color: var(--accent); }
    .pager { display: flex; gap: 10px; align-items: center; margin-top: 16px; flex-wrap: wrap; }
    .pager button {
      background: #12141a;
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 6px 10px;
      cursor: pointer;
      font: inherit;
    }
    .pager button:disabled { opacity: 0.4; cursor: default; }
    .bar {
      height: 8px;
      background: #2a3140;
      border-radius: 99px;
      overflow: hidden;
      margin: 2px 0;
    }
    .bar > span { display: block; height: 100%; background: var(--accent); }
    .bar.warn > span { background: var(--warn); }
    .bar.bad > span { background: var(--bad); }
    #toast {
      position: fixed;
      bottom: 20px;
      left: 50%;
      transform: translateX(-50%);
      background: #243044;
      border: 1px solid var(--accent);
      color: var(--text);
      padding: 10px 14px;
      border-radius: 10px;
      opacity: 0;
      pointer-events: none;
      transition: opacity .15s;
      max-width: min(90vw, 520px);
    }
    #toast.show { opacity: 1; }
  </style>
</head>
<body>
  <h1>HesBi Telegram Bot</h1>
  <div class="sub">Локальная панель · <span id="url"></span> · автообновление 3с</div>
  <div class="grid" id="identity"></div>
  <div class="grid" id="cards"></div>
  <h2>Расход токенов</h2>
  <div class="grid" id="usage"></div>
  <div class="card" id="models"></div>
  <h2>Скорость ответа</h2>
  <p class="lead">От начала обработки запроса до момента, когда Telegram принял сообщение. Не до окончания работы агента.</p>
  <div class="grid" id="speed"></div>
  <h2>Буфер файлов</h2>
  <p class="lead">Файлы из чатов скачиваются <a href="#" id="open-folder">сюда</a>, чтобы агент мог их открыть. При переполнении удаляются самые старые.</p>
  <div class="grid" id="buffer"></div>
  <h2>Чаты</h2>
  <div class="card"><div id="chats" class="muted">Загрузка…</div></div>
  <h2>Последние сообщения (история)</h2>
  <div class="card">
    <form class="filters" id="filters">
      <label>Чат
        <select id="f-chat"><option value="">Все</option></select>
      </label>
      <label>Отправитель
        <select id="f-sender"><option value="">Все</option></select>
      </label>
      <label>Дата от
        <input id="f-from" type="date" />
      </label>
      <label>Дата до
        <input id="f-to" type="date" />
      </label>
      <label>Текст
        <input id="f-q" type="search" placeholder="поиск" />
      </label>
      <label>На странице
        <select id="f-size">
          <option value="25">25</option>
          <option value="50">50</option>
          <option value="100">100</option>
        </select>
      </label>
      <button type="button" id="f-reset">Сбросить</button>
    </form>
    <div id="recent" class="muted">Загрузка…</div>
    <div class="pager" id="pager"></div>
  </div>
  <div id="toast" role="status"></div>
  <script>
    const $ = (id) => document.getElementById(id);
    const histState = { page: 1 };
    let toastTimer = 0;
    let qTimer = 0;
    let histBusy = false;

    function esc(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"
      }[c]));
    }
    function num(n) {
      return Number(n || 0).toLocaleString("ru-RU");
    }
    function setHtml(el, html) {
      if (el.dataset.html === html) return;
      el.dataset.html = html;
      el.innerHTML = html;
    }
    function card(label, value, cls="") {
      return `<div class="card"><div class="label">${esc(label)}</div>
        <div class="value ${cls}">${value}</div></div>`;
    }
    function showToast(text) {
      const el = $("toast");
      el.textContent = text;
      el.classList.add("show");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => el.classList.remove("show"), 1600);
    }
    async function copyText(text) {
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      showToast("Скопировано: " + text);
    }
    document.body.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-copy]");
      if (!btn) return;
      e.preventDefault();
      copyText(btn.getAttribute("data-copy") || "");
    });
    $("open-folder").addEventListener("click", async (e) => {
      e.preventDefault();
      try {
        const response = await fetch("/api/open-folder", { method: "POST" });
        if (!response.ok) throw new Error("open failed");
        showToast("Папка открыта в проводнике");
      } catch (err) {
        showToast("Не удалось открыть папку");
      }
    });
    function chip(value) {
      if (!value) return "";
      return `<button type="button" class="copy-btn" data-copy="${esc(value)}">${esc(value)}</button>`;
    }
    function uptime(sec) {
      const s = Math.max(0, Number(sec) || 0);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      if (h > 0) return h + " ч " + m + " мин";
      return m + " мин";
    }
    function bytes(n) {
      const v = Number(n) || 0;
      if (v < 1024) return Math.round(v) + " Б";
      if (v < 1048576) return (v / 1024).toFixed(1) + " КБ";
      if (v < 1073741824) return (v / 1048576).toFixed(1) + " МБ";
      return (v / 1073741824).toFixed(2) + " ГБ";
    }
    function dur(ms) {
      if (ms == null || ms === "") return "—";
      const n = Number(ms);
      if (!Number.isFinite(n)) return "—";
      if (n < 1000) return Math.round(n) + " мс";
      const s = n / 1000;
      if (s < 60) return (s < 10 ? s.toFixed(1) : String(Math.round(s))) + " с";
      const m = Math.floor(s / 60);
      const rem = Math.round(s % 60);
      return m + " мин " + rem + " с";
    }
    function speedCard(title, block) {
      const b = block || {};
      if (!b.count) {
        return card(title, "<span class='muted'>пока нет отправленных ответов</span>");
      }
      return card(title, `
        <div>среднее ${dur(b.avg_ms)}</div>
        <div class="muted">мин ${dur(b.min_ms)} · макс ${dur(b.max_ms)}</div>
        <div class="muted">ответов ${num(b.count)}</div>
      `);
    }
    function tokenCards(title, block) {
      const b = block || {};
      const note = (b.runs && !b.known_runs)
        ? `<div class="hint">запросов ${num(b.runs)}, токены от Cursor не пришли</div>`
        : "";
      return card(title, `
        <div>всего ${num(b.total_tokens)}</div>
        <div class="muted">in ${num(b.input_tokens)} · out ${num(b.output_tokens)}</div>
        <div class="muted">cache read ${num(b.cache_read_tokens)} · write ${num(b.cache_write_tokens)}</div>
        <div class="muted">запросов ${num(b.runs)}${b.reasoning_tokens ? " · reasoning " + num(b.reasoning_tokens) : ""}</div>
        ${note}
      `);
    }

    async function refresh() {
      try {
        const r = await fetch("/api/status", { cache: "no-store" });
        const d = await r.json();
        $("url").textContent = d.panel_url || "";
        const botName = d.bot_display_name || "";
        const botUser = d.bot_username ? "@" + d.bot_username : "";
        const adminName = d.admin_display_name || "";
        const adminUser = d.admin_username ? "@" + d.admin_username : "";
        setHtml($("identity"), [
          `<div class="card"><div class="label">Бот</div>
            <div class="copy-row">${chip(botName)}${chip(botUser)}${chip(d.bot_id ? String(d.bot_id) : "")}</div>
            <div class="hint">нажмите, чтобы скопировать</div></div>`,
          `<div class="card"><div class="label">Админ</div>
            <div class="copy-row">${chip(adminName)}${chip(adminUser)}${chip(d.admin_user_id ? String(d.admin_user_id) : "")}</div>
            <div class="hint">только этот пользователь пишет боту в личке</div></div>`,
        ].join(""));

        const bridgeCls = d.bridge_ready ? "ok" : "bad";
        const gateCls = d.gate_locked ? "bad" : (d.gate_elevated ? "warn" : "");
        const usage = d.usage || {};
        setHtml($("cards"), [
          card("Tray / global status", esc(d.global_status)),
          card("Model", esc(d.model)),
          card("DM session", esc(d.dm_session_label)),
          card("Cursor bridge", d.bridge_ready ? "ready" : "down", bridgeCls),
          card("Bridge last error", esc(d.bridge_last_error || "—"), d.bridge_last_error ? "bad" : "muted"),
          card("Safety gate", esc(d.gate_label), gateCls),
          card("Uptime", esc(uptime(d.uptime_seconds))),
          card("History messages", esc(d.history_total)),
          card("Сообщений сегодня", esc(usage.messages_today ?? 0)),
          card("Отправителей", esc(usage.senders_total ?? 0)),
          card("Allowlisted chats", esc((d.chats || []).length)),
        ].join(""));

        const last = usage.last_run;
        let lastHtml = "<span class='muted'>ещё не было запросов к агенту</span>";
        if (last) {
          const dur = last.duration_ms >= 1000
            ? (last.duration_ms / 1000).toFixed(1) + " с"
            : (last.duration_ms || 0) + " мс";
          lastHtml = `<div>${esc(last.at)} · ${esc(last.model || "—")} · ${esc(last.status || "—")}</div>
            <div class="muted">токены ${num(last.total_tokens)} (in ${num(last.input_tokens)} / out ${num(last.output_tokens)}) · ${esc(dur)}</div>
            ${last.usage_known ? "" : "<div class='hint'>usage не пришёл</div>"}`;
        }
        setHtml($("usage"), [
          tokenCards("Сегодня", usage.tokens_today),
          tokenCards("Всего", usage.tokens_all),
          card("Последний запрос", lastHtml),
        ].join(""));

        const speed = usage.reply_speed || {};
        const lastSpeed = speed.last;
        let lastSpeedHtml = "<span class='muted'>ещё не было отправленного ответа</span>";
        if (lastSpeed) {
          lastSpeedHtml = `${dur(lastSpeed.response_ms)}<div class="muted">${esc(lastSpeed.at || "")}</div>`;
        }
        setHtml($("speed"), [
          card("Последний", lastSpeedHtml),
          speedCard("Сегодня", speed.today),
          speedCard("Всего", speed.all),
        ].join(""));

        const buf = d.file_buffer || {};
        const pct = Number(buf.percent || 0);
        const barCls = pct >= 95 ? "bad" : (pct >= 80 ? "warn" : "");
        setHtml($("buffer"), [
          card("Заполнено", `
            <div>${bytes(buf.used_bytes)} из ${bytes(buf.limit_bytes)} · ${pct}%</div>
            <div class="bar ${barCls}"><span style="width:${Math.min(pct, 100)}%"></span></div>
            <div class="muted">файлов ${num(buf.files)}</div>
          `),
          card("Не влезло", `
            <div>слишком большие ${num(buf.skipped_too_large)}</div>
            <div class="muted">нет места ${num(buf.skipped_no_room)} · вытеснено ${num(buf.evicted_files)}</div>
          `),
        ].join(""));

        const models = usage.by_model || [];
        if (!models.length) {
          setHtml($("models"), "<span class='muted'>Разбивка по моделям появится после первого ответа агента.</span>");
        } else {
          setHtml($("models"), `<table><thead><tr>
            <th>Модель</th><th>Запросы</th><th>Всего</th><th>In</th><th>Out</th>
            </tr></thead><tbody>` + models.map((m) => `<tr>
              <td class="mono">${esc(m.model)}</td>
              <td>${num(m.runs)}</td>
              <td>${num(m.total_tokens)}</td>
              <td>${num(m.input_tokens)}</td>
              <td>${num(m.output_tokens)}</td>
            </tr>`).join("") + "</tbody></table>");
        }

        const chats = d.chats || [];
        if (!chats.length) {
          setHtml($("chats"), "<span class='muted'>Нет allowlisted чатов</span>");
        } else {
          setHtml($("chats"), `<table><thead><tr>
            <th>Чат</th><th>id</th><th>agent</th><th>history</th><th>activity</th>
            </tr></thead><tbody>` + chats.map((c) => `<tr>
              <td>${esc(c.title)}</td>
              <td class="mono">${esc(c.chat_id)}</td>
              <td class="mono">${esc(c.agent_id || "—")}</td>
              <td>${esc(c.history_count)}</td>
              <td><span class="pill">${esc(c.activity || "idle")}</span>
                <div class="muted mono">${esc(c.last_detail || "")}</div></td>
            </tr>`).join("") + "</tbody></table>");
        }
      } catch (e) {
        $("cards").innerHTML = card("Error", esc(e), "bad");
      }
    }

    function fillSelect(el, options, placeholder) {
      const cur = el.value;
      const sig = options.map((o) => o.value + "\\0" + o.label).join("\\n");
      if (el.dataset.sig === sig) return false;
      el.dataset.sig = sig;
      el.innerHTML = `<option value="">${esc(placeholder)}</option>` + options.map((o) =>
        `<option value="${esc(o.value)}">${esc(o.label)}</option>`
      ).join("");
      const keep = [...el.options].some((o) => o.value === cur);
      el.value = keep ? cur : "";
      return el.value !== cur;
    }

    function histQuery() {
      const p = new URLSearchParams();
      p.set("page", String(histState.page));
      p.set("page_size", $("f-size").value || "25");
      const chat = $("f-chat").value;
      const sender = $("f-sender").value;
      const from = $("f-from").value;
      const to = $("f-to").value;
      const q = $("f-q").value.trim();
      if (chat) p.set("chat_id", chat);
      if (sender) p.set("sender", sender);
      if (from) p.set("date_from", from);
      if (to) p.set("date_to", to);
      if (q) p.set("q", q);
      return p;
    }

    async function refreshHistory(opts) {
      if (histBusy) return;
      if (opts && opts.resetPage) histState.page = 1;
      histBusy = true;
      try {
        const r = await fetch("/api/history?" + histQuery().toString(), { cache: "no-store" });
        const d = await r.json();
        const facets = d.facets || {};
        const chatDropped = fillSelect(
          $("f-chat"),
          (facets.chats || []).map((c) => ({
            value: String(c.chat_id),
            label: c.title + " (" + c.count + ")",
          })),
          "Все"
        );
        const senderDropped = fillSelect(
          $("f-sender"),
          (facets.senders || []).map((s) => ({
            value: s.key,
            label: s.label + " (" + s.count + ")",
          })),
          "Все"
        );
        if (chatDropped || senderDropped) {
          histBusy = false;
          histState.page = 1;
          return refreshHistory();
        }
        const items = d.items || [];
        if (!d.total) {
          setHtml($("recent"), "<span class='muted'>Нет сообщений по этому фильтру</span>");
        } else {
          setHtml($("recent"), `<table><thead><tr>
            <th>when</th><th>chat</th><th>from</th><th>type</th><th>text</th>
            </tr></thead><tbody>` + items.map((m) => `<tr>
              <td class="mono">${esc(m.date_iso)}</td>
              <td>${esc(m.chat_title || m.chat_id)}</td>
              <td>${esc(m.from_display_name || m.from_username || m.from_user_id || "?")}</td>
              <td>${esc(m.content_type)}${m.has_media ? " 📎" : ""}${m.is_edited ? " ✎" : ""}</td>
              <td class="mono">${esc(m.body || "")}</td>
            </tr>`).join("") + "</tbody></table>");
        }
        const page = d.page || 1;
        const pages = d.pages || 1;
        setHtml($("pager"), `
          <button type="button" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>Назад</button>
          <span class="muted">стр. ${page} из ${pages} · ${num(d.total)} сообщений</span>
          <button type="button" data-page="${page + 1}" ${page >= pages ? "disabled" : ""}>Дальше</button>`);
      } catch (e) {
        $("recent").innerHTML = `<span class="bad">${esc(e)}</span>`;
      } finally {
        histBusy = false;
      }
    }

    $("pager").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-page]");
      if (!btn || btn.disabled) return;
      histState.page = Number(btn.getAttribute("data-page"));
      refreshHistory();
    });
    ["f-chat", "f-sender", "f-from", "f-to", "f-size"].forEach((id) => {
      $(id).addEventListener("change", () => refreshHistory({ resetPage: true }));
    });
    $("f-q").addEventListener("input", () => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => refreshHistory({ resetPage: true }), 300);
    });
    $("f-reset").addEventListener("click", () => {
      $("f-chat").value = "";
      $("f-sender").value = "";
      $("f-from").value = "";
      $("f-to").value = "";
      $("f-q").value = "";
      $("f-size").value = "25";
      refreshHistory({ resetPage: true });
    });

    refresh();
    refreshHistory();
    setInterval(() => { refresh(); refreshHistory(); }, 3000);
  </script>
</body>
</html>
"""


class LocalStatusPanel:
    """Tiny localhost-only status UI (127.0.0.1)."""

    def __init__(
        self,
        get_snapshot: SnapshotFn,
        query_history: HistoryFn,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        public_port: int | None = None,
    ) -> None:
        self._get_snapshot = get_snapshot
        self._query_history = query_history
        self._host = host
        self._port = port
        self._public_port = public_port or port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._public_port}/"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/favicon.ico", self._handle_favicon)
        app.router.add_get("/api/status", self._handle_api)
        app.router.add_get("/api/history", self._handle_history)
        app.router.add_post("/api/open-folder", self._handle_open_folder)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            host=self._host,
            port=self._port,
            reuse_address=True,
        )
        await self._site.start()
        logger.info(
            "Local status panel public %s (bind %s:%s)",
            self.url,
            self._host,
            self._port,
        )

    async def stop(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                logger.debug("panel site stop ignored", exc_info=True)
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.debug("panel runner cleanup ignored", exc_info=True)
            self._runner = None

    async def _handle_index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _handle_favicon(self, _request: web.Request) -> web.Response:
        if not _FAVICON_PATH.is_file():
            raise web.HTTPNotFound()
        return web.Response(
            body=_FAVICON_PATH.read_bytes(),
            content_type="image/x-icon",
        )

    async def _handle_api(self, _request: web.Request) -> web.Response:
        try:
            payload = self._get_snapshot()
            payload["panel_url"] = self.url
            return web.json_response(payload)
        except Exception as exc:
            logger.exception("status snapshot failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_open_folder(self, _request: web.Request) -> web.Response:
        folder = BUFFER_DIR.resolve()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)  # Windows Explorer
        except OSError as exc:
            logger.exception("open file buffer folder failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        return web.json_response({"ok": True})

    async def _handle_history(self, request: web.Request) -> web.Response:
        q = request.query
        try:
            page = int(q.get("page") or "1")
            page_size = int(q.get("page_size") or "25")
            chat_raw = (q.get("chat_id") or "").strip()
            chat_id = int(chat_raw) if chat_raw else None
        except ValueError:
            return web.json_response({"error": "bad query"}, status=400)
        try:
            payload = self._query_history(
                chat_id=chat_id,
                sender=(q.get("sender") or "").strip() or None,
                date_from=(q.get("date_from") or "").strip() or None,
                date_to=(q.get("date_to") or "").strip() or None,
                text_query=(q.get("q") or "").strip() or None,
                page=page,
                page_size=page_size,
            )
            return web.json_response(payload)
        except Exception as exc:
            logger.exception("history query failed")
            return web.json_response({"error": str(exc)}, status=500)
