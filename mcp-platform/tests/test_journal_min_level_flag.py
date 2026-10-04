"""Стражи платформенной половины блока настроек агента.

Раньше здесь жил страж флага ``--log-min-level`` и страж «в реестре нет
настройки порога». Оба устарели: значение приходит блоком
(``--agent-settings-file``, ``specs/runtime/platform-settings``), а ключ блока
**обязан** быть настройкой реестра с ``owner=OWNER_AGENT`` — в этом весь смысл
канала: словарь приёма объявлен платформой, и агент своего не имеет.

Что осталось прежним: порог доезжает до обоих писателей журнала процесса,
применяется по общим правилам ``normalize_level`` / ``level_rank``
(``libs/enterprise_common/eventing/models.py``) и различается в трёх случаях —
не задан / корректен / незнаком (отказ на старте, а не откат к ``INFO``).

* ``TestArgvParsing`` — ``--agent-settings-file`` разбирается, и ``main``
  действительно передаёт разобранный путь в ``build``: разбор, к которому никто
  не обращается, выглядит как работающая настройка;
* ``TestThresholdReachesTheWriter`` — значение из блока доезжает до писателя
  журнала и видно в стартовом логе вместе с составом блока;
* ``TestWriterAppliesTheThreshold`` — писатель действительно отбрасывает
  события ниже порога;
* ``TestRefusals`` — незнакомое значение роняет подъём, синоним ``WARNING``
  разбирается;
* ``TestOneDeclaration`` — объявление порога одно: в ``platform.json`` его нет,
  а в реестре оно есть ровно одно и с владельцем «агент».
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

#: Ключ блока и имя настройки, которую он объявляет. Ключ совпадает с путём в
#: конфигурации агента — на стороне платформы он приходит ключом блока.
BLOCK_KEY = "logging.db.min_level"
SETTING_NAME = "ENTERPRISE_LOG_MIN_LEVEL"


def _block_file(tmp_path: Path, value: str | None = "WARN") -> Path:
    """Файл блока агента: один ключ, ровно то, что прислал бы агент."""
    path = tmp_path / "agent-settings.json"
    path.write_text(
        json.dumps({} if value is None else {BLOCK_KEY: value}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


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
    """``--agent-settings-file`` разбирается так же, как ``--profile``."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["--agent-settings-file", "C:/ws/agent-settings.json"], "agent-settings.json"),
            (["--agent-settings-file=C:/ws/agent-settings.json"], "agent-settings.json"),
            (
                ["--capabilities", "data", "--agent-settings-file", "C:/ws/block.json"],
                "block.json",
            ),
        ],
    )
    def test_flag_is_parsed_from_argv(self, argv: list[str], expected: str) -> None:
        parsed = enterprise_server._agent_settings_path_from_argv(argv)
        assert parsed is not None
        assert parsed.name == expected
        assert parsed.is_absolute()

    def test_absent_flag_is_none(self) -> None:
        assert enterprise_server._agent_settings_path_from_argv(["--profile", "test"]) is None
        assert enterprise_server._agent_settings_path_from_argv([]) is None

    def test_flag_without_a_value_is_refused(self) -> None:
        """Флаг без значения — отказ, а не «текущий каталог».

        Пустой путь развернулся бы в ``.``, и платформа прочитала бы чужой файл
        вместо блока: имя, под которым ищется блок, выглядело бы объявленным, а
        значения в нём не было бы.
        """
        with pytest.raises(InfrastructureError, match="agent-settings-file"):
            enterprise_server._agent_settings_path_from_argv(["--agent-settings-file", " "])
        with pytest.raises(InfrastructureError, match="agent-settings-file"):
            enterprise_server._agent_settings_path_from_argv(["--agent-settings-file="])

    def test_flag_is_threaded_into_build(self) -> None:
        """``main`` передаёт разобранный путь в ``build``.

        Разбор, к которому никто не обращается, — это настройка, которая
        выглядит рабочей и не действует: ровно тот класс дефекта, который
        страж обязан ловить.
        """
        statements = _main_statements()
        build_calls = [item for item in statements if item.startswith("build(") or "= build(" in item]
        assert build_calls, "в main нет вызова build()"
        assert any(
            "agent_settings_path=_agent_settings_path_from_argv(" in item
            for item in build_calls
        ), (
            f"вызов build() в main не передаёт путь к блоку: {build_calls}. "
            "Флаг разбирается и не применяется."
        )


