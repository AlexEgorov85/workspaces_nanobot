"""Операции журналирования: ``upsert_question_run`` и ``purge_logs``.

Проверяется не «функция вызвалась», а свойства, которые ломаются тихо:

* **нет ``ON CONFLICT``** — Greenplum 6.5 (PostgreSQL 9.4) его не поддерживает,
  и ошибка возникает не в тесте, а на боевой базе при первом же вопросе. Upsert
  обязан быть двухшаговым: UPDATE, затем INSERT ... WHERE NOT EXISTS;
* **второй шаг не теряет гонку** — «строки нет и после UPDATE» закрывается
  именно INSERT'ом с проверкой NOT EXISTS, а не «и так сработает»;
* ``update_only`` **не затирает контекст вопроса** — на этом строится завершение
  прогона: если бы полный набор колонок записался повторно, вопрос и личность
  потерялись бы в момент завершения;
* пустой ``request_id`` — отказ, а не запись: строка без ключа не upsert-ится,
  и ошибка при этом была бы тихой потерей контекста вопроса;
* ``retention_days <= 0`` не удаляет старые записи, но чистит пустые
  stream-чанки — они не несут смысла ни в какой момент.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from libs.enterprise_common.errors import InvalidRequestError

from servers.enterprise.capabilities.data.service.main import DataService

LOGS = ("public", "agent_gateway_logs")
RUNS = ("public", "agent_question_runs")


class FakeCursor:
    def __init__(self, log: list[tuple[str, list[Any]]], rowcounts: dict[str, int]) -> None:
        self._log = log
        self._rowcounts = rowcounts
        self._description = None
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, list(params or [])))
        head = sql.lstrip().split()[0].upper()
        # По умолчанию запись, которая что-то затронула, сообщает об этом:
        # сервис обязан отличать запись от её отсутствия по ``rowcount``, и
        # фейк, у которого UPDATE и INSERT всегда дают 0, проверял бы ровно
        # тот мир, где операция врёт. ``DELETE`` остаётся нулём — эти тесты
        # проверяют чистку, которой по умолчанию ничего не удаляет.
        default = 1 if head in ("UPDATE", "INSERT") else 0
        self.rowcount = self._rowcounts.get(head, default)

    def close(self) -> None:
        return None


class FakeConn:
    def __init__(self, log: list[tuple[str, list[Any]]], rowcounts: dict[str, int]) -> None:
        self._log = log
        self._rowcounts = rowcounts

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._log, self._rowcounts)


class FakePool:
    def __init__(self, rowcounts: dict[str, int] | None = None) -> None:
        self.log: list[tuple[str, list[Any]]] = []
        self._rowcounts = rowcounts or {}

    def run(self, job: Any) -> Any:
        return job(FakeConn(self.log, self._rowcounts))


def _service(pool: FakePool | None = None) -> DataService:
    return DataService(db=pool or FakePool(), log_table=LOGS, question_runs_table=RUNS)


def _sql(pool: FakePool) -> str:
    return "\n".join(statement for statement, _ in _statements(pool))


def _statements(pool: FakePool) -> list[tuple[str, list[Any]]]:
    """Заявленные операции без служебной обёртки.

    ``DataService.submit`` выполняет ``SET statement_timeout = ...`` перед
    заданием на воркере пула. Это его забота, и в разбор попадать не должно:
    иначе индексы первого оператора смещаются, и тест проверял бы обёртку.
    """
    return [
        entry
        for entry in pool.log
        if not entry[0].lstrip().upper().startswith("SET STATEMENT_TIMEOUT")
    ]


class TestGreenplumCompatibility:
    """Целевая база — Greenplum 6.5. Это не теория, а причина отказа."""

    def test_no_on_conflict_anywhere(self) -> None:
        """``ON CONFLICT DO UPDATE`` появился в Greenplum 7.

        На 6.5 такой upsert падает на первой же записи, и падает в бою, а не в
        тесте: тесты с фейковым курсором не знают про диалект.
        """
        pool = FakePool()
        _service(pool).upsert_question_run("req-1", question="вопрос")
        assert "ON CONFLICT" not in _sql(pool).upper(), _sql(pool)

    def test_upsert_is_two_step_update_then_insert(self) -> None:
        pool = FakePool()
        _service(pool).upsert_question_run("req-1", question="вопрос")
        heads = [statement.lstrip().split()[0].upper() for statement, _ in _statements(pool)]
        assert heads == ["UPDATE", "INSERT"], heads

    def test_insert_guards_against_race_with_not_exists(self) -> None:
        """Второй шаг обязан проверять отсутствие строки.

        Без ``WHERE NOT EXISTS`` параллельный вызов вставил бы дубликат по
        первичному ключу и упал бы на целостности, а не на нашем коде.
        """
        pool = FakePool()
        _service(pool).upsert_question_run("req-1", question="вопрос")
        insert_sql = _statements(pool)[1][0].upper()
        assert "WHERE NOT EXISTS" in insert_sql, insert_sql

    def test_retention_interval_avoids_make_interval(self) -> None:
        """``make_interval`` в Greenplum 6.5 нет — интервал собирается строкой."""
        pool = FakePool()
        _service(pool).purge_logs(30)
        assert "make_interval" not in _sql(pool).lower(), _sql(pool)
        assert "|| ' days')::interval" in _sql(pool), _sql(pool)


class TestQuestionRunUpsert:
    def test_empty_request_id_is_refused(self) -> None:
        """Строка без ключа не upsert-ится: отказ, а не тихая потеря."""
        pool = FakePool()
        with pytest.raises(InvalidRequestError):
            _service(pool).upsert_question_run("   ", question="вопрос")
        assert not _statements(pool), "запрос ушёл в базу при пустом request_id"

    def test_full_upsert_writes_context_columns(self) -> None:
        pool = FakePool()
        _service(pool).upsert_question_run(
            "req-1",
            session_id="s",
            user_id="u",
            chat_id="c",
            channel="cli",
            agent_id="a",
            parent_request_id="p",
            is_subagent=True,
            status="running",
            question="вопрос",
        )
        sql = _sql(pool)
        for column in ("session_id", "user_id", "chat_id", "is_subagent", "question"):
            assert column in sql, f"колонка {column} не пишется: {sql}"

    def test_update_only_does_not_touch_context(self) -> None:
        """Завершение прогона не должно стирать вопрос и личность.

        Это главный риск ``update_only``: полный набор колонок при повторной
        записи уничтожил бы контекст вопроса в тот самый момент, когда он
        больше всего нужен — при закрытии прогона.
        """
        pool = FakePool()
        _service(pool).upsert_question_run(
            "req-1", status="finished", summary="итог", response="ответ", update_only=True
        )
        update_sql = _statements(pool)[0][0]
        # Проверяется список колонок в SET, а не текст SQL целиком: слово
        # ``question`` есть в имени самой таблицы, и проверка подстроки
        # запрещала бы колонку из-за имени таблицы.
        set_clause = update_sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        for column in ("user_id", "question", "session_id", "chat_id", "channel"):
            assert f"{column} =" not in set_clause, f"{column} затирается: {set_clause}"
        assert "COALESCE" in set_clause, "ответ и media должны переживать None"

    def test_media_is_serialised_to_text(self) -> None:
        """``media`` живёт в TEXT-колонке: в базу идёт JSON-строка, а не список."""
        pool = FakePool()
        _service(pool).upsert_question_run("req-1", media=[{"path": "a.txt"}])
        params = _statements(pool)[1][1]
        assert any(isinstance(value, str) and value.startswith("[") for value in params), params

    def test_media_none_stays_none(self) -> None:
        """Пустой media не превращается в строку ``"[]"``: это разные вещи."""
        pool = FakePool()
        _service(pool).upsert_question_run("req-1")
        params = _statements(pool)[1][1]
        assert None in params, params


class TestPurgeLogs:
    def test_zero_retention_keeps_old_rows(self) -> None:
        pool = FakePool()
        counters = _service(pool).purge_logs(0)
        # Чистка пустых outbound-чанков не зависит от retention.
        assert any("outbound_final" in s for s, _ in _statements(pool))
        assert counters["events"] == 0 and counters["question_runs"] == 0

    def test_cleans_the_outbound_types_that_actually_exist(self) -> None:
        """Чистка бьёт по тем типам, которые агент действительно пишет.

        Регрессия: в списке стоял ``outbound_delta`` — типа, которого в базе
        нет ни одной строки, то есть вечный no-op, — а ``outbound_intermediate``
        (пустые чанки потока) не вычищался никогда. Проверка идёт по SQL, а не
        по константе: иначе переименование обеих строк прошло бы молча.
        """
        pool = FakePool()
        _service(pool).purge_logs(0)
        delete = next(s for s, _ in _statements(pool) if "outbound" in s)
        assert "'outbound_final'" in delete, delete
        assert "'outbound_intermediate'" in delete, delete
        assert "outbound_delta" not in delete, delete

    def test_empty_outbound_keeps_rows_with_media(self) -> None:
        """Реальная отправка файла с пустым текстом — не мусор.

        У такой строки есть ``media``, и удаление стёрло бы сам факт доставки.
        """
        pool = FakePool()
        _service(pool).purge_logs(0)
        sql = _sql(pool)
        assert "(payload->'media') IS NULL" in sql, sql

    def test_retention_deletes_from_both_tables(self) -> None:
        pool = FakePool()
        _service(pool).purge_logs(30)
        deletes = [s for s, _ in _statements(pool) if s.lstrip().upper().startswith("DELETE")]
        assert len(deletes) == 3, deletes
        assert any('"public"."agent_gateway_logs"' in s for s in deletes)
        assert any('"public"."agent_question_runs"' in s for s in deletes)

    def test_negative_retention_is_refused(self) -> None:
        """Отрицательное значение стёрло бы всё, включая свежее."""
        pool = FakePool()
        with pytest.raises(InvalidRequestError):
            _service(pool).purge_logs(-1)
        assert not _statements(pool), "запрос ушёл в базу при отрицательном retention"

    def test_non_integer_retention_is_refused(self) -> None:
        pool = FakePool()
        with pytest.raises(InvalidRequestError):
            _service(pool).purge_logs("много")  # type: ignore[arg-type]
        assert not pool.log

    def test_can_skip_empty_outbound(self) -> None:
        pool = FakePool()
        _service(pool).purge_logs(0, remove_empty_outbound=False)
        assert not any("outbound" in s for s, _ in _statements(pool))


class TestRetentionIsTheServersRule:
    """Срок хранения объявлен в ``platform.json`` и обязан применяться сервером.

    Раньше обе настройки читались реестром, но не читались кодом: очистка
    получала срок аргументом, а ``data.log_retention_days = 90`` был
    декорацией. Агент свою копию журнала больше не ведёт, поэтому «кто задаёт
    срок» больше не вопрос двух владельцев — это платформа.
    """

    def _configured(self, pool: FakePool, **kw: Any) -> DataService:
        return DataService(
            db=pool, log_table=LOGS, question_runs_table=RUNS, **kw
        )

    def test_no_argument_uses_configured_retention(self) -> None:
        pool = FakePool()
        self._configured(pool, log_retention_days=90).purge_logs()
        deletes = [s for s, _ in _statements(pool) if s.lstrip().upper().startswith("DELETE")]
        assert any('"public"."agent_question_runs"' in s for s in deletes), deletes

    def test_no_argument_uses_configured_empty_outbound(self) -> None:
        pool = FakePool()
        self._configured(pool, log_retention_days=0, purge_empty_outbound=False).purge_logs()
        assert not any("outbound" in s for s, _ in _statements(pool))

    def test_argument_overrides_configuration(self) -> None:
        """Аргумент остаётся переопределением, иначе разовая чистка невозможна."""
        pool = FakePool()
        self._configured(pool, log_retention_days=90).purge_logs(0)
        counters_stmts = _statements(pool)
        assert not any(
            '"public"."agent_question_runs"' in s for s, _ in counters_stmts
        ), counters_stmts

    def test_defaults_need_no_configuration(self) -> None:
        """Сервис без настроек не падает и по умолчанию ничего не вычищает."""
        pool = FakePool()
        counters = _service(pool).purge_logs()
        assert counters == {"empty_outbound": 0, "events": 0, "question_runs": 0}



class TestOperationsAreNotModelFacing:
    """Обе операции служебные: модель не должна ни писать в журнал, ни удалять."""

    def test_operations_exist_and_are_runtime_only(self) -> None:
        from libs.enterprise_common.container import ToolContainer

        from servers.enterprise.capabilities.data.tools.purge_logs import (
            create_tool as create_purge,
        )
        from servers.enterprise.capabilities.data.tools.upsert_question_run import (
            create_tool as create_upsert,
        )

        container = ToolContainer(services={"data": _service()})
        for factory in (create_upsert, create_purge):
            tool = factory(container)
            assert "runtime-only" in tool.tags, tool.name
            assert tool.permissions, tool.name
            assert "sql" not in inspect.signature(tool.handler).parameters

    def test_purge_has_no_free_form_target(self) -> None:
        """Удалять можно только по сроку хранения, а не «эти строки».

        Произвольная цель удаления в аргументе — это операция ``DELETE FROM
        переданная_строка``, и никакая проверка её не спасёт.
        """
        from libs.enterprise_common.container import ToolContainer

        from servers.enterprise.capabilities.data.tools.purge_logs import (
            create_tool as create_purge,
        )

        container = ToolContainer(services={"data": _service()})
        tool = create_purge(container)
        properties = set((tool.input_schema.get("properties") or {}))
        assert properties <= {"retention_days", "remove_empty_outbound"}, properties
