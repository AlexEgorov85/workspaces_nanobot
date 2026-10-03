"""Пробные имена не пишутся в продовую таблицу журнала.

Дефект, который файл закрывает, — не «имена выглядят неопрятно». Замер на боевой
базе дал 20 строк за двое суток (``live.db_probe`` — 6, ``probe_write_batch`` —
6, ``probe_data`` — 2, ``probe_data_batch`` — 2, ``probe_write_check`` — 2,
``smoke.e2e`` — 2), и у **всех** ``actor IS NULL``: писала платформа.

Причина в том, где стояло правило. Оно было объявлено и применялось только в
``libs/enterprise_common/eventing/writer.py`` (метод ``emit``), а ``DataService``
в этот writer не заходит: он пишет своим ``_write_events`` (сырой ``INSERT``).
Операция ``log_events`` — это и есть путь, которым агент сбрасывает батч, и
через который любое имя извне уходит в ``agent_gateway_logs`` без фильтра.

Проверяется не «функция вызвалась», а **куда именно ушло событие**:

* **проба не доходит до БД** — ни одного ``INSERT`` в таблицу журнала, а не
  «вернулось ``dropped`` и где-то там ничего не записалось»;
* **остальной батч выживает** — одно пробное имя среди событий оборота не
  уносит остальные: отказ всем вызовом здесь означал бы «одна проба стоила
  журнала всего оборота»;
* **отказ виден, а не молчалив** — событие учтено в ``dropped``, имя названо
  в operational-логе процесса, счётчик виден в ``stats()``. Молчащая вычистка
  неотличима от «проб не было»;
* **правило одно** — ответ сервиса совпадает с ``is_probe_event_type`` на всём
  объявленном множестве. Своя копия списка разъехалась бы с объявленной при
  первой же правке, и это ровно тот класс дефекта, который файл закрывает.

Права на запись. Тест не подключается к базе вовсе: пул ниже только запоминает
заявления, а имена таблиц берутся из объявления тестового профиля
(``platform.json``), поэтому тест не знает боевых имён и остаётся безопасным
независимо от того, куда направлен профиль. Живой прогон здесь потребовал бы
``seq``/``occurred_at`` на тестовой таблице, которых в ней ещё нет, и проверял
бы форму SQL, а не результат.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from libs.enterprise_common.eventing.types import (
    PROBE_EVENT_NAMES,
    PROBE_EVENT_PREFIXES,
    TOOL_STARTED,
    is_known,
)

from servers.enterprise.capabilities.data.service.main import DataService

SERVICE_LOGGER = "servers.enterprise.capabilities.data.service.main"

#: Имена из замера на боевой базе — обе формы: префиксы и точное имя.
SMOKE = "smoke.e2e"
PROBE_PREFIX = "probe_write_batch"
EXACT_NAME = "live.db_probe"


# ---------------------------------------------------------------------------
# Записывающий пул: ни одного соединения, только факт и текст заявлений
# ---------------------------------------------------------------------------


class _Cursor:
    """Курсор, который помнит ``execute`` и ничего не выполняет."""

    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))

    @property
    def rowcount(self) -> int:
        return 0


class _Conn:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    def cursor(self) -> _Cursor:
        return _Cursor(self._log)


class _RecordingPool:
    """Пул, который помнит заявления и НЕ подключается к базе.

    Отдельный класс, а не подмена ``_write_events``: доказательство должно
    быть о том, что дошло до уровня SQL, а не о том, какая функция вызвана.
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.connections = 0

    def run(self, job: Any) -> Any:
        self.connections += 1
        return job(_Conn(self.statements))


def _journal_table() -> tuple[str, str]:
    """Таблица журнала ТЕСТОВОГО профиля — как объявила платформа.

    Имя не зашито: тест не знает боевой таблицы и не может случайно проверить
    не то. Суффикс ``_test`` проверяется здесь же — профиль, направленный на
    боевую таблицу, обязан остановить тест, а не записать в неё.
    """
    from libs.enterprise_common.settings import read_profile_overlay

    overlay = read_profile_overlay("test")
    schema, _, name = overlay.get("data.log_table", "").partition(".")
    if not name.strip().endswith("_test"):
        raise AssertionError(f"тест журнала опасен: в имени {name!r} нет суффикса _test")
    return schema.strip(), name.strip()


def _service(**kwargs: Any) -> tuple[DataService, _RecordingPool]:
    pool = _RecordingPool()
    service = DataService(
        db=pool,
        log_table=_journal_table(),
        question_runs_table=("public", "agent_question_runs_test"),
        **kwargs,
    )
    return service, pool


