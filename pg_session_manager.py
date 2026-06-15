"""SessionManager that stores sessions in PostgreSQL instead of JSONL files.

Usage in gateway.py::

    from pg_session_manager import PGSessionManager

    session_manager = PGSessionManager(
        workspace=config.workspace_path,
        dsn="postgresql://user:pass@localhost:5432/nanobot",
    )

    agent = AgentLoop.from_config(config, bus, session_manager=session_manager)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.session.manager import Session, SessionManager, _message_preview_text

# workspace/utils/db.py — added to path so utils.db can be found
_workspace = str(Path(__file__).resolve().parent / "workspace")
if _workspace not in sys.path:
    sys.path.insert(0, _workspace)

from utils.db import sync_transaction, DB_RETRYABLE_ERRORS



MESSAGES_TABLE = "session_messages"
METADATA_TABLE = "session_meta"

# All column names that might appear on a message dict
_MESSAGE_COLUMNS = (
    "tool_calls", "tool_call_id", "name", "reasoning_content",
    "thinking_blocks", "media", "cli_apps", "mcp_presets",
    "injected_event", "_command", "_channel_delivery",
)
_JSON_COLUMNS = {
    "tool_calls", "thinking_blocks", "media", "cli_apps", "mcp_presets",
}


class PGSessionManager(SessionManager):
    """Drop-in for SessionManager backed by PostgreSQL via asyncpg."""

    def __init__(
        self,
        workspace: Path,
        dsn: str = "",
        schema: str = "public",
        messages_table: str = "session_messages",
        meta_table: str = "session_meta",
        **kwargs: Any,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._schema = schema
        self._fq_meta = self._quote(f"{schema}.{meta_table}")
        self._fq_messages = self._quote(f"{schema}.{messages_table}")
        self._cache: dict[str, Session] = {}
        self.sessions_dir = workspace / "sessions"
        self.legacy_sessions_dir = self.sessions_dir
        if dsn:
            from utils.db import configure as _cfg
            _cfg(dsn)

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------
    # SessionManager interface
    # ------------------------------------------------------------------

    @staticmethod
    def safe_key(key: str) -> str:
        from nanobot.utils.helpers import safe_filename
        return safe_filename(key.replace(":", "_"))

    def _get_session_path(self, key: str) -> Path:
        return self.sessions_dir / f"{self.safe_key(key)}.jsonl"

    def get_or_create(self, key: str) -> Session:
        if key in self._cache:
            return self._cache[key]
        session = self._load(key)
        if session is None:
            session = Session(key=key)
        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        try:
            return sync_transaction(lambda conn: self._async_load(conn, key))
        except DB_RETRYABLE_ERRORS:
            logger.warning("DB unavailable, falling back to JSONL for session {}", key)
            return super()._load(key)

    async def _async_load(
        self, conn: asyncpg.Connection, key: str
    ) -> Session | None:
        from asyncpg import Record

        meta = await conn.fetchrow(
            f"SELECT * FROM {self._fq_meta} WHERE session_key = $1",
            key,
        )
        if meta is None:
            return None

        rows = await conn.fetch(
            f"SELECT * FROM {self._fq_messages} "
            f"WHERE session_key = $1 ORDER BY seq ASC",
            key,
        )

        messages = []
        for r in rows:
            msg: dict[str, Any] = {"role": r["role"], "content": r["content"] or ""}
            if r.get("msg_timestamp"):
                msg["timestamp"] = r["msg_timestamp"]
            for col in _MESSAGE_COLUMNS:
                val = r.get(col)
                if val is not None:
                    # backward compat: старые данные могли быть сохранены
                    # как json.dumps(str) до установки JSONB-кодека
                    if isinstance(val, str) and col in _JSON_COLUMNS:
                        val = json.loads(val)
                    msg[col] = val
            # reasoning_content — модель не должна видеть свои прошлые
            # размышления; Mistral API их отвергает, DeepSeek сам добавляет
            # пустые при необходимости.
            msg.pop("reasoning_content", None)
            messages.append(msg)

        return Session(
            key=key,
            messages=messages,
            created_at=meta["created_at"].replace(tzinfo=None) if meta["created_at"] else datetime.now(),
            updated_at=meta["updated_at"].replace(tzinfo=None) if meta["updated_at"] else datetime.now(),
            metadata=dict(meta["metadata"] or {}),
            last_consolidated=meta["last_consolidated"],
        )

    def save(self, session: Session, *, fsync: bool = False) -> None:
        try:
            sync_transaction(lambda conn: self._async_save(conn, session))
        except DB_RETRYABLE_ERRORS:
            logger.warning("DB unavailable, falling back to JSONL for session {}", session.key)
            super().save(session, fsync=fsync)

    async def _async_save(
        self, conn: asyncpg.Connection, session: Session
    ) -> None:
        # metadata — dict, asyncpg JSONB-кодек сам сделает json.dumps
        metadata_val = session.metadata or {}
        updated_at = datetime.now()

        result = await conn.execute(
            f"UPDATE {self._fq_meta} SET "
            f"updated_at = $2, "
            f"last_consolidated = $3, "
            f"metadata = $4 "
            f"WHERE session_key = $1",
            session.key, updated_at,
            session.last_consolidated, metadata_val,
        )
        if result == "UPDATE 0":
            await conn.execute(
                f"INSERT INTO {self._fq_meta} "
                f"(session_key, created_at, updated_at, last_consolidated, metadata) "
                f"VALUES ($1, $2, $3, $4, $5)",
                session.key, session.created_at, updated_at,
                session.last_consolidated, metadata_val,
            )

        await conn.execute(
            f"DELETE FROM {self._fq_messages} WHERE session_key = $1",
            session.key,
        )

        for seq, msg in enumerate(session.messages):
            await self._async_insert_message(conn, session.key, seq, msg)

    async def _async_insert_message(
        self,
        conn: asyncpg.Connection,
        session_key: str,
        seq: int,
        msg: dict[str, Any],
    ) -> None:
        cols = [
            "session_key", "seq", "role", "content",
            "tool_calls", "tool_call_id", "name", "reasoning_content",
            "thinking_blocks", "media", "cli_apps", "mcp_presets",
            "injected_event", "_command", "_channel_delivery", "msg_timestamp",
        ]
        vals: dict[str, Any] = {
            "session_key": session_key,
            "seq": seq,
            "role": msg.get("role", "user"),
            "content": msg.get("content", ""),
            "msg_timestamp": msg.get("timestamp"),
        }
        for col in _MESSAGE_COLUMNS:
            val = msg.get(col)
            if val is not None:
                vals[col] = val
            else:
                vals[col] = None

        placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
        col_list = ", ".join(cols)
        await conn.execute(
            f"INSERT INTO {self._fq_messages} ({col_list}) VALUES ({placeholders})",
            *[vals[c] for c in cols],
        )

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def delete_session(self, key: str) -> bool:
        self.invalidate(key)
        try:
            return sync_transaction(lambda conn: self._async_delete_session(conn, key))
        except DB_RETRYABLE_ERRORS:
            logger.warning("DB unavailable, falling back to JSONL delete for session {}", key)
            return super().delete_session(key)

    async def _async_delete_session(
        self, conn: asyncpg.Connection, key: str
    ) -> bool:
        result = await conn.execute(
            f"DELETE FROM {self._fq_meta} WHERE session_key = $1",
            key,
        )
        return "DELETE" in result and result.split()[-1] != "0"

    def list_sessions(self) -> list[dict[str, Any]]:
        try:
            return sync_transaction(self._async_list_sessions)
        except DB_RETRYABLE_ERRORS:
            logger.warning("DB unavailable, falling back to JSONL for session list")
            return super().list_sessions()

    async def _async_list_sessions(
        self, conn: asyncpg.Connection
    ) -> list[dict[str, Any]]:
        meta_rows = await conn.fetch(
            f"SELECT session_key, created_at, updated_at, metadata "
            f"FROM {self._fq_meta} ORDER BY updated_at DESC"
        )

        out: list[dict[str, Any]] = []
        for meta in meta_rows:
            key = meta["session_key"]
            _raw = meta["metadata"]
            if isinstance(_raw, str):
                _raw = json.loads(_raw)
            meta_dict = dict(_raw or {})
            title = meta_dict.get("title") if isinstance(meta_dict.get("title"), str) else ""

            rows = await conn.fetch(
                f"SELECT role, content FROM {self._fq_messages} "
                f"WHERE session_key = $1 ORDER BY seq ASC LIMIT 10",
                key,
            )
            preview = ""
            for row in rows:
                text = _message_preview_text(dict(row))
                if text:
                    preview = text
                    break

            out.append({
                "key": key,
                "created_at": meta["created_at"].isoformat() if meta["created_at"] else None,
                "updated_at": meta["updated_at"].isoformat() if meta["updated_at"] else None,
                "title": title,
                "preview": preview,
            })
        return out

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        try:
            session = self._load(key)
            if session is None:
                return None
            return self._session_payload(session)
        except DB_RETRYABLE_ERRORS:
            logger.warning("DB unavailable, falling back to JSONL read for session {}", key)
            return super().read_session_file(key)

    @staticmethod
    def _session_payload(session: Session) -> dict[str, Any]:
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }

    def flush_all(self) -> int:
        flushed = 0
        for key, session in list(self._cache.items()):
            try:
                self.save(session)
                flushed += 1
            except Exception:
                logger.warning("Failed to flush session {}", key, exc_info=True)
        return flushed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _quote(ident: str) -> str:
        parts = ident.split(".")
        return ".".join(f'"{p}"' for p in parts)
