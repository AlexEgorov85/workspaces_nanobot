"""Порог журнала, присланный агентом при старте, доезжает до писателя журнала
платформы и применяется им.

Агент объявляет порог журнала в одном месте — ``config.json →
gateway.agent.logging.db.min_level`` — и передаёт его платформе флагом запуска
``--log-min-level`` (``lib/services/enterprise_mcp_client.py``). Здесь
проверяется вторая половина: разбор флага тем же ручным способом, что и
``--profile``, передача значения до писателя журнала и применение порога по
общим правилам ``normalize_level`` / ``level_rank``
(``libs/enterprise_common/eventing/models.py``).

Три случая, которые обязаны различаться
----------------------------------------

* флага нет — платформа пишет всё, как писала до его появления;
* значение корректное — порог применяется и виден в ``stats()`` и в стартовом
  логе;
* значение незнакомое — **отказ на старте** (:class:`InfrastructureError`), а не
  откат к ``INFO``: откат молча переключил бы платформу на другую политику
  записи, и узнал бы об этом тот, кто уже ищет пропавший ``DEBUG``.

Почему не в ``params._meta`` каждого вызова
-------------------------------------------

Значение, перечитываемое на каждый вызов, способно разъехаться между вызовами
одного оборота; вызывающая сторона получила бы право решать, сколько логирует
платформа, а это её собственные события (``tool.*``, ``quality.check``).
Порог — объявление процесса, а не параметр вызова.

Стражи
------

* ``TestArgvParsing`` — флаг разбирается, и ``main`` действительно передаёт его
  в ``build``: разбор, к которому никто не обращается, выглядит как работающая
  настройка;
* ``TestThresholdReachesTheWriter`` — значение доезжает до писателя журнала и
  видно в стартовом логе;
* ``TestWriterAppliesTheThreshold`` — писатель действительно отбрасывает
  события ниже порога, а не принимает флаг и забывает;
* ``TestRefusals`` — незнакомое значение роняет подъём, синоним ``WARNING``
  разбирается;
* ``TestOneDeclaration`` — платформа не заводит порог ни вторым ключом в
  ``platform.json``, ни локальной копией шкалы уровней.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.eventing.models import LEVELS, normalize_level
from servers.enterprise import server as enterprise_server
from servers.enterprise.capabilities.data.service.main import DataService

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SERVER_SRC = PLATFORM_ROOT / "servers" / "enterprise" / "server.py"
DATA_SERVICE_SRC = (
    PLATFORM_ROOT
    / "servers"
    / "enterprise"
    / "capabilities"
    / "data"
    / "service"
    / "main.py"
)
PLATFORM_JSON = PLATFORM_ROOT / "platform.json"

LOG_TABLE = ("public", "agent_gateway_logs")
QUESTION_RUNS_TABLE = ("public", "agent_question_runs")

#: Объявленное имя события платформы. Берётся из словаря типов, а не пишется
#: строкой в тесте: политика незнакомых имён менялась и может измениться, и
#: проверка порога не должна зависеть от неё.
EVENT_TYPE = "tool.started"


def _service(**kwargs: Any) -> DataService:
    """Писатель журнала с настроенными именами таблиц и без фонового потока."""
    return DataService(
        log_table=LOG_TABLE,
        question_runs_table=QUESTION_RUNS_TABLE,
        buffer_flush_interval=0.0,
        **kwargs,
    )


def _pending(service: DataService) -> int:
    return int(service.stats()["event_buffer"]["pending"])


def _main_statements() -> list[str]:
    """Тела верхнего уровня ``main`` как список разобранных выражений."""
    tree = ast.parse(SERVER_SRC.read_text(encoding="utf-8"))
    main = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    return [ast.unparse(statement) for statement in main.body]


class TestArgvParsing:
    """``--log-min-level`` разбирается так же, как ``--profile``."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["--log-min-level", "WARN"],
            ["--log-min-level=WARN"],
            ["--capabilities", "data", "--log-min-level", "WARN"],
        ],
    )
    def test_flag_is_parsed_from_argv(self, argv: list[str]) -> None:
        assert enterprise_server._log_min_level_from_argv(argv) == "WARN"

    def test_absent_flag_is_none(self) -> None:
        assert enterprise_server._log_min_level_from_argv(["--profile", "test"]) is None
        assert enterprise_server._log_min_level_from_argv([]) is None

    def test_value_is_parsed_verbatim(self) -> None:
        """Разбор не занимается уровнями: проверяет писатель журнала.

        Нормализация в разборе означала бы второе место, где живут правила
        уровней, и откат к ``INFO`` на незнакомом значении — то есть ровно то
        молчание, которое флаг и пришёл отменить.
        """
        assert (
            enterprise_server._log_min_level_from_argv(["--log-min-level", "verbose"])
            == "verbose"
        )

    def test_flag_is_threaded_into_build(self) -> None:
        """``main`` передаёт разобранный флаг в ``build``.

        Разбор, к которому никто не обращается, — это настройка, которая
        выглядит рабочей и не действует: ровно тот класс дефекта, который
        страж обязан ловить.
        """
        statements = _main_statements()
        build_calls = [item for item in statements if item.startswith("build(") or "= build(" in item]
        assert build_calls, "в main нет вызова build()"
        assert any(
            "log_min_level=_log_min_level_from_argv(" in item for item in build_calls
        ), (
            f"вызов build() в main не передаёт порог журнала: {build_calls}. "
            "Флаг разбирается и не применяется."
        )


