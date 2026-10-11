# PC Admin Bot

Tray app that listens to your Telegram messages and forwards them to a local Cursor Agent (`cursor-sdk`).

## Requirements

- Windows
- Python 3.11+ (`python` and `pythonw` on PATH)
- Cursor account API key from [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations)
- Telegram bot token + your numeric Telegram user id in `env.env`

## Config (`env.env`)

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_USER_ID=...
CURSOR_API_KEY=...
CURSOR_MODEL=composer-2.5
CURSOR_CWD=F:\Yandex.Disk\Work\Project PC admin bot
CONFIRM_PASSWORD=your-secret
```

`CURSOR_MODEL` and `CURSOR_CWD` are optional (defaults: `composer-2.5` and this project folder).

Only your `TELEGRAM_ADMIN_USER_ID` is treated as **admin**. In private chat everyone else gets `access denied`. In groups/channels non-admin messages are ignored completely.

### Groups / channels

- **Join control:** the Bot API cannot join via invite links. Paste an invite link (`t.me/+…` / `joinchat/…`) in DM — the bot arms a ~30 min wait and tells you to open the link and **add it yourself**. It stays only if **you** added it (`my_chat_member`); if anyone else adds it, the bot leaves immediately. Allowed chats are stored in `chat_allowlist.json`.
- Trigger (admin only): **@mention** the bot, or **Reply** to one of the bot’s messages (no `@` needed). A Reply carries the message id; the bot looks that message up and passes its text to the agent.
- Each Telegram chat has its own Cursor agent session (`chat_sessions.json`, key `chat:{chat_id}`) — context does not leak across chats or into private topics.
- **Чат-агент** (DM reply keyboard): pick a known group/channel agent and talk to that same session from private chat (replies stay in DM). **Личные темы** switches DM back to private topics. Topics buttons work only in private-topic mode.
- **Чаты**: list known memberships (allowlist + sessions; Bot API has no global “all chats” call). Per chat: **Выйти** (confirm → `leave_chat` + revoke allowlist) or **Убрать** if the bot is no longer a member.
- Shared-chat prompts tell the agent that everyone can see the reply (no dumping secrets).
- Suspicious / secret-looking requests from a group: bot posts a short notice, then asks for `CONFIRM_PASSWORD` in **DM**. After the password, you choose **Только в личку** or **В общий чат**.
- **Rights:** in groups, admin is **not** required for `@mention` replies if members/bots may send messages (default privacy mode sees mentions). In **channels**, admin with Post messages is required. On join / rights change the bot DMs a short rights check. If it cannot post, it stays silent in that chat (DM still works after `/start`).
- **Group Privacy:** for full message logging set BotFather `/setprivacy` → **Disable**, then remove and re-add the bot. With privacy ON the bot often only receives `@mention` / commands / replies.
- **Chat history DB:** allowlisted groups/channels write every received message to local SQLite `chat_history.sqlite` (who / when / text / formatting). Files and photos are downloaded into `file_buffer/` (cap `FILE_BUFFER_MAX_MB`, default 512) so a later prompt can ask the agent to work with them. Telegram itself will not give the bot a file larger than 20 MB. When the buffer is full, the oldest files are removed. A reply or a written message id always loads that one stored message (and the message it replies to). A search loads every matching message, and a request for the whole chat loads every stored message — but only after the bot asks whether to take all of them or a date range, and the user confirms. Ordinary questions do not attach the log. Telegram does not provide backlog before the bot joined.
- **Files back to chat:** if the request needs a document or image, the agent saves it under `agent_outbox/` (Word via `python-docx`, png/jpg, pdf, xlsx, …). The bot sends new files with the reply: images as photos, other files as documents. A file you attach to the request (up to 20 MB) is saved locally and the agent can read that path. A Google Drive file is readable when that folder is synced on this PC, or when you attach the file.

### Password gate (`CONFIRM_PASSWORD`)

Suspicious / harmful-looking prompts require this password. After success, elevated access lasts **10 minutes** (`CONFIRM_ELEVATION_MINUTES`) — dangerous prompts are not challenged again in that window. **Changing or creating a topic clears elevation.** The password message is deleted from the chat after the bot reads it. 3 wrong attempts → lock until tray restart. `/cancel` aborts the challenge. Empty `CONFIRM_PASSWORD` disables the gate.

In Telegram private chat, use the reply keyboard:

- **Модель** — pick from Cursor `ListModels` (stored in `bot_state.json`, applied per message).
- **Темы** / **Новая тема** — each private topic is a separate SDK agent session (`agent_id`) so context does not mix in one thread.
- **Чат-агент** / **Чаты** — switch DM to a group agent, or list/leave memberships.

## Setup

```bat
cd /d "F:\Yandex.Disk\Work\Project PC admin bot"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

