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
    """Polls ``conversation_questions`` for pending user messages and writes
    responses (and real-time reasoning) into ``conversation_answers``.

    Config (in ``config.json`` under ``channels.postgres``)::

        {
            "enabled": true,
            "dsn": "postgresql://user:pass@localhost:5432/nanobot",
            "schema": "public",
            "questions_table": "conversation_questions",
            "answers_table": "conversation_answers",
            "poll_interval": 2.0,
            "processing_timeout": 300,
            "allow_from": ["*"]
        }

    Table schemas (see ``create_table.sql``):

    **conversation_questions**
        id, chat_id, user_id, conversation_id, content, media,
        metadata, status (pending|processing|completed|failed),
        created_at, updated_at

    **conversation_answers**
        id, question_id (FK), chat_id, conversation_id,
        content, reasoning, metadata, buttons,
        status (thinking|streaming|completed|failed),
        created_at, updated_at

    The ``reasoning`` column is updated in real-time as the model thinks;
    the web server can poll ``conversation_answers`` and display incremental
    reasoning before the final answer appears in ``content``.
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
        self._questions_table: str = _get("questions_table", "conversation_questions")
        self._answers_table: str = _get("answers_table", "conversation_answers")
        self._fq_questions: str = f"{self._schema}.{self._questions_table}"
        self._fq_answers: str = f"{self._schema}.{self._answers_table}"
        self._poll_interval: float = float(_get("poll_interval", 2.0))
        self._processing_timeout: int = int(_get("processing_timeout", 600))
        self._pool: Any = None
        self._poll_task: asyncio.Task | None = None
        self._stream_buffers: dict[str, str] = {}
        # question_id -> {answer_id, tool_events, reasoning_buf}
        self._msg_ctx: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        await self._ensure_tables()
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info(
            "Polling {} every {}s (processing timeout {}s)",
            self._fq_questions,
            self._poll_interval,
            self._processing_timeout,
        )

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        if self._pool:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    async def _ensure_tables(self) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._fq_questions} (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    chat_id         TEXT,
                    user_id         TEXT,
                    conversation_id UUID NOT NULL,
                    content         TEXT NOT NULL,
                    media           JSON DEFAULT '[]'::json,
                    metadata        JSON DEFAULT '{{}}'::json,
                    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._fq_answers} (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    question_id     UUID REFERENCES {self._fq_questions}(id),
                    chat_id         TEXT,
                    conversation_id UUID NOT NULL,
                    content         TEXT,
                    reasoning       TEXT,
                    metadata        JSON DEFAULT '{{}}'::json,
                    buttons         JSON DEFAULT '[]'::json,
                    status          TEXT NOT NULL DEFAULT 'thinking'
                        CHECK (status IN ('thinking', 'streaming', 'completed', 'failed')),
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._unstick_processing()
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception:
                self.logger.exception("Poll error")
            await asyncio.sleep(self._poll_interval)

    async def _unstick_processing(self) -> None:
        """Release questions stuck in ``processing`` beyond the timeout (max 3 retries).
        Also fail orphaned answers stuck in ``thinking``/``streaming``."""
        pool = await self._get_pool()
        max_retries = 3
        timeout = timedelta(seconds=self._processing_timeout)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, metadata FROM {self._fq_questions}
                WHERE status = 'processing' AND updated_at + $1 < NOW()
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
                        UPDATE {self._fq_questions}
                        SET status = 'failed', metadata = $1::json, updated_at = NOW()
                        WHERE id = $2
                        """,
                        json.dumps(meta),
                        msg_id,
                    )
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_answers}
                        SET status = 'failed', updated_at = NOW()
                        WHERE question_id = $1 AND status IN ('thinking', 'streaming')
                        """,
                        msg_id,
                    )
                    self.logger.warning(
                        "Question {} exceeded max retries ({}/{})",
                        msg_id, retry_count, max_retries,
                    )
                else:
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_questions}
                        SET status = 'pending', metadata = $1::json, updated_at = NOW()
                        WHERE id = $2
                        """,
                        json.dumps(meta),
                        msg_id,
                    )
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_answers}
                        SET status = 'failed', updated_at = NOW()
                        WHERE question_id = $1 AND status IN ('thinking', 'streaming')
                        """,
                        msg_id,
                    )
                    self.logger.warning(
                        "Released stuck question {} (retry {}/{})",
                        msg_id, retry_count, max_retries,
                    )

                self._msg_ctx.pop(msg_id, None)

    async def _poll_once(self) -> None:
        """Claim the oldest pending question and forward it to the agent."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE {self._fq_questions}
                SET status = 'processing', updated_at = NOW()
                WHERE id = (
                    SELECT id FROM {self._fq_questions}
                    WHERE status = 'pending'
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

        question_id = str(row["id"])
        conv_id = str(row["conversation_id"])
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

        # Insert answer placeholder so the web server can start polling
        answer_id = await self._insert_answer(question_id, chat_id, conv_id)

        meta: dict[str, Any] = {
            "message_id": question_id,
            "answer_id": answer_id,
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
            self.logger.exception("Failed to dispatch question {}", question_id)
            await self._mark_failed(question_id, answer_id, "dispatch_error")

    async def _insert_answer(self, question_id: str, chat_id: str, conv_id: str) -> str:
        """Create a ``thinking`` answer row and store its id in ``_msg_ctx``."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._fq_answers}
                    (question_id, chat_id, conversation_id, status, created_at, updated_at)
                VALUES ($1, $2, $3, 'thinking', NOW(), NOW())
                RETURNING id
                """,
                question_id,
                chat_id,
                conv_id,
            )
            answer_id = str(row["id"])
            self._msg_ctx[question_id] = {"answer_id": answer_id, "tool_events": [], "reasoning_buf": []}
            self.logger.debug("Inserted answer placeholder {} for question {}", answer_id, question_id)
            return answer_id

    async def _mark_failed(self, question_id: str, answer_id: str | None, reason: str) -> None:
        """Mark a question and its answer as failed."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT conversation_id FROM {self._fq_questions} WHERE id = $1",
                question_id,
            )
            conv_id = str(row["conversation_id"]) if row else question_id

            if answer_id:
                await conn.execute(
                    f"""
                    UPDATE {self._fq_answers}
                    SET content = $1, metadata = $2::json, status = 'failed', updated_at = NOW()
                    WHERE id = $3
                    """,
                    f"Internal error: {reason}",
                    json.dumps({"error": reason}),
                    answer_id,
                )

            await conn.execute(
                f"""
                UPDATE {self._fq_questions}
                SET status = 'failed', updated_at = NOW()
                WHERE id = $1
                """,
                question_id,
            )
            self._msg_ctx.pop(question_id, None)

    # ------------------------------------------------------------------
    # Reasoning — real-time streaming
    # ------------------------------------------------------------------

    async def send_reasoning_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Write a reasoning chunk to the answer row immediately (direct call path)."""
        answer_id = self._resolve_answer_id(metadata)
        if not answer_id:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {self._fq_answers} SET reasoning = COALESCE(reasoning, '') || $1, updated_at = NOW() WHERE id = $2",
                delta,
                answer_id,
            )

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
                answer_id = self._resolve_answer_id(meta)
                if answer_id:
                    pool = await self._get_pool()
                    async with pool.acquire() as conn:
                        await conn.execute(
                            f"UPDATE {self._fq_answers} SET reasoning = COALESCE(reasoning, '') || $1, updated_at = NOW() WHERE id = $2",
                            msg.content,
                            answer_id,
                        )
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
        answer_id = ctx.get("answer_id") or meta.get("answer_id")
        if not answer_id:
            self.logger.warning("send: no answer_id for msg_id={}", msg_id)
            return

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
                await conn.execute(
                    f"""
                    UPDATE {self._fq_answers}
                    SET content = $1, metadata = $2::json, buttons = $3::json,
                        status = 'completed', updated_at = NOW()
                    WHERE id = $4
                    """,
                    msg.content,
                    json.dumps(meta),
                    json.dumps(msg.buttons or []),
                    answer_id,
                )
                if msg_id:
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_questions}
                        SET status = 'completed', updated_at = NOW()
                        WHERE id = $1 AND status = 'processing'
                        """,
                        msg_id,
                    )
        except Exception:
            self.logger.exception("Failed to write response for {}", chat_id)
            if msg_id:
                await self._mark_failed(msg_id, answer_id, "write_error")

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        meta = dict(metadata or {})
        stream_id = meta.get("_stream_id", chat_id)

        if meta.get("_stream_end"):
            msg_id = meta.get("origin_message_id") or meta.get("message_id")
            ctx = self._msg_ctx.pop(msg_id, {}) if msg_id else {}
            answer_id = ctx.get("answer_id") or meta.get("answer_id")

            if ctx.get("tool_events"):
                existing = meta.get("_tool_events", [])
                meta["_tool_events"] = existing + ctx["tool_events"]
            if ctx.get("reasoning_buf"):
                meta["_reasoning"] = " ".join(ctx["reasoning_buf"])

            content = self._stream_buffers.pop(stream_id, "")
            if content and answer_id:
                conv_id = meta.get("conversation_id") or meta.get("conv_id") or chat_id
                pool = await self._get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        f"""
                        UPDATE {self._fq_answers}
                        SET content = $1, metadata = $2::json, status = 'completed', updated_at = NOW()
                        WHERE id = $3
                        """,
                        content,
                        json.dumps(meta | {"streamed": True}),
                        answer_id,
                    )
                    if msg_id:
                        await conn.execute(
                            f"""
                            UPDATE {self._fq_questions}
                            SET status = 'completed', updated_at = NOW()
                            WHERE id = $1 AND status = 'processing'
                            """,
                            msg_id,
                        )
        else:
            buf = self._stream_buffers.get(stream_id, "")
            self._stream_buffers[stream_id] = buf + delta

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_answer_id(self, metadata: dict[str, Any] | None) -> str | None:
        """Extract ``answer_id`` from metadata or ``_msg_ctx`` by question id."""
        meta = metadata or {}
        answer_id = meta.get("answer_id")
        if answer_id:
            return str(answer_id)
        msg_id = meta.get("origin_message_id") or meta.get("message_id")
        if msg_id:
            ctx = self._msg_ctx.get(msg_id)
            if ctx:
                return ctx.get("answer_id")
        return None

    async def _resolve_conv_id(self, chat_id: str, msg: OutboundMessage) -> str | None:
        """Extract conversation_id from msg.metadata."""
        meta = msg.metadata
        if meta and isinstance(meta, dict):
            cid = meta.get("conversation_id") or meta.get("conv_id")
            if cid:
                return str(cid)
            # Fallback: look up from the question row
            msg_id = meta.get("origin_message_id") or meta.get("message_id")
            if msg_id:
                pool = await self._get_pool()
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        f"SELECT conversation_id FROM {self._fq_questions} WHERE id = $1",
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
            "questions_table": "conversation_questions",
            "answers_table": "conversation_answers",
            "poll_interval": 2.0,
            "processing_timeout": 600,
            "allow_from": ["*"],
        }
