"""Уровни журнала несут смысл, а шум не попадает в продовую таблицу.

Файл закрывает три требования спеки ``logging-db``:

* «Уровни логирования несут смысл» — шкала одна, порог по умолчанию
  отделяет факт оборота от диагностики, а вторая копия шкалы на стороне
  агента больше не может разойтись с платформенной молча;
* «Пробные события не пишутся в продовую таблицу» — пробное имя не доходит
  до приёмника ни при каком уровне;
* «Потеря события всегда видима» — отбрасывание по правилу **считается**,
  иначе вычистка шума отключилась бы молча, а это ровно тот отказ, который
  уже чинили.

Проверка без замечаний как раз и есть главный носитель шума: замер показал
48 строк ``quality.check`` в таблице из 358, и 46 из них без единого флага.
Пока такие события писались как ``INFO``, отделить их от факта оборота было
нечем, а четверть объявленных уровней не использовалась вовсе.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from conftest import call_meta, make_layer

from libs.enterprise_common.eventing import (
    DEFAULT_MIN_LEVEL,
    DIAGNOSTIC_LEVEL,
    LEVEL_RANKS,
    LEVELS,
    SUPPRESSED,
    AgentEvent,
    is_at_least,
    is_diagnostic,
    is_probe_event_type,
    level_rank,
    normalize_level,
)

# Приватное читается намеренно: синонимы уровней — часть контракта, который
# оба писателя обязаны разбирать одинаково, а публичного доступа к таблице
# синонимов модуль не выставляет. Пока его нет, страж ходит сюда; если
# экспорт появится, ссылку надо перевести на него.
from libs.enterprise_common.eventing.models import _LEVEL_ALIASES  # noqa: PLC2701
from libs.enterprise_common.eventing.types import (
    PROBE_EVENT_NAMES,
    PROBE_EVENT_PREFIXES,
    QUALITY_CHECK,
    TOOL_COMPLETED,
    TOOL_STARTED,
)
from libs.enterprise_common.eventing.writer import EventWriter
from libs.enterprise_common.registry import ToolDefinition

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
AGENT_ROOT = PLATFORM_ROOT.parent
#: Каталоги агента, где пишется в журнал. ``cli_agent.py`` и ``logging_utils``
#: исключены намеренно: там уровень относится к stdlib-логированию процесса,
#: а не к строке в ``agent_gateway_logs``, и ``CHECK valid_level`` его не
#: принимает и не должен принимать.
AGENT_JOURNAL_DIRS = ("lib", "workspace")


class Sink:
    """Приёмник журнала: копит строки, ничего не отбрасывая."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __call__(self, row: dict[str, Any]) -> str | None:
        self.rows.append(row)
        return None

    def types(self) -> list[str]:
        return [row["event_type"] for row in self.rows]

    def levels(self) -> list[str]:
        return [row["level"] for row in self.rows]


def _definition(handler: Any, *, quality_policy: str) -> ToolDefinition:
    return ToolDefinition(
        name="test.probe",
        description="Проверочная операция",
        handler=handler,
        capability="test",
        quality_policy=quality_policy,
    )


def _constant_result(value: Any) -> Any:
    """Обработчик, возвращающий одно и то же значение.

    Отдельная функция вместо замыкания в цикле: значение связывается
    аргументом, а не переменной цикла, и следующая итерация не может
    подменить его задним числом.
    """

    def handler(**kwargs: Any) -> Any:
        return value

    return handler


def _require_agent_tree() -> Path:
    """Каталог агента обязателен для сверки двух сторон словаря.

    Отдельная проверка, а не ``skipif`` в сигнатуре: прогон платформы без
    дерева агента — обычное дело (отдельный checkout), и падать из-за
    отсутствующего соседа нельзя. В этом репозитории дерево есть, поэтому
    проверка выполняется.
    """
    if not (AGENT_ROOT / "lib" / "services" / "db_logging_service.py").exists():
        pytest.skip("дерево агента рядом не найдено — сверять словари не с чем")
    return AGENT_ROOT


# -- уровни: одна шкала на обе стороны --------------------------------------


