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
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

import asyncpg


class _SharedDB:
    def __init__(self):
        self._dsn: str = ""
        self._pool: Optional[asyncpg.Pool] = None
        self._sync_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="db_sync"
        )

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    def configure(self, dsn: str) -> None:
        """Задать DSN (вызывается из gateway.py при старте)."""
        self._dsn = dsn
        self._close()

    def _close(self) -> None:
        if self._pool is not None:
            self._pool.terminate()
            self._pool = None

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            if not self._dsn:
                raise RuntimeError(
                    "SharedDB не инициализирован: вызовите db.configure(dsn) "
                    "или заполните pg.dsn в gateway_settings.py"
                )
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn, min_size=1, max_size=1
            )
        return self._pool

    # ------------------------------------------------------------------
    # Транзакция (асинхронная)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self):
        """Асинхронная транзакция: все операции в одном подключении.

        Пример::

            async with db.transaction() as conn:
                row = await conn.fetchrow("SELECT ... FOR UPDATE", arg)
                await conn.execute("UPDATE ...", arg)
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    # ------------------------------------------------------------------
    # Простые запросы (асинхронные)
    # ------------------------------------------------------------------

    async def execute(
        self, sql: str, *args: Any
    ) -> Optional[str]:
        """INSERT/UPDATE/DELETE — вернуть статус."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        """SELECT — вернуть список строк."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def fetchone(self, sql: str, *args: Any) -> Optional[asyncpg.Record]:
        """SELECT — вернуть одну строку."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    # ------------------------------------------------------------------
    # Синхронные обёртки (для PGSessionManager)
    # ------------------------------------------------------------------

    def _run_sync(self, coro_factory: Callable[[], Any]) -> Any:
        """Запустить корутину синхронно в отдельном потоке со своим event loop.

        Нужен чтобы не блокировать работающий event loop основного потока.
        """
        future = self._sync_executor.submit(
            lambda: asyncio.run(coro_factory())
        )
        return future.result()

    def sync_execute(self, sql: str, *args: Any) -> Optional[str]:
        return self._run_sync(lambda: self.execute(sql, *args))

    def sync_fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        return self._run_sync(lambda: self.fetch(sql, *args))

    def sync_fetchone(self, sql: str, *args: Any) -> Optional[asyncpg.Record]:
        return self._run_sync(lambda: self.fetchone(sql, *args))

    def sync_transaction(
        self, async_fn: Callable[[asyncpg.Connection], Any]
    ) -> Any:
        """Запустить async_fn(connection) в синхронном контексте.

        async_fn получает asyncpg.Connection и может делать несколько
        запросов в одной транзакции.
        """
        async def _wrapper():
            async with self.transaction() as conn:
                return await async_fn(conn)

        return self._run_sync(_wrapper)


db = _SharedDB()