Silent tray launch (recommended — no CMD window):

- double-click `scripts\run.vbs`
- or `scripts\run.bat` (calls the VBS)

Visible console (for debugging):

```bat
.venv\Scripts\python.exe -m src.main
```

You should see **PC Admin Bot** in the system tray and `pythonw.exe` / `python.exe` in Task Manager. Use tray **Quit** or End Task anytime.

## Autostart with Windows

Preferred (normal `.lnk` shortcut, not a script dropped into Startup):

```bat
scripts\install_autostart.bat
```

or:

```bat
.venv\Scripts\python.exe -m src.autostart install
```

Creates `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\PC Admin Bot.lnk` → `scripts\run.vbs`.

If Kaspersky deletes the installer bat, use the **manual** method (safest):

```bat
.venv\Scripts\python.exe -m src.autostart manual
```

Then in the opened Startup folder create a shortcut to `scripts\run.vbs`.

Remove with:

```bat
scripts\uninstall_autostart.bat
```

## Kaspersky / antivirus notes

- This app is **not** packed into a single `.exe` (PyInstaller-style builds often trigger false positives).
- Process name stays `pythonw.exe` — transparent and easy to kill.
- Network use is outbound HTTPS to Telegram and Cursor. No connectivity scanners / “internet monitor” behavior.
- `cursor-sdk` may spawn a local `cursor-sdk-bridge` helper (localhost only) — that is normal for the Cursor SDK, not a custom hidden service.
- On Windows the app launches the bridge via bundled `node.exe` with `CREATE_NO_WINDOW` so a CMD window does not flash.
- Do **not** drop generated `.bat`/`.vbs` into Startup — that pattern looks like malware. Use a `.lnk` shortcut (installer above) or create the shortcut manually.
- If Kaspersky quarantines `install_autostart.bat`, restore/exclude the project folder or use `python -m src.autostart manual`.
- On first run, if Kaspersky asks about network access for `python.exe` / `pythonw.exe` / `node.exe`, allow it.
- Cursor bridge uses bundled `node.exe`, which does **not** trust the Windows/Kaspersky certificate store by itself. On startup the app exports Windows root CAs to `.certs/windows-roots.pem` and sets `NODE_EXTRA_CA_CERTS`. If you still see `internal: Network request failed`, either allow `node.exe` through Kaspersky HTTPS inspection or set `CURSOR_BRIDGE_INSECURE_TLS=1` in `env.env` (disables Node TLS verify — less safe).
- If Telegram works but the agent replies with auth errors, refresh `CURSOR_API_KEY` on [API keys](https://cursor.com/dashboard/api).

## Local status page

**http://127.0.0.1:8787/** stays on localhost even when the bot is stopped. That page is an offline screen with **Запустить приложение**: it starts the tray app, waits until the panel answers, then reloads into the live status page. Tray menu → **Open status page**. The live page shows bot/admin names (click to copy), tray status, Cursor bridge, safety gate, token usage, per-chat activity, and logged messages with filters.

Port **8765** is used by another local app (Project 5) — this bot defaults to **8787**. Override with `STATUS_PANEL_HOST` / `STATUS_PANEL_PORT` in `env.env` if needed.

## Logs

`pc_admin_bot.log` in the project root.
