"""Загрузчик снимка не превышает размер пула PostgreSQL.

Инвариант тот же, что у пула (``test_enterprise_data_db.py::test_pool_never_exceeds_max_conn``):
поток загрузки занимает соединение на всё время своего SQL, поэтому
``max_workers > max_conn`` — это ожидание слотов, которых нет. Загрузчик
ограничивает себя сам, в ``_effective_workers()``, а не полагается на то, что
вызывающий прочитал документацию.

Подход повторяет пуловый: сценарий, в котором превышение проявилось бы, плюс
проверка инварианта. Фиксируется не «сколько раз попали» (это ничего не
доказывает), а **пиковая одновременная занятость пула** и число потоков
исполнителя.
"""

from __future__ import annotations

import threading
import time
from typing import Any


from libs.enterprise_data.loader import SnapshotLoadService

TABLES = [f"oarb.t{i}" for i in range(8)]
MAX_CONN = 2


class CountingPool:
    """Пул, который считает одновременную занятость и задерживает работу.

    ``parties`` — сколько слотов ждём одновременно. Оно равно числу потоков
    загрузки: пока нужное число не набралось, вызовы висят на событии, и пик
    измеряется на настоящем пересечении, а не на удаче планировщика.
    """

    def __init__(self, max_conn: int = MAX_CONN, parties: int = 1) -> None:
        self._max_conn = max_conn
        self._parties = max(1, parties)
        self._lock = threading.Lock()
        self._inside = 0
        self._full = threading.Event()
        self.configured: list[str] = []
        self.peak = 0
        self.calls = 0
        self.threads: set[str] = set()

    def configure(self, dsn: str) -> None:
        self.configured.append(dsn)

    def run(self, fn):
        with self._lock:
            self._inside += 1
            self.calls += 1
            self.peak = max(self.peak, self._inside)
            self.threads.add(threading.current_thread().name)
            if self._inside >= self._parties:
                self._full.set()
        try:
            # Ждём, пока слоты действительно конкурируют: без этого пик мог бы
            # случайно совпасть с max_conn и тест прошёл бы на сломанном коде.
            self._full.wait(timeout=5)
            time.sleep(0.01)
            return fn(_EmptyConn())
        finally:
            with self._lock:
                self._inside -= 1


class _EmptyCursor:
    def execute(self, sql, params=None) -> None:
        return None

    def fetchall(self) -> list:
        return []

    def fetchone(self) -> None:
        return None

    def close(self) -> None:
        return None


class _EmptyConn:
    def cursor(self, cursor_factory=None) -> _EmptyCursor:
        return _EmptyCursor()

    def close(self) -> None:
        return None


class NullStore:
    def ensure_schema(self, table: str, columns, schema_meta=None) -> bool:
        return True

    def replace_records(self, table: str, records) -> bool:
        return True

    def upsert_records(self, table: str, records, *, key_column=None) -> bool:
        raise AssertionError("загрузчик не должен вызывать частичную запись")


class TestMaxWorkersBound:
    def test_effective_workers_never_exceeds_pool(self) -> None:
        s = SnapshotLoadService(
            store=NullStore(),  # type: ignore[arg-type]
            tables=list(TABLES),
            max_workers=8,
            max_conn=2,
        )
        assert s._effective_workers() == 2
        assert s.get_stats()["max_workers"] == 2
        assert s.get_stats()["max_conn"] == 2

    def test_bounded_by_pool_even_if_caller_asks_for_more(self) -> None:
        """Вызывающий, прочитавший не тот параметр, не может превысить пул."""
        s = SnapshotLoadService(
            store=NullStore(),  # type: ignore[arg-type]
            tables=list(TABLES),
            max_workers=64,
            max_conn=1,
        )
        assert s._effective_workers() == 1

    def test_not_more_than_table_count(self) -> None:
        s = SnapshotLoadService(
            store=NullStore(),  # type: ignore[arg-type]
            tables=["oarb.audits"],
            max_workers=8,
            max_conn=8,
        )
        assert s._effective_workers() == 1

    def test_single_worker_runs_in_caller_thread(self) -> None:
        """``max_conn=1`` → загрузка вообще не поднимает потоков.

        Это и есть требование «блокирует до конца, фоновых потоков не
        порождает»: пул из одного слота не даёт загрузке распараллелиться.
        """
        pool = CountingPool(max_conn=1, parties=1)
        s = SnapshotLoadService(
            store=NullStore(),  # type: ignore[arg-type]
            pool=pool,  # type: ignore[arg-type]
            tables=list(TABLES),
            max_workers=8,
            max_conn=1,
        )
        result = s.load()
        assert result.loaded_tables == len(TABLES)
        assert pool.peak == 1
        assert pool.threads == {threading.current_thread().name}


