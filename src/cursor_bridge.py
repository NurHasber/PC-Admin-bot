from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cursor_sdk import (
    AgentBusyError,
    AgentOptions,
    AsyncClient,
    CursorAgentError,
    LocalAgentOptions,
    NetworkError,
    SendOptions,
)

from .bot_state import BotStateStore, TopicRecord
from .chat_sessions import ChatSession, ChatSessionStore, private_key
from .config import Settings
from .node_tls import prepare_node_tls_env
from .outbox import collect_new_deliverables, deliverable_instructions, snapshot_outbox
from .win_silent import resolve_silent_bridge_command, silent_asyncio_subprocesses

logger = logging.getLogger(__name__)


def _assistant_text_from_message(message: Any) -> str:
    """Best-effort extract of assistant text from an SDK stream message."""
    msg_type = getattr(message, "type", None)
    nested = getattr(message, "message", None)
    nested_type = getattr(nested, "type", None)
    if msg_type != "assistant" and nested_type != "assistant":
        return ""
    payload = nested if nested is not None else message
    content = getattr(payload, "content", None) or []
    chunks: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "") or ""
            if text:
                chunks.append(text)
    return "".join(chunks)


def _is_bridge_dead(exc: BaseException) -> bool:
    if isinstance(exc, NetworkError):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "connecterror",
            "all connection attempts failed",
            "bridge request failed",
            "connection refused",
            "actively refused",
        )
    )


def _is_agent_busy(exc: BaseException) -> bool:
    if isinstance(exc, AgentBusyError):
        return True
    text = str(exc).lower()
    return "already has active run" in text or "agent busy" in text


