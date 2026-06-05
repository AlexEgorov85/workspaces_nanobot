import threading
import asyncio
from contextlib import contextmanager
from typing import Any, Callable, Optional

import psycopg2
import psycopg2.extras


class _SharedDB:
    """Единый коннектор к PostgreSQL для gateway, каналов и навыков.

    Использует один psycopg2-коннекшн с блокировкой (threading.Lock).
    В каждый момент времени выполняется не более одного запроса.

    Первый вызов любого метода инициализирует коннекшн из настроек.

    Пример (синхронный)::

        from utils.db import db
        rows = db.fetch("SELECT * FROM my_table")

    Пример (асинхронный)::

        rows = await db.afetch("SELECT * FROM my_table")

    Пример (транзакция)::

        with db.connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM t")
            cur.execute("INSERT INTO t VALUES (1)")
            conn.commit()
    """

    def __init__(self):
        self._dsn: str = ""
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    def configure(self, dsn: str) -> None:
        """Задать DSN (вызывается из gateway.py при старте)."""
        self._dsn = dsn
        self._close()

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _ensure(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            if not self._dsn:
                raise RuntimeError(
                    "SharedDB не инициализирован: вызовите db.configure(dsn) "
                    "или заполните pg.dsn в gateway_settings.py"
                )
            self._conn = psycopg2.connect(self._dsn)
            # Оставляем autocommit=False — вызывающий сам управляет commit/rollback
        return self._conn

    # ------------------------------------------------------------------
    # Сырой коннекшн (для транзакций)
    # ------------------------------------------------------------------

    @contextmanager
    def connection(self) -> psycopg2.extensions.connection:
        """Контекстный менеджер: заблокированный коннекшн для транзакций.

        Вызывающий сам делает commit / rollback::

            with db.connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM t WHERE id = %s", (1,))
                conn.commit()
        """
        with self._lock:
            yield self._ensure()

    async def aconnection(self):
        """Асинхронный контекстный менеджер (обёртка вокруг sync connection)."""
        loop = asyncio.get_running_loop()
        conn = await loop.run_in_executor(None, self._lock_acquire)
        try:
            yield conn
        finally:
            self._lock.release()

    def _lock_acquire(self):
        self._lock.acquire()
        return self._ensure()

    # ------------------------------------------------------------------
    # Простые запросы (авто-commit)
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: Optional[list] = None) -> Optional[list[tuple]]:
        """Выполнить SQL, вернуть строки если есть (sync)."""
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                conn.commit()
                if cur.description:
                    return cur.fetchall()
                return None

    def fetch(self, sql: str, params: Optional[list] = None) -> list[dict[str, Any]]:
        """SELECT — вернуть список строк как dict (sync)."""
        with self.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                conn.commit()
                return cur.fetchall()

    def fetchone(self, sql: str, params: Optional[list] = None) -> Optional[dict[str, Any]]:
        """SELECT — вернуть одну строку как dict (sync)."""
        with self.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                conn.commit()
                return cur.fetchone()

    # ------------------------------------------------------------------
    # Многошаговые операции
    # ------------------------------------------------------------------

    def with_connection(self, fn: Callable, *args, **kwargs) -> Any:
        """Выполнить fn(conn, *args, **kwargs) под блокировкой с auto-commit.

        Все операции внутри fn выполняются атомарно (один захват блокировки).
        fn получает сырой psycopg2-connection и должна сама делать commit/rollback.
        """
        with self.connection() as conn:
            return fn(conn, *args, **kwargs)

    async def awith_connection(self, fn: Callable, *args, **kwargs) -> Any:
        """Асинхронная версия with_connection."""
        return await asyncio.to_thread(self.with_connection, fn, *args, **kwargs)

    # ------------------------------------------------------------------
    # Асинхронные обёртки
    # ------------------------------------------------------------------

    async def aexecute(self, sql: str, params: Optional[list] = None) -> Optional[list[tuple]]:
        return await asyncio.to_thread(self.execute, sql, params)

    async def afetch(self, sql: str, params: Optional[list] = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.fetch, sql, params)

    async def afetchone(self, sql: str, params: Optional[list] = None) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self.fetchone, sql, params)


db = _SharedDB()
