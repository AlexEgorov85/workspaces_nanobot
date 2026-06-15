"""
Единый коннектор к PostgreSQL через asyncpg.

Использует один пул asyncpg (min_size=1, max_size=1).
В каждый момент времени выполняется не более одного запроса.

Асинхронные методы (для PostgresChannel, db_analyzer)::

    from utils.db import db
    rows = await db.fetch("SELECT * FROM my_table")

Синхронные методы (для PGSessionManager)::

    rows = db.sync_fetch("SELECT * FROM my_table")

Транзакция (асинхронная)::

    async with db.transaction() as conn:
        row = await conn.fetchrow("SELECT ... FOR UPDATE", arg)
        await conn.execute("UPDATE ...", arg)

Параметры — в стиле asyncpg ($1, $2, ...).
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

_MAX_RETRIES = 10
_RETRY_DELAY = 1.0   # seconds, doubles each attempt
_RETRY_MAX_DELAY = 15.0

_RETRYABLE = (
    asyncpg.CannotConnectNowError,
    asyncpg.ConnectionFailureError,
    asyncpg.ConnectionDoesNotExistError,
    OSError,
    ConnectionError,
)


class _SharedDB:
    def __init__(self):
        self._dsn: str = ""
        self._pool_min: int = 1
        self._pool_max: int = 1
        self._pool: Optional[asyncpg.Pool] = None
        self._sync_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="db_sync"
        )

    @property
    def dsn(self) -> str:
        """Текущий DSN (пустая строка если configure() не вызывался)."""
        return self._dsn

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    def configure(self, dsn: str, min_size: int = 1, max_size: int = 1) -> None:
        """Задать DSN и параметры пула (вызывается из gateway.py при старте).

        Args:
            dsn: Строка подключения.
            min_size: Минимум соединений в пуле (для async-запросов).
            max_size: Максимум соединений в пуле (для async-запросов).
                      Очередь встроена в asyncpg — при max_size занятых
                      соединений acquire() ждёт освобождения.
        """
        self._dsn = dsn
        self._pool_min = min_size
        self._pool_max = max_size
        self._close()

    def _close(self) -> None:
        if self._pool is not None:
            self._pool.terminate()
            self._pool = None

    @staticmethod
    async def _init_jsonb(conn: asyncpg.Connection) -> None:
        """Зарегистрировать декодер JSONB → dict на подключении.

        asyncpg по умолчанию возвращает JSONB как str;
        этот кодек делает так, что все JSONB-колонки
        возвращаются как Python dict/list сразу.
        """
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    async def _retry(self, coro_factory):
        last_exc = None
        delay = _RETRY_DELAY
        for attempt in range(_MAX_RETRIES):
            try:
                return await coro_factory()
            except _RETRYABLE as e:
                last_exc = e
                if attempt < _MAX_RETRIES - 1:
                    logger.warning(
                        "DB retry %d/%d after %.1fs: %s",
                        attempt + 1, _MAX_RETRIES, delay, e,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _RETRY_MAX_DELAY)
        raise last_exc

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=self._pool_min,
                max_size=self._pool_max,
                init=self._init_jsonb,
            )
        return self._pool

    async def _get_conn(self) -> asyncpg.Connection:
        """Создать отдельное подключение (не из пула) — для sync-операций."""
        return await self._retry(self._do_connect)

    async def _do_connect(self) -> asyncpg.Connection:
        if not self._dsn:
            raise RuntimeError(
                "SharedDB не инициализирован: вызовите db.configure(dsn) "
                "или заполните pg.dsn в gateway_settings.py"
            )
        conn = await asyncpg.connect(dsn=self._dsn)
        try:
            await self._init_jsonb(conn)
            return conn
        except Exception:
            await conn.close()
            raise

    # ------------------------------------------------------------------
    # Транзакция (асинхронная)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self):
        """Асинхронная транзакция: все операции в одном подключении."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    # ------------------------------------------------------------------
    # Простые запросы (асинхронные)
    # ------------------------------------------------------------------

    async def execute(self, sql: str, *args: Any) -> Optional[str]:
        """INSERT/UPDATE/DELETE — вернуть статус."""
        return await self._retry(lambda: self._do_execute(sql, *args))

    async def _do_execute(self, sql: str, *args: Any) -> Optional[str]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        """SELECT — вернуть список строк."""
        return await self._retry(lambda: self._do_fetch(sql, *args))

    async def _do_fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def fetchone(self, sql: str, *args: Any) -> Optional[asyncpg.Record]:
        """SELECT — вернуть одну строку."""
        return await self._retry(lambda: self._do_fetchone(sql, *args))

    async def _do_fetchone(self, sql: str, *args: Any) -> Optional[asyncpg.Record]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        """SELECT — вернуть одно значение (первая колонка первой строки)."""
        return await self._retry(lambda: self._do_fetchval(sql, *args))

    async def _do_fetchval(self, sql: str, *args: Any) -> Any:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    # ------------------------------------------------------------------
    # Синхронные обёртки (для PGSessionManager)
    # Используют отдельное подключение в своём event loop (executor thread).
    # ------------------------------------------------------------------

    def _run_sync(self, coro_factory: Callable[[], Any]) -> Any:
        future = self._sync_executor.submit(
            lambda: asyncio.run(coro_factory())
        )
        return future.result()

    def sync_execute(self, sql: str, *args: Any) -> Optional[str]:
        async def _run():
            conn = await self._get_conn()
            try:
                return await conn.execute(sql, *args)
            finally:
                await conn.close()
        return self._run_sync(_run)

    def sync_fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        async def _run():
            conn = await self._get_conn()
            try:
                return await conn.fetch(sql, *args)
            finally:
                await conn.close()
        return self._run_sync(_run)

    def sync_fetchone(self, sql: str, *args: Any) -> Optional[asyncpg.Record]:
        async def _run():
            conn = await self._get_conn()
            try:
                return await conn.fetchrow(sql, *args)
            finally:
                await conn.close()
        return self._run_sync(_run)

    def sync_transaction(
        self, async_fn: Callable[[asyncpg.Connection], Any]
    ) -> Any:
        async def _run():
            conn = await self._get_conn()
            try:
                async with conn.transaction():
                    return await async_fn(conn)
            finally:
                await conn.close()

        return self._run_sync(_run)


db = _SharedDB()
