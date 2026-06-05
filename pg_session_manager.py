"""SessionManager that stores sessions in PostgreSQL instead of JSONL files.

Usage in gateway.py::

    from pg_session_manager import PGSessionManager

    session_manager = PGSessionManager(
        workspace=config.workspace_path,
        dsn="postgresql://user:pass@localhost:5432/nanobot",
    )
    session_manager.ensure_tables()

    agent = AgentLoop.from_config(config, bus, session_manager=session_manager)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2.extras
from loguru import logger

from nanobot.session.manager import Session, SessionManager, _message_preview_text
from utils.db import db


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
    """Drop-in for SessionManager backed by PostgreSQL.

    Uses a sync connection pool (psycopg2) so it matches the base class
    synchronous ``save()`` / ``_load()`` / ``list_sessions()`` interface.
    Long-running async callers should wrap these in ``run_in_executor``.
    """

    def __init__(
        self,
        workspace: Path,
        dsn: str = "",
        schema: str = "public",
        messages_table: str = "session_messages",
        meta_table: str = "session_meta",
        min_conn: int = 1,
        max_conn: int = 4,
        pool_timeout: float = 5.0,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._schema = schema
        self._fq_meta = self._quote(f"{schema}.{meta_table}")
        self._fq_messages = self._quote(f"{schema}.{messages_table}")
        self._cache: dict[str, Session] = {}
        # For legacy compatibility — SessionManager stores these
        self.sessions_dir = workspace / "sessions"
        self.legacy_sessions_dir = self.sessions_dir  # not used
        if dsn:
            db.configure(dsn)

    def close(self) -> None:
        """Совместимость с SessionManager: ничего не делаем, коннекшн глобальный."""
        pass

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------

    def ensure_tables(self) -> None:
        with db.connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        CREATE TABLE IF NOT EXISTS {self._fq_meta} (
                            session_key      TEXT PRIMARY KEY,
                            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            last_consolidated INT NOT NULL DEFAULT 0,
                            metadata         JSONB NOT NULL DEFAULT '{{}}'::jsonb
                        )
                    """)
                    cur.execute(f"""
                        CREATE TABLE IF NOT EXISTS {self._fq_messages} (
                            id               BIGSERIAL PRIMARY KEY,
                            session_key      TEXT NOT NULL
                                REFERENCES {self._fq_meta}(session_key) ON DELETE CASCADE,
                            seq              INT NOT NULL,
                            role             TEXT NOT NULL,
                            content          TEXT,
                            msg_timestamp    TEXT,
                            tool_calls       JSONB,
                            tool_call_id     TEXT,
                            name             TEXT,
                            reasoning_content TEXT,
                            thinking_blocks  JSONB,
                            media            JSONB,
                            cli_apps         JSONB,
                            mcp_presets      JSONB,
                            injected_event   TEXT,
                            _command         BOOLEAN,
                            _channel_delivery BOOLEAN,
                            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
                    cur.execute(f"""
                        CREATE INDEX IF NOT EXISTS idx_{MESSAGES_TABLE}_sk_seq
                        ON {self._fq_messages} (session_key, seq)
                    """)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

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
        with db.connection() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        f"SELECT * FROM {self._fq_meta} WHERE session_key = %s",
                        (key,),
                    )
                    meta = cur.fetchone()
                    if meta is None:
                        return None

                    cur.execute(
                        f"SELECT * FROM {self._fq_messages} WHERE session_key = %s ORDER BY seq ASC",
                        (key,),
                    )
                    rows = cur.fetchall()
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        messages = []
        for r in rows:
            msg: dict[str, Any] = {"role": r["role"], "content": r["content"] or ""}
            if r.get("msg_timestamp"):
                msg["timestamp"] = r["msg_timestamp"]
            for col in _MESSAGE_COLUMNS:
                val = r.get(col)
                if val is not None:
                    msg[col] = val
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
        with db.connection() as conn:
            try:
                with conn.cursor() as cur:
                    metadata_json = json.dumps(session.metadata, ensure_ascii=False, default=str)
                    updated_at = datetime.now()
                    cur.execute(
                        f"""UPDATE {self._fq_meta} SET
                                updated_at = %s,
                                last_consolidated = %s,
                                metadata = %s::jsonb
                            WHERE session_key = %s""",
                        (updated_at, session.last_consolidated, metadata_json, session.key),
                    )
                    if cur.rowcount == 0:
                        cur.execute(
                            f"""INSERT INTO {self._fq_meta}
                                    (session_key, created_at, updated_at, last_consolidated, metadata)
                                VALUES (%s, %s, %s, %s, %s::jsonb)""",
                            (session.key, session.created_at, updated_at,
                             session.last_consolidated, metadata_json),
                        )

                    cur.execute(
                        f"DELETE FROM {self._fq_messages} WHERE session_key = %s",
                        (session.key,),
                    )

                    for seq, msg in enumerate(session.messages):
                        self._insert_message(cur, session.key, seq, msg)

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        self._cache[session.key] = session

    def _insert_message(
        self,
        cur: psycopg2.extensions.cursor,
        session_key: str,
        seq: int,
        msg: dict[str, Any],
    ) -> None:
        values: dict[str, Any] = {
            "session_key": session_key,
            "seq": seq,
            "role": msg.get("role", "user"),
            "content": msg.get("content", ""),
        }
        for col in _MESSAGE_COLUMNS:
            val = msg.get(col)
            if val is not None:
                values[col] = json.dumps(val, ensure_ascii=False, default=str) if col in _JSON_COLUMNS else val
            else:
                values[col] = None
        values["msg_timestamp"] = msg.get("timestamp")

        cols = ", ".join(values)
        placeholders = ", ".join(f"%({k})s" for k in values)
        cur.execute(
            f"INSERT INTO {self._fq_messages} ({cols}) VALUES ({placeholders})",
            values,
        )

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def delete_session(self, key: str) -> bool:
        self.invalidate(key)
        with db.connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {self._fq_meta} WHERE session_key = %s",
                        (key,),
                    )
                    deleted = cur.rowcount > 0
                conn.commit()
                return deleted
            except Exception:
                conn.rollback()
                raise

    def list_sessions(self) -> list[dict[str, Any]]:
        with db.connection() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(f"""
                        SELECT m.session_key, m.created_at, m.updated_at, m.metadata
                        FROM {self._fq_meta} m
                        ORDER BY m.updated_at DESC
                    """)
                    meta_rows = cur.fetchall()

                    out: list[dict[str, Any]] = []
                    for meta in meta_rows:
                        key = meta["session_key"]
                        meta_dict = dict(meta["metadata"] or {})
                        title = meta_dict.get("title") if isinstance(meta_dict.get("title"), str) else ""

                        cur.execute(
                            f"""
                            SELECT role, content FROM {self._fq_messages}
                            WHERE session_key = %s
                            ORDER BY seq ASC LIMIT 10
                            """,
                            (key,),
                        )
                        preview = ""
                        for row in cur.fetchall():
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
                    conn.commit()
                    return out
            except Exception:
                conn.rollback()
                raise

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        session = self._load(key)
        if session is None:
            return None
        return self._session_payload(session)

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
        """Quote a PostgreSQL identifier (schema.table)."""
        parts = ident.split(".")
        return ".".join(f'"{p}"' for p in parts)

    @staticmethod
    def _session_payload(session):
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }
