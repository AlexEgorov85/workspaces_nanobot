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


class PostgresChannel(BaseChannel):
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

    ``max_concurrent`` controls how many messages the channel dispatches to
    the agent simultaneously (default 1, meaning strictly sequential).
    Higher values let the agent queue fill faster; the agent still processes
    one message at a time.

    Messages from the same ``conversation_id`` are never dispatched in
    parallel — the channel waits for the active one to finish before
    picking the next from that conversation, regardless of
    ``max_concurrent``.

    Table schema (``conversation_messages``)::

        id              UUID PK DEFAULT uuid_generate_v4()
        chat_id         TEXT
        user_id         TEXT
        conversation_id UUID NOT NULL
        role            TEXT NOT NULL CHECK (IN ('user','assistant','system'))
        content         TEXT NOT NULL
        media           JSONB DEFAULT '[]'
        metadata        JSONB DEFAULT '{}'
        reply_to        UUID          — points to user message for assistant replies
        buttons         JSONB DEFAULT '[]'
        status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (IN ('pending','processing','completed','failed'))
        created_at      TIMESTAMPTZ DEFAULT NOW()
        updated_at      TIMESTAMPTZ DEFAULT NOW()

    User messages arrive with ``role='user'`` and ``status='pending'``.
    The channel claims them (``status → 'processing'``), creates an
    ``role='assistant'`` row linked via ``reply_to``, and streams
    reasoning deltas into ``metadata.reasoning`` until the final
    response is written into ``content``.
    """

    name = "postgres"
    display_name = "PostgreSQL"
    send_progress = True
    send_tool_hints = True
    show_reasoning = True

    def __init__(self, config: Any, bus: MessageBus) -> None:
        super().__init__(config, bus)

        def _get(key: str, default: Any = None) -> Any:
            return (
                config.get(key, default)
                if isinstance(config, dict)
                else getattr(config, key, default)
            )

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
        self._inflight_conv: dict[str, str] = {}  # user_msg_id -> conversation_id
        self._conv_inflight: set[str] = set()     # conversation_ids currently busy
        self._pool: Any = None
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
        await self._ensure_tables()
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
        if self._pool:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    async def _ensure_tables(self) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._fq_table} (
                    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    chat_id         TEXT,
                    user_id         TEXT,
                    conversation_id UUID NOT NULL,
                    role            TEXT NOT NULL
                        CHECK (role IN ('user', 'assistant', 'system')),
                    content         TEXT NOT NULL,
                    media           JSONB DEFAULT '[]'::jsonb,
                    metadata        JSONB DEFAULT '{{}}'::jsonb,
                    reply_to        UUID,
                    buttons         JSONB DEFAULT '[]'::jsonb,
                    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

    # ------------------------------------------------------------------
    # Reasoning batch flush
    # ------------------------------------------------------------------

    async def _flush_reasoning_loop(self) -> None:
        """Flush accumulated reasoning buffers to DB periodically."""
        while self._running:
            await asyncio.sleep(self._flush_interval)
            try:
                await self._flush_reasoning()
            except Exception:
                self.logger.exception("Flush reasoning error")

    async def _flush_reasoning(self) -> None:
        """Write all dirty reasoning buffers to DB in a single batch."""
        if not self._reasoning_buffers:
            return
        buffers = self._reasoning_buffers
        self._reasoning_buffers = {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            for assistant_msg_id, delta in buffers.items():
                if delta:
                    row = await conn.fetchrow(
                        f"SELECT metadata FROM {self._fq_table} WHERE id = $1 FOR UPDATE",
                        assistant_msg_id,
                    )
                    meta = row["metadata"] or {}
                    if isinstance(meta, str):
                        meta = json.loads(meta)
                    reasoning = (meta.get("reasoning") or "") + delta
                    meta["reasoning"] = reasoning
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET metadata = $1::jsonb, updated_at = NOW() WHERE id = $2",
                        json.dumps(meta, ensure_ascii=False),
                        assistant_msg_id,
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
            except Exception:
                self.logger.exception("Poll error")
            await asyncio.sleep(self._poll_interval)

    async def _unstick_processing(self) -> None:
        """Release user messages stuck in ``processing`` beyond the timeout (max 3 retries).
        Also fail orphaned assistant messages stuck in ``processing``."""
        pool = await self._get_pool()
        max_retries = 3
        timeout = timedelta(seconds=self._processing_timeout)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, metadata FROM {self._fq_table}
                WHERE role = 'user' AND status = 'processing' AND updated_at + $1 < NOW()
                FOR UPDATE
                """,
                timeout,
            )

            for row in rows:
                msg_id = str(row["id"])
                meta = row["metadata"] or {}
                if isinstance(meta, str):
                    meta = json.loads(meta) if meta else {}

                retry_count = meta.get("retry_count", 0) + 1
                meta["retry_count"] = retry_count

                if retry_count >= max_retries:
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_table}
                        SET status = 'failed', metadata = $1::jsonb, updated_at = NOW()
                        WHERE id = $2
                        """,
                        json.dumps(meta),
                        msg_id,
                    )
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_table}
                        SET status = 'failed', updated_at = NOW()
                        WHERE reply_to = $1 AND role = 'assistant' AND status = 'processing'
                        """,
                        msg_id,
                    )
                    self.logger.warning(
                        "User msg {} exceeded max retries ({}/{})",
                        msg_id, retry_count, max_retries,
                    )
                else:
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_table}
                        SET status = 'pending', metadata = $1::jsonb, updated_at = NOW()
                        WHERE id = $2
                        """,
                        json.dumps(meta),
                        msg_id,
                    )
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_table}
                        SET status = 'failed', updated_at = NOW()
                        WHERE reply_to = $1 AND role = 'assistant' AND status = 'processing'
                        """,
                        msg_id,
                    )
                    self.logger.warning(
                        "Released stuck user msg {} (retry {}/{})",
                        msg_id, retry_count, max_retries,
                    )

                self._msg_ctx.pop(msg_id, None)
                self._release_slot(msg_id)
                self._reasoning_buffers.clear()

            # Also fail orphaned assistant messages not linked to a failing user msg
            await conn.execute(
                f"""
                UPDATE {self._fq_table}
                SET status = 'failed', updated_at = NOW()
                WHERE role = 'assistant' AND status = 'processing' AND updated_at + $1 < NOW()
                """,
                timeout,
            )

    async def _poll_once(self) -> None:
        """Claim the oldest pending user message and forward it to the agent."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE {self._fq_table}
                SET status = 'processing', updated_at = NOW()
                WHERE id = (
                    SELECT id FROM {self._fq_table}
                    WHERE role = 'user' AND status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE
                )
                AND status = 'pending'
                RETURNING id, chat_id, user_id, conversation_id, content, media, metadata, created_at
                """
            )
            if row is None:
                return

            user_msg_id = str(row["id"])
            conv_id = str(row["conversation_id"])

            # Не диспатчим, если из этого conversation_id уже есть активное сообщение
            if conv_id in self._conv_inflight:
                await conn.execute(
                    f"""
                    UPDATE {self._fq_table}
                    SET status = 'pending', updated_at = NOW()
                    WHERE id = $1
                    """,
                    user_msg_id,
                )
                self.logger.debug(
                    "Deferred msg {} from busy conversation {}", user_msg_id, conv_id,
                )
                return
        chat_id = str(row["chat_id"]) if row["chat_id"] else conv_id
        user_id = str(row["user_id"]) if row["user_id"] else conv_id
        content = row["content"] or ""

        raw_meta = row["metadata"] or {}
        if isinstance(raw_meta, str):
            raw_meta = json.loads(raw_meta) if raw_meta else {}

        raw_media = row["media"] or []
        if isinstance(raw_media, str):
            raw_media = json.loads(raw_media) if raw_media else []
        media: list[str] = raw_media if isinstance(raw_media, list) else []

        # Insert assistant placeholder so the web server can start polling
        assistant_msg_id = await self._insert_assistant_message(user_msg_id, chat_id, conv_id)

        await self._semaphore.acquire()
        self._inflight.add(user_msg_id)
        self._inflight_conv[user_msg_id] = conv_id
        self._conv_inflight.add(conv_id)

        meta: dict[str, Any] = {
            "message_id": user_msg_id,
            "answer_id": assistant_msg_id,
            "conversation_id": conv_id,
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

    async def _insert_assistant_message(self, user_msg_id: str, chat_id: str, conv_id: str) -> str:
        """Create a ``processing`` assistant row and store its id in ``_msg_ctx``."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._fq_table}
                    (chat_id, conversation_id, role, content, reply_to, status, created_at, updated_at)
                VALUES ($1, $2, 'assistant', '', $3, 'processing', NOW(), NOW())
                RETURNING id
                """,
                chat_id,
                conv_id,
                user_msg_id,
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
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if assistant_msg_id:
                await conn.execute(
                    f"""
                    UPDATE {self._fq_table}
                    SET content = $1, metadata = $2::jsonb, status = 'failed', updated_at = NOW()
                    WHERE id = $3
                    """,
                    f"Internal error: {reason}",
                    json.dumps({"error": reason}),
                    assistant_msg_id,
                )

            await conn.execute(
                f"""
                UPDATE {self._fq_table}
                SET status = 'failed', updated_at = NOW()
                WHERE id = $1
                """,
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
                ctx = self._msg_ctx.setdefault(msg_id, {"tool_events": [], "reasoning_buf": []})
                if "_tool_events" in meta:
                    ctx["tool_events"].extend(meta["_tool_events"])
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
        if assistant_msg_id and assistant_msg_id in self._reasoning_buffers:
            delta = self._reasoning_buffers.pop(assistant_msg_id, "")
            if delta:
                pool = await self._get_pool()
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        f"SELECT metadata FROM {self._fq_table} WHERE id = $1 FOR UPDATE",
                        assistant_msg_id,
                    )
                    meta_row = row["metadata"] or {}
                    if isinstance(meta_row, str):
                        meta_row = json.loads(meta_row)
                    reasoning = (meta_row.get("reasoning") or "") + delta
                    meta_row["reasoning"] = reasoning
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET metadata = $1::jsonb, updated_at = NOW() WHERE id = $2",
                        json.dumps(meta_row, ensure_ascii=False),
                        assistant_msg_id,
                    )

        if ctx.get("reasoning_buf"):
            meta["_reasoning"] = " ".join(ctx["reasoning_buf"])
        if ctx.get("tool_events"):
            existing = meta.get("_tool_events", [])
            meta["_tool_events"] = existing + ctx["tool_events"]

        chat_id = msg.chat_id
        conv_id = await self._resolve_conv_id(chat_id, msg)
        if not conv_id:
            self.logger.warning("send: no conv_id for chat_id={}, skipping response", chat_id)
            return

        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT metadata FROM {self._fq_table} WHERE id = $1 FOR UPDATE",
                    assistant_msg_id,
                )
                existing_meta = row["metadata"] or {}
                if isinstance(existing_meta, str):
                    existing_meta = json.loads(existing_meta)
                existing_meta.update(meta)
                await conn.execute(
                    f"""
                    UPDATE {self._fq_table}
                    SET content = $1,
                        metadata = $2::jsonb,
                        buttons = $3::jsonb,
                        status = 'completed', updated_at = NOW()
                    WHERE id = $4
                    """,
                    msg.content,
                    json.dumps(existing_meta, ensure_ascii=False),
                    json.dumps(msg.buttons or []),
                    assistant_msg_id,
                )
                if msg_id:
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_table}
                        SET status = 'completed', updated_at = NOW()
                        WHERE id = $1
                        """,
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

            if ctx.get("tool_events"):
                existing = meta.get("_tool_events", [])
                meta["_tool_events"] = existing + ctx["tool_events"]
            if ctx.get("reasoning_buf"):
                meta["_reasoning"] = " ".join(ctx["reasoning_buf"])

            content = self._stream_buffers.pop(stream_id, "")
            if content and assistant_msg_id:
                conv_id = meta.get("conversation_id") or meta.get("conv_id") or chat_id
                pool = await self._get_pool()
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        f"SELECT metadata FROM {self._fq_table} WHERE id = $1 FOR UPDATE",
                        assistant_msg_id,
                    )
                    existing_meta = row["metadata"] or {}
                    if isinstance(existing_meta, str):
                        existing_meta = json.loads(existing_meta)
                    existing_meta.update(meta | {"streamed": True})
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_table}
                        SET content = $1,
                            metadata = $2::jsonb,
                            status = 'completed', updated_at = NOW()
                        WHERE id = $3
                        """,
                        content,
                        json.dumps(existing_meta, ensure_ascii=False),
                        assistant_msg_id,
                    )
                    if msg_id:
                        await conn.execute(
                            f"""
                            UPDATE {self._fq_table}
                            SET status = 'completed', updated_at = NOW()
                            WHERE id = $1
                            """,
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
        if user_msg_id in self._inflight_conv:
            conv_id = self._inflight_conv.pop(user_msg_id)
            self._conv_inflight.discard(conv_id)

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

    async def _resolve_conv_id(self, chat_id: str, msg: OutboundMessage) -> str | None:
        """Extract conversation_id from msg.metadata or the user message row."""
        meta = msg.metadata
        if meta and isinstance(meta, dict):
            cid = meta.get("conversation_id") or meta.get("conv_id")
            if cid:
                return str(cid)
            msg_id = meta.get("origin_message_id") or meta.get("message_id")
            if msg_id:
                pool = await self._get_pool()
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        f"SELECT conversation_id FROM {self._fq_table} WHERE id = $1",
                        msg_id,
                    )
                    if row:
                        return str(row["conversation_id"])
        self.logger.warning("_resolve_conv_id: no conv_id in metadata for {}", chat_id)
        return None

    # ------------------------------------------------------------------
    # Connection pool
    # ------------------------------------------------------------------

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(
                dsn=self._dsn, min_size=1, max_size=2
            )
        return self._pool

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
