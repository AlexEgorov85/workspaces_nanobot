"""SessionManager, хранящий сессии в PostgreSQL вместо JSONL-файлов.

Использование в gateway.py::

    from pg_session_manager import PGSessionManager

    session_manager = PGSessionManager(
        workspace=config.workspace_path,
        dsn="postgresql://user:pass@localhost:5432/nanobot",
    )

    agent = AgentLoop.from_config(config, bus, session_manager=session_manager)

Таблицы ``public.agent_session_meta`` и ``public.agent_session_messages``
создаются вручную скриптами ``sql/session/create_public_agent_session_meta.sql``
и ``sql/session/create_public_agent_session_messages.sql``.

При недоступности БД ошибка пробрасывается — никакого скрытого падения
на JSONL-файлы нет.

Импорт ``utils.db`` работает потому, что вызывающий (gateway.py / cli_agent.py /
тесты) уже добавил ``workspace/`` в ``sys.path``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.session.manager import Session, SessionManager, _message_preview_text

from utils.db import transaction
from psycopg2.extras import Json, execute_values


# Все колонки, которые могут появиться в сообщении сессии (кроме базовых)
_MESSAGE_COLUMNS = (
    "tool_calls", "tool_call_id", "name", "reasoning_content",
    "thinking_blocks", "media", "cli_apps", "mcp_presets",
    "injected_event", "_command", "_channel_delivery",
)
_JSON_COLUMNS = {
    # колонки, которые хранятся как JSON (а не текст) и при чтении
    # требуют json.loads()
    "tool_calls", "thinking_blocks", "media", "cli_apps", "mcp_presets",
}


class PGSessionManager(SessionManager):
    """Замена SessionManager с хранением в PostgreSQL через psycopg2.

    Полностью повторяет интерфейс SessionManager, но все данные хранит
    в двух таблицах: ``agent_session_meta`` и ``agent_session_messages``.

    При ошибках БД (DB_RETRYABLE_ERRORS) исключение пробрасывается —
    отката на JSONL-файлы нет.
    """

    def __init__(
        self,
        workspace: Path,
        dsn: str = "",
        schema: str = "public",
        messages_table: str = "",
        meta_table: str = "",
        **kwargs: Any,
    ) -> None:
        if not messages_table or not meta_table:
            raise ValueError(
                "PGSessionManager: messages_table и meta_table обязательны "
                "(channels.postgres.messages_table / meta_table). "
                f"messages_table={messages_table!r}, meta_table={meta_table!r}"
            )
        self.workspace = Path(workspace).expanduser().resolve()
        # Инициализируем базовый класс: задаёт sessions_dir (нужен фреймворку
        # для WebUI-эндпоинтов /api/sessions и read_session_metadata), кеш и пр.
        super().__init__(workspace=self.workspace)
        self._schema = schema
        # fully-qualified имена таблиц с кавычками (через _quote)
        self._fq_meta = self._quote(f"{schema}.{meta_table}")
        self._fq_messages = self._quote(f"{schema}.{messages_table}")
        # кеш загруженных сессий (Session → key)
        self._cache: dict[str, Session] = {}
        if dsn:
            from utils.db import configure as _cfg
            _cfg(dsn)

    def close(self) -> None:
        """Закрыть менеджер. С psycopg2 пул не используется — ничего не делаем."""
        pass

    # ------------------------------------------------------------------
    # Интерфейс SessionManager (get_or_create / save / load / delete)
    # ------------------------------------------------------------------

    def get_or_create(self, key: str) -> Session:
        """Вернуть сессию по ключу (из кеша или из БД), создав если нет."""
        if key in self._cache:
            return self._cache[key]
        session = self._load(key)
        if session is None:
            session = Session(key=key)
        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Загрузить сессию из БД. Ошибка БД пробрасывается — без JSONL-отката."""
        with transaction() as conn:
            return self._load_inner(conn, key)

    def _load_inner(self, conn, key: str) -> Session | None:
        """Загрузить сессию из БД.

        Читает:
           1. Одну строку из ``agent_session_meta`` по session_key
           2. Все строки из ``agent_session_messages`` по session_key (ORDER BY seq)

        Собирает Session с распаковкой JSON-колонок.
        """
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
        """Сохранить сессию в БД. Ошибка БД пробрасывается — без JSONL-отката.

        Использует batch-INSERT (execute_values) для сообщений,
        что сокращает количество запросов с N+1 до 2-3.
        """
        with transaction() as conn:
            self._save_inner(conn, session)

    def _save_inner(self, conn, session: Session) -> None:
        """Сохранить сессию в БД.

        Алгоритм:
           1. UPSERT в agent_session_meta (UPDATE → если 0 rows → INSERT)
          2. DELETE всех старых сообщений сессии
          3. batch-INSERT всех текущих сообщений через ``execute_values``

        ``execute_values`` собирает все строки в один INSERT с множеством
        VALUES, что радикально reduces количество запросов.
        """
        metadata_val = session.metadata or {}
        updated_at = datetime.now()

        with conn.cursor() as cur:
            # UPSERT метаданных сессии
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

            # Удаляем старые сообщения сессии (заменяем целиком)
            cur.execute(
                f"DELETE FROM {self._fq_messages} WHERE session_key = %s",
                (session.key,),
            )

        # batch-INSERT всех сообщений одним запросом
        if session.messages:
            cols = _MESSAGE_COLUMNS
            all_cols = ["session_key", "seq", "role", "content", "msg_timestamp"] + list(cols)
            rows = []
            for seq, msg in enumerate(session.messages):
                row = [
                    session.key, seq,
                    msg.get("role", "user"),
                    msg.get("content", ""),
                    msg.get("timestamp"),
                ]
                for col in cols:
                    val = msg.get(col)
                    if isinstance(val, list) and col in _JSON_COLUMNS:
                        val = Json(val)
                    row.append(val)
                rows.append(row)

            with conn.cursor() as cur:
                execute_values(
                    cur,
                    f"INSERT INTO {self._fq_messages} ({', '.join(all_cols)}) VALUES %s",
                    rows,
                    page_size=500,
                )

    def invalidate(self, key: str) -> None:
        """Удалить сессию из кеша (не из БД)."""
        self._cache.pop(key, None)

    def delete_session(self, key: str) -> bool:
        """Удалить сессию из БД и из кеша. Ошибка БД пробрасывается.

        Удаляет сначала сообщения, потом meta — это необходимо для
        Greenplum 6.25, где внешние ключи не поддерживаются.
        """
        self.invalidate(key)
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self._fq_messages} WHERE session_key = %s",
                    (key,),
                )
                cur.execute(
                    f"DELETE FROM {self._fq_meta} WHERE session_key = %s",
                    (key,),
                )
                return cur.rowcount > 0

    def list_sessions(self) -> list[dict[str, Any]]:
        """Вернуть список всех сессий (meta + preview первого сообщения)."""
        with transaction() as conn:
            return self._list_sessions_inner(conn)

    def _list_sessions_inner(self, conn) -> list[dict[str, Any]]:
        """Внутренняя реализация list_sessions.

        Для каждой сессии читает:
          — метаданные (ключ, даты, заголовок)
          — превью (первые 10 сообщений, берёт первое непустое)
        """
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
        """Вернуть полный payload сессии (meta + все сообщения).

        Ошибка БД пробрасывается — без JSONL-отката.
        """
        session = self._load(key)
        if session is None:
            return None
        return self._session_payload(session)

    @staticmethod
    def _session_payload(session: Session) -> dict[str, Any]:
        """Сериализовать сессию в dict для HTTP-ответа."""
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }

    def flush_all(self) -> int:
        """Сохранить все закешированные сессии в БД.

        Используется при shutdown gateway. Если сохранение одной сессии
        упало, остальные всё равно сохраняются (ошибка логируется).
        """
        flushed = 0
        for key, session in list(self._cache.items()):
            try:
                self.save(session)
                flushed += 1
            except Exception:
                logger.warning("Failed to flush session {}", key, exc_info=True)
        return flushed

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _validate_ident(part: str) -> None:
        """Проверить, что часть идентификатора безопасна.

        Разрешены только буквы, цифры, подчёркивания и знак доллара.
        Если часть содержит другие символы — ValueError.
        """
        if not part or not part.replace("_", "").replace("$", "").isalnum():
            raise ValueError(f"Unsafe SQL identifier part: {part!r}")

    @staticmethod
    def _quote(ident: str) -> str:
        """Экранировать идентификатор (схема.таблица) кавычками.

        Пример: ``public.agent_session_messages`` → ``"public"."agent_session_messages"``

        Вызывает ``ValueError``, если любая часть идентификатора содержит
        недопустимые символы (защита от SQL injection).
        """
        parts = ident.split(".")
        for part in parts:
            PGSessionManager._validate_ident(part)
        return ".".join(f'"{p}"' for p in parts)
