"""
Единый коннектор к PostgreSQL / Greenplum через psycopg2.

Архитектура — «одна очередь + пул соединений» (вместо connect-per-op):

  * все подсистемы (PostgresChannel, PGSessionManager, DbLoggingService,
    AuditSyncService, Streamlit, инструменты) шлют задачи в ОДНУ общую
    job-очередь;
  * пул воркеров (1..N, по умолчанию 1) разбирает очередь; каждый воркер
    владеет единственным psycopg2-соединением и выполняет задачи
    последовательно;
  * ``transaction()`` / ``async_transaction()`` получают ЭКСКЛЮЗИВНУЮ аренду
    конкретного соединения (lease_id): пока транзакция открыта, это
    соединение не берёт чужие задачи;
  * при обрыве соединение закрывается и пересоздаётся с backoff — без
    «шторма» из десятков параллельных connect;
  * воркер без живого соединения (не смог подключиться после
    ``connect_max_retries``) не отнимает задачи у подключённых: он берёт
    обычную задачу, только когда в пуле нет ни одного воркера с живым
    соединением. При полной недоступности БД задачи быстро падают с ошибкой,
    а не висят в очереди вечно.

Пул ограничен ``channels.postgres.pool`` (min_conn / max_conn / pool_timeout),
поэтому на сервере никогда не бывает больше ``max_conn`` одновременных
подключений с этого процесса — проблема "too many connections" решена
на уровне архитектуры, а не ретраями.

Синхронный API — основной. Асинхронный API — надстройка через
``asyncio.to_thread``.

Пример::

    from utils.db import configure, execute, fetchone, transaction

    configure("postgresql://user:pass@localhost:5432/mydb")
    execute("INSERT INTO t (x) VALUES (%s)", 42)
    row = fetchone("SELECT * FROM t WHERE id = %s", 1)
    with transaction() as conn:
        conn.execute("UPDATE t SET x = %s WHERE id = %s", 7, 1)

ВАЖНО: функции, выполняемые внутри ``run(fn)`` / job, работают с сырым
psycopg2-соединением в воркер-потоке и НЕ должны вызывать публичный API
этого модуля (иначе тупик: воркер ждёт сам себя).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Callable, Deque, Dict, List, Optional

import psycopg2
import psycopg2.extras
import psycopg2.extensions

# Глобальный адаптер: psycopg2 автоматически сериализует dict → JSONB
psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)

logger = logging.getLogger(__name__)

DB_RETRYABLE_ERRORS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
    ConnectionError,
    OSError,
)

_dsn: str = ""

# ---------------------------------------------------------------------------
# Пул (конфигурация)
# ---------------------------------------------------------------------------

_DEFAULT_POOL = {
    "min_conn": 1,
    "max_conn": 4,
    "pool_timeout": 5.0,
    "queue_maxsize": 10000,
    "reconnect_backoff_sec": 1.0,
    "reconnect_backoff_max_sec": 60.0,
    "connect_max_retries": 5,
    "idle_timeout_sec": 60.0,
    "job_max_retries": 3,
}

_pool_cfg: Dict[str, Any] = dict(_DEFAULT_POOL)


def set_pool_config(cfg: dict) -> None:
    """Переопределить параметры пула (min_conn/max_conn/pool_timeout/...).

    Применяется до первого вызова воркера; уже созданный пул не ресайзится
    автоматически (для смены размера вызывайте ``start()`` заново).
    """
    global _pool_cfg
    merged = dict(_DEFAULT_POOL)
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k in merged and v is not None:
                merged[k] = v
    _pool_cfg = merged


# ---------------------------------------------------------------------------
# Job-очередь
# ---------------------------------------------------------------------------


class _JobResult:
    """Однократный контейнер результата: воркер кладёт значение/ошибку."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value: Any = None
        self._error: Optional[BaseException] = None

    def set_result(self, value: Any) -> None:
        self._value = value
        self._event.set()

    def set_error(self, exc: BaseException) -> None:
        self._error = exc
        self._event.set()

    def get(self) -> Any:
        self._event.wait()
        if self._error is not None:
            raise self._error
        return self._value


class _Job:
    """Задача для воркера. ``lease_id != 0`` — задача эксклюзивной транзакции."""

    __slots__ = ("fn", "lease_id", "result", "retries")

    def __init__(
        self,
        fn: Callable[[Any], Any],
        lease_id: int = 0,
        result: Optional[_JobResult] = None,
    ) -> None:
        self.fn = fn
        self.lease_id = lease_id
        self.result = result or _JobResult()
        self.retries = 0


