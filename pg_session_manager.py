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

from utils.db import transaction, DB_RETRYABLE_ERRORS
from psycopg2.extras import Json


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
    """Drop-in for SessionManager backed by PostgreSQL via psycopg2."""

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
            with transaction() as conn:
                return self._load_inner(conn, key)
        except DB_RETRYABLE_ERRORS:
            logger.warning("DB unavailable, falling back to JSONL for session {}", key)
            return super()._load(key)

    def _load_inner(self, conn, key: str) -> Session | None:
        meta = None
        rows_raw_list = None

        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {self._fq_meta} WHERE session_key = %s", (key,))
            col_names = [desc[0] for desc in cur.description]
            meta_row = cur.fetchone()
            if meta_row is None:
                return None
            meta = dict(zip(col_names, meta_row))

        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {self._fq_messages} WHERE session_key = %s ORDER BY seq ASC", (key,))
            col_names = [desc[0] for desc in cur.description]
            rows_raw_list = [dict(zip(col_names, r)) for r in cur.fetchall()]

        messages: list[dict[str, Any]] = []
        for r in rows_raw_list:
            msg = {"role": r["role"], "content": r["content"] or ""}
            if r.get("msg_timestamp"):
                msg["timestamp"] = r["msg_timestamp"]
            for col in _MESSAGE_COLUMNS:
                val = r.get(col)
                if val is not None:
                    if isinstance(val, str) and col in _JSON_COLUMNS:
                        val = json.loads(val)
                    msg[col] = val
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
            with transaction() as conn:
                self._save_inner(conn, session)
        except DB_RETRYABLE_ERRORS:
            logger.warning("DB unavailable, falling back to JSONL for session {}", session.key)
            super().save(session, fsync=fsync)

    def _save_inner(self, conn, session: Session) -> None:
        metadata_val = session.metadata or {}
        updated_at = datetime.now()

        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {self._fq_meta} SET "
                f"updated_at = %s, "
                f"last_consolidated = %s, "
                f"metadata = %s "
                f"WHERE session_key = %s",
                (updated_at, session.last_consolidated, metadata_val, session.key),
            )
            if cur.rowcount == 0:
                cur.execute(
                    f"INSERT INTO {self._fq_meta} "
                    f"(session_key, created_at, updated_at, last_consolidated, metadata) "
                    f"VALUES (%s, %s, %s, %s, %s)",
                    (session.key, session.created_at, updated_at,
                     session.last_consolidated, metadata_val),
                )

            cur.execute(
                f"DELETE FROM {self._fq_messages} WHERE session_key = %s",
                (session.key,),
            )

        for seq, msg in enumerate(session.messages):
            self._insert_message(conn, session.key, seq, msg)

    def _insert_message(
        self,
        conn,
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
            if isinstance(val, list) and col in _JSON_COLUMNS:
                val = Json(val)
            vals[col] = val

        col_list = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._fq_messages} ({col_list}) VALUES ({placeholders})",
                [vals[c] for c in cols],
            )

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def delete_session(self, key: str) -> bool:
        self.invalidate(key)
        try:
            with transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {self._fq_meta} WHERE session_key = %s",
                        (key,),
                    )
                    return cur.rowcount > 0
        except DB_RETRYABLE_ERRORS:
            logger.warning("DB unavailable, falling back to JSONL delete for session {}", key)
            return super().delete_session(key)

    def list_sessions(self) -> list[dict[str, Any]]:
        try:
            with transaction() as conn:
                return self._list_sessions_inner(conn)
        except DB_RETRYABLE_ERRORS:
            logger.warning("DB unavailable, falling back to JSONL for session list")
            return super().list_sessions()

    def _list_sessions_inner(self, conn) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT session_key, created_at, updated_at, metadata "
                f"FROM {self._fq_meta} ORDER BY updated_at DESC"
            )
            col_names = [desc[0] for desc in cur.description]
            meta_rows_raw = cur.fetchall()

        meta_rows = [dict(zip(col_names, r)) for r in meta_rows_raw]

        out: list[dict[str, Any]] = []
        for meta in meta_rows:
            key = meta["session_key"]
            _raw = meta["metadata"]
            if isinstance(_raw, str):
                _raw = json.loads(_raw)
            meta_dict = dict(_raw or {})
            title = meta_dict.get("title") if isinstance(meta_dict.get("title"), str) else ""

            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT role, content FROM {self._fq_messages} "
                    f"WHERE session_key = %s ORDER BY seq ASC LIMIT 10",
                    (key,),
                )
                preview = ""
                for row in cur:
                    text = _message_preview_text({
                        "role": row[0],
                        "content": row[1],
                    })
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
