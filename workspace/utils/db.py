"""
Единый коннектор к PostgreSQL через psycopg2.
Короткоживущие соединения: connect -> query -> close.
Синхронный API — основной, асинхронный — надстройка через asyncio.to_thread.
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

# Allow passing plain dict as JSONB parameter
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
    global _dsn
    _dsn = dsn


def _connect() -> psycopg2.extensions.connection:
    if not _dsn:
        raise RuntimeError(
            "SharedDB не инициализирован: вызовите configure(dsn) "
            "или заполните pg.dsn в gateway_settings.py"
        )
    try:
        conn = psycopg2.connect(_dsn)
    except Exception:
        raise
    try:
        psycopg2.extras.register_json(conn, globally=False)
        conn.autocommit = True
        return conn
    except Exception:
        conn.close()
        raise


def _retry(fn: Callable[[], Any]) -> Any:
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
    """Execute INSERT/UPDATE/DELETE, return command tag (e.g. 'INSERT 0 1')."""
    def _work():
        conn = None
        try:
            conn = _connect()
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.statusmessage
        finally:
            if conn:
                conn.close()
    return _retry(_work)


def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
    """Execute SELECT, return list of dicts."""
    def _work():
        conn = None
        try:
            conn = _connect()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, args)
                return [dict(r) for r in cur.fetchall()]
        finally:
            if conn:
                conn.close()
    return _retry(_work)


def fetchone(sql: str, *args: Any) -> Optional[dict[str, Any]]:
    """Execute SELECT, return single dict or None."""
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
                conn.close()
    return _retry(_work)


def fetchval(sql: str, *args: Any) -> Any:
    """Execute SELECT, return first column of first row or None."""
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
                conn.close()
    return _retry(_work)


@contextmanager
def transaction():
    """Sync transaction context manager. Yields psycopg2 connection (autocommit=False)."""
    conn = _connect()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Async API (wraps sync in asyncio.to_thread)
# ---------------------------------------------------------------------------

class _AsyncConnectionWrapper:
    """Wraps a sync psycopg2 connection so it can be used from async code."""

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
    """Async transaction. Yields _AsyncConnectionWrapper with async methods."""
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
        await asyncio.to_thread(conn.close)


async def async_execute(sql: str, *args: Any) -> Optional[str]:
    return await asyncio.to_thread(execute, sql, *args)


async def async_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
    return await asyncio.to_thread(fetch, sql, *args)


async def async_fetchone(sql: str, *args: Any) -> Optional[dict[str, Any]]:
    return await asyncio.to_thread(fetchone, sql, *args)


async def async_fetchval(sql: str, *args: Any) -> Any:
    return await asyncio.to_thread(fetchval, sql, *args)