def _inserts(pool: _RecordingPool) -> list[tuple[str, Any]]:
    return [row for row in pool.statements if "INSERT INTO" in row[0]]


def _journal_events(pool: _RecordingPool) -> list[str]:
    """Типы событий, реально ушедшие в таблицу журнала.

    Порядок колонок задан в ``_write_events``: тип события — второй параметр
    после ключа строки, ``timestamp`` база подставляет сама.
    """
    out: list[str] = []
    for _sql, params in _inserts(pool):
        # Параметры приходят кортежем (в бою — тоже: контракт ``db.execute``
        # — один параметр на плейсхолдер), и приводить их к списку нельзя:
        # список в этом месте попадал в санитизацию как единственный параметр.
        row = params if isinstance(params, (list, tuple)) else [params]
        out.append(str(row[1]))
    return out


# ---------------------------------------------------------------------------
# Проба не доходит до БД
# ---------------------------------------------------------------------------


class TestProbeNeverReachesTheDatabase:
    def test_single_entry_refuses_and_writes_nothing(self) -> None:
        service, pool = _service()
        assert service.log_event(SMOKE) == "dropped"
        service._buffer.flush()
        assert _inserts(pool) == [], "пробное имя дошло до таблицы журнала"
        # Соединение не бралось вовсе: отказ случился ДО пула, а не после
        # неудачной попытки записи.
        assert pool.connections == 0

    def test_batch_entry_refuses_and_writes_nothing(self) -> None:
        service, pool = _service()
        result = service.log_events(
            [{"event_type": PROBE_PREFIX}, {"event_type": EXACT_NAME}]
        )
        service._buffer.flush()
        assert result == {"accepted": 0, "dropped": 2}, result
        assert _inserts(pool) == [], "пробное имя дошло до таблицы журнала"

    def test_exact_name_is_caught_too_not_only_the_prefixes(self) -> None:
        """``live.db_probe`` — точное имя, а не префикс.

        Проверка «на префиксы» его бы пропустила, и ровно эти 6 строк в боевой
        таблице остались бы на месте.
        """
        service, pool = _service()
        assert EXACT_NAME in PROBE_EVENT_NAMES
        service.log_event(EXACT_NAME)
        service._buffer.flush()
        assert _journal_events(pool) == []

    def test_prefix_probe_is_caught_too(self) -> None:
        service, pool = _service()
        assert any(PROBE_PREFIX.startswith(p) for p in PROBE_EVENT_PREFIXES)
        service.log_event(PROBE_PREFIX)
        service._buffer.flush()
        assert _journal_events(pool) == []

    def test_case_does_not_decide_whether_it_is_a_probe(self) -> None:
        """Регистр — не различие: в таблице это один и тот же мусор."""
        service, pool = _service()
        assert service.log_event("SMOKE.e2e") == "dropped"
        service._buffer.flush()
        assert _inserts(pool) == []


# ---------------------------------------------------------------------------
# Остальной батч выживает
# ---------------------------------------------------------------------------


class TestTheRestOfTheBatchSurvives:
    def test_ordinary_events_of_the_same_batch_are_recorded(self) -> None:
        """Батч агента смешанный: пробное имя не имеет права унести остальное."""
        service, pool = _service()
        result = service.log_events(
            [
                {"event_type": TOOL_STARTED, "name": "grep"},
                {"event_type": SMOKE, "name": "grep"},
                {"event_type": PROBE_PREFIX},
                {"event_type": TOOL_STARTED, "name": "read_file"},
            ]
        )
        service._buffer.flush()
        assert result == {"accepted": 2, "dropped": 2}, result
        assert _journal_events(pool) == [TOOL_STARTED, TOOL_STARTED]

    def test_counters_add_up_to_the_batch_size(self) -> None:
        """``accepted + dropped == len(events)``.

        Иначе вызывающий не видит, сколько именно строк не будет, и «принято 2»
        из батча в четыре события читается как «принято всё».
        """
        service, _ = _service()
        batch = [
            {"event_type": TOOL_STARTED},
            {"event_type": SMOKE},
            {"event_type": TOOL_STARTED},
        ]
        result = service.log_events(batch)
        assert result["accepted"] + result["dropped"] == len(batch), result

    def test_real_event_is_untouched_by_the_rule(self) -> None:
        """Контроль: правило не стало граблей."""
        service, pool = _service()
        assert service.log_event(TOOL_STARTED) == "accepted"
        service._buffer.flush()
        assert _journal_events(pool) == [TOOL_STARTED]


