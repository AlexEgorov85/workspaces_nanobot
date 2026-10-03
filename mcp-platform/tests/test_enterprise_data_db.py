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
import tempfile
import threading
import time
from pathlib import Path
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
    set_job_class_config,
    set_pool_config,
    transaction,
    try_submit,
)
import libs.enterprise_data.db as _db_module
from libs.enterprise_common.settings import Settings, pool_config

#: Корень платформы — нужен тестам, читающим настоящий platform.json.
PLATFORM_ROOT = Path(__file__).resolve().parent.parent

#: Заглушки для подстановок из platform.json. Тесты пула не подключаются к
#: базе, но реестр требует, чтобы файл разрешился целиком: незаполненная
#: подстановка останавливает сборку настроек, и это правильно. Настоящие
#: секреты живут в ``mcp-platform/.secrets.env`` и в тесты не попадают.
#:
#: ``LLM_API_KEY`` — не про пул: он в списке потому, что ключ провайдера тоже
#: приходит в ``platform.json`` подстановкой, и любая сборка настроек
#: разворачивает весь файл целиком. Список подстановок растёт вместе с
#: файлом — и это правильный порядок событий: сначала объявляем секрет, потом
#: на него ссылаемся.
#:
#: ``NANOBOT_WORKSPACE`` — тоже не про пул и не секрет: им объявлен
#: ``execution.session_root``, корень файлов сессии, и он обязан лежать внутри
#: рабочего каталога агента (change 2026-10-03-session-files, п. 1.1).
#: Значение уводится в TEMP, чтобы прогон не писал в каталог платформы.
_DUMMY_SECRETS = {
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "test",
    "LLM_API_KEY": "test",
    "EMBED_TOKEN": "test",
    "NANOBOT_WORKSPACE": str(Path(tempfile.gettempdir()) / "nanobot-platform-tests"),
}


def _pool(overrides: dict | None = None, **kwargs) -> dict:
    """Полный набор ключей пула с подменой: как на процессе, только из файла.

    Значений в коде нет — ``set_pool_config`` требует всего набора, иначе
    «остальное как было» означало бы наличие собственных дефолтов у пула.
    Принимает и словарь, и пары-ключи: в тестах встречаются оба вида.
    """
    merged = dict(_POOL_FROM_FILE)
    merged.update(overrides or {})
    merged.update(kwargs)
    return merged


