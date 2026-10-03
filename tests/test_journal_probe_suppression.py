"""Пробные имена обязаны не доходить до продовой таблицы с ОБЕИХ сторон.

Дефект, который файл закрывает. Правило «Пробные события не пишутся в продовую
таблицу» объявлено один раз, у платформы
(``mcp-platform/libs/enterprise_common/eventing/types.py``:
``PROBE_EVENT_PREFIXES``, ``PROBE_EVENT_NAMES``, ``is_probe_event_type``), и
применяется на обоих её входах журнала (``DataService._suppress_probe``:
``log_event`` и ``log_events[...]``). **В агенте фильтра не было вообще**:
поиск по дереву агента не находил ни ``probe``, ни ``is_probe_event_type``, то
есть пробное событие, отправленное агентом, уходило в таблицу обычной строкой,
тогда как то же имя от платформы подавлялось. Замер по боевой базе: 20 пробных
строк (``live.db_probe``, ``probe_write_batch``, ``smoke.e2e`` и др.), у всех
``actor IS NULL`` — писала платформа.

Почему нельзя «просто импортировать правило». Агент не импортирует ничего из
``mcp-platform/`` и не должен: платформа — отдельная поставка, поднимаемая
подпроцессом со своим ``sys.path``, и по границе процессов уходит только имя
контура. Сведение сторон импортом сломало бы ровно эту границу (тот же запрет
без импорта держит ``tests/test_journal_event_name_alignment.py``). Поэтому
правило копируется, а расхождение поведения запрещает страж — поведением, по
всей области входов, а не равенством словарей.

Проверяется четыре вещи:

* **объявление** совпадает с платформенным (AST, без импорта) — чтобы новое
  пробное имя, добавленное у платформы, требовало решения и здесь, а не
  разъезжалось молча;
* **поведение** на пробных именах: событие снято, счётчик растёт, имя названо
  один раз;
* **поведение** на рабочих именах: обычные события оборота не сняты (правило
  не должно превратиться в «не писать ничего»);
* **порядок**: пробное имя проверяется раньше уровня, как на платформенном
  входе, — проба приходит снаружи и про уровень своего не знает.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

import pytest

AGENT_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_TYPES = (
    AGENT_ROOT / "mcp-platform" / "libs" / "enterprise_common" / "eventing" / "types.py"
)
SERVICE_LOGGER = "lib.services.db_logging_service"

#: Имена, которые требование называет пробными. Не «примеры» — выборка из
#: боевой таблицы, где такие строки нарисовала платформа.
PROBE_NAMES: tuple[str, ...] = (
    "live.db_probe",
    "probe_write_batch",
    "probe_",
    "smoke.e2e",
    "smoke.",
    "SMOKE.E2E",
    "  probe_x  ",
)

#: Рабочие имена оборота. Ни одно не начинается с ``smoke.``/``probe_`` и не
#: равно ``live.db_probe`` — значит, сняты быть не могут. Сюда же — имя, которое
#: похоже на пробное, но правилом не названо: ``live.db_probe_v2`` отличается от
#: ``live.db_probe`` только суффиксом, и ловить его «на глаз» нельзя — тогда
#: правило разъедется с платформенным.
WORKLOAD_NAMES: tuple[str, ...] = (
    "agent.started",
    "agent.responded",
    "tool.started",
    "tool.completed",
    "llm.exchanged",
    "quality.check",
    "artifact.created",
    "live.db_probe_v2",
    "not_smoke.test",
    "smoketest.x",
)


def _platform_probe_rule() -> tuple[tuple[str, ...], frozenset[str]]:
    """Прочитать правило платформы как данные, без импорта пакета.

    Импорт ``libs.*`` поднял бы платформу в процесс тестов агента, а правило
    нужно здесь как список строк, и он лежит в дереве рядом.
    """
    tree = ast.parse(PLATFORM_TYPES.read_text(encoding="utf-8"), filename=str(PLATFORM_TYPES))
    prefixes: tuple[str, ...] | None = None
    names: frozenset[str] | None = None
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id == "PROBE_EVENT_PREFIXES" and isinstance(node.value, ast.Tuple):
            prefixes = tuple(
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant)
            )
        elif node.target.id == "PROBE_EVENT_NAMES" and isinstance(node.value, ast.Call):
            names = frozenset(
                element.value
                for element in node.value.args[0].elts  # type: ignore[union-attr]
                if isinstance(element, ast.Constant)
            )
    assert prefixes and names, (
        "правило пробных имён у платформы перестало быть читаемым как данные — "
        "либо объявление переехало, либо страж молчит"
    )
    return prefixes, names


def _service(min_level: str = "DEBUG") -> Any:
    """Настоящий писатель без worker-потока (``start()`` не зовётся)."""
    from lib.services.db_logging_service import DbLoggingService

    return DbLoggingService(
        dsn="", table_name="x", question_runs_table="y", min_level=min_level
    )


def _log(service: Any, event_type: str, level: str = "INFO") -> bool:
    from lib.services.db_logging_service import LogEvent

    return service.log_event(LogEvent(event_type=event_type, level=level))


class TestAgentDeclaresTheSameProbeRule:
    def test_prefixes_and_names_match_the_platform(self) -> None:
        """Копия правила обязана совпадать с объявленной у платформы.

        Набор не «примерно тот же»: добавление пробного имени у платформы
        обязано требовать решения здесь же, иначе третья проба снова окажется в
        продовой таблице по вине одной стороны.
        """
        from lib.services.db_logging_service import PROBE_EVENT_NAMES, PROBE_EVENT_PREFIXES

        platform_prefixes, platform_names = _platform_probe_rule()
        assert tuple(PROBE_EVENT_PREFIXES) == platform_prefixes
        assert frozenset(PROBE_EVENT_NAMES) == platform_names

    def test_the_declared_rule_is_the_spec_one(self) -> None:
        """Правило — ровно то, что названо требованием, без добавок.

        Расширение правила «на всякий случай» выглядит безобидно, пока не
        отнимет у реального события право попасть в журнал.
        """
        from lib.services.db_logging_service import PROBE_EVENT_NAMES, PROBE_EVENT_PREFIXES

        assert tuple(PROBE_EVENT_PREFIXES) == ("smoke.", "probe_")
        assert frozenset(PROBE_EVENT_NAMES) == {"live.db_probe"}


class TestProbeEventsNeverReachTheTable:
    @pytest.mark.parametrize("event_type", PROBE_NAMES)
    def test_probe_event_is_not_queued(self, event_type: str) -> None:
        service = _service()
        assert _log(service, event_type) is False
        assert service._queue.qsize() == 0, "пробное имя ушло в очередь записи"

    @pytest.mark.parametrize("event_type", WORKLOAD_NAMES)
    def test_workload_event_is_queued(self, event_type: str) -> None:
        service = _service()
        assert _log(service, event_type) is True
        assert service._queue.qsize() == 1, (
            f"рабочее имя {event_type!r} снято пробным правилом"
        )

    def test_suppressed_probe_gets_no_event_time(self) -> None:
        """Снятое событие не метит момент события — как и отброшенное по порогу.

        Иначе след подавления остался бы в статистике события, которого в
        журнале нет.
        """
        from lib.services.db_logging_service import LogEvent

        service = _service()
        event = LogEvent(event_type="smoke.e2e")
        assert service.log_event(event) is False
        assert event.metadata is None

    def test_probe_is_checked_before_level(self) -> None:
        """Порядок как на платформенном входе: сначала проба, потом уровень.

        Пробное имя приходит извне и про уровень своего не знает; если бы
        уровень проверялся первым, проба с неразобранным уровнем падала бы с
        отказом по шкале и попадала в счётчик отказов, а не в счётчик проб.
        """
        from lib.services.db_logging_service import LogEvent

        service = _service(min_level="DEBUG")
        # Уровень вне шкалы НЕ отказывает пробное событие: проба снята раньше.
        assert service.log_event(LogEvent(event_type="smoke.e2e", level="BOGUS")) is False
        stats = service.get_stats()
        assert stats["rejected_levels"] == {}, (
            "пробное имя дошло до разбора уровня — порядок проверок нарушен"
        )
        assert stats["suppressed_probe_events"] == {"smoke.e2e": 1}

        # Контроль: то же значение уровня у обычного имени отказывает — значит
        # порядок проверяется, а не просто отключился разбор уровней.
        assert service.log_event(LogEvent(event_type="agent.started", level="BOGUS")) is False
        assert service.get_stats()["rejected_levels"] == {"BOGUS": 1}


class TestSuppressionIsVisible:
    def test_counter_is_reported_in_stats(self) -> None:
        service = _service()
        for _ in range(3):
            _log(service, "live.db_probe")
        _log(service, "probe_write_batch")
        _log(service, "agent.started")

        stats = service.get_stats()
        assert stats["suppressed_probe_events"] == {
            "live.db_probe": 3,
            "probe_write_batch": 1,
        }
        assert stats["written"] == 0
        # Счётчик назван тем же ключом, что у платформы
        # (``DataService.stats()``): журнал читается по одному набору полей.
        assert "suppressed_probe_events" in service.report_stats()

    def test_name_is_named_once_not_per_event(self, caplog: Any) -> None:
        """Поток проб не превращает предупреждение в шум.

        Один раз на имя, а не на событие: иначе предупреждение, ради которого
        его поднимают, станет самым частым текстом в логе.
        """
        service = _service()
        with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
            for _ in range(5):
                _log(service, "smoke.e2e")
            _log(service, "live.db_probe")

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING and "smoke.e2e" in record.getMessage()
        ]
        assert len(warnings) == 1, f"имя пробного события названо {len(warnings)} раз"
        assert any("live.db_probe" in r.getMessage() for r in caplog.records)

    def test_refusal_names_the_rule_and_where_it_lives(self, caplog: Any) -> None:
        """Сообщение обязано называть правило, а не только факт отказа."""
        service = _service()
        with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
            _log(service, "probe_write_batch")

        message = "\n".join(
            record.getMessage()
            for record in caplog.records
            if "probe_write_batch" in record.getMessage()
        )
        assert "suppressed_probe_events" in message
        assert "eventing/types.py" in message, (
            "отказ не называет место, где правило объявлено — искать придётся вслепую"
        )

    def test_a_quiet_run_says_nothing(self, caplog: Any) -> None:
        service = _service()
        with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
            for _ in range(3):
                _log(service, "tool.started")

        assert [r.getMessage() for r in caplog.records] == []
        assert service.get_stats()["suppressed_probe_events"] == {}