class CursorBridge:
    """Local Cursor SDK client with per-topic / per-chat agent sessions."""

    def __init__(
        self,
        settings: Settings,
        store: BotStateStore,
        chat_sessions: ChatSessionStore | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._chats = chat_sessions or ChatSessionStore()
        self._client: Any | None = None
        self._agent: Any | None = None
        self._usage_hook: Callable[[dict[str, Any]], None] | None = None
        self._active_session_key: str = private_key()
        self._lock = asyncio.Lock()
        self._ready = False
        self.last_error: str | None = None
        self.last_deliverables: list[Path] = []
        self._silent_cm: Any | None = None

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def store(self) -> BotStateStore:
        return self._store

    @property
    def chats(self) -> ChatSessionStore:
        return self._chats

    @property
    def selected_model(self) -> str:
        return self._store.state.selected_model

    def set_usage_hook(self, hook: Callable[[dict[str, Any]], None] | None) -> None:
        self._usage_hook = hook

    def _note_usage(self, result: Any) -> None:
        hook = self._usage_hook
        if hook is None:
            return
        usage = getattr(result, "usage", None)
        model = self._store.state.selected_model
        model_sel = getattr(result, "model", None)
        if model_sel is not None:
            mid = getattr(model_sel, "id", None) or ""
            if mid:
                model = str(mid)
        payload: dict[str, Any] = {
            "created_at": time.time(),
            "session_key": self._active_session_key,
            "model": model or "",
            "status": str(getattr(result, "status", "") or ""),
            "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
            "usage_known": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }
        if usage is not None:
            payload["usage_known"] = 1
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "total_tokens",
            ):
                payload[key] = int(getattr(usage, key, 0) or 0)
            payload["reasoning_tokens"] = int(getattr(usage, "reasoning_tokens", 0) or 0)
        try:
            hook(payload)
        except Exception:
            logger.exception("usage hook failed")

    def _agent_options(self) -> AgentOptions:
        return AgentOptions(
            model=self._store.state.selected_model,
            api_key=self._settings.cursor_api_key,
            local=LocalAgentOptions(cwd=str(self._settings.cursor_cwd)),
        )

    async def start(self) -> None:
        async with self._lock:
            await self._ensure_ready_unlocked(force_relaunch=False)

    async def stop(self) -> None:
        async with self._lock:
            await self._cleanup_unlocked()

    async def _ensure_ready_unlocked(self, *, force_relaunch: bool) -> None:
        if force_relaunch:
            await self._cleanup_unlocked()
        elif self._ready and self._client is not None and self._agent is not None:
            try:
                await self._client.ping()
                return
            except Exception:
                logger.warning("Cursor bridge ping failed; relaunching")
                await self._cleanup_unlocked()

        try:
            await self._launch_unlocked()
            topic = self._store.state.active_topic()
            if topic is None:
                raise RuntimeError("No active topic in bot state")
            await self._activate_topic_unlocked(topic)
            self._ready = True
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Failed to start Cursor agent")
            await self._cleanup_unlocked()
            raise

    async def _launch_unlocked(self) -> None:
        prepare_node_tls_env()
        bridge_cmd = resolve_silent_bridge_command()
        logger.info("Launching Cursor bridge silently: %s", bridge_cmd[0])
        # Keep CREATE_NO_WINDOW patch for the whole bridge lifetime.
        self._silent_cm = silent_asyncio_subprocesses()
        await self._silent_cm.__aenter__()
        try:
            self._client = await AsyncClient.launch_bridge(
                bridge_cmd,
                workspace=str(self._settings.cursor_cwd),
            )
            await self._client.__aenter__()
        except Exception:
            await self._exit_silent_cm()
            raise

    async def _exit_silent_cm(self) -> None:
        if self._silent_cm is not None:
            try:
                await self._silent_cm.__aexit__(None, None, None)
            except Exception:
                logger.debug("silent cm exit ignored", exc_info=True)
            self._silent_cm = None

    async def _cleanup_unlocked(self) -> None:
        self._ready = False
        await self._close_agent_unlocked()
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error closing Cursor client")
            self._client = None
        await self._exit_silent_cm()

    async def _close_agent_unlocked(self) -> None:
        if self._agent is not None:
            try:
                await self._agent.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error closing Cursor agent")
            self._agent = None

    async def _activate_binding_unlocked(
        self,
        *,
        session_key: str,
        title: str,
        agent_id: str,
        persist_agent_id,
    ) -> None:
        if self._client is None:
            raise RuntimeError("Cursor client is not started")
        await self._close_agent_unlocked()
        self._active_session_key = session_key
        options = self._agent_options()
        if agent_id:
            try:
                self._agent = await self._client.resume_agent(
                    agent_id,
                    options=options,
                )
                await self._agent.__aenter__()
                logger.info(
                    "Resumed session %s (%s) agent_id=%s model=%s",
                    session_key,
                    title,
                    agent_id,
                    self._store.state.selected_model,
                )
                await self._cancel_active_runs_unlocked(agent_id)
                return
            except Exception:
                logger.exception(
                    "Resume failed for session %s; creating new agent", session_key
                )
                persist_agent_id("")
        self._agent = await self._client.create_agent(options=options)
        await self._agent.__aenter__()
        new_id = str(getattr(self._agent, "agent_id", "") or "")
        persist_agent_id(new_id)
        logger.info(
            "Created agent for session %s (%s) agent_id=%s model=%s",
            session_key,
            title,
            new_id,
            self._store.state.selected_model,
        )

    async def _activate_topic_unlocked(self, topic: TopicRecord) -> None:
        await self._activate_binding_unlocked(
            session_key=private_key(),
            title=topic.title,
            agent_id=topic.agent_id,
            persist_agent_id=lambda aid: self._store.set_topic_agent_id(topic.id, aid),
        )

    async def _activate_chat_unlocked(self, session: ChatSession) -> None:
        await self._activate_binding_unlocked(
            session_key=session.key,
            title=session.title,
            agent_id=session.agent_id,
            persist_agent_id=lambda aid: self._chats.set_agent_id(session.key, aid),
        )

    async def _ensure_session_unlocked(
        self,
        session_key: str,
        *,
        chat_title: str | None = None,
    ) -> None:
        await self._ensure_ready_unlocked(force_relaunch=False)
        if session_key == private_key() or not session_key.startswith("chat:"):
            topic = self._store.state.active_topic()
            if topic is None:
                raise RuntimeError("No active private topic")
            if (
                self._active_session_key != private_key()
                or self._agent is None
                or str(getattr(self._agent, "agent_id", "")) != topic.agent_id
            ):
                await self._activate_topic_unlocked(topic)
            return

        session = self._chats.get_or_create(
            session_key, chat_title or session_key
        )
        if (
            self._active_session_key != session.key
            or self._agent is None
            or str(getattr(self._agent, "agent_id", "")) != session.agent_id
        ):
            await self._activate_chat_unlocked(session)

    async def list_models(self) -> list[tuple[str, str]]:
        async with self._lock:
            await self._ensure_ready_unlocked(force_relaunch=False)
            assert self._client is not None
            items = await self._client.list_models(
                api_key=self._settings.cursor_api_key
            )
        result: list[tuple[str, str]] = []
        for item in items:
            label = (item.display_name or item.id).strip() or item.id
            result.append((item.id, label))
        result.sort(key=lambda pair: pair[1].lower())
        return result

    async def set_model(self, model_id: str) -> None:
        async with self._lock:
            self._store.set_model(model_id)
            logger.info("Selected model: %s", model_id)

    async def list_topics(self) -> list[TopicRecord]:
        topics = sorted(
            self._store.state.topics,
            key=lambda t: t.updated_at,
            reverse=True,
        )
        return topics

    async def switch_topic(self, topic_id: str) -> TopicRecord:
        async with self._lock:
            topic = self._store.activate_topic(topic_id)
            if topic is None:
                raise ValueError(f"Unknown topic: {topic_id}")
            await self._ensure_ready_unlocked(force_relaunch=False)
            await self._activate_topic_unlocked(topic)
            return topic

    async def new_topic(self, title: str | None = None) -> TopicRecord:
        async with self._lock:
            name = title or self._store.next_default_topic_title()
            topic = self._store.add_topic(name)
            await self._ensure_ready_unlocked(force_relaunch=False)
            await self._activate_topic_unlocked(topic)
            return topic

    async def _cancel_active_runs_unlocked(self, agent_id: str) -> int:
        if self._client is None or not agent_id:
            return 0
        cancelled = 0
        try:
            page = await self._client.list_runs(agent_id)
            items = list(getattr(page, "items", None) or [])
        except Exception:
            logger.exception("list_runs failed for agent %s", agent_id)
            return 0

        terminal = {"finished", "completed", "error", "cancelled", "canceled"}
        for run in items:
            snapshot = getattr(run, "snapshot", None)
            status = (
                getattr(run, "status", None)
                or getattr(snapshot, "status", None)
                or ""
            )
            status_s = str(status).lower().removeprefix("run_status_")
            run_id = (
                getattr(run, "id", None)
                or getattr(run, "run_id", None)
                or getattr(snapshot, "id", None)
                or ""
            )
            if not run_id:
                continue
            if status_s in terminal:
                continue
            try:
                await self._client.cancel_run(str(run_id), agent_id=agent_id)
                cancelled += 1
                logger.info(
                    "Cancelled non-terminal run %s status=%s on agent %s",
                    run_id,
                    status_s or "?",
                    agent_id,
                )
            except Exception:
                logger.exception("cancel_run failed for %s", run_id)
        return cancelled

    async def _recover_busy_agent_unlocked(self) -> None:
        """Clear stuck run; if needed, recreate agent for the active session."""
        agent_id = str(getattr(self._agent, "agent_id", "") or "")
        session_key = self._active_session_key
        if session_key.startswith("chat:"):
            session = self._chats.get_or_create(session_key, session_key)
            if not agent_id:
                agent_id = session.agent_id
            if agent_id:
                await self._cancel_active_runs_unlocked(agent_id)
            self._chats.set_agent_id(session_key, "")
            session = self._chats.get_or_create(session_key, session.title)
            await self._activate_chat_unlocked(session)
            return

        topic = self._store.state.active_topic()
        if not agent_id and topic is not None:
            agent_id = topic.agent_id
        if agent_id:
            await self._cancel_active_runs_unlocked(agent_id)
        if topic is None:
            return
        self._store.set_topic_agent_id(topic.id, "")
        await self._activate_topic_unlocked(topic)

    async def _run_once_unlocked(self, text: str) -> str:
        if self._agent is None:
            raise RuntimeError("Cursor agent is not ready")
        if self._active_session_key == private_key():
            topic = self._store.state.active_topic()
            if topic is not None:
                self._store.rename_topic_if_default(topic.id, text)
        else:
            self._chats.touch(self._active_session_key)

        send_opts = SendOptions(model=self._store.state.selected_model)
        try:
            run = await self._agent.send(text, send_opts)
        except Exception as send_exc:
            if not _is_agent_busy(send_exc):
                raise
            logger.warning(
                "Agent busy on send (%s); cancelling stuck run / recreating", send_exc
            )
            await self._recover_busy_agent_unlocked()
            if self._agent is None:
                raise RuntimeError("Cursor agent missing after busy recovery") from send_exc
            run = await self._agent.send(text, send_opts)
        streamed: list[str] = []
        try:
            async for message in run.messages():
                piece = _assistant_text_from_message(message)
                if piece:
                    streamed.append(piece)
        except Exception:
            logger.debug("Run stream read failed", exc_info=True)
        result = await run.wait()
        self._note_usage(result)
        reply = (getattr(result, "result", None) or "").strip()
        if not reply:
            try:
                reply = ((await run.text()) or "").strip()
            except Exception:
                reply = ""
        if not reply and streamed:
            reply = "".join(streamed).strip()

        if getattr(result, "status", None) == "error":
            detail = reply or "SDK returned status=error without details"
            self.last_error = f"{result.id}: {detail}"
            logger.error("Cursor run failed id=%s detail=%s", result.id, detail[:500])
            return (
                "Cursor agent run failed.\n"
                f"Run id: {result.id}\n"
                f"Details: {detail}"
            )

        self.last_error = None
        if self._active_session_key == private_key():
            topic = self._store.state.active_topic()
            if topic is not None:
                self._store.touch_topic(topic.id)
        else:
            self._chats.touch(self._active_session_key)
        return reply or "(empty agent reply)"

    async def ask(
        self,
        text: str,
        *,
        session_key: str | None = None,
        chat_title: str | None = None,
        system_prefix: str | None = None,
    ) -> str:
        prompt = text
        head = [deliverable_instructions()]
        if system_prefix:
            head.insert(0, system_prefix.rstrip())
        prompt = "\n\n".join(head) + "\n\n---\nUser request:\n" + text

        key = session_key or private_key()
        async with self._lock:
            await self._ensure_session_unlocked(key, chat_title=chat_title)
            before = snapshot_outbox()
            self.last_deliverables = []
            try:
                reply = await self._run_once_unlocked(prompt)
            except Exception as exc:
                if not _is_bridge_dead(exc):
                    if isinstance(exc, CursorAgentError):
                        message = getattr(exc, "message", str(exc))
                        self.last_error = message
                        logger.error(
                            "Cursor send error: %s retryable=%s",
                            message,
                            getattr(exc, "is_retryable", None),
                        )
                    else:
                        self.last_error = str(exc)
                        logger.exception("Cursor ask failed")
                    raise

                logger.warning(
                    "Cursor bridge died (%s); relaunching and retrying once", exc
                )
                try:
                    await self._ensure_ready_unlocked(force_relaunch=True)
                    await self._ensure_session_unlocked(key, chat_title=chat_title)
                    reply = await self._run_once_unlocked(prompt)
                except Exception as retry_exc:
                    self.last_error = str(retry_exc)
                    logger.exception("Cursor ask failed after bridge relaunch")
                    raise
            self.last_deliverables = collect_new_deliverables(before)
            return reply