class TestThresholdReachesTheWriter:
    """Значение из блока доезжает до писателя журнала capability ``data``."""

    def test_block_value_reaches_the_journal_writer(self, tmp_path: Path) -> None:
        block = _block_file(tmp_path, "ERROR")
        _, _, container = enterprise_server.build(
            enterprise_server._capabilities_from_argv(["--capabilities", "data"]),
            agent_settings_path=block,
        )
        data = container.services.get("data")
        assert data is not None
        assert data.stats()["min_level"] == "ERROR"

    def test_block_value_reaches_the_execution_writer(self, tmp_path: Path) -> None:
        """Обе половины журнала процесса режут по одному правилу.

        Раньше порог доезжал до писателя ``data`` флагом, а писатель слоя
        исполнения оставался на своём дефолту: при ``DEBUG`` внутренние события
        платформы выпадали, а агентские проходили. Сам писатель проверяет
        ``tests/test_journal_writer_threshold_wiring.py``; здесь — что сборка
        сервера не забыла про него и не передала мимо слоя настроек.
        """
        block = _block_file(tmp_path, "ERROR")
        server, _, _ = enterprise_server.build(
            enterprise_server._capabilities_from_argv(["--capabilities", "data"]),
            agent_settings_path=block,
        )
        assert server is not None

    def test_without_a_block_nothing_is_filtered(self) -> None:
        """Блока нет — пишем всё; это не «дефолт INFO».

        Подстановка дефолта означала бы, что у платформы появился собственный
        порог, о котором никто не объявлял, и вопрос «каким уровнем пишется
        журнал» получил бы два ответа.
        """
        _, _, container = enterprise_server.build()
        data = container.services.get("data")
        assert data is not None
        assert data.stats()["min_level"] is None

    def test_startup_log_reports_the_applied_threshold(self, tmp_path: Path, caplog) -> None:
        """Оператор видит применённый порог в стартовом логе.

        Значение берётся у писателя, а не из блока: строка должна показывать
        то, что действительно применяется, иначе незнакомый уровень, тихо
        упавший в дефолт, выглядел бы как заданный.
        """
        block = _block_file(tmp_path, "WARN")
        with caplog.at_level(logging.INFO, logger="servers.enterprise.server"):
            enterprise_server.build(agent_settings_path=block)
        assert "порог журнала: WARN" in caplog.text

    def test_startup_log_says_when_there_is_no_threshold(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="servers.enterprise.server"):
            enterprise_server.build()
        assert "порог журнала: не задан" in caplog.text

    def test_startup_log_reports_the_block(self, tmp_path: Path, caplog) -> None:
        """Стартовый лог называет и блок, и его содержимое.

        Блок без такой строки выглядел бы как настройка, которая применилась
        неизвестно откуда; пустой блок и отсутствующий — разные состояния, и
        читатель лога обязан различать их.
        """
        block = _block_file(tmp_path, "WARN")
        with caplog.at_level(logging.INFO, logger="servers.enterprise.server"):
            enterprise_server.build(agent_settings_path=block)
        assert "настройки агента" in caplog.text
        assert f"{BLOCK_KEY}='WARN'" in caplog.text

    def test_startup_log_says_when_there_is_no_block(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="servers.enterprise.server"):
            enterprise_server.build()
        assert "настройки агента: блок настроек агента не передан" in caplog.text


class TestWriterAppliesTheThreshold:
    """Писатель отбрасывает события ниже порога, а не принимает блок в молчку."""

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
        assert "порог" in str(exc_info.value)

    def test_unknown_level_fails_the_build(self, tmp_path: Path) -> None:
        with pytest.raises(InfrastructureError):
            enterprise_server.build(agent_settings_path=_block_file(tmp_path, "verbose"))

    def test_unknown_key_fails_the_build(self, tmp_path: Path) -> None:
        """Неизвестный ключ блока останавливает подъём с называнием ключа.

        Опечатка в ключе, съеденная молча, выглядела бы как «настроек нет»,
        а платформа писала бы по своему дефолту.
        """
        path = tmp_path / "agent-settings.json"
        path.write_text(json.dumps({"logging.db.min_lvl": "WARN"}), encoding="utf-8")
        with pytest.raises(InfrastructureError) as exc_info:
            enterprise_server.build(agent_settings_path=path)
        assert "logging.db.min_lvl" in str(exc_info.value)

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

    def test_registry_declares_the_threshold_for_the_agent(self) -> None:
        """Ключ блока — настройка реестра с владельцем «агент».

        Требование спеки: «ключ блока MUST быть настройкой, объявленной в
        реестре платформы с ``owner=OWNER_AGENT``». Обратное — настройки в
        реестре нет — означало бы, что словарь приёма объявлен негде, и блок
        нечем наполнить. Прежний страж требовал обратного: он опирался на
        доставку флагом, которой больше нет.
        """
        from libs.enterprise_common.settings import (
            BY_NAME,
            OWNER_AGENT,
            agent_settings_dictionary,
        )

        setting = BY_NAME[SETTING_NAME]
        assert setting.owner == OWNER_AGENT, (
            f"{SETTING_NAME} объявлена с владельцем {setting.owner!r}, а блок "
            "принимает только агентские настройки."
        )
        assert setting.key == BLOCK_KEY
        assert BLOCK_KEY in agent_settings_dictionary()

    def test_threshold_key_is_absent_from_the_platform_file_dictionary(self) -> None:
        """Ключ блока не может быть ключом ``platform.json``.

        Иначе у значения появились бы два владельца, а вопрос «откуда взялось»
        — два ответа, один из которых молча перекрывает другой.
        """
        from libs.enterprise_common.settings import BY_FILE_KEY

        assert BLOCK_KEY not in BY_FILE_KEY

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
            and node.targets[0].id.isupper()
            and isinstance(node.value, ast.List)
        )
        assert declared == [], (
            f"в писателе журнала объявлена копия шкалы уровней: {declared}. "
            "Шкала живёт в libs/enterprise_common/eventing/models.py."
        )
