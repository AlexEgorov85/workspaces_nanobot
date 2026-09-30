"""Тесты пула соединений ``libs.enterprise_data.db``.

Проверяют пул, транзакции и санитизацию параметров ровно на той копии,
которая живёт в платформе. Копия существует потому, что MCP-сервер
поднимается без агента: доступ к PostgreSQL — механизм платформы, и он
не должен приезжать из ``nanobot`` как чужой capability. Поэтому тесты
переехали вместе с модулем и проверяют именно его.

Тест самодостаточен: ни одна фикстура/хелпер корневого ``tests/`` здесь не
нужна. ``libs`` уже в ``sys.path`` — его кладёт ``pythonpath`` из
``mcp-platform/pyproject.toml``; никаких ручных вставок в ``sys.path``,
никаких импортов агента.
"""

from __future__ import annotations

import re
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from libs.enterprise_data.db import (
    PoolTimeoutError,
    async_execute,
    async_fetch,
    async_fetchone,
    async_fetchval,
    async_transaction,
    configure,
    execute,
    fetch,
    fetchone,
    fetchval,
    get_stats,
    resolve_dsn,
    run,
    set_pool_config,
    transaction,
)
import libs.enterprise_data.db as _db_module


@pytest.fixture(autouse=True)
def mock_psycopg2():
    """Подменить драйвер; teardown = shutdown пула."""
    with (
        patch.dict("sys.modules"),
        patch("psycopg2.connect") as mock_connect,
        patch("psycopg2.extras.Json", lambda x: x),
        patch("psycopg2.extras.RealDictCursor") as mock_rdc,
        patch("psycopg2.extras.register_json"),
        patch("psycopg2.extensions.register_adapter"),
    ):
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_cur = MagicMock()
        mock_cur.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value = mock_conn
        mock_rdc.return_value = MagicMock()

        # Модуль импортирован на уровне файла и видит настоящий psycopg2 —
        # подменяется только соединение и курсоры. Так DB_RETRYABLE_ERRORS
        # остаётся классом настоящего драйвера, и тесты ретрая ловят
        # настоящий psycopg2.OperationalError, а не мок.
        _db = _db_module

        # Reset DSN и пул перед каждым тестом. ``configure("")`` ничего
        # не сбрасывает (пустой DSN игнорируется), поэтому глобал чистим
        # напрямую — иначе тест, оставивший DSN, ломал бы соседний.
        _db._dsn = ""
        _db._pool_cfg = dict(_db._DEFAULT_POOL)

        yield {
            "mock_connect": mock_connect,
            "mock_conn": mock_conn,
            "mock_cur": mock_cur,
            "configure": configure,
            "resolve_dsn": resolve_dsn,
            "execute": execute,
            "fetch": fetch,
            "fetchone": fetchone,
            "fetchval": fetchval,
            "transaction": transaction,
            "async_execute": async_execute,
            "async_fetch": async_fetch,
            "async_fetchone": async_fetchone,
            "async_fetchval": async_fetchval,
            "async_transaction": async_transaction,
            "run": run,
            "get_stats": get_stats,
            "set_pool_config": set_pool_config,
            "PoolTimeoutError": PoolTimeoutError,
            "_db": _db,
        }

        # Teardown: остановить воркеры, чтобы они не жили между тестами
        _db.shutdown()
        _db._manager = None


