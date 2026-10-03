"""Порог оператора доезжает до обоих писателей журнала процесса.

Агент объявляет порог журнала в одном месте — ``config.json →
gateway.agent.logging.db.min_level`` — и передаёт его платформе флагом запуска
``--log-min-level``. Значение доезжало до **одного** писателя: до буфера журнала
capability ``data``. Второй писатель — ``EventWriter`` слоя исполнения,
создававшийся в ``build_execution_layer`` без порога, — резал внутренние события
платформы по своему значению по умолчанию ``INFO``.

Итог был ровно тот, который требование запрещает: при ``DEBUG`` в боевом
конфиге события агента писались, а ``tool.*`` и ``quality.check`` выпадали. Одна
настройка давала два разных правила записи, и разъезд был молчалив — оба
писателя отчитывались «журнал пишется», и ни один не признавал, что фильтрует
по-своему.

Почему это не было замечено раньше
----------------------------------
У ``EventWriter`` параметр ``min_level`` был, и он был проверен — но только в
тестах, которые этот параметр **сами и передавали**
(``test_journal_noise_policy.py``). Ни один тест не собирал слой исполнения без
порога и не сравнивал его решение с решением буфера журнала. Обе половины по
отдельности выглядели исправными: у каждой был свой тест на «порог применяется»,
и оба проходили. Расхождение жило между ними, а не внутри них.

Что здесь проверяется
---------------------
* ``TestThresholdReachesTheEventWriter`` — значение доезжает и **меняет
  поведение**: при ``DEBUG`` внутреннее событие доходит до приёмника, при
  ``None`` не фильтруется ничего;
* ``TestBothWritersAgree`` — на одном пороге оба писателя процесса дают один и
  тот же ответ (это и есть «журнал одинаковый в MCP и в агенте»);
* ``TestWiringIsNotOptional`` — порог нельзя забыть: каждый вызов
  ``EventWriter(...)`` и ``build_execution_layer(...)`` в ``libs/`` и
  ``servers/`` обязан передавать его явно;
* ``TestNoSecondScaleOfLevels`` — у писателя нет своей шкалы уровней.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.eventing.models import AgentEvent  # noqa: E402
from libs.enterprise_common.eventing.types import (  # noqa: E402
    QUALITY_CHECK,
    TOOL_STARTED,
)
from libs.enterprise_common.eventing.writer import (  # noqa: E402
    ACCEPTED,
    SUPPRESSED,
)
from libs.enterprise_common.execution.factory import build_execution_layer  # noqa: E402
from servers.enterprise.capabilities.data.service.main import DataService  # noqa: E402

WRITER_SRC = PLATFORM_ROOT / "libs" / "enterprise_common" / "eventing" / "writer.py"
SERVER_SRC = PLATFORM_ROOT / "servers" / "enterprise" / "server.py"

#: Корни, где ищутся вызовы писателя и сборки слоя. Собственные ``tests`` в
#: список не входят: тест строит писателя намеренно, с тем порогом, который
#: проверяет.
RUNTIME_ROOTS = (PLATFORM_ROOT / "libs", PLATFORM_ROOT / "servers")

LOG_TABLE = ("public", "agent_gateway_logs")
QUESTION_RUNS_TABLE = ("public", "agent_question_runs")

#: Все уровни шкалы журнала — перебор по ней проверяет решение обоих писателей.
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


class Sink:
    """Приёмник строк журнала: складывает в список, ничего не знает о буфере."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __call__(self, row: dict[str, Any]) -> str | None:
        self.rows.append(row)
        return None


def _service(**kwargs: Any) -> DataService:
    """Писатель журнала capability ``data`` без фонового потока и без БД."""
    return DataService(
        log_table=LOG_TABLE,
        question_runs_table=QUESTION_RUNS_TABLE,
        buffer_flush_interval=0.0,
        **kwargs,
    )


def _writer(session_root: Path, **kwargs: Any) -> tuple[Any, Sink]:
    """Слой исполнения с порогом оператора и приёмником, который его видит."""
    sink = Sink()
    layer = build_execution_layer(
        {}, sink=sink, session_root=session_root, **kwargs
    )
    return layer, sink


def _call_sites(name: str) -> list[tuple[str, ast.Call]]:
    """Все вызовы функции с указанным именем в рантаймовом коде платформы."""
    sites: list[tuple[str, ast.Call]] = []
    for root in RUNTIME_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == name
                ):
                    where = f"{path.relative_to(PLATFORM_ROOT).as_posix()}:{node.lineno}"
                    sites.append((where, node))
    return sites