# ---------------------------------------------------------------------------
# Отказ виден, а не молчалив
# ---------------------------------------------------------------------------


class TestSuppressionIsNeverSilent:
    def test_dropped_is_reported_to_the_caller(self) -> None:
        """Вызывающий читает потери по ``dropped`` — тем же полем, что и
        переполнение буфера. Различить два вида потери можно по ``stats()``,
        спутать — нельзя."""
        service, _ = _service()
        assert service.log_event(SMOKE) == "dropped"

    def test_suppressed_names_are_visible_in_stats(self) -> None:
        """Ноль и «ничего не приходило» — разные вещи, и по журналу их не
        различить: счётчик делает вычистку наблюдаемой."""
        service, _ = _service()
        service.log_event(SMOKE)
        service.log_event(SMOKE)
        service.log_event(EXACT_NAME)
        stats = service.stats()["suppressed_probe_events"]
        assert stats == {SMOKE: 2, EXACT_NAME: 1}, stats

    def test_probe_is_named_in_the_process_log(self, caplog: Any) -> None:
        with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
            service, _ = _service()
            service.log_event(SMOKE)
        assert any(SMOKE in record.getMessage() for record in caplog.records), [
            record.getMessage() for record in caplog.records
        ]

    def test_name_is_named_once_not_per_event(self, caplog: Any) -> None:
        """Иначе вычистка шума заменила бы мусор в таблице мусором в логе."""
        with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
            service, _ = _service()
            for _ in range(5):
                service.log_event(SMOKE)
        said = [r.getMessage() for r in caplog.records if SMOKE in r.getMessage()]
        assert len(said) == 1, said

    def test_probe_is_not_counted_as_a_foreign_event_type(self) -> None:
        """Проба не должна выглядеть ещё и опечаткой в схеме имён.

        Иначе вычистка шума отчитывалась бы как «агент присылает незнакомые
        имена», и её отключение выглядело бы как починка словаря.
        """
        service, _ = _service()
        service.log_event(SMOKE)
        service.log_events([{"event_type": PROBE_PREFIX}])
        assert service.stats()["unknown_event_types"] == {}

    def test_suppression_precedes_the_strict_dictionary(self) -> None:
        """Строгий словарь не должен превращать пробу в отказ всего вызова.

        «smoke.x» нет в словаре типов никогда, и при ``strict`` проверка словаря
        упала бы с ``InvalidRequestError`` — то есть одна проба в обороте снова
        стоила бы всего батча.
        """
        service, pool = _service(unknown_event_type_policy="strict")
        assert is_known(SMOKE) is False
        assert service.log_event(SMOKE) == "dropped"
        result = service.log_events([{"event_type": PROBE_PREFIX}])
        assert result == {"accepted": 0, "dropped": 1}, result
        service._buffer.flush()
        assert _inserts(pool) == []


# ---------------------------------------------------------------------------
# Правило объявлено один раз
# ---------------------------------------------------------------------------


class TestTheRuleIsTheDeclaredOne:
    @pytest.mark.parametrize("name", sorted(PROBE_EVENT_NAMES))
    def test_declared_exact_names_are_all_refused(self, name: str) -> None:
        service, pool = _service()
        assert service.log_event(name) == "dropped"
        service._buffer.flush()
        assert _inserts(pool) == []

    @pytest.mark.parametrize("prefix", PROBE_EVENT_PREFIXES)
    def test_declared_prefixes_are_all_refused(self, prefix: str) -> None:
        service, pool = _service()
        assert service.log_event(prefix + "что-нибудь") == "dropped"
        service._buffer.flush()
        assert _inserts(pool) == []

    @pytest.mark.parametrize(
        "name", [TOOL_STARTED, "agent.started", "quality.check", "tool.probe", "probes.x"]
    )
    def test_names_outside_the_rule_pass_through(self, name: str) -> None:
        """Отсекается ровно объявленное, и ничего рядом.

        ``tool.probe`` и ``probes.x`` — соседи, которые выглядят пробными
        человеку; по объявленному правилу это обычные имена, и выдумывать
        правило шире объявленного нельзя. Политика словаря задана явно, чтобы
        тест не зависел от значения в ``platform.json``.
        """
        service, pool = _service(unknown_event_type_policy="soft")
        assert service.log_event(name) == "accepted", name
        service._buffer.flush()
        assert _journal_events(pool) == [name]
