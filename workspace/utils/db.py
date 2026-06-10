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
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

import asyncpg


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
        """Задать DSN и параметры пула (вызывается из gateway.py при старте)."""
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

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            if not self._dsn:
                raise RuntimeError(
                    "SharedDB не инициализирован: вызовите db.configure(dsn) "
                    "или заполните pg.dsn в gateway_settings.py"
                )
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=self._pool_min,
                max_size=self._pool_max,
                init=self._init_jsonb,
            )
        return self._pool

    async def _get_conn(self) -> asyncpg.Connection:
        """Создать отдельное подключение (не из пула) — для sync-операций."""
        if not self._dsn:
            raise RuntimeError(
                "SharedDB не инициализирован: вызовите db.configure(dsn) "
                "или заполните pg.dsn в gateway_settings.py"
            )
        conn = await asyncpg.connect(dsn=self._dsn)
        await self._init_jsonb(conn)
        return conn

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
    # Используют отдельный коннекшн (не из пула) в своём event loop,
    # чтобы не пересекаться с async-потоком.
    # ------------------------------------------------------------------

    def _run_sync(self, coro_factory: Callable[[], Any]) -> Any:
        """Запустить корутину синхронно в executor-треде со своим event loop.

        Нужен чтобы не блокировать работающий event loop основного потока.
        Создаёт отдельный asyncpg-коннекшн (не из пула) — thread-safe.
        """
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
        """Запустить async_fn(connection) в синхронном контексте.

        Создаёт отдельное подключение (не из пула), выполняет
        async_fn внутри транзакции, закрывает подключение.
        """
        async def _run():
            conn = await self._get_conn()
            try:
                async with conn.transaction():
                    return await async_fn(conn)
            finally:
                await conn.close()

        return self._run_sync(_run)


db = _SharedDB()