class TestThresholdReachesTheEventWriter:
    """Значение доезжает до писателя и меняет то, что попадает в журнал."""

    def test_layer_keeps_the_operator_threshold(self, tmp_path: Path) -> None:
        layer, _ = _writer(tmp_path, min_level="DEBUG")
        assert layer.writer.min_level == "DEBUG"
        assert layer.stats()["event_writer"]["min_level"] == "DEBUG"

    def test_platform_event_below_the_old_default_reaches_the_sink(
        self, tmp_path: Path
    ) -> None:
        """Регрессия: при ``DEBUG`` внутреннее событие платформы писалось.

        Именно ``quality.check`` уровня ``DEBUG`` выпадал, пока порог не
        доезжал: писатель резал его дефолтом ``INFO``, и платформа писала себя по
        другому правилу, чем агент.
        """
        layer, sink = _writer(tmp_path, min_level="DEBUG")
        assert (
            layer.writer.emit(AgentEvent(event_type=QUALITY_CHECK, level="DEBUG"))
            == ACCEPTED
        )
        assert [row["event_type"] for row in sink.rows] == [QUALITY_CHECK]

    def test_threshold_above_the_level_still_suppresses(self, tmp_path: Path) -> None:
        layer, sink = _writer(tmp_path, min_level="WARN")
        assert layer.writer.emit(AgentEvent(event_type=TOOL_STARTED, level="INFO")) == SUPPRESSED
        assert sink.rows == []
        assert layer.writer.stats()["suppressed_noise"] == 1

    def test_no_threshold_means_write_everything(self, tmp_path: Path) -> None:
        """``None`` — «порог не задан», а не «дефолт ``INFO``».

        Так же трактует флаг писатель capability ``data``: без ``--log-min-level``
        платформа пишет всё. Подстановка ``INFO`` здесь означала бы, что сервер,
        поднятый без агента, режет то, что буфер журнала пропускает, — то есть
        два порога в одном процессе, причём в обратную сторону.
        """
        layer, sink = _writer(tmp_path, min_level=None)
        assert layer.writer.min_level is None
        assert layer.stats()["event_writer"]["min_level"] is None
        assert layer.writer.emit(AgentEvent(event_type=TOOL_STARTED, level="DEBUG")) == ACCEPTED
        assert [row["level"] for row in sink.rows] == ["DEBUG"]

    def test_alias_is_understood_by_the_shared_rule(self, tmp_path: Path) -> None:
        layer, _ = _writer(tmp_path, min_level="WARNING")
        assert layer.writer.min_level == "WARN"

    def test_unparsable_threshold_stops_the_build(self, tmp_path: Path) -> None:
        """Незнакомый уровень — отказ, а не тихая подмена.

        В рантайме до писателя такое значение не доходит: сборка capability
        ``data`` разбирает флаг раньше и падает с ``InfrastructureError``.
        Проверяется всё же второй фильтр на пути события — молчаливый откат
        переключил бы журнал на другую политику записи, и узнал бы об этом тот,
        кто уже ищет пропавшее событие.
        """
        with pytest.raises(ValueError, match="неизвестный уровень журнала"):
            _writer(tmp_path, min_level="verbose")


class TestBothWritersAgree:
    """Один порог — один ответ у обоих писателей процесса.

    ``EventWriter`` и ``DataService.accept`` пишут в одну таблицу
    ``agent_gateway_logs`` из одного процесса. Если их решения расходятся, то в
    одной и той же таблице событие окажется то записанным, то нет — в
    зависимости от того, кто его породил, — и по журналу это неотличимо от
    «события не было».
    """

    @pytest.mark.parametrize("threshold", [None, *LEVELS])
    @pytest.mark.parametrize("level", LEVELS)
    def test_same_decision_for_the_same_threshold(
        self, tmp_path: Path, threshold: str | None, level: str
    ) -> None:
        service = _service(min_level=threshold)
        layer, _ = _writer(tmp_path, min_level=threshold)

        by_writer = layer.writer.emit(AgentEvent(event_type=TOOL_STARTED, level=level))
        by_buffer = service.log_event(TOOL_STARTED, level=level)

        assert (by_writer == ACCEPTED) is (by_buffer == "accepted"), (
            f"порог={threshold!r} уровень={level}: писатель слоя — {by_writer}, "
            f"буфер журнала — {by_buffer}"
        )
        assert service.stats()["min_level"] == layer.writer.stats()["min_level"]

    def test_reported_threshold_is_the_one_that_was_given(self, tmp_path: Path) -> None:
        """Оба объявляют одно и то же значение — то, которое прислал оператор.

        Расхождение в отчётности опаснее расхождения в поведении: баннер печатает
        порог по одному писателю, и второй, режущий иначе, в отчётности не
        участвует.
        """
        service = _service(min_level="ERROR")
        layer, _ = _writer(tmp_path, min_level="ERROR")
        assert service.stats()["min_level"] == "ERROR"
        assert layer.stats()["event_writer"]["min_level"] == "ERROR"


