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

# Ошибки соединения — ретраим и падаем на JSONL
DB_RETRYABLE_ERRORS = (
    asyncpg.CannotConnectNowError,
    asyncpg.ConnectionFailureError,
    asyncpg.ConnectionDoesNotExistError,
    OSError,
    ConnectionError,
)


class _SharedDB:
    def __init__(self):
        self._dsn: str = ""
        self._pool_min: int = 0
        self._pool_max: int = 1
        self._pool_acquire_timeout: int = 30
        self._pool: Optional[asyncpg.Pool] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def dsn(self) -> str:
        """Текущий DSN (пустая строка если configure() не вызывался)."""
        return self._dsn

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    def configure(self, dsn: str, min_size: int = 0, max_size: int = 1, acquire_timeout: int = 30) -> None:
        """Задать DSN и параметры пула (вызывается из gateway.py при старте).

        Args:
            dsn: Строка подключения.
            min_size: Минимум соединений в пуле.
            max_size: Максимум соединений в пуле (1 = один запрос в моменте).
            acquire_timeout: Секунд ждать освобождения коннекшена, затем
                             asyncio.TimeoutError / PoolAcquireTimeoutError.
        """
        self._dsn = dsn
        self._pool_min = min_size
        self._pool_max = max_size
        self._pool_acquire_timeout = acquire_timeout
        self._close()

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Вернуть сохранённый loop или получить текущий."""
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        if self._loop is None:
            raise RuntimeError(
                "SharedDB не инициализирован: вызовите db.configure(dsn) "
                "в асинхронном контексте"
            )
        return self._loop

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
            except DB_RETRYABLE_ERRORS as e:
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
                timeout=self._pool_acquire_timeout,
            )
        return self._pool

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
        """Запустить корутину и дождаться результата.

        * Если есть running event loop (gateway) — шедулит на главный loop
          через ``run_coroutine_threadsafe``, использует общий пул (1 коннекшн).
        * Если loop нет (streamlit, CLI) — закрывает старый пул (если есть),
          создаёт свежий во временном event loop, выполняет, закрывает.
        """
        try:
            loop = self._get_loop()
        except RuntimeError:
            self._close()
            return self._run_with_temp_pool(coro_factory)
        future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        return future.result()

    def _run_with_temp_pool(self, coro_factory: Callable[[], Any]) -> Any:
        """Создать пул во временном event loop, выполнить, закрыть."""
        async def _work():
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=0, max_size=1,
                init=self._init_jsonb,
                timeout=self._pool_acquire_timeout,
            )
            try:
                return await coro_factory()
            finally:
                await self._pool.close()
                self._pool = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(_work())).result()

    def sync_execute(self, sql: str, *args: Any) -> Optional[str]:
        return self._run_sync(lambda: self.execute(sql, *args))

    def sync_fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        return self._run_sync(lambda: self.fetch(sql, *args))

    def sync_fetchone(self, sql: str, *args: Any) -> Optional[asyncpg.Record]:
        return self._run_sync(lambda: self.fetchone(sql, *args))

    def sync_transaction(
        self, async_fn: Callable[[asyncpg.Connection], Any]
    ) -> Any:
        async def _run():
            async with self.transaction() as conn:
                return await async_fn(conn)

        return self._run_sync(_run)


db = _SharedDB()
