"""Capability ``data``: два входа, предел стоимости, изоляция, права очереди.

Тесты идут на подставном модуле пула: сервис не должен требовать живой БД,
иначе проверять его поведение можно только интеграционно, то есть дорого.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.enterprise_common.errors import (  # noqa: E402
    InfrastructureError,
    InvalidRequestError,
)
from servers.enterprise.capabilities.data.service.main import DataService  # noqa: E402
from servers.enterprise.capabilities.data.service.writer import (  # noqa: E402
    DROPPED,
    EventBuffer,
)


# --- подставной пул --------------------------------------------------------


class ScriptedCursor:
    def __init__(self, conn: "ScriptedConn") -> None:
        self._conn = conn
        self.description: list[tuple[str, ...]] | None = None

    def __enter__(self) -> "ScriptedCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.statements.append((sql, params))
        if "SELECT" in sql.upper() or "UPDATE" in sql.upper():
            self.description = [("c1",)]
        else:
            self.description = None

    def fetchone(self) -> tuple[object, ...] | None:
        if self._conn.rows:
            return self._conn.rows.pop(0)
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows, self._conn.rows = self._conn.rows, []
        return rows


class ScriptedConn:
    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.rows = list(rows or [])
        self.statements: list[tuple[str, object]] = []

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self)


def _log_row(row_id: str) -> tuple[object, ...]:
    """Строка журнала в том виде, в котором её отдаёт SELECT из ``history_search``.

    Порядок колонок зафиксирован в SQL сервиса: id, timestamp, event_type,
    name, level, summary, payload.
    """
    return (row_id, "2026-01-01T00:00:00Z", "turn.completed", "turn", "info", "ок", {})


def _fake_db(rows: list[tuple[object, ...]] | None = None) -> ModuleType:
    conn = ScriptedConn(rows)
    module = ModuleType("fake_db")
    module.conn = conn  # type: ignore[attr-defined]

    def run(job):  # noqa: ANN001, ANN202
        return job(conn)

    def execute(sql: str, params: object = None) -> None:
        conn.statements.append((sql, params))

    module.run = run  # type: ignore[attr-defined]
    module.execute = execute  # type: ignore[attr-defined]
    return module


#: Имена таблиц журнала приходят из ``platform.json``. Сервис без них
#: отказывает явно, а не пишет в таблицу по умолчанию (см.
#: ``DataService._require_log_table``), поэтому любой тест, трогающий
#: журнал, обязан назвать таблицу. Подстановка ниже — единственное место,
#: где это имя появляется: одна правка развёртывания не превращается в
#: правку двадцати тестов.
LOG_TABLE = ("public", "agent_gateway_logs")
QUESTION_RUNS_TABLE = ("public", "agent_question_runs")


def _service(**kwargs: object) -> DataService:
    """``DataService`` с настроенными именами таблиц журнала."""
    return DataService(
        log_table=LOG_TABLE,
        question_runs_table=QUESTION_RUNS_TABLE,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def service() -> DataService:
    return _service(
        db=_fake_db(),
        expected_tables=("public.agent_gateway_logs", "public.agent_conversation_messages"),
        buffer_flush_interval=0.0,
    )


# --- два входа -------------------------------------------------------------


class TestTwoEntries:
    def test_submit_is_blocking_and_returns_result(self, service: DataService) -> None:
        assert service.submit(lambda conn: 42) == 42

    def test_accept_never_raises_and_returns_accepted(self, service: DataService) -> None:
        assert service.log_event("turn.completed", summary="ок") == "accepted"

    def test_accept_does_not_write_immediately(self, service: DataService) -> None:
        """Неблокирующий вход не должен ждать воркер пула.

        Если запись события ждёт соединения, запрос модели начинает
        конкурировать с журналом — и проигрывает, потому что потеря события
        безвозвратна и не сопровождается ошибкой.
        """
        service.log_event("turn.completed")
        assert service._db.conn.statements == []  # type: ignore[union-attr]

    def test_overflow_drops_instead_of_blocking(self) -> None:
        flushed: list[list[dict[str, object]]] = []
        buffer = EventBuffer(lambda batch: flushed.append(batch), maxlen=2, flush_interval=0.0)
        assert buffer.accept({"n": 1}) is None
        assert buffer.accept({"n": 2}) is None
        assert buffer.accept({"n": 3}) == DROPPED
        assert buffer.stats()["dropped"] == 1
        assert buffer.stats()["pending"] == 2

    def test_flush_is_batched(self) -> None:
        batches: list[list[dict[str, object]]] = []
        buffer = EventBuffer(lambda batch: batches.append(batch), maxlen=100, flush_interval=0.0, batch_size=2)
        for i in range(4):
            buffer.accept({"n": i})
        buffer.flush()
        buffer.flush()
        assert [len(b) for b in batches] == [2, 2]
        assert buffer.stats()["written"] == 4

    def test_flush_error_does_not_propagate(self) -> None:
        """Ошибка БД не должна подниматься в ход агента."""

        def broken(batch: list[dict[str, object]]) -> None:
            raise RuntimeError("БД недоступна")

        buffer = EventBuffer(broken, maxlen=100, flush_interval=0.0)
        buffer.accept({"n": 1})
        buffer.flush()  # не бросает
        assert buffer.stats()["flush_errors"] == 1


# --- серверный предел стоимости (2.13) -------------------------------------


class TestStatementTimeout:
    def test_timeout_is_set_and_reset_around_job(self) -> None:
        db = _fake_db()
        svc = _service(db=db, statement_timeout_ms=1234, buffer_flush_interval=0.0)
        svc.submit(lambda conn: None)
        statements = [sql for sql, _ in db.conn.statements]  # type: ignore[union-attr]
        assert "SET statement_timeout = 1234" in statements
        assert statements[-1].strip() == "SET statement_timeout = 0"

    def test_timeout_is_reset_even_when_job_fails(self) -> None:
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)

        def boom(conn: object) -> None:
            raise RuntimeError("сбой")

        with pytest.raises(InfrastructureError):
            svc.submit(boom)
        statements = [sql for sql, _ in db.conn.statements]  # type: ignore[union-attr]
        assert statements[-1].strip() == "SET statement_timeout = 0"

    def test_max_rows_caps_result(self) -> None:
        rows = [_log_row("r1"), _log_row("r2"), _log_row("r3"), _log_row("r4")]
        svc = _service(db=_fake_db(rows), max_rows=3, buffer_flush_interval=0.0)
        page = svc.history_search(session_id="s1", limit=100)
        assert len(page.hits) == 3
        assert page.truncated is True


# --- изоляция (2.11) -------------------------------------------------------


class TestHistorySearchIsolation:
    def test_scope_is_required(self, service: DataService) -> None:
        with pytest.raises(InvalidRequestError, match="области видимости"):
            service.history_search(query="ошибка")

    def test_session_id_alone_is_enough(self, service: DataService) -> None:
        page = service.history_search(session_id="s1")
        assert page.hits == ()

    def test_query_is_parameterized_not_interpolated(self) -> None:
        """Значение не попадает в текст запроса — только в параметры."""
        svc = _service(db=_fake_db(), buffer_flush_interval=0.0)
        svc.history_search(session_id="s1", query="'; DROP TABLE x; --")
        # Последним идёт ``SET statement_timeout = 0`` — сброс предела, а не
        # сам запрос, поэтому оператор ищем по списку, а не по индексу.
        select = [s for s in svc._db.conn.statements if s[0].lstrip().startswith("SELECT")]  # type: ignore[union-attr]
        assert select, "сервис не выполнял SELECT"
        sql, params = select[-1]
        assert "DROP TABLE" not in sql
        assert "DROP TABLE" in str(params)

    def test_next_offset_only_when_more_rows(self) -> None:
        svc = _service(db=_fake_db([_log_row("r1"), _log_row("r2")]), buffer_flush_interval=0.0)
        page = svc.history_search(session_id="s1", limit=1)
        assert page.next_offset == 1

        svc2 = _service(db=_fake_db([_log_row("r1")]), buffer_flush_interval=0.0)
        assert svc2.history_search(session_id="s1", limit=10).next_offset is None


class TestHistorySearchFilters:
    """Фильтры, без которых поиск по журналу неполон.

    ``tool_name`` и ``until`` пришли из агентского контракта: адаптер
    ``history_search`` сохраняет модельную поверхность, значит операция
    обязана уметь всё, чем она пользовалась раньше. Потеря фильтра здесь
    выглядела бы как «иногда поиск не находит то, что раньше находил».
    """

    def _last_select(self, svc: DataService) -> tuple[str, tuple]:
        selects = [
            s
            for s in svc._db.conn.statements
            if s[0].lstrip().startswith("SELECT")
        ]
        assert selects, "сервис не выполнял SELECT"
        return selects[-1]

    def test_tool_name_filters_by_name(self) -> None:
        svc = _service(db=_fake_db(), buffer_flush_interval=0.0)
        svc.history_search(session_id="s1", tool_name="compact_context")
        sql, params = self._last_select(svc)
        assert "name = %s" in sql
        assert "compact_context" in str(params)

    def test_until_adds_upper_time_bound(self) -> None:
        svc = _service(db=_fake_db(), buffer_flush_interval=0.0)
        svc.history_search(session_id="s1", since="2026-01-01", until="2026-02-01")
        sql, params = self._last_select(svc)
        assert '"timestamp" >= %s' in sql
        assert '"timestamp" <= %s' in sql
        assert "2026-01-01" in str(params)
        assert "2026-02-01" in str(params)

    def test_filters_are_parameterized(self) -> None:
        """Значения фильтров не попадают в текст запроса."""
        svc = _service(db=_fake_db(), buffer_flush_interval=0.0)
        svc.history_search(
            session_id="s1", tool_name="x'; DROP TABLE y; --", until="'; --"
        )
        sql, params = self._last_select(svc)
        assert "DROP TABLE" not in sql
        assert "DROP TABLE" in str(params)

    def test_filters_do_not_weaken_isolation(self) -> None:
        """Фильтры не отменяют требование области видимости."""
        svc = _service(db=_fake_db(), buffer_flush_interval=0.0)
        with pytest.raises(InvalidRequestError, match="области видимости"):
            svc.history_search(tool_name="compact_context", until="2026-02-01")
        # Запрос не выполнялся вовсе.
        assert not [
            s
            for s in svc._db.conn.statements
            if s[0].lstrip().startswith("SELECT")
        ]


# --- schema_check ----------------------------------------------------------


class TestSchemaCheck:
    def test_missing_tables_reported(self) -> None:
        rows = [("public", "a")]
        svc = _service(db=_fake_db(rows), expected_tables=("public.a", "public.b"), buffer_flush_interval=0.0)
        report = svc.schema_check()
        assert report["ok"] is False
        assert report["missing"] == ["public.b"]

    def test_all_present(self) -> None:
        rows = [("public", "a"), ("public", "b")]
        svc = _service(db=_fake_db(rows), expected_tables=("public.a", "public.b"), buffer_flush_interval=0.0)
        assert svc.schema_check()["ok"] is True

    def test_empty_expectation_rejected(self) -> None:
        svc = _service(db=_fake_db(), buffer_flush_interval=0.0)
        with pytest.raises(InvalidRequestError, match="ни одной ожидаемой таблицы"):
            svc.schema_check()


# --- log_event -------------------------------------------------------------


class TestLogEvent:
    def test_empty_event_type_rejected(self, service: DataService) -> None:
        with pytest.raises(InvalidRequestError, match="event_type"):
            service.log_event("  ")

    def test_stats_expose_buffer(self, service: DataService) -> None:
        service.log_event("turn.started")
        stats = service.stats()
        assert stats["event_buffer"]["pending"] == 1
        assert stats["max_rows"] > 0

    # -- запись батча -------------------------------------------------------

    def test_flush_writes_one_insert_per_event(self) -> None:
        """Сброс батча реально доходит до БД.

        Регрессия: ``_write_events`` звал ``execute(sql, rows)``, а контракт
        ``db.execute(sql, *args)`` — один параметр на плейсхолдер. Список
        строк уходил в санитизацию как единственный параметр, ``clean_text``
        на нём падал, и ``log_event`` отвечал «accepted», не записав
        ничего. На фейковом пуле это не ловилось: падение жило внутри
        ``EventBuffer.flush``, который глотает исключение по замыслу.
        """
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)
        svc.log_event("a", summary="первое")
        svc.log_event("b", name="tool", session_id="s1", user_id="u1")
        svc._buffer.flush()

        inserts = [s for s in db.conn.statements if s[0].lstrip().startswith("INSERT")]
        assert len(inserts) == 2, f"ожидался INSERT на каждое событие, получили {inserts}"

    def test_placeholder_count_matches_row_width(self) -> None:
        """Ширина строки обязана совпадать с числом плейсхолдеров.

        PostgreSQL проверил бы это сам, но на фейковом пуле расхождение
        осталось бы незамеченным до первого реального INSERT.
        """
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)
        svc.log_event("a", payload={"k": "v"}, session_id="s1")
        svc._buffer.flush()

        sql, params = next(
            s for s in db.conn.statements if s[0].lstrip().startswith("INSERT")
        )
        assert sql.count("%s") == len(params)
        assert json.loads(params[5]) == {"k": "v"}

    def test_failed_flush_is_counted_not_raised(self) -> None:
        """Ошибка записи не поднимается наружу, но видна в счётчике."""
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)
        svc.log_event("a")

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("db down")

        db.run = _boom
        svc._buffer.flush()

        assert svc.stats()["event_buffer"]["flush_errors"] == 1
        assert svc.stats()["event_buffer"]["written"] == 0
