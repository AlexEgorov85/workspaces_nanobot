"""PostgreSQL / Greenplum channel — bridges web server DB with nanobot agent."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import timedelta
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from utils.db import async_fetchval as fetchval, async_execute as execute, async_fetchone as fetchone, async_transaction as transaction, async_fetch as fetch
from psycopg2.extras import Json


def _decode_jsonb(val: Any) -> dict:
    """Безопасно декодировать JSONB значение.

    psycopg2 с register_json возвращает dict.
    Старые записи (сохранённые до перехода) могут быть строкой.
    """
    if val is None:
        return {}
    if isinstance(val, str):
        return json.loads(val)
    if isinstance(val, dict):
        return val
    return dict(val) if val else {}


class PostgresChannel(BaseChannel):
    name = "postgres"
    """Polls ``conversation_messages`` for pending user messages and writes
    assistant responses (and real-time reasoning) back into the same table.

    Config (in ``config.json`` under ``channels.postgres``)::

        {
            "enabled": true,
            "dsn": "postgresql://user:pass@localhost:5432/nanobot",
            "schema": "public",
            "table_name": "conversation_messages",
            "poll_interval": 2.0,
            "max_concurrent": 1,
            "processing_timeout": 300,
            "allow_from": ["*"]
        }
    """

    def __init__(self, config: dict, bus: MessageBus) -> None:
        super().__init__(config, bus)
        _get = config.get

        self._dsn: str = _get("dsn", "")
        self._schema: str = _get("schema", "public")
        self._table_name: str = _get("table_name", "conversation_messages")
        self._fq_table: str = f"{self._schema}.{self._table_name}"
        self._poll_interval: float = float(_get("poll_interval", 2.0))
        self._processing_timeout: int = int(_get("processing_timeout", 600))
        self._flush_interval: float = float(_get("flush_interval", 2.0))
        self._max_concurrent: int = int(_get("max_concurrent", 1))
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._inflight: set[str] = set()
        self._chat_inflight: set[str] = set()     # chat_ids currently busy
        self._msg_chat: dict[str, str] = {}       # user_msg_id -> chat_id
        self._poll_task: asyncio.Task | None = None
        self._stream_buffers: dict[str, str] = {}
        self._reasoning_buffers: dict[str, str] = {}  # assistant_msg_id -> accumulated delta
        self._flush_task: asyncio.Task | None = None
        # user_msg_id -> {assistant_msg_id, tool_events, reasoning_buf}
        self._msg_ctx: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_reasoning_loop())
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info(
            "Polling {} every {}s (processing timeout {}s)",
            self._fq_table,
            self._poll_interval,
            self._processing_timeout,
        )

    async def stop(self) -> None:
        self._running = False
        await self._flush_reasoning()
        if self._flush_task:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
        if self._poll_task:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        pass  # db — глобальный singleton, закрывается при выходе

    # ------------------------------------------------------------------
    # Reasoning batch flush
    # ------------------------------------------------------------------

    async def _flush_reasoning_loop(self) -> None:
        """Flush accumulated reasoning buffers to DB periodically."""
        while self._running:
            await asyncio.sleep(self._flush_interval)
            try:
                await self._flush_reasoning()
            except Exception as e:
                self.logger.error("Flush reasoning error: {}", e)

    async def _flush_reasoning(self) -> None:
        """Write all dirty reasoning buffers to DB in a single batch."""
        if not self._reasoning_buffers:
            return
        buffers = self._reasoning_buffers
        self._reasoning_buffers = {}
        for assistant_msg_id, delta in buffers.items():
            if delta:
                row = await fetchone(
                    f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                    assistant_msg_id,
                )
                if not row:
                    continue
                meta = _decode_jsonb(row["metadata"])
                reasoning = (meta.get("reasoning") or "") + delta
                meta["reasoning"] = reasoning
                await execute(
                    f"UPDATE {self._fq_table} SET metadata = %s, updated_at = NOW() WHERE id = %s",
                    meta, assistant_msg_id,
                )

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._unstick_processing()
                if len(self._inflight) < self._max_concurrent:
                    await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Poll error: {}", e)
            await asyncio.sleep(self._poll_interval)

    async def _unstick_processing(self) -> None:
        """Release user messages stuck in ``processing`` beyond the timeout (max 3 retries).
        Also fail orphaned assistant messages stuck in ``processing``."""
        max_retries = 3
        timeout_s = self._processing_timeout

        async with transaction() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, metadata FROM {self._fq_table}
                WHERE role = 'user' AND status = 'processing'
                AND updated_at + interval '1 second' * %s < NOW()
                """,
                timeout_s,
            )

            for row in rows:
                msg_id = str(row["id"])
                meta = _decode_jsonb(row["metadata"])
                retry_count = meta.get("retry_count", 0) + 1
                meta["retry_count"] = retry_count

                if retry_count >= max_retries:
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'failed', "
                        f"metadata = %s, updated_at = NOW() WHERE id = %s",
                        meta, msg_id,
                    )
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'failed', "
                        f"updated_at = NOW() WHERE reply_to = %s AND role = 'assistant' AND status = 'processing'",
                        msg_id,
                    )
                    self.logger.warning(
                        "User msg {} exceeded max retries ({}/{})",
                        msg_id, retry_count, max_retries,
                    )
                else:
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'pending', "
                        f"metadata = %s, updated_at = NOW() WHERE id = %s",
                        meta, msg_id,
                    )
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'failed', "
                        f"updated_at = NOW() WHERE reply_to = %s AND role = 'assistant' AND status = 'processing'",
                        msg_id,
                    )
                    self.logger.warning(
                        "Released stuck user msg {} (retry {}/{})",
                        msg_id, retry_count, max_retries,
                    )

            # Also fail orphaned assistant messages
            await conn.execute(
                f"UPDATE {self._fq_table} SET status = 'failed', "
                f"updated_at = NOW() WHERE role = 'assistant' AND status = 'processing' "
                f"AND updated_at + interval '1 second' * %s < NOW()",
                timeout_s,
            )

    async def _poll_once(self) -> None:
        """Claim the oldest pending user message and forward it to the agent."""
        row = await fetchone(
            f"""
            UPDATE {self._fq_table}
            SET status = 'processing', updated_at = NOW()
            WHERE id = (
                SELECT id FROM {self._fq_table}
                WHERE role = 'user' AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
            )
            AND status = 'pending'
            RETURNING id, chat_id, user_id, content, media, metadata, created_at
            """
        )
        if row is None:
            return

        user_msg_id = str(row["id"])
        chat_id = str(row["chat_id"]) if row["chat_id"] else str(row["user_id"])
        user_id = str(row["user_id"]) if row["user_id"] else chat_id

        # Не диспатчим, если из этого chat_id уже есть активное сообщение
        if chat_id in self._chat_inflight:
            await execute(
                f"UPDATE {self._fq_table} SET status = 'pending', updated_at = NOW() WHERE id = %s",
                user_msg_id,
            )
            self.logger.debug(
                "Deferred msg {} from busy chat {}", user_msg_id, chat_id,
            )
            return

        content = row["content"] or ""

        raw_meta = _decode_jsonb(row["metadata"])

        raw_media = row["media"] or []
        if isinstance(raw_media, str):
            raw_media = json.loads(raw_media) if raw_media else []
        media: list[str] = raw_media if isinstance(raw_media, list) else []

        # Insert assistant placeholder so the web server can start polling
        assistant_msg_id = await self._insert_assistant_message(user_msg_id, chat_id)

        await self._semaphore.acquire()
        self._inflight.add(user_msg_id)
        self._chat_inflight.add(chat_id)
        self._msg_chat[user_msg_id] = chat_id

        meta: dict[str, Any] = {
            "message_id": user_msg_id,
            "answer_id": assistant_msg_id,
            **raw_meta,
        }

        try:
            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
                media=media,
                metadata=meta,
            )
        except Exception:
            self.logger.exception("Failed to dispatch user message {}", user_msg_id)
            await self._mark_failed(user_msg_id, assistant_msg_id, "dispatch_error")

    async def _insert_assistant_message(self, user_msg_id: str, chat_id: str) -> str:
        """Create a ``processing`` assistant row and store its id in ``_msg_ctx``."""
        row = await fetchone(
            f"""
            INSERT INTO {self._fq_table}
                (chat_id, role, content, reply_to, status, created_at, updated_at)
            VALUES (%s, 'assistant', '', %s, 'processing', NOW(), NOW())
            RETURNING id
            """,
            chat_id, user_msg_id,
        )
        assistant_msg_id = str(row["id"])
        self._msg_ctx[user_msg_id] = {
            "assistant_msg_id": assistant_msg_id,
            "tool_events": [],
            "reasoning_buf": [],
        }
        self.logger.debug(
            "Inserted assistant placeholder {} for user msg {}",
            assistant_msg_id, user_msg_id,
        )
        return assistant_msg_id

    async def _mark_failed(self, user_msg_id: str, assistant_msg_id: str | None, reason: str) -> None:
        """Mark a user message and its assistant reply as failed."""
        async with transaction() as conn:
            if assistant_msg_id:
                await conn.execute(
                    f"UPDATE {self._fq_table} SET content = %s, metadata = %s, "
                    f"status = 'failed', updated_at = NOW() WHERE id = %s",
                    f"Internal error: {reason}", {"error": reason}, assistant_msg_id,
                )
            await conn.execute(
                f"UPDATE {self._fq_table} SET status = 'failed', updated_at = NOW() WHERE id = %s",
                user_msg_id,
            )
        self._msg_ctx.pop(user_msg_id, None)
        self._release_slot(user_msg_id)
        if assistant_msg_id:
            self._reasoning_buffers.pop(assistant_msg_id, None)

    # ------------------------------------------------------------------
    # Reasoning — real-time streaming into metadata.reasoning
    # ------------------------------------------------------------------

    async def send_reasoning_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Buffer a reasoning chunk for batch flush into ``metadata.reasoning``."""
        assistant_msg_id = self._resolve_assistant_msg_id(metadata)
        if assistant_msg_id:
            buf = self._reasoning_buffers.get(assistant_msg_id, "")
            self._reasoning_buffers[assistant_msg_id] = buf + delta
            return
        # Fallback: assistant_msg_id not yet resolved — buffer in _msg_ctx
        msg_id = (metadata or {}).get("origin_message_id") or (metadata or {}).get("message_id")
        if msg_id:
            ctx = self._msg_ctx.setdefault(msg_id, {})
            ctx.setdefault("reasoning_buf", []).append(delta)

    async def send_reasoning_end(
        self, chat_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        pass

    # ------------------------------------------------------------------
    # Send (outbound)
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        meta = dict(msg.metadata or {})
        msg_id = meta.get("origin_message_id") or meta.get("message_id")

        # --- Reasoning progress — write to DB in real-time ---
        if meta.get("_reasoning_delta"):
            if msg.content:
                assistant_msg_id = self._resolve_assistant_msg_id(meta)
                if assistant_msg_id:
                    buf = self._reasoning_buffers.get(assistant_msg_id, "")
                    self._reasoning_buffers[assistant_msg_id] = buf + msg.content
                elif msg_id:
                    ctx = self._msg_ctx.setdefault(msg_id, {})
                    ctx.setdefault("reasoning_buf", []).append(msg.content)
            return

        if meta.get("_reasoning_end"):
            return

        # --- Intermediate progress/tool-event messages — accumulate, don't write ---
        if meta.get("_progress"):
            if msg_id:
                if len(self._msg_ctx) > 100:
                    self._msg_ctx.clear()
                ctx = self._msg_ctx.setdefault(msg_id, {"reasoning_buf": []})
            return

        # --- Control signals ---
        if meta.get("_turn_end"):
            return

        # --- Final response — merge accumulated context, write to DB ---
        ctx = self._msg_ctx.pop(msg_id, {}) if msg_id else {}
        self._release_slot(msg_id)
        assistant_msg_id = ctx.get("assistant_msg_id") or meta.get("answer_id")
        if not assistant_msg_id:
            self.logger.warning("send: no assistant_msg_id for msg_id={}", msg_id)
            return

        # Flush any pending buffered reasoning before final answer
        reasoning_parts = []
        if assistant_msg_id and assistant_msg_id in self._reasoning_buffers:
            delta = self._reasoning_buffers.pop(assistant_msg_id, "")
            if delta:
                reasoning_parts.append(delta)
        if ctx.get("reasoning_buf"):
            reasoning_parts = ctx["reasoning_buf"] + reasoning_parts
        if reasoning_parts:
            combined = " ".join(reasoning_parts)
            row = await fetchone(
                f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                assistant_msg_id,
            )
            if row:
                meta_row = _decode_jsonb(row["metadata"])
                meta_row["reasoning"] = (meta_row.get("reasoning") or "") + combined
                await execute(
                    f"UPDATE {self._fq_table} SET metadata = %s, updated_at = NOW() WHERE id = %s",
                    meta_row, assistant_msg_id,
                )

        chat_id = msg.chat_id

        try:
            async with transaction() as conn:
                row = await conn.fetchrow(
                    f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                    assistant_msg_id,
                )
                existing_meta = _decode_jsonb(row["metadata"]) if row else {}
                existing_meta.update(meta)
                await conn.execute(
                    f"UPDATE {self._fq_table} "
                    f"SET content = %s, metadata = %s, buttons = %s, "
                    f"status = 'completed', updated_at = NOW() WHERE id = %s",
                    msg.content, existing_meta,
                    Json(msg.buttons or []), assistant_msg_id,
                )
                if msg_id:
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'completed', "
                        f"updated_at = NOW() WHERE id = %s",
                        msg_id,
                    )
        except Exception:
            self.logger.exception("Failed to write response for {}", chat_id)
            if msg_id:
                await self._mark_failed(msg_id, assistant_msg_id, "write_error")

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        meta = dict(metadata or {})
        stream_id = meta.get("_stream_id", chat_id)

        if meta.get("_stream_end"):
            msg_id = meta.get("origin_message_id") or meta.get("message_id")
            ctx = self._msg_ctx.pop(msg_id, {}) if msg_id else {}
            self._release_slot(msg_id)
            assistant_msg_id = ctx.get("assistant_msg_id") or meta.get("answer_id")

            if ctx.get("reasoning_buf"):
                meta["reasoning"] = " ".join(ctx["reasoning_buf"])

            content = self._stream_buffers.pop(stream_id, "")
            if content and assistant_msg_id:
                async with transaction() as conn:
                    row = await conn.fetchrow(
                        f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                        assistant_msg_id,
                    )
                    existing_meta = _decode_jsonb(row["metadata"]) if row else {}
                    existing_meta.update(meta | {"streamed": True})
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET content = %s, "
                        f"metadata = %s, status = 'completed', updated_at = NOW() WHERE id = %s",
                        content, existing_meta, assistant_msg_id,
                    )
                    if msg_id:
                        await conn.execute(
                            f"UPDATE {self._fq_table} SET status = 'completed', "
                            f"updated_at = NOW() WHERE id = %s",
                            msg_id,
                        )
        else:
            buf = self._stream_buffers.get(stream_id, "")
            self._stream_buffers[stream_id] = buf + delta

    # ------------------------------------------------------------------
    # Concurrency slot
    # ------------------------------------------------------------------

    def _release_slot(self, user_msg_id: str) -> None:
        """Release a concurrency slot if this message holds one."""
        if user_msg_id in self._inflight:
            self._inflight.discard(user_msg_id)
            self._semaphore.release()
        chat_id = self._msg_chat.pop(user_msg_id, None)
        if chat_id:
            self._chat_inflight.discard(chat_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_assistant_msg_id(self, metadata: dict[str, Any] | None) -> str | None:
        """Extract ``assistant_msg_id`` from metadata or ``_msg_ctx`` by user msg id."""
        meta = metadata or {}
        answer_id = meta.get("answer_id")
        if answer_id:
            return str(answer_id)
        msg_id = meta.get("origin_message_id") or meta.get("message_id")
        if msg_id:
            ctx = self._msg_ctx.get(msg_id)
            if ctx:
                return ctx.get("assistant_msg_id")
        return None

    # ------------------------------------------------------------------
    # Default config (auto-injected by ``nanobot onboard`` via entry_points)
    # ------------------------------------------------------------------

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {
            "enabled": True,
            "dsn": "postgresql://user:pass@localhost:5432/nanobot",
            "schema": "public",
            "table_name": "conversation_messages",
            "poll_interval": 2.0,
            "flush_interval": 2.0,
            "max_concurrent": 1,
            "processing_timeout": 600,
            "allow_from": ["*"],
        }