class TestPoolIsNeverOverSubscribed:
    def test_peak_concurrency_stays_within_max_conn(self) -> None:
        """Главный страж: пиковая занятость пула не выше ``max_conn``.

        Сценарий тот же, что у пула: восемь таблиц, пул на два слота, старт
        одновременно. Снятие ограничения в ``_effective_workers()`` поднимает
        пик до 4+ и роняет проверку.
        """
        pool = CountingPool(max_conn=MAX_CONN)
        s = SnapshotLoadService(
            store=NullStore(),  # type: ignore[arg-type]
            pool=pool,  # type: ignore[arg-type]
            tables=list(TABLES),
            max_workers=8,
            max_conn=MAX_CONN,
        )

        result = s.load()

        assert result.loaded_tables == len(TABLES)
        assert result.errors == 0
        assert pool.calls == 2 * len(TABLES)  # схема + данные на таблицу
        assert pool.peak <= MAX_CONN, (
            f"загрузчик занял пул сверх max_conn: пик {pool.peak} > {MAX_CONN}"
        )
        assert len(pool.threads) <= MAX_CONN, (
            f"потоков загрузки больше, чем слотов в пуле: {sorted(pool.threads)}"
        )

    def test_all_load_threads_are_joined(self) -> None:
        """После возврата из ``load()`` потоков загрузки не остаётся."""
        pool = CountingPool(max_conn=MAX_CONN)
        s = SnapshotLoadService(
            store=NullStore(),  # type: ignore[arg-type]
            pool=pool,  # type: ignore[arg-type]
            tables=list(TABLES),
            max_workers=8,
            max_conn=MAX_CONN,
        )
        before = {t.name for t in threading.enumerate()}
        s.load()
        leaked = {
            t.name
            for t in threading.enumerate()
            if t.name.startswith("snapshot-load") and t.name not in before
        }
        assert leaked == set()


class TestWriteFailureIsNotHidden:
    def test_store_refusal_surfaces_in_result(self) -> None:
        """Отказ хранилища не должен выглядеть как успешная загрузка."""

        class RefusingStore(NullStore):
            def replace_records(self, table: str, records) -> bool:
                return False

        s = SnapshotLoadService(
            store=RefusingStore(),  # type: ignore[arg-type]
            pool=CountingPool(),  # type: ignore[arg-type]
            tables=["oarb.audits"],
        )
        result = s.load()
        assert result.loaded_tables == 0
        assert result.errors == 1
        assert result.rows_total == 0
        assert result.loaded_at is not None


def test_loader_does_not_own_the_pool() -> None:
    """Загрузчик не создаёт и не закрывает пул — он его занимает.

    Отдельного вызова, кроме ``configure``/``run``, у внедрённого источника
    быть не должно: иначе у сервера появятся два владельца соединений.
    """

    class StrictPool:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def configure(self, dsn: str) -> None:
            self.calls.append("configure")

        def run(self, fn: Any) -> Any:
            self.calls.append("run")
            return fn(_EmptyConn())

        def close(self) -> None:  # pragma: no cover - вызов означал бы ошибку
            self.calls.append("close")

    pool = StrictPool()
    s = SnapshotLoadService(
        store=NullStore(),  # type: ignore[arg-type]
        pool=pool,  # type: ignore[arg-type]
        dsn="postgresql://u@h/db",
        tables=["oarb.audits"],
    )
    s.load()
    assert set(pool.calls) == {"configure", "run"}