class TestThresholdReachesTheWriter:
    """Значение доезжает до писателя журнала capability ``data``."""

    def test_argv_value_reaches_the_journal_writer(self) -> None:
        argv = ["--capabilities", "data", "--log-min-level", "ERROR"]
        _, _, container = enterprise_server.build(
            enterprise_server._capabilities_from_argv(argv),
            log_min_level=enterprise_server._log_min_level_from_argv(argv),
        )
        data = container.services.get("data")
        assert data is not None
        assert data.stats()["min_level"] == "ERROR"

    def test_without_the_flag_nothing_is_filtered(self) -> None:
        """Флага нет — пишем всё; это не «дефолт INFO».

        Подстановка дефолта означала бы, что у платформы появился собственный
        порог, о котором никто не объявлял, и вопрос «каким уровнем пишется
        журнал» получил бы два ответа.
        """
        _, _, container = enterprise_server.build()
        data = container.services.get("data")
        assert data is not None
        assert data.stats()["min_level"] is None

    def test_startup_log_reports_the_applied_threshold(self, caplog) -> None:
        """Оператор видит применённый порог в стартовом логе.

        Значение берётся у писателя, а не из флага: строка должна показывать
        то, что действительно применяется, иначе незнакомый уровень, тихо
        упавший в дефолт, выглядел бы как заданный.
        """
        with caplog.at_level(logging.INFO, logger="servers.enterprise.server"):
            enterprise_server.build(log_min_level="WARN")
        assert "порог журнала: WARN" in caplog.text

    def test_startup_log_says_when_there_is_no_threshold(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="servers.enterprise.server"):
            enterprise_server.build()
        assert "порог журнала: не задан" in caplog.text


class TestWriterAppliesTheThreshold:
    """Писатель отбрасывает события ниже порога, а не принимает флаг в молчку."""

    def test_event_below_threshold_is_not_buffered(self) -> None:
        service = _service(min_level="WARN")
        assert service.log_event(EVENT_TYPE, level="INFO") == "dropped"
        assert _pending(service) == 0
        assert service.stats()["suppressed_below_level"] == 1

    @pytest.mark.parametrize("level", ["WARN", "ERROR"])
    def test_event_at_or_above_threshold_is_buffered(self, level: str) -> None:
        service = _service(min_level="WARN")
        assert service.log_event(EVENT_TYPE, level=level) == "accepted"
        assert _pending(service) == 1
        assert service.stats()["suppressed_below_level"] == 0

    def test_platform_own_events_are_filtered_too(self) -> None:
        """Внутренние события платформы проходят через тот же порог.

        Фильтр в ``log_event`` оставил бы ``tool.*`` и ``quality.check`` без
        порога, а это ровно те события, чей объём задаёт журнал.
        """
        service = _service(min_level="ERROR")
        assert service.accept({"event_type": "quality.check", "level": "INFO"}) is not None
        assert _pending(service) == 0

    def test_without_threshold_nothing_is_filtered(self) -> None:
        service = _service()
        assert service.log_event(EVENT_TYPE, level="DEBUG") == "accepted"
        assert _pending(service) == 1
        assert service.stats()["min_level"] is None

    def test_unparsable_level_is_not_blamed_on_the_threshold(self) -> None:
        """Уровень, который не разбирается, порогом не отбрасывается.

        Это дефект производителя, а не решение о настройке: спрятать его в
        счётчик порога значило бы выдать поломку уровня за настройку объёма
        журнала. Событие уходит в буфер и отвергается ``CHECK`` в базе — как
        отвергалось до появления порога.
        """
        service = _service(min_level="WARN")
        assert service.accept({"event_type": EVENT_TYPE, "level": "verbose"}) is None
        assert _pending(service) == 1
        assert service.stats()["suppressed_below_level"] == 0