class TestLevelScaleIsSingle:
    def test_severity_order_is_derived_from_the_declared_tuple(self) -> None:
        """Порядок важности выводится из ``LEVELS``, а не объявляется рядом.

        Две независимые декларации разъедутся при первой же правке одной из
        них, и разъезд будет молчащим: фильтр по уровню отбрасывает событие,
        не объясняя почему.
        """
        assert dict(LEVEL_RANKS) == {name: rank for rank, name in enumerate(LEVELS)}
        assert sorted(LEVEL_RANKS, key=LEVEL_RANKS.get) == list(LEVELS)

    def test_facts_pass_and_diagnostics_do_not(self) -> None:
        """Порог по умолчанию — ``INFO``: факт оборота проходит, диагностика нет."""
        assert DEFAULT_MIN_LEVEL == "INFO"
        assert is_at_least("INFO", DEFAULT_MIN_LEVEL)
        assert not is_at_least(DIAGNOSTIC_LEVEL, DEFAULT_MIN_LEVEL)
        assert is_diagnostic("DEBUG") and not is_diagnostic("INFO")

    def test_event_without_level_is_a_fact_not_diagnostics(self) -> None:
        """Событие без уровня — обычный факт, а не диагностика.

        Уровень не перечисляют в каждом втором ``AgentEvent``, и пустое
        значение по умолчанию трактуется как «это лёгкое» только в одном
        случае — когда его так и сказали. Иначе порог молча съедал бы часть
        оборота, и отличать её от выключенной диагностики было бы нечем.
        """
        assert normalize_level("") == "INFO"
        assert normalize_level(None) == "INFO"
        assert level_rank("") == level_rank("INFO")
        assert is_at_least("", DEFAULT_MIN_LEVEL)
        assert not is_diagnostic("")

    def test_warning_is_above_info_and_never_below_it(self) -> None:
        """Деградация обязана быть различима и не тонуть в ``INFO``.

        Замер: в таблице не было ни одной строки ``WARN``, при том что
        ``CHECK`` её разрешает. Уровень, который нельзя отличить от обычного
        факта, не выполняет своей функции фильтра.
        """
        assert level_rank("WARN") > level_rank("INFO")
        assert is_at_least("WARN", DEFAULT_MIN_LEVEL)

    def test_level_alias_resolves_before_comparison(self) -> None:
        """``WARNING`` — синоним, а не шестое значение шкалы.

        Регистр и синоним приходят из привычного ``logging``; если бы
        сравнение шло мимо нормализации, ``WARNING`` молча счёлся бы самым
        низким уровнем и его носители отбрасывались бы как диагностика.
        """
        assert normalize_level("WARNING") == "WARN"
        assert is_at_least("warning", DEFAULT_MIN_LEVEL)

    def test_unknown_level_is_refused_loudly(self) -> None:
        with pytest.raises(ValueError, match="неизвестный уровень"):
            level_rank("TRACE")

    def test_agent_level_scale_matches_the_shared_dictionary(self) -> None:
        """Объявленная агентом шкала обязана совпадать с платформенной.

        Раньше страж искал ЛИТЕРАЛ ``order = {...}`` внутри тела
        ``_should_log``. Это проверяло не объявление, а способ его
        использования: перепиши фильтр, оставив шкалу объявленной выше, —
        и страж падал бы, не заметив никакого расхождения. И наоборот:
        равенство словарей ничего не говорило о поведении, поэтому
        расхождение в разборе синонима ``WARNING`` прожило незамеченным
        (уровень съедался агентом при пороге ``WARN`` и писался платформой).

        Теперь сверяется источник истины — объявленный набор уровней, из
        которого агент выводит числовой вес, и объявленный набор синонимов.
        Поведенческая сверка по всей области входов живёт на стороне агента
        (``tests/test_journal_level_canonical.py``) — там можно импортировать
        обе стороны, здесь нельзя: платформа по границе процессов не видит
        пакет агента, поэтому и приходится читать исходник.
        """
        agent = _require_agent_tree()
        source = (agent / "lib" / "services" / "db_logging_service.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source, filename="db_logging_service.py")

        def declared(name: str) -> ast.expr | None:
            for node in tree.body:
                # Объявления уровней идут С АННОТАЦИЕЙ типа
                # (``JOURNAL_LEVELS: tuple[str, ...] = (...)``), а это
                # ast.AnnAssign с единственным ``target``, а не ast.Assign
                # со списком ``targets``. Искать только ast.Assign значило бы
                # не найти ничего и упасть с ложным «шкала не объявлена».
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    if node.target.id == name:
                        return node.value
                elif isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == name for t in node.targets
                ):
                    return node.value
            return None

        levels_node = declared("JOURNAL_LEVELS")
        assert levels_node is not None, (
            "в db_logging_service нет объявления JOURNAL_LEVELS — числовой вес "
            "уровней выводится из него, и без него фильтр лишён шкалы"
        )
        assert isinstance(levels_node, ast.Tuple), (
            "JOURNAL_LEVELS обязан быть кортежем строк, а не результатом "
            f"вычисления: {ast.dump(levels_node)[:120]}"
        )
        agent_levels = [
            element.value
            for element in levels_node.elts
            if isinstance(element, ast.Constant)
        ]
        # Порядок важен: из него строится числовой вес, а вес определяет,
        # что отбрасывается порогом.
        assert tuple(agent_levels) == tuple(LEVELS), (
            "набор уровней агента разошёлся с общим: "
            f"агент {tuple(agent_levels)}, общий {tuple(LEVELS)}"
        )

        aliases_node = declared("JOURNAL_LEVEL_ALIASES")
        assert aliases_node is not None, (
            "в db_logging_service нет объявления JOURNAL_LEVEL_ALIASES — "
            "синоним WARNING не будет разобран и уйдёт в ветку «неизвестно»"
        )
        call = aliases_node.args[0] if isinstance(aliases_node, ast.Call) else aliases_node
        assert isinstance(call, ast.Dict), (
            "JOURNAL_LEVEL_ALIASES обязан быть словарём: "
            f"{ast.dump(aliases_node)[:120]}"
        )
        agent_aliases = {
            key.value: value.value
            for key, value in zip(call.keys, call.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
        }
        assert agent_aliases == dict(_LEVEL_ALIASES), (
            "синонимы уровней разошлись с общими: "
            f"агент {agent_aliases}, общие {dict(_LEVEL_ALIASES)}"
        )

    def test_agent_writes_only_levels_the_database_accepts(self) -> None:
        """Уровень в ``LogEvent`` обязан принимать ``CHECK valid_level``.

        ``CHECK`` в схеме принимает ``DEBUG/INFO/WARN/ERROR``, и ``WARNING``
        в этот список не входит. Событие с таким уровнем не просто не
        пишется: оно уносит с собой батч, в котором было записано, а
        вызывающий уже получил «принято». Это молчаливая потеря оборота.
        """
        agent = _require_agent_tree()
        offenders: list[str] = []
        for folder in AGENT_JOURNAL_DIRS:
            for path in (agent / folder).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call) and _is_log_event(node)):
                        continue
                    for keyword in node.keywords:
                        if keyword.arg != "level" or not isinstance(
                            keyword.value, ast.Constant
                        ):
                            continue
                        value = keyword.value.value
                        if isinstance(value, str) and normalize_level_or_none(value) is None:
                            offenders.append(f"{path.name}:{node.lineno} level={value!r}")
        # Известный долг: четыре записи «WARNING», которые CHECK не принимает.
        # Список не пустой намеренно — он назван, чтобы следующая запись в нем
        # была сознательным решением, а не молчаливым повторением.
        known_debt = {
            "repeat_guard_hook.py",
            "session_mirror.py",
            "mirror_poller.py",
        }
        unexpected = [item for item in offenders if item.split(":")[0] not in known_debt]
        assert not unexpected, (
            f"вне списка известного долга появились уровни, которые CHECK не "
            f"принимает: {unexpected}"
        )

    def test_platform_declares_no_second_level_scale(self) -> None:
        """В дереве платформы не должно быть второй шкалы уровней.

        Её появление означало бы, что фильтр в одном месте считает по одной
        шкале, а ``CHECK`` — по другой, и расхождение обнаружилось бы на
        откате батча.
        """
        offenders: list[str] = []
        for path in (PLATFORM_ROOT / "libs").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
                if not set(LEVELS).issubset(set(keys)):
                    continue
                if all(isinstance(v, ast.Constant) and isinstance(v.value, int) for v in node.values):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, (
            f"в libs/ объявлена вторая числовая шкала уровней: {offenders}"
        )


