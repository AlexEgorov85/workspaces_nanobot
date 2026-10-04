"""Capability ``data``: два входа, предел стоимости, изоляция, права очереди.

Тесты идут на подставном модуле пула: сервис не должен требовать живой БД,
иначе проверять его поведение можно только интеграционно, то есть дорого.

Имена событий берутся из словаря
--------------------------------

Тесты механики — буфера, плейсхолдеров, счётчиков — пишут события под
**каноническими** именами из ``libs/enterprise_common/eventing/types.py``.
Выдуманное имя (``turn.completed``, ``a``, ``b``) не проверяет писатель: под
``platform.json → data.log_unknown_event_type_policy = strict`` партия с таким
именем отказывается целиком ещё до записи, и красный тест говорит о словаре,
а не о буфере. Проверка «имя вне словаря отказывает партию» живёт отдельно, в
``tests/test_journal_contract_visibility.py``; страж на выдуманные имена в
тестах — в ``tests/test_journal_fabricated_event_names.py``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.enterprise_common.errors import (  # noqa: E402
    InfrastructureError,
    InvalidRequestError,
)
from libs.enterprise_common.eventing.types import (  # noqa: E402
    AGENT_COMPLETED,
    AGENT_STARTED,
    TOOL_COMPLETED,
    TOOL_STARTED,
)
from libs.enterprise_data.audience import (  # noqa: E402
    ALL_AUDIENCES,
    JOB_AUDIENCE_MODEL,
)
from libs.enterprise_data.db import PoolBusyError  # noqa: E402
from servers.enterprise.capabilities.data.service.main import (  # noqa: E402
    AUDIENCE_RUNTIME,
    DataService,
)
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
        # Партии, ушедшие в БД одной вставкой. Сброс журнала больше не пишет
        # по строке: одна партия — одна запись, и считать надо партии, а не
        # строки, иначе тест продолжил бы требовать ровно того, что волна
        # классов работ как раз убрала.
        self.batches: list[tuple[str, list[tuple[object, ...]]]] = []
        # Шаблоны строк, переданные ``execute_values_on`` отдельным
        # аргументом. Список рядом с ``batches``, а не вместо него: форма
        # партии разбирается в других тестах, и менять её ради одного
        # нельзя. Сам факт передачи шаблона — часть контракта сброса.
        self.templates: list[str | None] = []

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self)


def _log_row(row_id: str) -> tuple[object, ...]:
    """Строка журнала в том виде, в котором её отдаёт SELECT из ``history_search``.

    Порядок колонок зафиксирован в SQL сервиса: id, timestamp, event_type,
    name, level, summary, payload.
    """
    return (row_id, "2026-01-01T00:00:00Z", AGENT_COMPLETED, "turn", "info", "ок", {})


def _fake_db(rows: list[tuple[object, ...]] | None = None) -> ModuleType:
    conn = ScriptedConn(rows)
    module = ModuleType("fake_db")
    module.conn = conn  # type: ignore[attr-defined]
    # Свободные места в пуле. Считаются, а не выдаются всегда: ``try_submit``
    # по контракту не ждёт места, и проверка «сброс отложен» держится ровно на
    # этом — при нуле свободных мест пул обязан отказать, а не подождать.
    module.free_workers = 1  # type: ignore[attr-defined]
    # Классы работ, в которых пул выполнял задания.
    module.audiences: list[str] = []  # type: ignore[attr-defined]

    def _accept(audience: str) -> None:
        if audience not in ALL_AUDIENCES:
            raise InfrastructureError(
                f"класс работы {audience!r} не объявлен; "
                f"объявлены: {sorted(ALL_AUDIENCES)}"
            )
        module.audiences.append(audience)  # type: ignore[attr-defined]

    def run(job, *, audience=JOB_AUDIENCE_MODEL):  # noqa: ANN001, ANN202
        _accept(audience)
        return job(conn)

    def try_submit(job, *, audience=JOB_AUDIENCE_MODEL):  # noqa: ANN001, ANN202
        """Постановка без ожидания места: место есть — выполняем, нет — отказ."""
        _accept(audience)
        if module.free_workers <= 0:  # type: ignore[attr-defined]
            raise PoolBusyError(f"у класса {audience!r} нет свободного места")
        module.free_workers -= 1  # type: ignore[attr-defined]
        try:
            return job(conn)
        finally:
            module.free_workers += 1  # type: ignore[attr-defined]

    def execute_values_on(  # noqa: ANN001, ANN202
        conn_: ScriptedConn,
        sql: str,
        rows: list[Sequence[object]],
        *,
        template: str | None = None,
        page_size: int = 200,
    ) -> int:
        """Одна пакетная вставка на партию — и одна запись в ``batches``.

        Партия кладётся в журнал вызовов одной строкой по намерению: разбивать
        её на строки здесь — значило бы вернуть воркер в очередь на весь обход,
        ради чего сброс и уходит через ``try_submit`` вместо ``run``.
        """
        batch = [tuple(row) for row in rows]
        conn_.batches.append((sql, batch))
        conn_.templates.append(template)
        return len(batch)

    module.run = run  # type: ignore[attr-defined]
    module.try_submit = try_submit  # type: ignore[attr-defined]
    module.execute_values_on = execute_values_on  # type: ignore[attr-defined]
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
        assert service.submit(lambda conn: 42, audience=JOB_AUDIENCE_MODEL) == 42

    def test_accept_never_raises_and_returns_accepted(self, service: DataService) -> None:
        assert service.log_event(AGENT_COMPLETED, summary="ок") == "accepted"

    def test_accept_does_not_write_immediately(self, service: DataService) -> None:
        """Неблокирующий вход не должен ждать воркер пула.

        Если запись события ждёт соединения, запрос модели начинает
        конкурировать с журналом — и проигрывает, потому что потеря события
        безвозвратна и не сопровождается ошибкой.
        """
        service.log_event(AGENT_COMPLETED)
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
        svc.submit(lambda conn: None, audience=JOB_AUDIENCE_MODEL)
        statements = [sql for sql, _ in db.conn.statements]  # type: ignore[union-attr]
        assert "SET statement_timeout = 1234" in statements
        assert statements[-1].strip() == "SET statement_timeout = 0"

    def test_timeout_is_reset_even_when_job_fails(self) -> None:
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)

        def boom(conn: object) -> None:
            raise RuntimeError("сбой")

        with pytest.raises(InfrastructureError):
            svc.submit(boom, audience=JOB_AUDIENCE_MODEL)
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

    def test_report_lists_the_tables_actually_checked(self) -> None:
        """Ответ обязан содержать ИМЕНА, а не только их количество.

        Агент сверяет по этому списку свои профильные таблицы с таблицами
        платформы: без имён расхождение (перекрыли в одном файле, а не в
        другом) видно только по содержимому боевого журнала.
        """
        rows = [("public", "a"), ("public", "b")]
        svc = _service(
            db=_fake_db(rows),
            expected_tables=("public.a", "public.b"),
            buffer_flush_interval=0.0,
        )
        report = svc.schema_check()
        assert sorted(report["tables"]) == ["public.a", "public.b"]
        assert report["expected"] == 2


# --- log_event -------------------------------------------------------------


def _top_level_count(template: str) -> int:
    """Число выражений в скобках шаблона — без вложенности и строковых литералов."""
    inner = template.strip()[1:-1]
    depth = 0
    quoted = False
    count = 1
    for char in inner:
        if char == "'":
            quoted = not quoted
            continue
        if quoted:
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count


def _driver_accepts(sql: str) -> None:
    """Разборщик самого драйвера обязан принять этот SQL.

    Это ровно тот разборщик, который держал журнал пустым: ``execute_values``
    зовёт ``_split_sql`` и требует ровно одного ``%s``. Имя приватное — да,
    и это осознанно: единственная проверка, которая говорит с драйвером, а не
    с нашим представлением о нём. Если psycopg2 её уберёт, тест падает
    вслух — контракт перепроверят, а не перестанут проверять молча.

    Шаблон сюда НЕ подаётся: его драйвер не разбирает, а подставляет в
    готовую строку от её длины, поэтому ``_split_sql`` на нём падает
    законно и ничего не говорит о вставке.
    """
    from psycopg2.extras import _split_sql

    _split_sql(sql.encode("utf-8"))


class TestLogEvent:
    def test_empty_event_type_rejected(self, service: DataService) -> None:
        with pytest.raises(InvalidRequestError, match="event_type"):
            service.log_event("  ")

    def test_stats_expose_buffer(self, service: DataService) -> None:
        service.log_event(AGENT_STARTED)
        stats = service.stats()
        assert stats["event_buffer"]["pending"] == 1
        assert stats["max_rows"] > 0

    # -- запись батча -------------------------------------------------------

    def test_flush_writes_one_batch_per_flush(self) -> None:
        """Сброс батча реально доходит до БД — одной вставкой на партию.

        Регрессия: ``_write_events`` звал ``execute(sql, rows)``, а контракт
        ``db.execute(sql, *args)`` — один параметр на плейсхолдер. Список
        строк уходил в санитизацию как единственный параметр, ``clean_text``
        на нём падал, и ``log_event`` отвечал «accepted», не записав
        ничего. На фейковом пуле это не ловилось: падение жило внутри
        ``EventBuffer.flush``, который глотает исключение по замыслу.

        Считается партия, а не строки: сброс ходит в пул через ``try_submit``
        и пишет через ``execute_values_on`` именно затем, чтобы обойтись
        одним воркером на весь батч. Возврат к построчной записи занял бы
        место в пуле на всё время обхода — и на тесте этого было бы не видно,
        потому что фейковый пул безусловно выдаёт место.
        """
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)
        svc.log_event(TOOL_STARTED, summary="первое")
        svc.log_event(TOOL_COMPLETED, name="tool", session_id="s1", user_id="u1")
        svc._buffer.flush()

        assert len(db.conn.batches) == 1, (  # type: ignore[union-attr]
            f"ожидалась одна вставка на партию, получили {db.conn.batches!r}"  # type: ignore[union-attr]
        )
        sql, rows = db.conn.batches[0]  # type: ignore[union-attr]
        assert sql.lstrip().startswith("INSERT"), sql
        assert len(rows) == 2, f"в партии должны быть оба события, получили {rows!r}"

    def test_journal_insert_matches_execute_values_contract(self) -> None:
        """SQL сброса обязан быть исполнимым для ``execute_values``.

        Проверять тут нечего ровно до первого реального INSERT: фейковый пул
        принимает любой текст, а ``EventBuffer.flush`` глотает отказ по замыслу.
        """
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)
        svc.log_event(TOOL_STARTED, payload={"k": "v"}, session_id="s1")
        svc._buffer.flush()

        sql, rows = db.conn.batches[0]  # type: ignore[union-attr]
        assert len(rows) == 1, rows
        row = rows[0]
        assert sql.count("%s") == 1, (
            f"execute_values требует ровно один плейсхолдер в SQL, а тут "
            f"{sql.count('%s')}: {sql}"
        )
        # Якорь не обёрнут в скобки. В скобках драйвер не разворачивает список
        # строк, а склеивает их в ОДНУ строку из записей — и PostgreSQL
        # отвечает «больше целевых столбцов, чем выражений».
        assert sql.rsplit("VALUES", 1)[1].strip() == "%s", sql
        # Шаблон передан: без него драйвер выводит число плейсхолдеров из
        # длины кортежа и не знает про `now()`.
        template = db.conn.templates[0]  # type: ignore[union-attr]
        assert template is not None, "execute_values без template не знает про now()"
        assert template.count("%s") == len(row), f"{template} против {row}"
        # Выражений в шаблоне столько же, сколько колонок в списке.
        columns = sql[sql.index("(") + 1 : sql.rindex(") VALUES")]
        assert _top_level_count(template) == columns.count(",") + 1, (
            f"{template} против списка колонок {columns}"
        )
        # И наконец — разборщик драйвера.
        _driver_accepts(sql)
        assert json.loads(row[5]) == {"k": "v"}

    def test_failed_flush_is_counted_not_raised(self) -> None:
        """Ошибка записи не поднимается наружу, но видна в счётчике."""
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)
        svc.log_event(TOOL_STARTED)

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("db down")

        # Падать должен путь, по которому журнал уходит в базу, то есть
        # постановка без ожидания. Раньше это был ``run``; подмена ``run``
        # после волны классов работ проверяла бы уже не тот путь.
        db.try_submit = _boom  # type: ignore[attr-defined]
        svc._buffer.flush()

        assert svc.stats()["event_buffer"]["flush_errors"] == 1
        assert svc.stats()["event_buffer"]["written"] == 0

    def test_busy_pool_defers_the_flush_instead_of_losing_events(self) -> None:
        """Занятый пул откладывает сброс, а не теряет партию.

        Это единственная новая гарантия журнала. Сброс ходит в пул через
        ``try_submit``, который не ждёт места, поэтому «место занято» —
        нормальный исход, а не авария: партия обязана вернуться в буфер и
        уйти следующим тиком. Считать отказ тем же числом, что и потерю
        (``dropped``), нельзя — по нему судят, теряется ли журнал, а он не
        теряется. Отдельно от потери: обычная ошибка записи батч отбрасывает
        и увеличивает ``flush_errors``, потому что повторять тот же батч
        бесконечно — значит забить память событиями, которые никто не запишет.
        """
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)
        svc.log_event(TOOL_STARTED, summary="событие")
        db.free_workers = 0  # type: ignore[attr-defined]

        assert svc._buffer.flush() == 0

        stats = svc.stats()["event_buffer"]
        assert stats["flush_rejected"] == 1, stats
        assert stats["dropped"] == 0, "отказ пула — не потеря события"
        assert stats["flush_errors"] == 0, "отказ пула — не ошибка записи"
        assert stats["written"] == 0
        assert stats["pending"] == 1, "партия обязана вернуться в буфер целиком"
        assert db.conn.batches == []  # type: ignore[union-attr]

        # Место освободилось — следующий тик проходит без потерь.
        db.free_workers = 1  # type: ignore[attr-defined]
        assert svc._buffer.flush() == 1
        assert svc.stats()["event_buffer"]["written"] == 1
        assert svc.stats()["event_buffer"]["flush_rejected"] == 1
        assert len(db.conn.batches) == 1  # type: ignore[union-attr]

    def test_flush_never_waits_for_a_free_worker(self) -> None:
        """Сброс не должен ждать место: иначе журнал становится виновником
        задержки запроса модели.

        Журнал — побочный след оборота. Если его сброс встаёт в очередь за
        местом в пуле, запрос модели конкурирует с журналом и проигрывает:
        потеря события безвозвратна и не сопровождается ошибкой. Проверка
        на подставном пуле возможна только потому, что ``try_submit`` считает
        места: при нуле он обязан отказать, а не выполнить задание.
        """
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)
        svc.log_event(TOOL_STARTED)
        db.free_workers = 0  # type: ignore[attr-defined]

        svc._buffer.flush()  # не бросает наружу

        assert db.conn.batches == []  # type: ignore[union-attr]
        assert svc.stats()["event_buffer"]["pending"] == 1

    def test_journal_flush_runs_in_the_runtime_job_class(self) -> None:
        """Журнал — внутренний поток платформы, а не работа модели.

        Сброс ждёт места без ожидания и повторяется следующим тиком, то есть
        занимает место в классе намеренно и надолго. В классе модели, где
        место ограничено и за это отвечает вызов инструмента агента, журнал
        вытеснил бы работу, ради которой он и пишется.
        """
        db = _fake_db()
        svc = _service(db=db, buffer_flush_interval=0.0)
        svc.log_event(TOOL_STARTED)
        svc._buffer.flush()
        assert db.audiences == [AUDIENCE_RUNTIME]  # type: ignore[union-attr]