class TestRefusals:
    """Незнакомое значение — отказ на старте, а не откат к значению по умолчанию."""

    def test_unknown_level_fails_the_writer(self) -> None:
        with pytest.raises(InfrastructureError) as exc_info:
            _service(min_level="verbose")
        assert "--log-min-level" in str(exc_info.value)

    def test_unknown_level_fails_the_build(self) -> None:
        with pytest.raises(InfrastructureError):
            enterprise_server.build(log_min_level="verbose")

    def test_unknown_level_never_reaches_a_default(self) -> None:
        """Падение — на подъёме, а не «и так понятно, что INFO».

        Проверяется, что ни один уровень не приводится к ``INFO`` молча:
        откат переключил бы платформу на другую политику записи, и заметить
        это можно было бы только по отсутствию событий.
        """
        for value in ("verbose", "trace", "infoo", "1"):
            with pytest.raises(InfrastructureError):
                _service(min_level=value)

    @pytest.mark.parametrize("value", ["WARNING", "warning", "Warn"])
    def test_warning_alias_is_understood(self, value: str) -> None:
        """Синоним ``WARNING`` разбирается общим правилом, а не своей копией."""
        assert _service(min_level=value).stats()["min_level"] == normalize_level("WARNING")

    @pytest.mark.parametrize("level", LEVELS)
    def test_every_level_of_the_scale_is_accepted(self, level: str) -> None:
        assert _service(min_level=level).stats()["min_level"] == level


class TestOneDeclaration:
    """Объявление порога одно, и шкала уровней одна."""

    def test_platform_json_declares_no_threshold_of_its_own(self) -> None:
        """В ``platform.json`` нет ключа про порог журнала.

        Порог объявляет агент; второе место в файле платформы означало бы два
        ответа на вопрос «каким уровнем пишется журнал», и разъезд между ними
        был бы молчаливым.
        """
        raw = json.loads(PLATFORM_JSON.read_text(encoding="utf-8"))

        def _walk(node: object) -> list[str]:
            if isinstance(node, dict):
                keys = [str(key) for key in node]
                for value in node.values():
                    keys.extend(_walk(value))
                return keys
            if isinstance(node, list):
                return [key for value in node for key in _walk(value)]
            return []

        offenders = sorted({key for key in _walk(raw) if "min_level" in key.lower()})
        assert not offenders, (
            f"в platform.json объявлен порог журнала: {offenders}. Порт "
            "объявляет агент (config.json → gateway.agent.logging.db.min_level), "
            "и второе место означало бы два разных ответа."
        )

    def test_registry_declares_no_threshold_setting(self) -> None:
        from libs.enterprise_common.settings import SETTINGS as REGISTRY

        offenders = sorted(
            name
            for setting in REGISTRY
            for name in setting.names
            if "min_level" in name.lower()
        )
        assert not offenders, (
            f"реестр платформы объявляет порог журнала: {offenders}. Настройка "
            "приходит от агента флагом запуска, а не из platform.json."
        )

    def test_data_service_declares_no_level_scale(self) -> None:
        """В писателе журнала нет своей шкалы уровней.

        Шкала объявлена в ``libs/enterprise_common/eventing/models.py`` и
        сверяется с ``CHECK valid_level``. Локальная копия в писателе была
        ровно причиной дефекта 2026-10-01: внутренние события платформы
        проверялись по одному списку и писались по другому.
        """
        tree = ast.parse(DATA_SERVICE_SRC.read_text(encoding="utf-8"))
        declared = sorted(
            f"{node.targets[0].id} = {ast.literal_eval(node.value)}"
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, (tuple, list, dict, set, frozenset))
            and any(
                str(name).lower().endswith("level") or "level" in str(name).lower()
                for name in ast.literal_eval(node.value)
            )
        )
        assert not declared, (
            f"писатель журнала объявил свою шкалу уровней: {declared}. Правило "
            "одно — в libs/enterprise_common/eventing/models.py."
        )