class TestWiringIsNotOptional:
    """Забыть порог трудно: сборка без него — ошибка стражей, а не сюрприз."""

    def test_every_event_writer_is_created_with_a_threshold(self) -> None:
        sites = _call_sites("EventWriter")
        assert sites, "скан не нашёл ни одного EventWriter(...) — страж молчит вхолостую"
        missing = [
            where
            for where, node in sites
            if not any(keyword.arg == "min_level" for keyword in node.keywords)
        ]
        assert not missing, (
            f"писатель событий создан без порога журнала: {missing}. Забытый "
            "порог — это второй фильтр на пути события: внутренние события "
            "платформы будут резаться по значению по умолчанию независимо от "
            "настройки оператора."
        )

    def test_every_execution_layer_is_built_with_a_threshold(self) -> None:
        sites = _call_sites("build_execution_layer")
        assert sites, "скан не нашёл ни одного build_execution_layer(...)"
        missing = [
            where
            for where, node in sites
            if not any(keyword.arg == "min_level" for keyword in node.keywords)
        ]
        assert not missing, (
            f"слой исполнения собран без порога журнала: {missing}. Слой — это "
            "и есть писатель внутренних событий платформы, и без явного значения "
            "он вернётся к своему дефолту."
        )

    def test_server_hands_the_parsed_flag_to_the_writer(self) -> None:
        """Наверх идёт именно то, что разобрано из ``--log-min-level``.

        Литерал, константа или второй разбор флага означали бы, что у журнала
        снова два ответа на вопрос «каким уровнем он пишется», причём
        разъедущихся молча: флаг разбирается, а применяется другое значение.
        """
        tree = ast.parse(SERVER_SRC.read_text(encoding="utf-8"), filename=str(SERVER_SRC))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_execution_layer"
        ]
        assert calls, "в server.py нет вызова build_execution_layer(...)"
        passed = [
            ast.unparse(keyword.value)
            for call in calls
            for keyword in call.keywords
            if keyword.arg == "min_level"
        ]
        assert passed == ["log_min_level"], (
            f"вызов build_execution_layer передаёт min_level={passed}, а не значение, "
            "разобранное из флага"
        )

    def test_server_does_not_fall_back_to_the_default_threshold(self) -> None:
        """В сборке сервера нет ссылки на ``DEFAULT_MIN_LEVEL``.

        Ссылка означала бы, что где-то на пути флага стоит подстановка, и при
        потерянном флаге платформа тихо вернулась бы к ``INFO`` — то есть
        ровно к тому поведению, которое чинится.
        """
        source = SERVER_SRC.read_text(encoding="utf-8")
        assert "DEFAULT_MIN_LEVEL" not in source, (
            "server.py ссылается на DEFAULT_MIN_LEVEL: порог по умолчанию не "
            "имеет права жить в сборке сервера, значение приходит от агента"
        )


class TestNoSecondScaleOfLevels:
    """Правило сравнения уровней одно — из ``eventing/models.py``."""

    def test_writer_module_declares_no_scale_of_its_own(self) -> None:
        tree = ast.parse(WRITER_SRC.read_text(encoding="utf-8"), filename=str(WRITER_SRC))
        declared = sorted(
            f"{node.targets[0].id} = {ast.literal_eval(node.value)}"
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, (tuple, list, dict, set, frozenset))
        )
        assert not declared, (
            f"писатель событий объявил свою шкалу уровней: {declared}. Порядок "
            "важности объявлен один раз — в libs/enterprise_common/eventing/models.py."
        )

    def test_writer_compares_levels_by_the_shared_rule(self) -> None:
        source = WRITER_SRC.read_text(encoding="utf-8")
        assert "is_at_least(" in source, (
            "писатель не зовёт общий is_at_least: значит, он сравнивает уровень "
            "по-своему, и правило разъедется с буфером журнала при первой же "
            "правке шкалы"
        )
        assert "LEVEL_RANKS" not in source, (
            "у писателя событий появилась своя числовая шкала уровней"
        )
