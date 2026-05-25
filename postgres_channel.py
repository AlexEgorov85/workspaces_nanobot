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
    """Polls a PostgreSQL or Greenplum table for pending user messages and writes back responses.

    Config (in ``config.json`` under ``channels.postgres``)::

        {
            "enabled": true,
            "dsn": "postgresql://user:pass@localhost:5432/nanobot",
            "table": "conversation_messages",
            "schema": "public",
            "poll_interval": 2.0,
            "processing_timeout": 300,
            "allow_from": ["*"]
        }

    Expected table schema (see ``create_table.sql``)::

        column           | type
        -----------------|-------------------------------
        id               | UUID / TEXT PRIMARY KEY
        chat_id          | TEXT (null → falls back to conversation_id)
        user_id          | TEXT (null → falls back to conversation_id)
        conversation_id  | UUID / TEXT NOT NULL
        role             | TEXT CHECK (user / assistant / system)
        content          | TEXT NOT NULL
        media            | JSON / JSONB
        metadata         | JSON / JSONB DEFAULT '{}'
        status           | TEXT CHECK (pending / processing / completed / failed)
        created_at       | TIMESTAMPTZ NOT NULL
        updated_at       | TIMESTAMPTZ NOT NULL
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
        self._table: str = _get("table", "conversation_messages")
        self._schema: str = _get("schema", "public")
        self._fqtable: str = f"{self._schema}.{self._table}"
        self._poll_interval: float = float(_get("poll_interval", 2.0))
        self._processing_timeout: int = int(_get("processing_timeout", 600))
        self._pool: Any = None
        self._poll_task: asyncio.Task | None = None
        self._stream_buffers: dict[str, str] = {}
        self._msg_ctx: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info(
            "Polling every {}s (processing timeout {}s)",
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
        """Release messages stuck in ``processing`` beyond the timeout (max 3 retries)."""
        pool = await self._get_pool()
        max_retries = 3
        timeout = timedelta(seconds=self._processing_timeout)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, metadata FROM {self._fqtable}
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
                        UPDATE {self._fqtable}
                        SET status = 'failed', metadata = $1::json, updated_at = NOW()
                        WHERE id = $2
                        """,
                        json.dumps(meta),
                        msg_id,
                    )
                    self.logger.warning(
                        "Message {} exceeded max retries ({}/{}), marking as failed",
                        msg_id, retry_count, max_retries,
                    )
                else:
                    await conn.execute(
                        f"""
                        UPDATE {self._fqtable}
                        SET status = 'pending', metadata = $1::json, updated_at = NOW()
                        WHERE id = $2
                        """,
                        json.dumps(meta),
                        msg_id,
                    )
                    self.logger.warning(
                        "Released stuck message {} (retry {}/{})",
                        msg_id, retry_count, max_retries,
                    )

                # Discard any accumulated context for this message
                self._msg_ctx.pop(msg_id, None)

    async def _poll_once(self) -> None:
        """Claim the oldest pending user message and forward it to the agent."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE {self._fqtable}
                SET status = 'processing', updated_at = NOW()
                WHERE id = (
                    SELECT id FROM {self._fqtable}
                    WHERE status = 'pending' AND role = 'user'
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

        msg_id = str(row["id"])
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

        meta: dict[str, Any] = {
            "message_id": msg_id,
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
            self.logger.exception("Failed to dispatch message {}", msg_id)
            await self._mark_failed(msg_id, conv_id, "dispatch_error")

    async def _mark_failed(self, msg_id: str, conv_id: str, reason: str, chat_id: str = "") -> None:
        """Mark a user message as failed and write an error response."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._fqtable}
                    (chat_id, conversation_id, role, content, metadata, status, created_at, updated_at)
                VALUES ($1, $2, 'assistant', $3, $4::json, 'failed', NOW(), NOW())
                """,
                chat_id or conv_id,
                conv_id,
                f"Internal error: {reason}",
                json.dumps({"error": reason, "original_message_id": msg_id}),
            )
            await conn.execute(
                f"""
                UPDATE {self._fqtable}
                SET status = 'failed', updated_at = NOW()
                WHERE id = $1
                """,
                msg_id,
            )

    # ------------------------------------------------------------------
    # Send (outbound)
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        meta = dict(msg.metadata or {})
        msg_id = meta.get("origin_message_id") or meta.get("message_id")

        # Intermediate progress/tool-event messages — accumulate, don't write to DB
        if meta.get("_progress"):
            if msg_id:
                if len(self._msg_ctx) > 100:
                    self._msg_ctx.clear()
                ctx = self._msg_ctx.setdefault(msg_id, {"tool_events": [], "reasoning": []})
                if "_tool_events" in meta:
                    ctx["tool_events"].extend(meta["_tool_events"])
            return

        # Control signals — skip DB write entirely
        if meta.get("_turn_end"):
            return

        # Final response — merge accumulated context into metadata
        if msg_id and msg_id in self._msg_ctx:
            ctx = self._msg_ctx.pop(msg_id)
            if ctx["tool_events"]:
                existing = meta.get("_tool_events", [])
                meta["_tool_events"] = existing + ctx["tool_events"]
            if ctx["reasoning"]:
                meta["_reasoning"] = " ".join(ctx["reasoning"])

        chat_id = msg.chat_id
        conv_id = self._resolve_conv_id(chat_id, msg)
        if not conv_id:
            self.logger.warning("send: no conv_id for chat_id={}, skipping response", chat_id)
            return

        reply_to = meta.get("message_id") or meta.get("origin_message_id")

        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {self._fqtable}
                        (chat_id, conversation_id, role, content, media, metadata, reply_to, buttons, status, created_at, updated_at)
                    VALUES ($1, $2, 'assistant', $3, $4::json, $5::json, $6, $7::json, 'completed', NOW(), NOW())
                    """,
                    chat_id,
                    conv_id,
                    msg.content,
                    json.dumps(msg.media or []),
                    json.dumps(meta),
                    reply_to,
                    json.dumps(msg.buttons or []),
                )
                if reply_to:
                    await conn.execute(
                        f"""
                        UPDATE {self._fqtable}
                        SET status = 'completed', updated_at = NOW()
                        WHERE id = $1 AND status = 'processing'
                        """,
                        reply_to,
                    )
        except Exception:
            self.logger.exception("Failed to write response for {}", chat_id)
            if reply_to:
                await self._mark_failed(reply_to, conv_id, "write_error")

    async def send_reasoning_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        meta = metadata or {}
        msg_id = meta.get("origin_message_id") or meta.get("message_id") or chat_id
        ctx = self._msg_ctx.setdefault(msg_id, {"tool_events": [], "reasoning": []})
        ctx["reasoning"].append(delta)

    async def send_reasoning_end(
        self, chat_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        pass

    def _resolve_conv_id(self, chat_id: str, msg: OutboundMessage) -> str | None:
        """Fallback: extract conversation_id from msg.metadata."""
        meta = msg.metadata
        if meta and isinstance(meta, dict):
            cid = meta.get("conversation_id") or meta.get("conv_id")
            if cid:
                return str(cid)
        self.logger.warning("_resolve_conv_id: no conv_id in metadata for {}", chat_id)
        return None

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        meta = dict(metadata or {})
        stream_id = meta.get("_stream_id", chat_id)

        if meta.get("_stream_end"):
            msg_id = meta.get("origin_message_id") or meta.get("message_id")
            if msg_id and msg_id in self._msg_ctx:
                ctx = self._msg_ctx.pop(msg_id)
                if ctx["tool_events"]:
                    existing = meta.get("_tool_events", [])
                    meta["_tool_events"] = existing + ctx["tool_events"]
                if ctx["reasoning"]:
                    meta["_reasoning"] = " ".join(ctx["reasoning"])

            content = self._stream_buffers.pop(stream_id, "")
            if content:
                conv_id = meta.get("conversation_id") or meta.get("conv_id") or chat_id
                reply_to = meta.get("message_id") or meta.get("origin_message_id")
                pool = await self._get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        f"""
                        INSERT INTO {self._fqtable}
                            (chat_id, conversation_id, role, content, metadata, reply_to, status, created_at, updated_at)
                        VALUES ($1, $2, 'assistant', $3, $4::json, $5, 'completed', NOW(), NOW())
                        """,
                        chat_id,
                        conv_id,
                        content,
                        json.dumps(meta | {"streamed": True}),
                        reply_to,
                    )
                    if reply_to:
                        await conn.execute(
                            f"""
                            UPDATE {self._fqtable}
                            SET status = 'completed', updated_at = NOW()
                            WHERE id = $1 AND status = 'processing'
                            """,
                            reply_to,
                        )

        else:
            buf = self._stream_buffers.get(stream_id, "")
            self._stream_buffers[stream_id] = buf + delta

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
            "table": "conversation_messages",
            "schema": "public",
            "poll_interval": 2.0,
            "processing_timeout": 600,
            "allow_from": ["*"],
        }