class PoolTimeoutError(RuntimeError):
    """Не удалось получить свободное соединение пула за pool_timeout."""


# ---------------------------------------------------------------------------
# Воркер (владелец одного соединения)
# ---------------------------------------------------------------------------


class _Worker(threading.Thread):
    def __init__(self, manager: "DBManager", index: int) -> None:
        super().__init__(name=f"db-pool-{index}", daemon=True)
        self._manager = manager
        self._index = index
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._lease_id: int = 0
        self._cursors: Dict[int, Any] = {}
        self._next_cursor_id: int = 0
        self._idle_since: Optional[float] = None
        self._connect_error: Optional[BaseException] = None

    # -- соединение ---------------------------------------------------------

    def _ensure_connected(self) -> bool:
        """Подключиться (если нет) с backoff. True — соединение живо.

        При неудаче ``connect_max_retries`` попыток — сдаёмся: job получит
        ошибку, воркер останется неподключённым (следующий job попробует снова).
        Так сервис, ждущий ``run()``, не блокируется навсегда при недоступной БД.
        """
        if self._conn is not None and not self._conn.closed:
            return True
        dsn = self._manager._dsn
        if not dsn:
            self._connect_error = RuntimeError(
                "DB пул не инициализирован: вызовите configure(dsn)"
            )
            return False
        backoff = self._manager._reconnect_backoff
        attempts = 0
        while attempts < self._manager._connect_max_retries:
            try:
                self._conn = psycopg2.connect(dsn, gssencmode="disable")
                self._conn.autocommit = True
                psycopg2.extras.register_json(self._conn, globally=False)
                self._manager._stats["connected"] += 1
                return True
            except Exception as exc:
                self._manager._stats["connect_errors"] += 1
                attempts += 1
                self._connect_error = exc
                if attempts >= self._manager._connect_max_retries:
                    break
                if self._manager._stop.wait(backoff):
                    return False
                backoff = min(backoff * 2, self._manager._reconnect_backoff_max)
                logger.warning(
                    "db-pool worker %d connect failed (%d/%d, retry %.1fs): %s",
                    self._index, attempts, self._manager._connect_max_retries,
                    backoff, exc,
                )
        return False

    def _drop_connection(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        self._cursors.clear()
        self._manager._stats["reconnects"] += 1

    # -- курсоры (для прокси transaction) -----------------------------------

    def _open_cursor(self, conn: Any, args: tuple, kwargs: dict) -> int:
        self._next_cursor_id += 1
        cid = self._next_cursor_id
        self._cursors[cid] = conn.cursor(*args, **kwargs)
        return cid

    def _cursor(self, cid: int) -> Any:
        cur = self._cursors.get(cid)
        if cur is None:
            raise RuntimeError(f"DB cursor {cid} not found on worker {self._index}")
        return cur

    def _close_cursor(self, cid: int) -> None:
        cur = self._cursors.pop(cid, None)
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass

    # -- главный цикл -------------------------------------------------------

    def run(self) -> None:
        while True:
            if not self._manager._running and not self._manager._queue:
                break
            job = self._manager._take_job(self)
            if job is None:
                # без работы: если воркеров больше min_conn — освобождаем пул
                if self._manager._maybe_shrink(self):
                    break
                continue
            self._execute_job(job)
        self._drop_connection()

    def _execute_job(self, job: _Job) -> None:
        try:
            if not self._ensure_connected():
                job.result.set_error(
                    self._connect_error
                    or RuntimeError("DB пул не инициализирован")
                )
                return
            try:
                value = job.fn(self._conn)
                job.result.set_result(value)
            except DB_RETRYABLE_ERRORS as exc:
                self._drop_connection()
                # транзакции не переподключаем — она уже сломана
                if job.lease_id == 0 and job.retries < self._manager._job_max_retries:
                    job.retries += 1
                    self._manager._requeue(job)
                else:
                    job.result.set_error(exc)
            except Exception as exc:
                job.result.set_error(exc)
        except Exception as exc:
            job.result.set_error(exc)


# ---------------------------------------------------------------------------
# Менеджер (одна очередь + пул воркеров)
# ---------------------------------------------------------------------------


class DBManager:
    def __init__(self, dsn: str = "") -> None:
        self._dsn = dsn
        self._queue: Deque[_Job] = collections.deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._workers: List[_Worker] = []
        self._running = False
        self._started = False
        self._lease_seq = 0
        self._lease_workers: Dict[int, _Worker] = {}

        self._min_conn = int(_pool_cfg.get("min_conn", 1))
        self._max_conn = int(_pool_cfg.get("max_conn", 4))
        self._pool_timeout = float(_pool_cfg.get("pool_timeout", 5.0))
        self._queue_maxsize = int(_pool_cfg.get("queue_maxsize", 10000))
        self._reconnect_backoff = float(_pool_cfg.get("reconnect_backoff_sec", 1.0))
        self._reconnect_backoff_max = float(
            _pool_cfg.get("reconnect_backoff_max_sec", 60.0)
        )
        self._idle_timeout = float(_pool_cfg.get("idle_timeout_sec", 60.0))
        self._job_max_retries = int(_pool_cfg.get("job_max_retries", 3))
        self._connect_max_retries = int(_pool_cfg.get("connect_max_retries", 5))
        self._lifecycle_lock = threading.Lock()

        self._stats = {
            "connected": 0,
            "connect_errors": 0,
            "reconnects": 0,
            "jobs": 0,
            "lease_acquired": 0,
        }
        self._stats_lock = threading.Lock()

    # -- жизненный цикл -----------------------------------------------------

    def start(self) -> "DBManager":
        if self._started:
            return self
        with self._lifecycle_lock:
            if self._started:
                return self
            self._running = True
            self._started = True
            self._stop.clear()
            for _ in range(self._min_conn):
                self._spawn_worker()
        return self

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            with self._cond:
                self._running = False
                self._stop.set()
                self._cond.notify_all()
            for w in list(self._workers):
                w.join(timeout=2.0)
            self._workers.clear()
            self._started = False

    def _spawn_worker(self) -> _Worker:
        w = _Worker(self, len(self._workers))
        self._workers.append(w)
        w.start()
        return w

    def _maybe_shrink(self, worker: _Worker) -> bool:
        with self._cond:
            if (
                len(self._workers) > self._min_conn
                and worker._lease_id == 0
                and worker._idle_since is not None
                and time.monotonic() - worker._idle_since > self._idle_timeout
            ):
                self._workers.remove(worker)
                return True
        return False

    def _take_job(self, worker: _Worker) -> Optional[_Job]:
        """Выбрать задачу, которую может выполнить этот воркер.

        Воркер в эксклюзивной транзакции берёт только задачи своего lease_id.
        Свободный воркер — только обычные (lease_id == 0) задачи.
        Неподключённый воркер уступает очередь подключённым свободным
        воркерам: он берёт задачу только если в пуле нет ни одного воркера
        с живым соединением (иначе он лишь зря жжёт время на retry-connect,
        отнимая работу у живых). При полной недоступности БД задачи быстро
        падают с ошибкой подключения, а не ждут в очереди вечно.
        """
        with self._cond:
            deadline = time.monotonic() + 0.2
            while True:
                if not self._running and not self._queue:
                    return None
                connected_free_exists = any(
                    w is not worker
                    and w._lease_id == 0
                    and w._conn is not None
                    and not w._conn.closed
                    for w in self._workers
                )
                worker_connected = (
                    worker._conn is not None and not worker._conn.closed
                )
                for i, job in enumerate(self._queue):
                    if job.lease_id == 0:
                        if worker._lease_id == 0:
                            if not worker_connected and connected_free_exists:
                                continue
                            self._queue.remove(job)
                            self._stats["jobs"] += 1
                            worker._idle_since = None
                            return job
                    else:
                        if worker._lease_id == job.lease_id:
                            self._queue.remove(job)
                            self._stats["jobs"] += 1
                            worker._idle_since = None
                            return job
                if worker._idle_since is None:
                    worker._idle_since = time.monotonic()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def _requeue(self, job: _Job) -> None:
        with self._cond:
            self._queue.appendleft(job)
            self._cond.notify_all()

    def _submit(self, job: _Job) -> _JobResult:
        """Положить задачу в общую очередь и дождаться результата."""
        self._ensure_started()
        with self._cond:
            if self._queue_maxsize > 0:
                deadline = time.monotonic() + self._pool_timeout
                while len(self._queue) >= self._queue_maxsize:
                    if time.monotonic() >= deadline:
                        job.result.set_error(PoolTimeoutError(
                            "DB job queue full (queue_maxsize exceeded)"
                        ))
                        return job.result
                    self._cond.wait(0.2)
            self._queue.append(job)
            # Автомасштаб: если все воркеры заняты/зализированы, а в очереди
            # есть задачи — дорастим пул до max_conn (простаивающие потом
            # уходят по idle_timeout в _maybe_shrink).
            while len(self._workers) < self._max_conn:
                free = sum(1 for w in self._workers if w._lease_id == 0)
                if len(self._queue) <= free:
                    break
                self._spawn_worker()
            self._cond.notify_all()
        return job.result

    def _ensure_started(self) -> None:
        if not self._started:
            self.start()

    # -- транзакции: эксклюзивная аренда соединения -------------------------

    def _acquire_lease(self) -> int:
        self._ensure_started()
        with self._cond:
            deadline = time.monotonic() + self._pool_timeout
            while True:
                free = next((w for w in self._workers if w._lease_id == 0), None)
                if free is None and len(self._workers) < self._max_conn:
                    free = self._spawn_worker()
                if free is not None:
                    self._lease_seq += 1
                    lease_id = self._lease_seq
                    free._lease_id = lease_id
                    self._lease_workers[lease_id] = free
                    self._stats["lease_acquired"] += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolTimeoutError(
                        f"no free DB connection after {self._pool_timeout}s "
                        f"(pool {len(self._workers)}/{self._max_conn})"
                    )
                self._cond.wait(0.2)
        # перевод соединения в транзакционный режим — первый job этой аренды
        begin = _Job(lambda conn: self._begin_tx(conn), lease_id=lease_id)
        self._submit(begin)
        begin.result.get()
        return lease_id

    @staticmethod
    def _begin_tx(conn: Any) -> None:
        conn.autocommit = False

    @staticmethod
    def _end_tx(conn: Any, commit: bool) -> None:
        if commit:
            conn.commit()
        else:
            conn.rollback()
        conn.autocommit = True

    def _release_lease(self, lease_id: int, commit: bool) -> None:
        job = _Job(
            lambda conn: self._end_tx(conn, commit),
            lease_id=lease_id,
        )
        try:
            self._submit(job).get()
        finally:
            with self._cond:
                w = self._lease_workers.pop(lease_id, None)
                if w is not None:
                    w._lease_id = 0
                self._cond.notify_all()

    def get_stats(self) -> dict:
        with self._stats_lock:
            stats = dict(self._stats)
        with self._cond:
            stats["queue_size"] = len(self._queue)
            stats["workers"] = len(self._workers)
            stats["running"] = self._running
        stats["min_conn"] = self._min_conn
        stats["max_conn"] = self._max_conn
        stats["pool_timeout"] = self._pool_timeout
        stats["connect_max_retries"] = self._connect_max_retries
        return stats


# ---------------------------------------------------------------------------
# Прокси транзакций (execute/fetch/cursor → job в воркер)
# ---------------------------------------------------------------------------


_UNSUPPORTED_ESCAPES = ("\\u0000", "\\u0001", "\\u0002", "\\u0003")


def _sanitize_param(value: Any) -> Any:
    """Заменяет невалидные Unicode-escape'ы в строковых параметрах.

    psycopg2 парсит строки-параметры на наличие ``\\u0000``/и т.п. как
    Unicode-escape и падает с ``UntranslatableCharacter`` ещё до отправки
    в PostgreSQL (если в данных встречается, например, JSON-литерал
    ``"\\u0000..."``). Заменяем ``\\u0000`` на реальный NUL — это
    корректное и идемпотентное значение, безопасное и для psycopg2,
    и для PostgreSQL.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return value
    if isinstance(value, str) and any(seq in value for seq in _UNSUPPORTED_ESCAPES):
        return value.replace("\\u0000", "\u0000")
    return value


def _sanitize_params(params: Any) -> Any:
    if params is None:
        return None
    if isinstance(params, (tuple, list)):
        return tuple(_sanitize_param(p) for p in params)
    if isinstance(params, dict):
        return {k: _sanitize_param(v) for k, v in params.items()}
    return _sanitize_param(params)


class _CursorProxy:
    """Прокси psycopg2-курсора: каждая операция — job на соединение аренды."""

    def __init__(self, proxy: "_ConnectionProxy", cid: int) -> None:
        self._proxy = proxy
        self._cid = cid

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        return self._proxy._run(lambda conn: fn(self._proxy._worker._cursor(self._cid)))

    def __enter__(self) -> "_CursorProxy":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def connection(self) -> "_ConnectionProxy":
        return self._proxy

    @property
    def description(self) -> Any:
        return self._run(lambda cur: cur.description)

    @property
    def rowcount(self) -> int:
        return self._run(lambda cur: cur.rowcount)

    @property
    def statusmessage(self) -> Optional[str]:
        return self._run(lambda cur: cur.statusmessage)

    def execute(self, sql: str, params: Any = None) -> None:
        # NB: передаём params as-is (psycopg2 трактует None как «нет
        # параметров» и НЕ пытается делать %-форматирование). Если передать
        # `()`, psycopg2 выполнит подстановку и упадёт на литералах '%' в
        # данных (например, «16.7%» в контенте сообщения) — это ломает
        # ``execute_values`` для сессий с таким контентом.
        safe = _sanitize_params(params)
        self._run(lambda cur: cur.execute(sql, safe))

    def mogrify(self, sql: str, params: Any = None) -> bytes:
        safe = _sanitize_params(params)
        return self._run(lambda cur: cur.mogrify(sql, safe))

    def __iter__(self) -> Any:
        # Совместимость с psycopg2-протоколом: ``for row in cur:`` / ``list(cur)``.
        # Весь результат вытаскиваем одним job-ом через fetchall().
        return iter(self.fetchall())

    def fetchone(self) -> Any:
        return self._run(lambda cur: cur.fetchone())

    def fetchall(self) -> list:
        return self._run(lambda cur: cur.fetchall())

    def fetchmany(self, size: int = 100) -> list:
        return self._run(lambda cur: cur.fetchmany(size))

    def close(self) -> None:
        self._proxy._worker._close_cursor(self._cid)


class _ConnectionProxy:
    """Прокси соединения внутри транзакции (синхронный API)."""

    def __init__(self, manager: "DBManager", lease_id: int) -> None:
        self._manager = manager
        self._lease_id = lease_id
        self._worker = self._manager._lease_workers[lease_id]

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        job = _Job(fn, lease_id=self._lease_id)
        self._manager._submit(job)
        return job.result.get()

    @property
    def encoding(self) -> str:
        return self._run(lambda conn: conn.encoding)

    def cursor(self, *args: Any, **kwargs: Any) -> _CursorProxy:
        cid = self._run(lambda conn: self._worker._open_cursor(conn, args, kwargs))
        return _CursorProxy(self, cid)

    def execute(self, sql: str, *args: Any) -> Any:
        params = _sanitize_params(args if args else None)

        def _work(conn: Any) -> Any:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.statusmessage
        return self._run(_work)

    def fetch(self, sql: str, *args: Any) -> list:
        params = _sanitize_params(args if args else None)

        def _work(conn: Any) -> list:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        return self._run(_work)

    def fetchrow(self, sql: str, *args: Any) -> Optional[dict]:
        params = _sanitize_params(args if args else None)

        def _work(conn: Any) -> Optional[dict]:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None
        return self._run(_work)

    def fetchone(self, sql: str, *args: Any) -> Optional[dict]:
        return self.fetchrow(sql, *args)

    def fetchval(self, sql: str, *args: Any) -> Any:
        params = _sanitize_params(args if args else None)

        def _work(conn: Any) -> Any:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return row[0] if row else None
        return self._run(_work)


class _AsyncConnectionWrapper:
    """Обёртка синхронного прокси для async-кода (через asyncio.to_thread)."""

    def __init__(self, proxy: _ConnectionProxy) -> None:
        self._proxy = proxy

    async def fetch(self, sql: str, *args: Any) -> list:
        return await asyncio.to_thread(self._proxy.fetch, sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict]:
        return await asyncio.to_thread(self._proxy.fetchrow, sql, *args)

    async def execute(self, sql: str, *args: Any) -> Any:
        return await asyncio.to_thread(self._proxy.execute, sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await asyncio.to_thread(self._proxy.fetchval, sql, *args)


# ---------------------------------------------------------------------------
# Глобальный менеджер
# ---------------------------------------------------------------------------

_manager: Optional[DBManager] = None
_manager_lock = threading.Lock()


def _get_manager() -> DBManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = DBManager(dsn=resolve_dsn())
    if not _manager._started:
        _manager.start()
    return _manager


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def resolve_dsn() -> str:
    """Вернуть DSN: явный через configure(), иначе channels.postgres.dsn."""
    if _dsn:
        return _dsn
    try:
        from config import SETTINGS

        node = SETTINGS.get("channels", {}).get("postgres", {})
        if isinstance(node, dict):
            return node.get("dsn") or ""
    except Exception:
        pass
    return ""


def configure(dsn: str) -> None:
    """Настроить DSN для подключения к БД (идемпотентно)."""
    global _dsn
    if dsn and dsn != _dsn:
        _dsn = dsn
        if _manager is not None:
            _manager._dsn = dsn


def start() -> DBManager:
    """Запустить пул (воркеры подключаются лениво при первой задаче)."""
    return _get_manager().start()


def shutdown() -> None:
    """Остановить пул и закрыть все соединения."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()


def get_stats() -> dict:
    return _get_manager().get_stats()


def run(fn: Callable[[Any], Any]) -> Any:
    """Выполнить ``fn(conn)`` на свободном соединении пула (без транзакции).

    ``fn`` получает сырой psycopg2-conn в воркер-потоке; НЕ вызывайте внутри
    публичный API этого модуля (тупик).
    """
    job = _Job(fn)
    return _get_manager()._submit(job).get()


# ---------------------------------------------------------------------------
# Sync API
# ---------------------------------------------------------------------------


def execute(sql: str, *args: Any) -> Optional[str]:
    """Выполнить INSERT/UPDATE/DELETE, вернуть command tag."""
    params = _sanitize_params(args if args else None)

    def _work(conn: Any) -> Optional[str]:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.statusmessage
    return _get_manager()._submit(_Job(_work)).get()


def fetch(sql: str, *args: Any) -> list:
    """Выполнить SELECT, вернуть список строк как dict."""
    params = _sanitize_params(args if args else None)

    def _work(conn: Any) -> list:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    return _get_manager()._submit(_Job(_work)).get()


def fetchone(sql: str, *args: Any) -> Optional[dict]:
    """Выполнить SELECT, вернуть одну строку как dict или None."""
    params = _sanitize_params(args if args else None)

    def _work(conn: Any) -> Optional[dict]:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
    return _get_manager()._submit(_Job(_work)).get()


def fetchval(sql: str, *args: Any) -> Any:
    """Выполнить SELECT, вернуть первую колонку первой строки или None."""
    params = _sanitize_params(args if args else None)

    def _work(conn: Any) -> Any:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None
    return _get_manager()._submit(_Job(_work)).get()


@contextmanager
def transaction():
    """Синхронная транзакция: эксклюзивная аренда соединения пула.

    Внутри контекста возвращается прокси соединения; все операции уходят
    job-ами в воркер на том же соединении, чужие задачи в это время ждут.
    """
    manager = _get_manager()
    lease_id = manager._acquire_lease()
    proxy = _ConnectionProxy(manager, lease_id)
    try:
        yield proxy
    except BaseException:
        manager._release_lease(lease_id, commit=False)
        raise
    else:
        manager._release_lease(lease_id, commit=True)


# ---------------------------------------------------------------------------
# Async API (wraps sync in asyncio.to_thread)
# ---------------------------------------------------------------------------


async def async_execute(sql: str, *args: Any) -> Optional[str]:
    return await asyncio.to_thread(execute, sql, *args)


async def async_fetch(sql: str, *args: Any) -> list:
    return await asyncio.to_thread(fetch, sql, *args)


async def async_fetchone(sql: str, *args: Any) -> Optional[dict]:
    return await asyncio.to_thread(fetchone, sql, *args)


async def async_fetchval(sql: str, *args: Any) -> Any:
    return await asyncio.to_thread(fetchval, sql, *args)


@asynccontextmanager
async def async_transaction():
    """Асинхронная транзакция (см. ``transaction``).

    Возвращает async-обёртку прокси: ``await conn.fetch(...)`` и т.п.
    """
    manager = _get_manager()
    lease_id = await asyncio.to_thread(manager._acquire_lease)
    proxy = _ConnectionProxy(manager, lease_id)
    wrapper = _AsyncConnectionWrapper(proxy)
    try:
        yield wrapper
    except BaseException:
        await asyncio.to_thread(manager._release_lease, lease_id, False)
        raise
    else:
        await asyncio.to_thread(manager._release_lease, lease_id, True)
