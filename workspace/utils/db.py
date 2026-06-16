"""
Единый коннектор к PostgreSQL / Greenplum через psycopg2.

Все операции создают короткоживущие соединения (connect → query → close).
Пул ThreadedConnectionPool убран — он вызывал "double free" на Windows
при работе с asyncio.

Синхронный API — основной.
Асинхронный API — надстройка через ``asyncio.to_thread``.

Пример::

    from utils.db import configure, execute, fetchone

    configure("postgresql://user:pass@localhost:5432/mydb")
    execute("INSERT INTO t (x) VALUES (%s)", 42)
    row = fetchone("SELECT * FROM t WHERE id = %s", 1)
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Callable, Optional

import psycopg2
import psycopg2.extras
import psycopg2.extensions

# Глобальный адаптер: psycopg2 автоматически сериализует dict → JSONB
psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)

logger = logging.getLogger(__name__)

_MAX_RETRIES = 15
_RETRY_DELAY = 1.0
_RETRY_MAX_DELAY = 15.0

DB_RETRYABLE_ERRORS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
    ConnectionError,
    OSError,
)

_dsn: str = ""


def configure(dsn: str) -> None:
    """Настроить DSN для подключения к БД.

    Вызов с тем же DSN — idempotent (повторный вызов игнорируется).
    """
    global _dsn
    if dsn and dsn != _dsn:
        _dsn = dsn


def _connect() -> psycopg2.extensions.connection:
    """Создать новое соединение с БД с автоматическими retry.

    Регистрирует JSON-адаптер и включает autocommit.

    Retry-логика:
      — "too many connections": до 50 раз, backoff 2→30с
      — остальные ошибки: до 15 раз, backoff 1→15с
    """
    if not _dsn:
        raise RuntimeError(
            "SharedDB не инициализирован: вызовите configure(dsn) "
            "или заполните pg.dsn в gateway_settings.py"
        )
    # libpq в psycopg2-binary (≥ 2.9.x) пытается использовать
    # GSSAPI-шифрование по умолчанию, но GP 6.25 / PG 9.4 его не
    # поддерживают, что вызывает ошибку соединения.
    delay = _RETRY_DELAY
    attempt = 0
    max_retries = _MAX_RETRIES
    while True:
        try:
            conn = psycopg2.connect(_dsn, gssencmode="disable")
        except DB_RETRYABLE_ERRORS as e:
            attempt += 1
            msg = str(e).lower()
            is_too_many = "too many connections" in msg
            if is_too_many:
                max_retries = max(max_retries, 50)
                delay = min(delay * 2, 30.0)
            else:
                delay = min(delay * 1.5, _RETRY_MAX_DELAY)
            if attempt >= max_retries:
                raise
            label = "too_many" if is_too_many else "other"
            logger.warning(
                "DB connect retry %d/%d (%s) after %.1fs: %s",
                attempt, max_retries, label, delay, e,
            )
            time.sleep(delay)
            continue
        except Exception as e:
            logger.error("DB connect non-retryable error: %s", e)
            raise
        try:
            psycopg2.extras.register_json(conn, globally=False)
            conn.autocommit = True
            return conn
        except Exception:
            conn.close()
            raise


def _disconnect(conn: psycopg2.extensions.connection) -> None:
    """Закрыть соединение с БД."""
    conn.close()


def _retry(fn: Callable[[], Any]) -> Any:
    """Повторить вызов fn при ошибках БД с exponential backoff.

    Повторяет до ``_MAX_RETRIES`` (15) раз.
    Начальная задержка ``_RETRY_DELAY`` (1с), удваивается до ``_RETRY_MAX_DELAY`` (15с).

    Исключения, не входящие в ``DB_RETRYABLE_ERRORS``, пробрасываются сразу.
    """
    last_exc = None
    delay = _RETRY_DELAY
    attempt = 0
    while True:
        try:
            return fn()
        except DB_RETRYABLE_ERRORS as e:
            last_exc = e
            attempt += 1
            if attempt >= _MAX_RETRIES:
                raise
            logger.warning("DB retry %d/%d after %.1fs: %s",
                           attempt, _MAX_RETRIES, delay, e)
            time.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY)


# ---------------------------------------------------------------------------
# Sync API
# ---------------------------------------------------------------------------

def execute(sql: str, *args: Any) -> Optional[str]:
    """Выполнить INSERT/UPDATE/DELETE, вернуть command tag (например 'INSERT 0 1')."""
    def _work():
        conn = None
        try:
            conn = _connect()
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.statusmessage
        finally:
            if conn:
                _disconnect(conn)
    return _retry(_work)


def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
    """Выполнить SELECT, вернуть список строк как dict (ключ → значение)."""
    def _work():
        conn = None
        try:
            conn = _connect()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, args)
                return [dict(r) for r in cur.fetchall()]
        finally:
            if conn:
                _disconnect(conn)
    return _retry(_work)


def fetchone(sql: str, *args: Any) -> Optional[dict[str, Any]]:
    """Выполнить SELECT, вернуть одну строку как dict или None."""
    def _work():
        conn = None
        try:
            conn = _connect()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, args)
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            if conn:
                _disconnect(conn)
    return _retry(_work)


def fetchval(sql: str, *args: Any) -> Any:
    """Выполнить SELECT, вернуть первую колонку первой строки или None."""
    def _work():
        conn = None
        try:
            conn = _connect()
            with conn.cursor() as cur:
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            if conn:
                _disconnect(conn)
    return _retry(_work)


@contextmanager
def transaction():
    """Синхронный контекстный менеджер транзакции.

    Атомарно выполняет группу операций:
      — при успехе: ``conn.commit()``
      — при ошибке: ``conn.rollback()`` (пробрасывает исключение)

    Всегда возвращает соединение в пул (или закрывает) в ``finally``.
    """
    conn = _connect()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _disconnect(conn)


# ---------------------------------------------------------------------------
# Async API (wraps sync in asyncio.to_thread)
# ---------------------------------------------------------------------------

class _AsyncConnectionWrapper:
    """Обёртка синхронного psycopg2-соединения для async-кода.

    Каждый метод выполняет запрос в пуле потоков (``asyncio.to_thread``),
    чтобы не блокировать event loop.
    """

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        def _work():
            with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, args)
                return [dict(r) for r in cur.fetchall()]
        return await asyncio.to_thread(_work)

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        def _work():
            with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, args)
                row = cur.fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_work)

    async def execute(self, sql: str, *args: Any) -> Optional[str]:
        def _work():
            with self._conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.statusmessage
        return await asyncio.to_thread(_work)


@asynccontextmanager
async def async_transaction():
    """Асинхронный контекстный менеджер транзакции.

    Создаёт синхронное соединение, оборачивает в ``_AsyncConnectionWrapper``,
    все операции выполняются через ``asyncio.to_thread``.

    При успехе → ``conn.commit()``, при ошибке → ``conn.rollback()``.
    """
    conn = _connect()
    conn.autocommit = False
    wrapper = _AsyncConnectionWrapper(conn)
    try:
        yield wrapper
        await asyncio.to_thread(conn.commit)
    except Exception:
        await asyncio.to_thread(conn.rollback)
        raise
    finally:
        await asyncio.to_thread(_disconnect, conn)


async def async_execute(sql: str, *args: Any) -> Optional[str]:
    """Асинхронный вариант ``execute`` — через asyncio.to_thread."""
    return await asyncio.to_thread(execute, sql, *args)


async def async_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
    """Асинхронный вариант ``fetch`` — через asyncio.to_thread."""
    return await asyncio.to_thread(fetch, sql, *args)


async def async_fetchone(sql: str, *args: Any) -> Optional[dict[str, Any]]:
    """Асинхронный вариант ``fetchone`` — через asyncio.to_thread."""
    return await asyncio.to_thread(fetchone, sql, *args)


async def async_fetchval(sql: str, *args: Any) -> Any:
    """Асинхронный вариант ``fetchval`` — через asyncio.to_thread."""
    return await asyncio.to_thread(fetchval, sql, *args)