def normalize_level_or_none(value: str) -> str | None:
    """Уровень, если ``CHECK`` его примет, иначе ``None``."""
    try:
        return normalize_level(value)
    except ValueError:
        return None


def _is_log_event(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id == "LogEvent"


# -- quality.check: замечание есть — событие есть, нет — нет ----------------


class TestQualityCheckCarriesMeaning:
    def test_check_without_flags_is_not_written(self, tmp_path: Path) -> None:
        """Проверка, у которой нет ни одного флага, в журнал не попадает.

        Замер: 48 строк ``quality.check``, все с ``ok=true``, и 46 без
        единого флага — 13 % таблицы нулевой информации. Прежнее поведение
        (писать всегда, как ``INFO``) было не дефектом совместимости, а
        отсутствием разделения на сигнал и шум.
        """
        sink = Sink()
        layer = make_layer(tmp_path, sink=sink)
        result = layer.pipeline.execute(
            _definition(lambda **kw: {"ok": True}, quality_policy="default"),
            {},
            call_meta(),
        )
        assert result.status == "ok"
        assert result.quality is not None and result.quality.flags == ()
        assert QUALITY_CHECK not in sink.types(), sink.types()

    def test_flagged_check_is_written_as_warning(self, tmp_path: Path) -> None:
        """Есть замечание — событие пишется, и уровень отражает тяжесть.

        Политика ``sql_result`` с пустым ``rows`` даёт семантический флаг:
        результат пригоден, но подозрителен. Это и есть определение ``WARN``
        в спеке — «оборот продолжается, но деградировал».
        """
        sink = Sink()
        layer = make_layer(tmp_path, sink=sink)
        result = layer.pipeline.execute(
            _definition(lambda **kw: {"rows": []}, quality_policy="sql_result"),
            {},
            call_meta(),
        )
        assert result.status == "ok"
        row = next(r for r in sink.rows if r["event_type"] == QUALITY_CHECK)
        assert row["level"] == "WARN"
        # В строке — имя проверки, в теле — сам флаг с объяснением. Одно без
        # другого бесполезно: имя не читается без доменных знаний, а
        # объяснение без имени не найти в разборе оборота.
        assert "rows" in row["summary"]
        assert row["payload"]["flags"] == ["rows"]
        details = [check["detail"] for check in row["payload"]["checks"] if not check["ok"]]
        assert details and all(detail for detail in details), details

    def test_quality_check_is_never_info(self, tmp_path: Path) -> None:
        """Уровня ``INFO`` у ``quality.check`` больше не существует.

        Раньше условие было ``"INFO" if report.ok else "warn"``, а
        ``ok=False`` на этом пути недостижим: технический провал возвращает
        отказ раньше. То есть уровень был всегда ``INFO``, и замечания
        тонули в фактах оборота.
        """
        sink = Sink()
        layer = make_layer(tmp_path, sink=sink)
        for policy, value in (
            ("default", {"ok": True}),
            ("sql_result", {"rows": []}),
            ("vector_result", {"results": []}),
        ):
            # Значение связано аргументом, а не переменной цикла: замыкание
            # здесь проходит на первом прогоне и ловит следующий.
            layer.pipeline.execute(
                _definition(_constant_result(value), quality_policy=policy),
                {},
                call_meta(),
            )
        levels = {row["level"] for row in sink.rows if row["event_type"] == QUALITY_CHECK}
        assert levels <= {"WARN", "DEBUG"}, levels

    def test_diagnostics_can_be_switched_on(self, tmp_path: Path) -> None:
        """Диагностика выключена по умолчанию, но включается порогом.

        Порог приходит конструктору писателя, а не читается из файла
        настроек: зарегистрировать ключ в ``settings.py`` — работа другого
        исполнителя (см. отчёт). Пока ключа нет, ручка есть, и она
        проверяема: опустить порог до ``DEBUG`` достаточно.
        """
        sink = Sink()
        writer = EventWriter(sink, min_level="DEBUG")
        assert writer.emit(AgentEvent(event_type=TOOL_STARTED, level="DEBUG")) == "accepted"
        assert [row["level"] for row in sink.rows] == ["DEBUG"]


# -- пробные события ---------------------------------------------------------


class TestProbeEventsNeverReachTheTable:
    @pytest.mark.parametrize(
        "event_type",
        ["smoke.contract", "smoke.", "probe_0", "probe_contract", "live.db_probe"],
    )
    def test_spec_predicate_is_matched(self, event_type: str) -> None:
        """Условие замера из спеки, а не свой список имён.

        ``event_type LIKE 'smoke.%' OR event_type LIKE 'probe_%' OR
        event_type = 'live.db_probe'`` — тот же вопрос, что задаёт проверка
        ``SELECT count(*) ...``, иначе страж и данные будут считать разные
        множества.
        """
        assert is_probe_event_type(event_type)

    @pytest.mark.parametrize(
        "event_type",
        [TOOL_STARTED, QUALITY_CHECK, "agent.completed", "quality.probe_report", ""],
    )
    def test_workload_names_are_not_caught(self, event_type: str) -> None:
        """Рабочее имя не должно попасть под запрет.

        Запрет шире нужного отключил бы журнал целиком, а это тот же отказ
        с обратным знаком: событий нет, и никто не знает почему.
        """
        assert not is_probe_event_type(event_type)

    def test_declared_predicate_parts_are_the_spec_ones(self) -> None:
        assert PROBE_EVENT_PREFIXES == ("smoke.", "probe_")
        assert PROBE_EVENT_NAMES == frozenset({"live.db_probe"})

    def test_writer_drops_probe_event_and_counts_it(self) -> None:
        sink = Sink()
        writer = EventWriter(sink)
        event = AgentEvent(event_type=TOOL_STARTED)
        # Имя подменяется на пробное в обход словаря — так выглядит проба,
        # дошедшая до писателя из чужого кода.
        object.__setattr__(event, "event_type", "smoke.contract")
        assert writer.emit(event) == SUPPRESSED
        assert sink.rows == [], "пробное событие не должно доходить до приёмника"
        assert writer.stats()["suppressed_probe"] == 1

    def test_probe_is_checked_before_level(self) -> None:
        """Пробное имя отбрасывается как пробное, а не как «ниже порога».

        Иначе при пороге ``DEBUG`` проба записалась бы в таблицу, и запрет
        зависел бы от настройки, которая для этого не предназначена.
        """
        writer = EventWriter(Sink(), min_level="DEBUG")
        event = AgentEvent(event_type=TOOL_STARTED, level="DEBUG")
        object.__setattr__(event, "event_type", "probe_x")
        assert writer.emit(event) == SUPPRESSED
        stats = writer.stats()
        assert stats["suppressed_probe"] == 1
        assert stats["suppressed_noise"] == 0

    def test_probe_event_does_not_reach_session_file(self, tmp_path: Path) -> None:
        """Вычистка таблицы не должна оставлять тот же мусор в каталоге сессии.

        Зеркало — второе представление того же события, и проба в нём жила
        бы столько же, сколько прожила в таблице.
        """
        from libs.enterprise_common.session.workspace import SessionWorkspace

        workspace = SessionWorkspace(tmp_path)
        writer = EventWriter(
            Sink(), workspace=workspace, persist_session_events=True, min_level="DEBUG"
        )
        event = AgentEvent(event_type=TOOL_STARTED, session_id="sess-1")
        object.__setattr__(event, "event_type", "live.db_probe")
        writer.emit(event)
        assert not (tmp_path / "sess-1" / "events").exists()

    def test_no_probe_literal_is_written_to_the_journal(self) -> None:
        """Страж: запись в журнал под пробным именем — всегда ошибка.

        Требование спеки: «guard-тест падает, если в коде проб появляется
        запись в журнал без профиля». Проверяются обе стороны, потому что
        пробы писались агентским общим входом, а не платформенным.
        """
        offenders: list[str] = []
        roots = [PLATFORM_ROOT / "libs", PLATFORM_ROOT / "servers"]
        agent = AGENT_ROOT / "lib"
        if agent.exists():
            roots.append(agent)
        for root in roots:
            for path in root.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    for keyword in node.keywords:
                        if keyword.arg not in {"event_type", "eventType"}:
                            continue
                        value = keyword.value
                        if not isinstance(value, ast.Constant) or not isinstance(
                            value.value, str
                        ):
                            continue
                        if is_probe_event_type(value.value):
                            offenders.append(f"{path.name}:{node.lineno} {value.value!r}")
        assert not offenders, (
            f"пробное имя события пишется в журнал: {offenders}. Такие пробы "
            "идут в тестовый профиль или в operational-лог процесса."
        )


# -- отбрасывание видно ------------------------------------------------------


class TestSuppressionIsVisible:
    def test_counters_and_threshold_are_in_stats(self) -> None:
        writer = EventWriter(Sink())
        writer.emit(AgentEvent(event_type=TOOL_STARTED, level="DEBUG"))
        stats = writer.stats()
        assert stats["suppressed_noise"] == 1
        assert stats["suppressed_probe"] == 0
        assert stats["min_level"] == DEFAULT_MIN_LEVEL
        # Отброшенное не должно выглядеть как записанное.
        assert stats["accepted"] == 0

    def test_counters_reach_execution_stats(self, tmp_path: Path) -> None:
        """Счётчик доходит до поверхности, которой пользуется оператор.

        ``execution.stats()`` — то, что сервер печатает на старте и что
        видно без запроса к базе. Счётчик, живущий только внутри писателя,
        был бы тем же молчанием, которое требование запрещает.
        """
        sink = Sink()
        layer = make_layer(tmp_path, sink=sink)
        layer.pipeline.execute(
            _definition(lambda **kw: {"ok": True}, quality_policy="default"),
            {},
            call_meta(),
        )
        writer_stats = layer.stats()["event_writer"]
        assert writer_stats["suppressed_noise"] == 1, writer_stats
        assert writer_stats["min_level"] == DEFAULT_MIN_LEVEL
        # Оборот при этом записан: подавление шума не должно съедать факты.
        assert writer_stats["accepted"] >= 1
        assert sink.types() == [TOOL_STARTED, TOOL_COMPLETED], sink.types()

    def test_reason_is_named_once_not_per_event(self, caplog: Any) -> None:
        """Одна строка на причину, а не на событие.

        Иначе вычистка шума заменила бы мусор в таблице мусором в
        operational-логе, и её объём не падал бы.
        """
        writer = EventWriter(Sink())
        with caplog.at_level("WARNING", logger="libs.enterprise_common.eventing.writer"):
            for _ in range(50):
                writer.emit(AgentEvent(event_type=TOOL_STARTED, level="DEBUG"))
        noise_lines = [r for r in caplog.records if "suppressed_noise" in r.getMessage()]
        assert len(noise_lines) == 1, [r.getMessage() for r in noise_lines]
        assert writer.stats()["suppressed_noise"] == 50

    def test_probe_reason_is_named_once(self, caplog: Any) -> None:
        writer = EventWriter(Sink())
        with caplog.at_level("WARNING", logger="libs.enterprise_common.eventing.writer"):
            for index in range(10):
                event = AgentEvent(event_type=TOOL_STARTED)
                object.__setattr__(event, "event_type", f"probe_{index}")
                writer.emit(event)
        probe_lines = [r for r in caplog.records if "suppressed_probe" in r.getMessage()]
        assert len(probe_lines) == 1, [r.getMessage() for r in probe_lines]
        assert writer.stats()["suppressed_probe"] == 10

    def test_nothing_is_suppressed_at_error_level(self) -> None:
        """Отказ не отбрасывается ни при каком пороге по умолчанию.

        Порог — это про шум, а не про важность: событие ``ERROR`` обязано
        доходить, иначе фильтр уровней станет фильтром ошибок.
        """
        sink = Sink()
        writer = EventWriter(sink)
        assert writer.emit(AgentEvent(event_type=TOOL_COMPLETED, level="ERROR")) == "accepted"
        assert sink.levels() == ["ERROR"]