class TestConfigure:
    def test_sets_dsn(self, mock_psycopg2):
        mock_psycopg2["configure"]("postgresql://u:p@h/db")
        assert mock_psycopg2["resolve_dsn"]() == "postgresql://u:p@h/db"

    def test_empty_dsn_skips_set(self, mock_psycopg2):
        _db = mock_psycopg2["_db"]

        _db._dsn = "existing"
        mock_psycopg2["configure"]("")
        assert mock_psycopg2["resolve_dsn"]() == "existing"

    def test_same_dsn_idempotent(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn1")
        mock_psycopg2["configure"]("dsn1")
        assert mock_psycopg2["resolve_dsn"]() == "dsn1"

    def test_changes_dsn(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn1")
        mock_psycopg2["configure"]("dsn2")
        assert mock_psycopg2["resolve_dsn"]() == "dsn2"


class TestResolveDsn:
    """resolve_dsn: явный DSN, иначе окружение платформы.

    Конфиг агента платформе недоступен — это и было целью переноса.
    DSN приходит либо из ``configure()``, либо из окружения процесса.
    """

    @staticmethod
    def _clean_env(monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("PG_DSN", raising=False)

    def test_explicit_dsn(self, mock_psycopg2, monkeypatch):
        self._clean_env(monkeypatch)
        mock_psycopg2["configure"]("postgresql://explicit@x/y")
        assert mock_psycopg2["resolve_dsn"]() == "postgresql://explicit@x/y"

    def test_configure_wins_over_env(self, mock_psycopg2, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://from-env@x/y")
        mock_psycopg2["configure"]("postgresql://explicit@configured/db")
        assert mock_psycopg2["resolve_dsn"]() == "postgresql://explicit@configured/db"

    def test_database_url_from_env(self, mock_psycopg2, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "postgresql://from-env@x/y")
        assert mock_psycopg2["resolve_dsn"]() == "postgresql://from-env@x/y"

    def test_pg_dsn_from_env(self, mock_psycopg2, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("PG_DSN", "postgresql://from-pg-dsn@x/y")
        assert mock_psycopg2["resolve_dsn"]() == "postgresql://from-pg-dsn@x/y"

    def test_database_url_wins_over_pg_dsn(self, mock_psycopg2, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "postgresql://primary@x/y")
        monkeypatch.setenv("PG_DSN", "postgresql://fallback@x/y")
        assert mock_psycopg2["resolve_dsn"]() == "postgresql://primary@x/y"

    def test_empty_env_value_falls_through_to_pg_dsn(self, mock_psycopg2, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setenv("PG_DSN", "postgresql://from-pg-dsn@x/y")
        assert mock_psycopg2["resolve_dsn"]() == "postgresql://from-pg-dsn@x/y"

    def test_empty_when_no_source(self, mock_psycopg2, monkeypatch):
        self._clean_env(monkeypatch)
        assert mock_psycopg2["resolve_dsn"]() == ""

    def test_no_fallback_from_parts(self, mock_psycopg2, monkeypatch):
        """Части host/port/dbname/user не собираются в DSN."""
        self._clean_env(monkeypatch)
        monkeypatch.setenv("DB_PASSWORD", "s3cret")
        assert mock_psycopg2["resolve_dsn"]() == ""


class TestExecute:
    def test_execute_returns_status(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].statusmessage = "INSERT 0 1"
        result = mock_psycopg2["execute"]("INSERT INTO t VALUES (%s)", 42)
        assert result == "INSERT 0 1"
        mock_psycopg2["mock_cur"].execute.assert_called_with(
            "INSERT INTO t VALUES (%s)", (42,)
        )

    def test_execute_empty_sql(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].statusmessage = None
        result = mock_psycopg2["execute"]("")
        assert result is None

    def test_execute_no_dsn_raises(self, mock_psycopg2):
        _db = mock_psycopg2["_db"]

        _db._dsn = ""
        with patch("libs.enterprise_data.db.resolve_dsn", return_value=""):
            with pytest.raises(RuntimeError, match="не инициализирован"):
                mock_psycopg2["execute"]("SELECT 1")

    def test_execute_retry_on_operational_error(self, mock_psycopg2):
        """Ретраябельная ошибка → воркер пересоздаёт соединение и повторяет job."""
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].execute.side_effect = [
            __import__("psycopg2").OperationalError("conn lost"),
            None,
        ]
        mock_psycopg2["mock_cur"].statusmessage = "UPDATE 5"
        result = mock_psycopg2["execute"]("UPDATE t SET x=1")
        assert result == "UPDATE 5"
        # соединение было пересоздано после обрыва
        assert mock_psycopg2["mock_connect"].call_count == 2


class TestFetch:
    def test_fetch_returns_dicts(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchall.return_value = [
            {"id": 1, "name": "foo"},
        ]
        result = mock_psycopg2["fetch"]("SELECT * FROM t")
        assert result == [{"id": 1, "name": "foo"}]

    def test_fetch_empty(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchall.return_value = []
        result = mock_psycopg2["fetch"]("SELECT * FROM t WHERE 1=0")
        assert result == []


class TestFetchOne:
    def test_fetchone_returns_row(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = {"id": 1}
        result = mock_psycopg2["fetchone"]("SELECT * FROM t WHERE id=%s", 1)
        assert result == {"id": 1}

    def test_fetchone_returns_none(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = None
        result = mock_psycopg2["fetchone"]("SELECT * FROM t WHERE 1=0")
        assert result is None


class TestFetchVal:
    def test_fetchval_returns_value(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = (42,)
        result = mock_psycopg2["fetchval"]("SELECT count(*) FROM t")
        assert result == 42

    def test_fetchval_no_rows(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = None
        result = mock_psycopg2["fetchval"]("SELECT max(id) FROM t")
        assert result is None


class TestRun:
    def test_run_executes_fn_with_conn(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        result = mock_psycopg2["run"](lambda conn: conn.encoding)
        assert result == mock_psycopg2["mock_conn"].encoding


class TestTransaction:
    def test_transaction_commits(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        with mock_psycopg2["transaction"]() as conn:
            conn.cursor().execute("INSERT INTO t VALUES (1)")
        mock_psycopg2["mock_conn"].commit.assert_called_once()
        # соединение остаётся в пуле — не закрывается после транзакции
        mock_psycopg2["mock_conn"].close.assert_not_called()

    def test_transaction_rollback_on_error(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        with pytest.raises(ValueError):
            with mock_psycopg2["transaction"]() as conn:
                conn.cursor().execute("INSERT INTO t VALUES (1)")
                raise ValueError("fail")
        mock_psycopg2["mock_conn"].rollback.assert_called_once()

    def test_transaction_restores_autocommit(self, mock_psycopg2):
        """После транзакции autocommit возвращается в True (воркер свободен)."""
        mock_psycopg2["configure"]("dsn")
        with mock_psycopg2["transaction"]():
            pass
        assert mock_psycopg2["mock_conn"].autocommit is True

    def test_transaction_cursor_iteration(self, mock_psycopg2):
        """Курсор транзакции итерируется как psycopg2 (``for row in cur``).

        Регрессия: потребители итерируют курсор напрямую —
        ``_CursorProxy`` должен поддерживать ``__iter__``.
        """
        mock_psycopg2["configure"]("dsn")
        # _CursorProxy вызывает _worker._cursor(cid) напрямую (без __enter__),
        # поэтому fetchall задаётся на том же объекте, что возвращает conn.cursor()
        cur_mock = mock_psycopg2["mock_conn"].cursor.return_value
        cur_mock.fetchall.return_value = [
            ("user", "Hello!"),
            ("assistant", "Hi!"),
        ]
        with mock_psycopg2["transaction"]() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT role, content FROM t")
                rows = list(cur)
        assert rows == [("user", "Hello!"), ("assistant", "Hi!")]

    def test_execute_none_params_not_converted_to_tuple(self, mock_psycopg2):
        """Баг-фикс 8d43dfb: execute(sql, None) не должен превращаться в ().

        Если передать `()`, psycopg2 делает %-форматирование и падает на
        литералах '%' в данных (например, «16.7%») — ломает execute_values
        при сохранении сессий (IndexError: tuple index out of range).
        """
        mock_psycopg2["configure"]("dsn")
        cur_mock = mock_psycopg2["mock_conn"].cursor.return_value
        with mock_psycopg2["transaction"]() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO t VALUES ('16.7%', %s)", None)
        cur_mock.execute.assert_called_once_with("INSERT INTO t VALUES ('16.7%', %s)", None)


class TestPool:
    def test_single_connection_reused(self, mock_psycopg2):
        """Пул N=1: все операции на одном соединении."""
        mock_psycopg2["set_pool_config"]({"min_conn": 1, "max_conn": 1})
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = (1,)
        for _ in range(5):
            mock_psycopg2["fetchval"]("SELECT 1")
        assert mock_psycopg2["mock_connect"].call_count == 1

    def test_connection_not_closed_between_ops(self, mock_psycopg2):
        """Соединение живёт в пуле, close не вызывается между операциями."""
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["fetchval"]("SELECT 1")
        mock_psycopg2["fetchval"]("SELECT 2")
        mock_psycopg2["mock_conn"].close.assert_not_called()

    def test_parallel_transactions_use_separate_connections(self, mock_psycopg2):
        """Две параллельные транзакции получают разные соединения (max_conn=2)."""
        mock_psycopg2["set_pool_config"]({"min_conn": 1, "max_conn": 2})
        mock_psycopg2["configure"]("dsn")

        results: list = []
        # барьер внутри транзакции гарантирует, что обе открыты одновременно
        inside = threading.Barrier(2)

        def _tx():
            with mock_psycopg2["transaction"]() as conn:
                conn.execute("UPDATE t SET x=1")
                inside.wait(timeout=5)
                results.append("ok")

        t1 = threading.Thread(target=_tx)
        t2 = threading.Thread(target=_tx)
        t1.start(); t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        assert mock_psycopg2["mock_connect"].call_count == 2

    def test_auto_scale_when_worker_leased(self, mock_psycopg2):
        """Пока транзакция держит воркер, обычная операция уходит на новый."""
        mock_psycopg2["set_pool_config"]({"min_conn": 1, "max_conn": 3})
        mock_psycopg2["configure"]("dsn")

        tx_done = threading.Event()
        tx_acquired = threading.Event()
        op_sent = threading.Event()

        def _tx():
            with mock_psycopg2["transaction"]() as conn:
                conn.execute("UPDATE t SET x=1")
                tx_acquired.set()
                # Lease держим до тех пор, пока главный поток действительно
                # отправил обычную операцию. Раньше здесь был sleep(0.2):
                # на загруженной машине окно успевало истечь, воркер
                # освобождался, и обычная операция уходила на него же —
                # call_count становился 1 вместо 2. Теперь перекрытие
                # гарантировано событием, а не гонкой со временем.
                op_sent.wait(timeout=30)
            tx_done.set()

        t = threading.Thread(target=_tx)
        t.start()
        assert tx_acquired.wait(timeout=30)
        # обычная операция из главного потока — пока lease занят
        mock_psycopg2["execute"]("UPDATE other SET x=1")
        op_sent.set()
        t.join(timeout=30)
        assert tx_done.is_set()
        # воркер транзакции + второй воркер для обычной операции
        assert mock_psycopg2["mock_connect"].call_count == 2

    def test_maybe_shrink_skips_never_idle_worker(self, mock_psycopg2):
        """Воркер без _idle_since (старт/shutdown) не роняет _maybe_shrink:
        TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'."""
        _db = mock_psycopg2["_db"]
        from libs.enterprise_data.db import DBManager

        mgr = DBManager()
        mgr._min_conn = 1
        mgr._idle_timeout = 60.0

        class FakeWorker:
            _lease_id = 0
            _idle_since = None

        mgr._workers = [FakeWorker(), FakeWorker()]
        # не должен бросать исключение и не должен «сжимать» не-идle-воркера
        assert mgr._maybe_shrink(mgr._workers[0]) is False

    def test_queue_full_raises_timeout(self, mock_psycopg2):
        """Переполненная очередь → PoolTimeoutError, а не вечный блок."""
        mock_psycopg2["set_pool_config"](
            {"min_conn": 1, "max_conn": 1, "queue_maxsize": 1, "pool_timeout": 0.2}
        )
        mock_psycopg2["configure"]("dsn")
        lock = threading.Lock()
        lock.acquire()  # держим воркера занятым

        def _block(conn):
            with lock:
                return "released"

        holder = threading.Thread(target=lambda: mock_psycopg2["run"](_block))
        holder.start()
        time.sleep(0.05)  # воркер уже исполняет _block и ждёт lock

        # первая операция заполняет очередь (воркер занят — её никто не заберёт)
        queued = threading.Thread(
            target=mock_psycopg2["fetchval"], args=("SELECT 1",)
        )
        queued.start()
        time.sleep(0.05)

        # вторая операция: очередь переполнена → PoolTimeoutError
        with pytest.raises(mock_psycopg2["PoolTimeoutError"]):
            mock_psycopg2["fetchval"]("SELECT 1")
        lock.release()
        holder.join(timeout=5)
        queued.join(timeout=5)

    def test_third_transaction_waits_for_free_worker(self, mock_psycopg2):
        """Сценарий из прода: при занятых 2 воркерах 3-я транзакция
        ждёт в очереди и дожидается (вместо PoolTimeoutError)."""
        mock_psycopg2["set_pool_config"]({"min_conn": 2, "max_conn": 2})
        mock_psycopg2["configure"]("dsn")

        # Событие срабатывает ТОЛЬКО когда оба воркера заняты.
        # Раньше здесь стоял Event, который устанавливался ПЕРВЫМ же
        # холдером: на загруженной машине второй поток не успевал взять
        # воркер, и 3-я транзакция честно занимала свободный — тест проходил
        # по неверной причине, а при настоящей проверке «оба заняты» падал.
        # Barrier здесь не годится: parties=2, а wait() звали трое (оба
        # холдера и главный поток), из-за чего барьер ломался. Поэтому —
        # счётчик под блокировкой.
        held = 0
        held_lock = threading.Lock()
        both_held = threading.Event()
        release = threading.Event()
        results = []

        def _tx(name):
            nonlocal held
            try:
                with mock_psycopg2["transaction"]() as conn:
                    conn.execute("UPDATE t SET x=1 WHERE name=%s", name)
                    with held_lock:
                        held += 1
                        if held >= 2:
                            both_held.set()
                    assert release.wait(timeout=30)
                    results.append(name)
            except Exception as exc:  # pragma: no cover
                results.append(f"{name}:{exc!r}")

        ta = threading.Thread(target=_tx, args=("a",))
        tb = threading.Thread(target=_tx, args=("b",))
        ta.start(); tb.start()
        assert both_held.wait(timeout=30)

        third_result = []
        third_started = threading.Event()

        def _third():
            third_started.set()
            try:
                with mock_psycopg2["transaction"]() as conn:
                    conn.execute("UPDATE t SET x=2")
                third_result.append("ok")
            except Exception as exc:
                third_result.append(f"err:{exc!r}")

        tc = threading.Thread(target=_third)
        tc.start()
        # Детерминированно ждём, что третья транзакция реально началась
        # (а не «прошло 0.3с от старта потока» — на загруженной машине поток
        # мог не успеть стартовать, и 3-я операция успевала завершиться, что
        # давало ложный флейк). Затем наблюдаем окно ~0.5с: третья должна
        # ждать в очереди (workers=2 заняты), не падая с ошибкой.
        assert third_started.wait(timeout=30)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            assert third_result == []
            time.sleep(0.02)
        release.set()
        ta.join(timeout=30); tb.join(timeout=30)
        tc.join(timeout=30)
        assert third_result == ["ok"]
        assert sorted(results) == ["a", "b"]

    def test_lease_released_when_begin_fails(self, mock_psycopg2):
        """Утечка лиза: если begin-задача падает, воркер возвращается в пул."""
        mock_psycopg2["set_pool_config"]({"min_conn": 1, "max_conn": 1})
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = (1,)
        _db = mock_psycopg2["_db"]

        with patch(
            "libs.enterprise_data.db.DBManager._begin_tx",
            side_effect=RuntimeError("begin boom"),
        ):
            with pytest.raises(RuntimeError, match="begin boom"):
                with mock_psycopg2["transaction"]() as conn:
                    pass

        mgr = _db._get_manager()
        assert mgr._lease_workers == {}
        assert all(w._lease_id == 0 for w in mgr._workers)
        # пул снова работает: обычная операция выполняется
        assert mock_psycopg2["fetchval"]("SELECT 1") == 1

    def test_lease_waiter_released_on_shutdown(self, mock_psycopg2):
        """Ждущая транзакция при shutdown не висит вечно — получает
        RuntimeError, а не блокируется навсегда."""
        mock_psycopg2["set_pool_config"]({"min_conn": 1, "max_conn": 1})
        mock_psycopg2["configure"]("dsn")
        _db = mock_psycopg2["_db"]
        mgr = _db._get_manager()

        held = threading.Event()

        def _keeper():
            with mock_psycopg2["transaction"]() as conn:
                conn.execute("UPDATE t SET x=1")
                held.set()
                time.sleep(10)

        t = threading.Thread(target=_keeper, daemon=True)
        t.start()
        assert held.wait(timeout=5)

        waiter_result = []

        def _waiter():
            try:
                with mock_psycopg2["transaction"]() as conn:
                    conn.execute("SELECT 1")
                waiter_result.append("ok")
            except Exception as exc:
                waiter_result.append(type(exc).__name__)

        w = threading.Thread(target=_waiter, daemon=True)
        w.start()
        time.sleep(0.3)  # waiter уже в queue-ожидании lease
        mgr.shutdown()
        w.join(timeout=5)
        assert waiter_result == ["RuntimeError"]

    def test_unconnected_worker_yields_to_connected(self, mock_psycopg2):
        """Неподключённый воркер не отнимает задачи у подключённых.

        Симуляция лимита БД (например CONNECTION LIMIT): первый connect
        успешен, все последующие падают. Подключённый воркер обслуживает
        все задачи, а неподключённые не жгут время на retry-connect.
        """
        mock_psycopg2["set_pool_config"](
            {
                "min_conn": 1,
                "max_conn": 5,
                "connect_max_retries": 1,
                "reconnect_backoff_sec": 0.01,
            }
        )
        mock_psycopg2["configure"]("dsn")
        import psycopg2

        real_connect = mock_psycopg2["mock_connect"]
        calls = {"n": 0}

        def _connect(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise psycopg2.OperationalError("too many connections")
            return mock_psycopg2["mock_conn"]

        real_connect.side_effect = _connect
        mock_psycopg2["mock_cur"].fetchone.return_value = (1,)

        # прогрев: воркер 0 подключается и живёт в пуле
        assert mock_psycopg2["fetchval"]("SELECT 1") == 1

        results = []
        errors = []

        def worker():
            try:
                results.append(mock_psycopg2["fetchval"]("SELECT 1"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert len(results) == 6
        # новые воркеры даже не пытались подключиться: работал только воркер 0
        assert real_connect.call_count == 1

    def test_connect_failure_returns_error_fast(self, mock_psycopg2):
        """Полная недоступность БД: задача падает с ошибкой, а не висит."""
        mock_psycopg2["set_pool_config"](
            {
                "min_conn": 1,
                "max_conn": 1,
                "connect_max_retries": 2,
                "reconnect_backoff_sec": 0.01,
            }
        )
        mock_psycopg2["configure"]("dsn")
        import psycopg2

        mock_psycopg2["mock_connect"].side_effect = psycopg2.OperationalError(
            "db down"
        )
        with pytest.raises(psycopg2.OperationalError, match="db down"):
            mock_psycopg2["fetchval"]("SELECT 1")

    def test_get_stats_keys(self, mock_psycopg2):
        stats = mock_psycopg2["get_stats"]()
        for k in (
            "workers", "queue_size", "running", "min_conn", "max_conn",
            "pool_timeout", "jobs", "lease_acquired", "connected_workers",
        ):
            assert k in stats

    def test_probe_connections_verifies_connectivity(self, mock_psycopg2):
        """probe_connections поднимает реальное соединение и отчитывается.

        Ленивый пул подключает лишь столько воркеров, сколько нужно под
        нагрузкой (неподключённые уступают подключённым), поэтому после
        warm-up может быть 1 живое соединение — суть probe в проверке
        доступности БД, а не в прогреве всех min_conn.
        """
        mock_psycopg2["set_pool_config"]({"min_conn": 2, "max_conn": 2})
        mock_psycopg2["configure"]("dsn")
        _db = mock_psycopg2["_db"]
        _db.probe_connections(timeout=5)
        stats = mock_psycopg2["get_stats"]()
        assert stats["workers"] == 2
        assert stats["connected_workers"] >= 1
        assert stats["failed_workers"] == 0

    def test_probe_connections_db_down_reports_failed(self, mock_psycopg2):
        """При недоступной БД probe_connections не бросает ошибку;
        воркеры помечаются как failed, счётчик connect_errors растёт."""
        import psycopg2

        mock_psycopg2["set_pool_config"](
            {
                "min_conn": 2,
                "max_conn": 2,
                "connect_max_retries": 1,
                "reconnect_backoff_sec": 0.01,
            }
        )
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_connect"].side_effect = psycopg2.OperationalError("down")
        _db = mock_psycopg2["_db"]
        _db.probe_connections(timeout=5)  # не должно упасть
        stats = mock_psycopg2["get_stats"]()
        assert stats["connected_workers"] == 0
        assert stats["failed_workers"] >= 1
        assert stats["connect_errors"] > 0

    def test_db_activity_flag_enables_printing(self, mock_psycopg2, capsys):
        """print_activity включает вывод [db-worker] активности при выполнении job."""
        mock_psycopg2["set_pool_config"](
            {"min_conn": 1, "max_conn": 1, "print_activity": True}
        )
        mock_psycopg2["configure"]("dsn")
        mgr = mock_psycopg2["_db"]._get_manager()
        assert mgr._print_activity is True
        mock_psycopg2["fetchval"]("SELECT 1")
        out = capsys.readouterr().out
        assert "[db-worker]" in out
        assert "взял job" in out
        # tag «кто» — вызывающая сторона: файл:строка (тест-файл)
        assert re.search(r"test_enterprise_data_db\.py:\d+", out)

    def test_transaction_jobs_have_tag_not_unknown(self, mock_psycopg2, capsys):
        """begin/end транзакции тегируются, без [unknown] (никто не остаётся без метки)."""
        mock_psycopg2["set_pool_config"](
            {"min_conn": 1, "max_conn": 1, "print_activity": True}
        )
        mock_psycopg2["configure"]("dsn")
        with mock_psycopg2["transaction"]() as conn:
            conn.fetchval("SELECT 1")
        out = capsys.readouterr().out
        assert "[db-worker]" in out
        assert "[unknown]" not in out
        # begin/end + fetch — все job-ы несут метку вызывающей стороны
        assert re.search(r"test_enterprise_data_db\.py:\d+", out)

    def test_set_pool_config_accepts_print_activity(self, mock_psycopg2):
        """print_activity — известный ключ конфига пула (не глотается)."""
        mock_psycopg2["_db"].set_pool_config({"print_activity": True})
        _db = mock_psycopg2["_db"]
        assert _db._pool_cfg.get("print_activity") is True


class TestAsyncAPI:
    @pytest.mark.asyncio
    async def test_async_execute(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].statusmessage = "INSERT 0 1"
        result = await mock_psycopg2["async_execute"]("INSERT INTO t VALUES (%s)", 1)
        assert result == "INSERT 0 1"

    @pytest.mark.asyncio
    async def test_async_fetch(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchall.return_value = [{"id": 1}]
        result = await mock_psycopg2["async_fetch"]("SELECT * FROM t")
        assert result == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_async_fetchone(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = {"id": 1}
        result = await mock_psycopg2["async_fetchone"]("SELECT * FROM t WHERE id=%s", 1)
        assert result == {"id": 1}

    @pytest.mark.asyncio
    async def test_async_fetchval(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = (42,)
        result = await mock_psycopg2["async_fetchval"]("SELECT count(*) FROM t")
        assert result == 42


class TestAsyncTransaction:
    @pytest.mark.asyncio
    async def test_async_transaction_commits(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        async with mock_psycopg2["async_transaction"]() as wrapper:
            await wrapper.execute("INSERT INTO t VALUES (1)")
        mock_psycopg2["mock_conn"].commit.assert_called_once()
        mock_psycopg2["mock_conn"].close.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_transaction_rollback(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        with pytest.raises(ValueError):
            async with mock_psycopg2["async_transaction"]() as wrapper:
                await wrapper.execute("INSERT INTO t VALUES (1)")
                raise ValueError("fail")
        mock_psycopg2["mock_conn"].rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_connection_wrapper_fetch(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        async with mock_psycopg2["async_transaction"]() as wrapper:
            mock_psycopg2["mock_cur"].fetchall.return_value = [{"id": 1}]
            result = await wrapper.fetch("SELECT * FROM t")
            assert result == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_async_connection_wrapper_fetchrow(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        async with mock_psycopg2["async_transaction"]() as wrapper:
            mock_psycopg2["mock_cur"].fetchone.return_value = {"id": 1}
            result = await wrapper.fetchrow("SELECT * FROM t WHERE id=%s", 1)
            assert result == {"id": 1}

    @pytest.mark.asyncio
    async def test_async_connection_wrapper_fetchrow_none(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        async with mock_psycopg2["async_transaction"]() as wrapper:
            mock_psycopg2["mock_cur"].fetchone.return_value = None
            result = await wrapper.fetchrow("SELECT * FROM t WHERE 1=0")
            assert result is None