#: Пул для тестов настраивается так же, как на процессе: реестр читает
#: platform.json. Значений в коде нет, и взять их больше неоткуда.
_POOL_FROM_FILE = pool_config(Settings(env=dict(_DUMMY_SECRETS), secrets={}))


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
        # Пул тоже чист: значений в коде нет, и ненастроенный пул честно
        # отказывается подключаться, а не берёт «какие есть».
        _db._dsn = ""
        _db._pool_cfg = {}
        set_pool_config(_POOL_FROM_FILE)
        saved_classes = {name: dict(values) for name, values in _db._job_class_cfg.items()}

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
            "set_job_class_config": set_job_class_config,
            "try_submit": try_submit,
            "PoolTimeoutError": PoolTimeoutError,
            "_db": _db,
        }

        # Teardown: остановить воркеры, чтобы они не жили между тестами
        _db.shutdown()
        _db._manager = None
        # Секция классов — тоже глобальная конфигурация пула: оставленная
        # после теста, она сделала бы следующий тест не тем, что он объявляет.
        # Восстанавливается снимком, а не очисткой: пустой раздел не проходит
        # проверку полноты, и чистить его «вручную» — значит обойти её.
        _db._job_class_cfg = saved_classes


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
    """resolve_dsn: только то, что задал ``configure()``.

    Раньше функция сама читала ``DATABASE_URL``, затем ``PG_DSN`` из
    окружения. Теперь этого нет: читателей окружения в платформе ровно один
    — реестр ``libs.enterprise_common.settings``, и он передаёт значение
    сюда явно. У секрета, к которому подключается процесс, не должно быть
    двух независимых ответов.
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

    def test_database_url_env_is_not_read_here(self, mock_psycopg2, monkeypatch):
        """Переменная процесса не подставляется сама.

        Регрессия второго разбора: значение пришло бы из окружения, пока
        реестр сообщает оператору, что применяется ``file:platform.json`` или
        дефолт. Расхождение двух ответов — это «подключилось не туда».
        """
        self._clean_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "postgresql://from-env@x/y")
        assert mock_psycopg2["resolve_dsn"]() == ""

    def test_pg_dsn_env_is_not_read_here(self, mock_psycopg2, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("PG_DSN", "postgresql://from-pg-dsn@x/y")
        assert mock_psycopg2["resolve_dsn"]() == ""

    def test_registry_resolved_dsn_is_what_the_pool_uses(
        self, mock_psycopg2, monkeypatch, tmp_path
    ):
        """Конец цепочки: то, что решил реестр, — то, с чем работает пул.

        Путь через файл: ``db.dsn`` приоритетнее окружения, и это нормальный
        случай развёртывания, где платформа владеет своим подключением.
        """
        import json

        from libs.enterprise_common.settings import Settings
        from libs.enterprise_data import db as data_db
        from servers.enterprise import server as enterprise_server

        self._clean_env(monkeypatch)
        raw = json.loads(
            (PLATFORM_ROOT / "platform.json").read_text(encoding="utf-8")
        )
        raw["db"]["dsn"] = "postgresql://${DB_USER}:${DB_PASSWORD}@host/db"
        config = tmp_path / "platform.json"
        config.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        settings = Settings(
            env={**_DUMMY_SECRETS, "DB_USER": "svc", "DB_PASSWORD": "resolved"},
            secrets={},
            file_path=config,
        )
        assert settings.source("DATABASE_URL") == "file:platform.json"
        enterprise_server._configure_dsn(settings)
        assert data_db.resolve_dsn() == "postgresql://svc:resolved@host/db"
        assert mock_psycopg2["resolve_dsn"]() == "postgresql://svc:resolved@host/db"

    def test_empty_env_value_changes_nothing(self, mock_psycopg2, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setenv("PG_DSN", "postgresql://from-pg-dsn@x/y")
        assert mock_psycopg2["resolve_dsn"]() == ""

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
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 1, "max_conn": 1, "reserved_workers": 0})
        )
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
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 1, "max_conn": 2, "reserved_workers": 0})
        )
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
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 1, "max_conn": 3, "reserved_workers": 0})
        )
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
            _pool(
                {
                    "min_conn": 1,
                    "max_conn": 1,
                    "reserved_workers": 0,
                    "queue_maxsize": 1,
                    "pool_timeout": 0.2,
                }
            )
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
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 2, "max_conn": 2, "reserved_workers": 0})
        )
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
        # Детерминированная проверка предпосылки вместо окна времени. Раньше
        # здесь стояло наблюдение 0.5с с утверждением «третья не завершилась»:
        # окно — монетка, и оно маскировало настоящий дефект — пул умел
        # вырасти выше max_conn, и третья транзакция получала лишний воркер
        # вместо ожидания. Теперь снимок пула в момент входа третьей
        # транзакции в аренду: оба воркера заняты, пул упёрся в потолок,
        # значит ждать она обязана.
        third_entered = threading.Event()
        third_view: dict = {}
        orig_acquire = _db_module.DBManager._acquire_lease
        from libs.enterprise_data.audience import JOB_AUDIENCE_MODEL

        # Подмена повторяет сигнатуру аренды целиком, включая ``audience``:
        # пропущенный параметр здесь означал бы TypeError у третьей
        # транзакции, и тест падал бы не на ожидании места, а на своём же
        # хелпере.
        def _watched_acquire(self, tag="", *, audience=JOB_AUDIENCE_MODEL):
            if threading.current_thread() is tc:
                with self._cond:
                    third_view.update(
                        workers=len(self._workers),
                        max_conn=self._max_conn,
                        free=sum(1 for w in self._workers if w._lease_id == 0),
                    )
                third_entered.set()
            return orig_acquire(self, tag, audience=audience)

        with patch.object(_db_module.DBManager, "_acquire_lease", _watched_acquire):
            tc.start()
            assert third_started.wait(timeout=30)
            assert third_entered.wait(timeout=30), "третья транзакция не дошла до аренды"
            assert third_view["workers"] == third_view["max_conn"], third_view
            assert third_view["free"] == 0, third_view
            release.set()
            ta.join(timeout=30); tb.join(timeout=30)
            tc.join(timeout=30)
        assert not tc.is_alive(), "третья транзакция застряла"
        assert third_result == ["ok"]
        assert sorted(results) == ["a", "b"]

    def test_pool_never_exceeds_max_conn(self, mock_psycopg2):
        """Инвариант: конкурентный старт и аренда не открывают лишних
        соединений.

        Регрессия, найденная этим прогоном: ``start()`` рос под
        ``_lifecycle_lock``, а ``_acquire_lease``/``_submit`` — под
        ``_cond``, поэтому проверка «пор пул не вырос» и сам рост были не
        атомарны. Потоки, одновременно поднявшие пул и взявшие аренду,
        давали 3-4 воркера при max_conn=2, и лишние транзакции проходили
        мимо ожидания. На старой версии сценарий воспроизводился в 15
        раундах из 40.
        """
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 2, "max_conn": 2, "reserved_workers": 0})
        )
        mock_psycopg2["configure"]("dsn")

        errors: list[str] = []
        gate = threading.Barrier(8, timeout=30)

        def _worker(i: int) -> None:
            try:
                gate.wait()
                with mock_psycopg2["transaction"]() as conn:
                    conn.execute("UPDATE t SET x=%s", i)
            except Exception as exc:
                errors.append(f"{i}:{exc!r}")

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "поток застрял в ожидании воркера"

        assert errors == [], errors
        mgr = mock_psycopg2["_db"]._get_manager()
        assert len(mgr._workers) <= mgr._max_conn, (
            f"пул вырос выше max_conn: {len(mgr._workers)} > {mgr._max_conn}"
        )

    def test_lease_released_when_begin_fails(self, mock_psycopg2):
        """Утечка лиза: если begin-задача падает, воркер возвращается в пул."""
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 1, "max_conn": 1, "reserved_workers": 0})
        )
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = (1,)
        _db = mock_psycopg2["_db"]

        with patch(
            "libs.enterprise_data.db.DBManager._begin_tx",
            side_effect=RuntimeError("begin boom"),
        ):
            with pytest.raises(RuntimeError, match="begin boom"):
                with mock_psycopg2["transaction"]():
                    pass

        mgr = _db._get_manager()
        assert mgr._lease_workers == {}
        assert all(w._lease_id == 0 for w in mgr._workers)
        # пул снова работает: обычная операция выполняется
        assert mock_psycopg2["fetchval"]("SELECT 1") == 1

    def test_lease_waiter_released_on_shutdown(self, mock_psycopg2):
        """Ждущая транзакция при shutdown не висит вечно — получает
        RuntimeError, а не блокируется навсегда.

        Держатель аренды не имеет права пережить тест: освобождая аренду уже
        после остановки пула, он ткнулся бы в глобальную конфигурацию, которую
        к тому времени переставили следующие тесты, и отказ ушёл бы в
        ``threading.excepthook`` мимо прогна. Поэтому он ждёт стоп-сигнала, а
        тест дожидается потока и проверяет, что тот закончил.
        """
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 1, "max_conn": 1, "reserved_workers": 0})
        )
        # Классы объявлены здесь, а не взяты из окружения: у ``_CLASSES`` у
        # модели ``leases: false``, а этот тест про аренду. Предел ожидания
        # нужен обоим потокам, но с разных сторон — ждущей аренде с запасом до
        # shutdown, а освобождению аренды ровно на израсходование предела.
        mock_psycopg2["set_job_class_config"]({
            "model": {
                "statement_timeout_ms": 15000,
                "queue_maxsize": 1,
                "wait_sec": 1.0,
                "leases": True,
            },
            "runtime": {
                "statement_timeout_ms": 5000,
                "queue_maxsize": 8,
                "wait_sec": 1.0,
                "leases": True,
            },
        })
        mock_psycopg2["configure"]("dsn")
        _db = mock_psycopg2["_db"]
        mgr = _db._get_manager()

        held = threading.Event()
        # Стоп-сигнал вместо сна: держатель отпускает аренду под присмотром
        # теста, а не через десять секунд после него.
        release = threading.Event()
        keeper_result: list[str] = []

        def _keeper():
            try:
                with mock_psycopg2["transaction"]() as conn:
                    conn.execute("UPDATE t SET x=1")
                    held.set()
                    # Предел — страховка на случай, если тест упал раньше, чем
                    # подал сигнал: поток обязан умереть сам, без внешнего join.
                    release.wait(timeout=30)
            except Exception as exc:
                # Отказ на освобождении аренды ожидаем, и почему — под проверкой
                # ниже. Ловить его здесь обязательно: иначе он ушёл бы в
                # threading.excepthook, и прогон об отказе не узнал бы.
                keeper_result.append(type(exc).__name__)

        t = threading.Thread(target=_keeper, daemon=True)
        t.start()
        try:
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
        finally:
            release.set()
            t.join(timeout=10)

        assert not t.is_alive(), "держатель аренды пережил тест"
        # Освобождение аренды, пережившей shutdown, отказывается — и это
        # законно: пул остановлен, воркер, взявший аренду, снят, а допуск
        # классом теперь один на всех, включая освобождение. Раньше этот отказ
        # случался через десять секунд после конца теста и всплывал в полном
        # прогоне как PytestUnhandledThreadExceptionWarning — то есть дефект был
        # виден только целиком, и никто его не ловил.
        assert keeper_result == ["PoolBusyError"], keeper_result

    def test_unconnected_worker_yields_to_connected(self, mock_psycopg2):
        """Неподключённый воркер не отнимает задачи у подключённых.

        Симуляция лимита БД (например CONNECTION LIMIT): первый connect
        успешен, все последующие падают. Подключённый воркер обслуживает
        все задачи, а неподключённые не жгут время на retry-connect.
        """
        mock_psycopg2["set_pool_config"](
            _pool(
                {
                "min_conn": 1,
                "max_conn": 5,
                "reserved_workers": 0,
                "connect_max_retries": 1,
                "reconnect_backoff_sec": 0.01,
                }
            )
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
            _pool(
                {
                "min_conn": 1,
                "max_conn": 1,
                "reserved_workers": 0,
                "connect_max_retries": 2,
                "reconnect_backoff_sec": 0.01,
                }
            )
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
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 2, "max_conn": 2, "reserved_workers": 0})
        )
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
            _pool(
                {
                "min_conn": 2,
                "max_conn": 2,
                "reserved_workers": 0,
                "connect_max_retries": 1,
                "reconnect_backoff_sec": 0.01,
                }
            )
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
        """print_activity включает вывод [db-worker] активности при выполнении job.

        Вывод идёт в **stderr**: процесс общается с агентом по stdio, а stdout —
        канал JSON-RPC. Тест раньше смотрел в ``out`` и тем самым закреплял
        поломку, которая ломала бы протокол целиком при включённом флаге.
        """
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 1, "max_conn": 1, "reserved_workers": 0, "print_activity": True})
        )
        mock_psycopg2["configure"]("dsn")
        mgr = mock_psycopg2["_db"]._get_manager()
        assert mgr._print_activity is True
        mock_psycopg2["fetchval"]("SELECT 1")
        captured = capsys.readouterr()
        out = captured.err
        assert captured.out == "", f"активность попала в stdout: {captured.out!r}"
        assert "[db-worker]" in out
        assert "взял job" in out
        # tag «кто» — вызывающая сторона: файл:строка (тест-файл)
        assert re.search(r"test_enterprise_data_db\.py:\d+", out)

    def test_transaction_jobs_have_tag_not_unknown(self, mock_psycopg2, capsys):
        """begin/end транзакции тегируются, без [unknown] (никто не остаётся без метки)."""
        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 1, "max_conn": 1, "reserved_workers": 0, "print_activity": True})
        )
        mock_psycopg2["configure"]("dsn")
        with mock_psycopg2["transaction"]() as conn:
            conn.fetchval("SELECT 1")
        out = capsys.readouterr().err
        assert "[db-worker]" in out
        assert "[unknown]" not in out
        # begin/end + fetch — все job-ы несут метку вызывающей стороны
        assert re.search(r"test_enterprise_data_db\.py:\d+", out)

    def test_set_pool_config_accepts_print_activity(self, mock_psycopg2):
        """print_activity — известный ключ конфига пула (не глотается)."""
        mock_psycopg2["_db"].set_pool_config(_pool({"print_activity": True}))
        _db = mock_psycopg2["_db"]
        assert _db._pool_cfg.get("print_activity") is True


class _FakeWorker:
    """Воркер для проверки разбора очереди без потоков и соединений.

    Настоящий ``_Worker`` — поток с живым соединением, и проверка «кого этот
    воркер возьмёт» на нём означала бы гонку. Здесь нужны только те поля,
    которые читает ``_take_job``.
    """

    def __init__(self, audiences, *, connected: bool = True) -> None:
        self._audiences = frozenset(audiences)
        self._lease_id = 0
        self._busy = False
        self._idle_since = None
        self._conn = MagicMock() if connected else None
        self._conn.closed = False


#: Классы работы для тестов ниже. Числа взяты не из файла, а подобраны так,
#: чтобы повод для отказа был виден в самой проверке: у модели ожидание нулевое
#: (ждать нечего, место есть всегда), у рантайма — ожидание и очередь есть.
_CLASSES = {
    "model": {
        "statement_timeout_ms": 15000,
        "queue_maxsize": 1,
        "wait_sec": 0.0,
        "leases": False,
    },
    "runtime": {
        "statement_timeout_ms": 5000,
        "queue_maxsize": 8,
        "wait_sec": 5.0,
        "leases": True,
    },
}


class TestJobClasses:
    """Поведение классов работы: резерв, отказ вместо ожидания, счётчики.

    Числа конфигурации в коде теста — не второй источник для применения: они
    задают **повод** для проверки, а не применяются на процессе.
    """

    def test_reserved_worker_refuses_model_work(self, mock_psycopg2):
        """Зарезервированный воркер не берёт модельную работу и ищет дальше.

        Ради этого всё затевалось: пока резерв не занят, место для модельной
        работы не исчезает. Проверяется не «какие аудитории у воркера», а что
        воркер с ними делает: пропуск — это «не моё», а не отказ, и подходящая
        работа ниже по очереди должна достаться тому, кому она принадлежит.
        """
        from libs.enterprise_data.audience import ALL_AUDIENCES, JOB_AUDIENCE_MODEL
        from libs.enterprise_data.audience import JOB_AUDIENCE_RUNTIME

        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 2, "max_conn": 2, "reserved_workers": 1})
        )
        mock_psycopg2["set_job_class_config"](_CLASSES)
        mock_psycopg2["configure"]("dsn")

        mgr = mock_psycopg2["_db"].DBManager("dsn")
        assert mgr._audiences_for(1) == {JOB_AUDIENCE_RUNTIME}, mgr._audiences_for(1)
        assert mgr._audiences_for(2) == ALL_AUDIENCES, mgr._audiences_for(2)
        reserved = _FakeWorker(mgr._audiences_for(1))
        universal = _FakeWorker(mgr._audiences_for(2))

        model_job = mock_psycopg2["_db"]._Job(lambda conn: "model", audience=JOB_AUDIENCE_MODEL)
        mgr._queue.append(model_job)
        assert mgr._take_job(reserved) is None, "зарезервированный воркер взял модельную работу"
        assert list(mgr._queue) == [model_job], "пропуск выбросил работу из очереди"
        assert mgr._take_job(universal) is model_job

        runtime_job = mock_psycopg2["_db"]._Job(lambda conn: "runtime", audience=JOB_AUDIENCE_RUNTIME)
        mgr._queue.append(runtime_job)
        assert mgr._take_job(reserved) is runtime_job, (
            "зарезервированный воркер не взял работу своего класса"
        )

    def test_try_submit_refuses_instead_of_waiting(self, mock_psycopg2):
        """Нет места — отказ, а не ожидание; работа в очередь не встаёт.

        На этом держится сброс журнала: батч, которому некуда деться,
        возвращается в буфер и уйдёт следующим тиком. Если бы отказ был
        «подождать и упасть по таймауту», буфер блокировался бы на
        собственном пуле, то есть на том, для которого он и работает.
        """
        from libs.enterprise_data.audience import JOB_AUDIENCE_RUNTIME
        from libs.enterprise_data.db import PoolBusyError, try_submit

        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 1, "max_conn": 1, "reserved_workers": 0})
        )
        mock_psycopg2["set_job_class_config"](_CLASSES)
        mock_psycopg2["configure"]("dsn")

        busy = threading.Event()
        release = threading.Event()

        def _hold(conn):
            busy.set()
            assert release.wait(timeout=30)
            return "held"

        holder = threading.Thread(
            target=lambda: try_submit(_hold, audience=JOB_AUDIENCE_RUNTIME), daemon=True
        )
        holder.start()
        assert busy.wait(timeout=30), "воркер не взял работу — проверять нечего"

        with pytest.raises(PoolBusyError):
            try_submit(lambda conn: "second", audience=JOB_AUDIENCE_RUNTIME)

        stats = mock_psycopg2["get_stats"]()
        assert stats["queue_size"] == 0, "отказавшая работа всё-таки встала в очередь"
        assert stats["audiences"][JOB_AUDIENCE_RUNTIME]["rejected"] == 1, stats

        # Отказ не оставил пул сломанным: освободив воркер, тот же вызов
        # проходит. Иначе «занято» превратилось бы в «пул встал».
        release.set()
        holder.join(timeout=30)
        assert try_submit(lambda conn: "after", audience=JOB_AUDIENCE_RUNTIME) == "after"

    def test_stats_carry_counters_per_class(self, mock_psycopg2):
        """По каждому классу видно, сколько стоит, выполняется и отказано.

        Без этого «модельная работа не идёт» и «журнал не пишется» выглядят
        снаружи одинаково, а разбираться приходится вслепую, гоняя трафик по
        живому контуру.
        """
        from libs.enterprise_data.audience import JOB_AUDIENCE_MODEL, JOB_AUDIENCE_RUNTIME
        from libs.enterprise_data.db import run, try_submit

        mock_psycopg2["set_pool_config"](
            _pool({"min_conn": 1, "max_conn": 1, "reserved_workers": 0})
        )
        mock_psycopg2["set_job_class_config"](_CLASSES)
        mock_psycopg2["configure"]("dsn")

        assert run(lambda conn: "m", audience=JOB_AUDIENCE_MODEL) == "m"
        assert try_submit(lambda conn: "r", audience=JOB_AUDIENCE_RUNTIME) == "r"

        stats = mock_psycopg2["get_stats"]()
        assert stats["reserved_workers"] == 0, stats
        buckets = stats["audiences"]
        assert set(buckets) == {JOB_AUDIENCE_MODEL, JOB_AUDIENCE_RUNTIME}, buckets
        for audience, bucket in buckets.items():
            assert set(bucket) == {
                "queued", "running", "rejected", "taken", "wait_total", "wait_max",
            }, (audience, bucket)
            assert bucket["taken"] == 1, (audience, bucket)
            assert bucket["queued"] == 0 and bucket["running"] == 0, (audience, bucket)
            assert bucket["rejected"] == 0, (audience, bucket)
            assert bucket["wait_max"] >= bucket["wait_total"] >= 0.0, (audience, bucket)


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
