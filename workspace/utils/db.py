"""
Единый коннектор к PostgreSQL через asyncpg.

Каждый запрос создаёт новое подключение и сразу его закрывает.
Нет пула — нет проблем с очередями, event loop, лимитом коннекшенов.
Если GP6 занят (too many connections), ретраим с backoff и ждём.

Асинхронные методы (PostgresChannel, db_analyzer)::

    from utils.db import fetch
    rows = await fetch("SELECT * FROM my_table")
    async with transaction() as conn:
        row = await conn.fetchrow("SELECT ... FOR UPDATE", arg)

Синхронные методы (PGSessionManager, streamlit)::

    from utils.db import sync_fetch
    rows = sync_fetch("SELECT * FROM my_table")
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

import asyncpg

logger = logging.getLogger(__name__)

_MAX_RETRIES = 10            # общие ошибки соединения
_MAX_RETRIES_TOO_MANY = 50   # too many connections (ждём пока освободится)
_RETRY_DELAY = 1.0
_RETRY_MAX_DELAY = 15.0

DB_RETRYABLE_ERRORS = (
    asyncpg.ConnectionFailureError,
    asyncpg.TooManyConnectionsError,
    OSError,
    ConnectionError,
)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_dsn: str = ""


def configure(dsn: str) -> None:
    """Задать DSN. Вызвать один раз при старте."""
    global _dsn
    _dsn = dsn


# ---------------------------------------------------------------------------
# Low-level connect / retry
# ---------------------------------------------------------------------------

async def _connect() -> asyncpg.Connection:
    """Создать новое подключение и зарегистрировать JSONB-кодек."""
    if not _dsn:
        raise RuntimeError(
            "SharedDB не инициализирован: вызовите configure(dsn) "
            "или заполните pg.dsn в gateway_settings.py"
        )
    conn = await asyncpg.connect(dsn=_dsn)
    try:
        await conn.set_type_codec("jsonb",
                                  encoder=json.dumps,
                                  decoder=json.loads,
                                  schema="pg_catalog")
        return conn
    except Exception:
        await conn.close()
        raise


async def _retry(coro_factory):
    """Выполнить coro_factory() с ретраем при ошибках соединения.

    * ``TooManyConnectionsError`` — до 50 попыток (ждём освобождения).
    * Остальные ошибки соединения — до 10 попыток.
    """
    last_exc = None
    delay = _RETRY_DELAY
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except DB_RETRYABLE_ERRORS as e:
            last_exc = e
            max_retries = _MAX_RETRIES_TOO_MANY if isinstance(e, asyncpg.TooManyConnectionsError) else _MAX_RETRIES
            attempt += 1
            if attempt >= max_retries:
                raise
            logger.warning("DB retry %d/%d after %.1fs: %s",
                           attempt, max_retries, delay, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _run_sync(coro_factory: Callable[[], Any]) -> Any:
    """Запустить корутину синхронно (через asyncio.run в треде)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


# ---------------------------------------------------------------------------
# Async API
# ---------------------------------------------------------------------------

async def execute(sql: str, *args: Any) -> Optional[str]:
    async def _work():
        conn = await _connect()
        try:
            return await conn.execute(sql, *args)
        finally:
            await conn.close()
    return await _retry(_work)


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    async def _work():
        conn = await _connect()
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()
    return await _retry(_work)


async def fetchone(sql: str, *args: Any) -> Optional[asyncpg.Record]:
    async def _work():
        conn = await _connect()
        try:
            return await conn.fetchrow(sql, *args)
        finally:
            await conn.close()
    return await _retry(_work)


async def fetchval(sql: str, *args: Any) -> Any:
    async def _work():
        conn = await _connect()
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()
    return await _retry(_work)


@asynccontextmanager
async def transaction():
    """Асинхронная транзакция: connect + yield + commit/rollback + close."""
    conn = await _connect()
    try:
        async with conn.transaction():
            yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Sync API  (для PGSessionManager, streamlit, CLI — где нет async)
# ---------------------------------------------------------------------------

def sync_execute(sql: str, *args: Any) -> Optional[str]:
    return _run_sync(lambda: execute(sql, *args))


def sync_fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    return _run_sync(lambda: fetch(sql, *args))


def sync_fetchone(sql: str, *args: Any) -> Optional[asyncpg.Record]:
    return _run_sync(lambda: fetchone(sql, *args))


def sync_transaction(async_fn: Callable[[asyncpg.Connection], Any]) -> Any:
    async def _run():
        async with transaction() as conn:
            return await async_fn(conn)
    return _run_sync(_run)



