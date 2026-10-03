"""Уровень, который агент пишет в журнал, обязан принимать ``CHECK valid_level``.

Дефект, который файл закрывает: четыре события писались с ``level="WARNING"``
(``repeat_guard_hook`` и три — в ``session_cold_sync_service``). ``CHECK``
``valid_level`` в ``sql/logs/create_public_agent_gateway_logs.sql:30`` знает
ровно четыре написания — ``DEBUG/INFO/WARN/ERROR``, — и синонима ``WARNING``
среди них нет. Такое событие не просто не пишется: отказ касается всего
батча, в котором оно лежало, а вызывающий к этому моменту уже получил
«принято». То есть из-за одного неверного слова молча исчезали соседние
события оборота.

Шкала одна на обе стороны: ``LEVELS`` в
``mcp-platform/libs/enterprise_common/eventing/models.py:49`` и ``CHECK`` в
схеме. Здесь они сверяются, а производители проверяются на запись только
уровней из этой шкалы — без списка исключений, чтобы откат любой из правок
ронял тест.

Три уровня защиты, по возрастанию строгости:

* ``test_check_*`` — контракт самой схемы (синоним не должен в неё попасть);
* ``test_no_producer_*`` — статический скан дерева агента: литерал вне
  шкалы и уровень, собранный динамически (f-строка, ``.upper()``);
* ``test_*_published_level`` — поведение: реально построенное событие.

Плюс четвёртый, о самом фильтре журнала: ``TestAgentFilterDecidesLikeThePlatform``
проверяет таблицу решений «уровень × порог» по всей области входов. Он заменил
страж, который сверял числовую копию шкалы агента с ``LEVEL_RANKS``: словари
совпадали, а поведение разошлось (синоним ``WARNING`` считался как ``INFO``, и
при пороге ``WARN`` событие терялось там, где платформа его писала). Проверять
надо РЕШЕНИЕ, а не совпадение объявлений.

Спека: ``openspec/specs/logging-db/spec.md`` § «Уровни логирования несут
смысл» и § «Потеря события всегда видима».
"""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from config import runtime_table  # noqa: F401  -- имена таблиц из конфигурации

AGENT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = AGENT_ROOT / "sql" / "logs" / "create_public_agent_gateway_logs.sql"
PLATFORM_MODELS = (
    AGENT_ROOT / "mcp-platform" / "libs" / "enterprise_common" / "eventing" / "models.py"
)
#: Каталоги агента, где собирается событие журнала. ``cli_agent.py`` и
#: ``utils/logging_utils.py`` исключены намеренно: там уровень относится к
#: stdlib-логированию процесса, а не к строке в ``agent_gateway_logs``.
JOURNAL_DIRS = ("lib", "workspace")

#: Шкала, объявленная платформой. Хардкодом намеренно: тест должен падать,
#: если словарь разъедется с ``CHECK``, — импортом он просто повторил бы
#: проверяемое утверждение.
CANONICAL: tuple[str, ...] = ("DEBUG", "INFO", "WARN", "ERROR")

_CHECK_RE = re.compile(
    r"CONSTRAINT\s+valid_level\s+CHECK\s*\(\s*level\s+IN\s*\((?P<levels>[^)]*)\)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Контракт схемы
# ---------------------------------------------------------------------------


def _accepted_levels() -> set[str]:
    """Уровни, которые принимает ``CHECK valid_level`` в DDL журнала."""
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    match = _CHECK_RE.search(text)
    assert match, (
        f"в {SCHEMA_SQL.name} нет CHECK valid_level — уровень стал бы "
        f"неограниченным, и стражи ниже проверяли бы пустоту"
    )
    return set(re.findall(r"'([^']+)'", match.group("levels")))


def _platform_levels() -> set[str]:
    """``LEVELS`` из словаря платформы, прочитанный без импорта.

    Импорт ``libs.*`` поднял бы весь пакет платформы в процессе тестов
    агента; здесь нужен только список имён, и он лежит в дереве рядом.
    """
    tree = ast.parse(PLATFORM_MODELS.read_text(encoding="utf-8"), filename=str(PLATFORM_MODELS))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "LEVELS":
            return {
                element.value
                for element in node.value.elts  # type: ignore[union-attr]
                if isinstance(element, ast.Constant)
            }
    raise AssertionError(f"в {PLATFORM_MODELS.name} нет объявления LEVELS")


class TestSchemaCheckIsTheContract:
    def test_check_accepts_exactly_the_platform_scale(self) -> None:
        """Шкала в базе и в словаре платформы — один и тот же набор.

        Расхождение означало бы, что одна из сторон валидирует событие, а
        другая — нет, и разъезд обнаружился бы на откате батча.
        """
        accepted = _accepted_levels()
        assert accepted == set(CANONICAL), (
            f"CHECK в схеме принимает {sorted(accepted)}, словарь платформы — "
            f"{sorted(CANONICAL)}"
        )
        assert accepted == _platform_levels()

    def test_check_does_not_accept_the_warning_synonym(self) -> None:
        """``WARNING`` — синоним на входе, а не значение шкалы.

        Платформа приводит его к ``WARN`` на границе запроса
        (``normalize_level``), поэтому в базу он не попадает ни с одной
        стороны. Появление синонима в ``CHECK`` сделало бы две разные
        шкалы на одной таблице.
        """
        accepted = _accepted_levels()
        assert "WARNING" not in accepted
        assert "WARN" in accepted

    def test_platform_declares_the_synonym_as_an_alias_only(self) -> None:
        """Словарь платформы: синоним отображается в ``WARN`` и не объявлен.

        Проверка сдерживает подмену: «нормализация» не должна превращаться в
        ``CHECK`` с пятью значениями, где пятое ничем не отличается от
        четвёртого по смыслу.
        """
        tree = ast.parse(
            PLATFORM_MODELS.read_text(encoding="utf-8"), filename=str(PLATFORM_MODELS)
        )
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (
                [node.target]
                if isinstance(node, ast.AnnAssign)
                else [target for target in node.targets if isinstance(target, ast.Name)]
            )
            if not any(getattr(target, "id", "") == "_LEVEL_ALIASES" for target in targets):
                continue
            aliases = {
                key.value: value.value
                for key, value in zip(node.value.keys, node.value.values, strict=True)
                if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
            }
        assert aliases, "в словаре платформы больше нет отображения синонимов"
        assert aliases == {"WARNING": "WARN"}
        assert "WARNING" not in _platform_levels()


# ---------------------------------------------------------------------------
# Производители агента: статический скан дерева
# ---------------------------------------------------------------------------


def _journal_sources() -> list[Path]:
    paths: list[Path] = []
    for folder in JOURNAL_DIRS:
        paths.extend(sorted((AGENT_ROOT / folder).rglob("*.py")))
    return paths


def _log_event_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "LogEvent"
    ]


def _level_keyword(call: ast.Call) -> ast.keyword | None:
    for keyword in call.keywords:
        if keyword.arg == "level":
            return keyword
    return None


def _module_constants(tree: ast.Module) -> dict[str, Any]:
    """Константы уровней уровня модуля: ``WARN_LEVEL = "WARN"``."""
    found: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        names = (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else [target for target in node.targets if isinstance(target, ast.Name)]
        )
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for name in names:
                if isinstance(name, ast.Name):
                    found[name.id] = value.value
    return found


def _enclosing_level_default(tree: ast.Module, name: str, inside: ast.AST) -> str | None:
    """Дефолт параметра ``level`` у функции, внутри которой встретилось имя.

    Продюсеры уровня в агене — это обёртки вида ``log_tool_call(...,
    level: str = "INFO")``, которые передают параметр в ``LogEvent``. Такой
    pass-through легален, пока дефолт канонический: вызывающий может сузить
    набор уровней, но не расширить его за пределы шкалы.

    Ищется только среди функций, реально объемлющих вызов: поиск «где-нибудь
    в файле» прощал бы вызов с любым именем, если где-то в модуле есть
    похожий параметр.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    current: ast.AST | None = parents.get(inside)
    while current is not None:
        default = _literal_level_default(current, name)
        if default is not None:
            return default
        current = parents.get(current)
    return None


def _literal_level_default(node: ast.AST, name: str) -> str | None:
    """Строковый дефолт параметра ``name`` у функции ``node``."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    args = node.args
    positional = [*getattr(args, "posonlyargs", []), *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(
        args.defaults
    )
    for arg, default in [*zip(positional, defaults), *zip(args.kwonlyargs, args.kw_defaults)]:
        if arg.arg == name and isinstance(default, ast.Constant) and isinstance(
            default.value, str
        ):
            return default.value
    return None


def _static_levels(node: ast.expr) -> set[str] | None:
    """Уровни, если выражение составлено только из строковых литералов.

    ``"ERROR" if flag else "INFO"`` — набор известен уже при чтении кода, то
    есть это не динамическая сборка. f-строка, ``.upper()`` и подстановка
    чужой переменной — наоборот, здесь неразрешимы: на входе писателя
    нормализации нет, и значение уедет в батч как есть.
    """
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else None
    if isinstance(node, ast.IfExp):
        body = _static_levels(node.body)
        orelse = _static_levels(node.orelse)
        if body is None or orelse is None:
            return None
        return body | orelse
    return None


class TestAgentProducersWriteOnlyAcceptedLevels:
    def test_no_producer_declares_a_level_the_check_rejects(self) -> None:
        """Литерал ``level=`` в ``LogEvent`` обязан быть из шкалы.

        Список исключений здесь намеренно отсутствует: он превратил бы страж
        в «долг, который можно тихо размножать» — ровно тем, чем был
        предыдущий долг из четырёх записей ``WARNING``.
        """
        accepted = _accepted_levels()
        offenders: list[str] = []
        for path in _journal_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for call in _log_event_calls(tree):
                keyword = _level_keyword(call)
                if keyword is None:
                    continue
                # Тернар из двух канонических уровней — тоже набор литералов
                # (их четыре на весь проект), поэтому он проверяется здесь, а не
                # в следующем тесте.
                static = _static_levels(keyword.value)
                if static is None:
                    continue
                bad = sorted(static - accepted)
                if bad:
                    rel = path.relative_to(AGENT_ROOT).as_posix()
                    offenders.append(f"{rel}:{call.lineno} level={bad}")
        assert not offenders, (
            "CHECK valid_level такие уровни не принимает, а отказ уносит весь "
            f"батч: {offenders}"
        )

    def test_no_producer_builds_a_level_dynamically(self) -> None:
        """Уровень не собирается на лету и не приходит из чужой шкалы.

        Динамическая сборка (f-строка, ``.upper()``, ``str(...)``) не
        попадает в ``CHECK`` на стороне Python: значение выглядит правдоподобно
        и уезжает в батч, где его отвергнут целиком. Допустим только
        pass-through именованного уровня с каноническим дефолтом.
        """
        accepted = _accepted_levels()
        offenders: list[str] = []
        for path in _journal_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            constants = _module_constants(tree)
            for call in _log_event_calls(tree):
                keyword = _level_keyword(call)
                if keyword is None or _static_levels(keyword.value) is not None:
                    continue
                if isinstance(keyword.value, ast.Name):
                    name = keyword.value.id
                    if name in constants:
                        if constants[name] not in accepted:
                            offenders.append(
                                f"{path.relative_to(AGENT_ROOT).as_posix()}:"
                                f"{call.lineno} {name}={constants[name]!r}"
                            )
                        continue
                    default = _enclosing_level_default(tree, name, call)
                    if default is not None and default in accepted:
                        continue
                offenders.append(
                    f"{path.relative_to(AGENT_ROOT).as_posix()}:{call.lineno} "
                    f"level={ast.unparse(keyword.value)}"
                )
        assert not offenders, (
            "уровень события строится динамически — он может выйти за пределы "
            f"шкалы молча: {offenders}"
        )


class TestAbsentLevelIsNotDowngraded:
    """Отсутствие уровня не должно молча оборачиваться в «лёгкое».

    Уровень перечисляют далеко не в каждом событии, и трактовка «нет уровня =
    диагностика» отбрасывала бы часть оборота по настройке, о которой
    никто не знал. Пустое значение — обычный факт (``INFO``).
    """

    def test_log_event_default_level_is_info(self) -> None:
        from lib.services.db_logging_service import LogEvent

        assert LogEvent(event_type="agent.started").level == "INFO"

    def test_empty_level_passes_the_info_threshold(self) -> None:
        from lib.services.db_logging_service import DbLoggingService

        should_log = DbLoggingService._should_log
        service = SimpleNamespace(_min_level="INFO")
        assert should_log(service, "") is True
        assert should_log(service, None) is True
        assert should_log(service, "INFO") is True

    def test_debug_is_still_filtered_out_by_the_info_threshold(self) -> None:
        """Нормализация не должна была «поднять» ``DEBUG`` до проходного.

        Иначе диагностика попадала бы в продовую таблицу по умолчанию.
        """
        from lib.services.db_logging_service import DbLoggingService

        service = SimpleNamespace(_min_level="INFO")
        assert DbLoggingService._should_log(service, "DEBUG") is False

    def test_synonym_is_resolved_and_not_counted_as_unknown(self) -> None:
        """``WARNING`` разбирается синонимом, а не проходит «как получится».

        Прежняя проверка утверждала, что фильтр агента не знает синонимов и
        потому не отбрасывает ``WARNING``, — то есть закрепляла маршрут
        дефекта как норму: неизвестное значение считалось как ``INFO``, и при
        пороге ``WARN`` событие терялось там, где платформа его писала.

        Теперь утверждение обратное: синоним **разбирается** (``WARN``), и в
        отказ по шкале он не попадает. Проверяется и вес, и счётчик отказов —
        «прошло с правильным уровнем» и «прошло потому, что непонятный уровень
        посчитали как обычный» выглядят снаружи одинаково.
        """
        from lib.services.db_logging_service import (
            DbLoggingService,
            JournalLevelError,
            normalize_journal_level,
        )

        assert normalize_journal_level("WARNING") == "WARN"
        service = SimpleNamespace(_min_level="INFO")
        assert DbLoggingService._should_log(service, "WARNING") is True
        # Настоящий отказ — только когда значения нет в шкале после разбора
        # синонимов. ``WARNING`` таким значением не является.
        with pytest.raises(JournalLevelError):
            normalize_journal_level("WARNINGS")


# ---------------------------------------------------------------------------
# Поведение фильтра: та же таблица решений, что у платформы
# ---------------------------------------------------------------------------
#
# Область входов объявлена явно: четыре уровня шкалы, синоним в трёх
# написаниях, регистр, пробелы, пустое значение и значения вне шкалы. Порогов
# ровно четыре — те, что имеет смысл выставить в ``config.json``.
#
# Таблица решений **объявлена здесь**, а не выведена из словарей обеих сторон:
# прежний страж сравнивал числовую копию агента с ``LEVEL_RANKS`` — словари
# совпадали, а поведение разошлось, и страж этого не заметил. Проверять надо
# РЕШЕНИЕ, а не совпадение объявлений; решение платформы закреплено её же
# проверками (``mcp-platform/tests/test_journal_noise_policy.py``,
# ``TestLevelScaleIsSingle``) — обе половины обязаны давать один ответ на
# одни и те же входы.
EXPECTED_DECISIONS: dict[str, dict[str, bool]] = {
    "DEBUG": {"DEBUG": True, "INFO": False, "WARN": False, "ERROR": False},
    "INFO": {"DEBUG": True, "INFO": True, "WARN": False, "ERROR": False},
    "WARN": {"DEBUG": True, "INFO": True, "WARN": True, "ERROR": False},
    "ERROR": {"DEBUG": True, "INFO": True, "WARN": True, "ERROR": True},
}

#: Написания, которые обязаны давать то же решение, что и их канон. Регистр,
#: пробелы и синоним приходят из привычного ``logging`` и из конфигурации.
SAME_DECISION_SPELLINGS: dict[str, str] = {
    "WARNING": "WARN",
    "warning": "WARN",
    "Warning": "WARN",
    " WARN ": "WARN",
    "warn": "WARN",
    "debug": "DEBUG",
    " info": "INFO",
    "  Error  ": "ERROR",
    # Пустое значение — обычный факт оборота, а не «лёгкое» событие:
    # трактовка «нет уровня = диагностика» выбросила бы часть оборота по
    # настройке, о которой никто не знал. Отсутствие значения (``None``)
    # проверяется отдельно — как ключ в словаре форм он сортировался бы с
    # остальными и ронял разбор.
    "": "INFO",
    "   ": "INFO",
}

#: Значения вне шкалы. Проверяются и по разбору, и по отказу фильтра.
LEVELS_OUTSIDE_SCALE: tuple[str, ...] = ("BOGUS", "TRACE", "5", "warn ing", "WARNINGS")


class TestAgentFilterDecidesLikeThePlatform:
    """Решение фильтра обязано совпадать с решением платформы на всех входах.

    Пять форм входа проверяются по одному правилу: синоним разбирается,
    регистр и пробелы не значат, пустое значение — обычный ``INFO``, а
    значение вне шкалы **отказывается**, а не подменяется.
    """

    @staticmethod
    def _service(min_level: str) -> Any:
        from lib.services.db_logging_service import DbLoggingService

        # Настоящий сервис, а не ``SimpleNamespace``: он нормализует порог на
        # сборке, и проверка заодно видит, что порог из конфигурации доехал
        # тем же правилом, что и уровень события.
        return DbLoggingService(
            dsn="", table_name="x", question_runs_table="y", min_level=min_level
        )

    @pytest.mark.parametrize("level", sorted(EXPECTED_DECISIONS))
    @pytest.mark.parametrize("threshold", ["DEBUG", "INFO", "WARN", "ERROR"])
    def test_level_against_threshold_matches_the_shared_table(
        self, level: str, threshold: str
    ) -> None:
        service = self._service(threshold)
        assert service._should_log(level) is EXPECTED_DECISIONS[level][threshold], (
            f"уровень {level!r} при пороге {threshold!r}: агент решил иначе, "
            "чем решила бы платформа по той же шкале"
        )

    @pytest.mark.parametrize("spelling", sorted(SAME_DECISION_SPELLINGS))
    @pytest.mark.parametrize("threshold", ["DEBUG", "INFO", "WARN", "ERROR"])
    def test_spelling_does_not_change_the_decision(
        self, spelling: str, threshold: str
    ) -> None:
        """Регистр, пробелы и синоним обязаны давать решение своего канона.

        Именно здесь жил дефект: ``WARNING`` уходил в «неизвестные», считался
        как ``INFO`` и при пороге ``WARN`` событие терялось — тихо, в месте,
        где платформа то же событие писала.
        """
        service = self._service(threshold)
        canonical = SAME_DECISION_SPELLINGS[spelling]
        assert service._should_log(spelling) is EXPECTED_DECISIONS[canonical][threshold], (
            f"{spelling!r} (канон {canonical!r}) при пороге {threshold!r} решён "
            "не так, как решён его канон"
        )

    def test_absent_level_is_a_fact_not_diagnostics(self) -> None:
        from lib.services.db_logging_service import normalize_journal_level

        assert normalize_journal_level(None) == "INFO"
        assert normalize_journal_level("") == "INFO"
        assert normalize_journal_level("   ") == "INFO"
        service = self._service("INFO")
        assert service._should_log(None) is True
        assert service._should_log("WARN") is True
        assert service._should_log(None) is EXPECTED_DECISIONS["INFO"]["INFO"]

    @pytest.mark.parametrize("value", LEVELS_OUTSIDE_SCALE)
    def test_value_outside_the_scale_is_refused(self, value: str) -> None:
        """Вне шкалы — отказ, а не тихая подмена.

        Подмена означала бы ровно то, от чего этот файл и защищает: событие,
        записанное в таблицу под чужим весом, и читатель журнала, который
        фильтрует по весу и об этом не знает.
        """
        from lib.services.db_logging_service import JournalLevelError, normalize_journal_level

        with pytest.raises(JournalLevelError):
            normalize_journal_level(value)

    @pytest.mark.parametrize("value", LEVELS_OUTSIDE_SCALE)
    def test_filter_refuses_instead_of_dropping(self, value: str) -> None:
        """Фильтр обязан поднять отказ, а не вернуть ``False`` молча.

        ``False`` на отказе по шкале означал бы «событие отброшено правилом»,
        а на неизвестном уровне — «мы не поняли, что это, и не сказали». Разница
        видна только в ``log_event``, который ловит отказ и считает его.
        """
        from lib.services.db_logging_service import JournalLevelError

        service = self._service("DEBUG")
        with pytest.raises(JournalLevelError):
            service._should_log(value)

    def test_agent_declares_the_same_levels_as_the_platform(self) -> None:
        """Объявления совпадают — и это дешёвая ловушка, а не доказательство.

        Проверка поведения выше даёт силу; эта нужна, чтобы расхождение
        объявлений заметили сразу, а не по факту упавшего события.
        """
        from lib.services.db_logging_service import JOURNAL_LEVELS

        assert set(JOURNAL_LEVELS) == _platform_levels()
        assert set(JOURNAL_LEVELS) == _accepted_levels()

    def test_agent_declares_no_second_numeric_scale(self) -> None:
        """Числовой вес выводится из объявленного набора, а не пишется рядом.

        Прежний фильтр держал литерал ``{"DEBUG": 0, "INFO": 1, ...}`` прямо в
        теле метода. Вторая числовая шкала разъезжается с первой при первой
        правке любой из них, и разъезд молчалив: фильтр отбрасывает событие, не
        объясняя почему.
        """
        source = (AGENT_ROOT / "lib" / "services" / "db_logging_service.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source, filename="db_logging_service.py")
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
            if not set(CANONICAL).issubset(set(keys)):
                continue
            if all(
                isinstance(value, ast.Constant) and isinstance(value.value, int)
                for value in node.values
            ):
                offenders.append(f"{node.lineno}")
        assert not offenders, (
            "в писателе объявлена вторая числовая шкала уровней: "
            f"строки {offenders}"
        )

    def test_service_normalizes_the_configured_threshold(self) -> None:
        """Порог из конфигурации разбирается тем же правилом, что и уровень.

        Иначе вторая половина расхождения была бы на месте: ``min_level:
        "warning"`` в ``config.json`` деградировал бы в дефолт, и журнал
        оказался бы отфильтрован по порогу, которого никто не задавал.
        """
        assert self._service(" warning ")._min_level == "WARN"

    def test_service_refuses_a_broken_threshold_at_build(self) -> None:
        """Опечатка в пороге ловится на сборке, а не первым потерянным событием."""
        from lib.services.db_logging_service import JournalLevelError

        with pytest.raises(JournalLevelError):
            self._service("verbose")


# ---------------------------------------------------------------------------
# Поведение: реально построенные события
# ---------------------------------------------------------------------------


class _Collector:
    """Двойник ``DbLoggingService``: копит события, ничего не отбрасывая."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    @staticmethod
    def is_running() -> bool:
        return True

    def log_event(self, event: Any) -> bool:
        self.events.append(event)
        return True

    def levels(self) -> list[str]:
        return [event.level for event in self.events]


class TestPublishedLevelsAreAcceptedByTheCheck:
    def test_repeat_guard_publishes_a_level_the_check_accepts(self) -> None:
        """Событие защитника от повторов доходит до батча с ``WARN``.

        Проверяется не литерал в исходнике, а событие, построенное обычным
        прогоном детектора: возврат к ``WARNING`` роняет тест.
        """
        from lib.core.project_settings import GatewayRepeatGuardSettings
        from lib.hooks.repeat_guard_hook import RepeatGuardHook

        hook = RepeatGuardHook(
            GatewayRepeatGuardSettings(mode="warn", window_size=10, max_repeats_in_window=2)
        )
        collector = _Collector()
        hook._db = collector
        ctx = SimpleNamespace(session_key="s1", iteration=0)
        call = SimpleNamespace(name="t", arguments={"v": 1})
        asyncio.run(hook.before_iteration(ctx))
        for _ in range(6):
            asyncio.run(hook.before_execute_tool(ctx, call, None, {"v": 1}))

        assert collector.events, "детектор не сработал — тест ничего не проверяет"
        accepted = _accepted_levels()
        assert set(collector.levels()) <= accepted, (
            f"уровень, который CHECK не примет: {collector.levels()} "
            f"(accepted={sorted(accepted)})"
        )
        assert collector.levels() == ["WARN"]

    @pytest.mark.parametrize(
        ("event_type", "method"),
        [
            ("agent.degraded", "_log_failure"),
            ("agent.degraded", "_log_stale_once"),
            ("agent.degraded", "_log_lag"),
        ],
    )
    def test_cold_sync_events_are_warn(self, event_type: str, method: str) -> None:
        """Три события холодной синхронизации пишутся как ``WARN``.

        Все три — «оборот продолжается, но деградировал», то есть ровно то
        определение ``WARN`` из спеки; понижать их до ``INFO`` нельзя
        (уровень перестаёт нести смысл), а писать ``WARNING`` нельзя (CHECK
        его не берёт, и страдает весь батч).

        Случайные вызовы передают поддельный результат зеркала: эти методы
        читают из него только поля ``payload``, а решение «писать или нет»
        вынесено в саму операцию платформы.
        """
        from lib.services.session_cold_sync_service import SessionColdSyncService

        collector = _Collector()
        service = SessionColdSyncService(
            session_manager=SimpleNamespace(),
            enterprise_mcp=SimpleNamespace(),
            replica_id="gw-1",
            db_logging_service=collector,
        )
        if method == "_log_failure":
            getattr(service, method)(RuntimeError("соединение отвалилось"))  # type: ignore[operator]
        else:
            getattr(service, method)(  # type: ignore[operator]
                "session-key",
                {"previous_updated_at": "2026-10-01T12:00:00+00:00", "sync_lag_seconds": 600},
            )

        assert [event.event_type for event in collector.events] == [event_type]
        accepted = _accepted_levels()
        assert set(collector.levels()) <= accepted, (
            f"уровень, который CHECK не примет: {collector.levels()}"
        )
        assert collector.levels() == ["WARN"]


# ---------------------------------------------------------------------------
# Канон уровня поимённо: событие, которому заказчик назначил уровень
# ---------------------------------------------------------------------------


#: Канонический уровень событий, которым уровень назначен РЕШЕНИЕМ, а не
#: дефолтом. Ключ — каноническое имя события, значение — уровень, в котором
#: оно обязано публиковаться.
#:
#: Зачем именно эта таблица, а не проверка по месту: уровень, заданный
#: дефолтом, однажды меняют обратно молча — и размен, о котором договорились,
#: тихо отменяется, не оставив ни ошибки, ни красного теста. Здесь решение
#: зафиксировано ПОИМЕННО и проверяется поведением (реально построенное
#: событие), а не чтением литерала.
#:
#: Это НЕ список исключений и не ослабление стража: он не разрешает первому
#: встреченному уровню что-либо, а добавляет второе, независимое утверждение —
#: «у этого события уровень ровно такой». Откат любой из правок роняет тест:
#: и смена уровня события, и попытка объявить его здесь другим.
EVENT_LEVEL_CANON: dict[str, str] = {
    # Решение заказчика 2026-10-03 дословно: «Вызов LLM нужно логировать, но с
    # типом DEBUG». ``llm.exchanged`` — носитель тел обмена с моделью
    # (~62 КБ на строку, ~59 КБ из них — дословная копия аргументов
    # tool-вызовов). Событие продолжает писаться, но помечается ``DEBUG``:
    # чтение журнала по умолчанию его не видит, и объём таблицы падает.
    "llm.exchanged": "DEBUG",
}


class TestNamedEventLevelsAreCanonical:
    """Уровень, назначенный событию решением, проверяется поимённо."""

    @staticmethod
    def _llm_exchange_events() -> list[Any]:
        """События одного оборота, собранные НАСТОЯЩИМ сервисом.

        Намеренно не ``_Collector``: двойник с собственной реализацией
        ``log_llm_call`` проверял бы сам себя, а не код. Здесь работает
        боевой путь целиком — хук → ``log_llm_call`` → ``log_event`` →
        очередь, — поэтому тест ловит и смену уровня в хуке, и смену
        дефолта в сигнатуре ``log_llm_call``.

        ``min_level="DEBUG"`` — чтобы фильтр не отбросил событие до
        входа в очередь: проверяется уровень ПОСТРОЕННОГО события, а не
        факт его фильтрации (о фильтре отдельный тест ниже).
        """
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from lib.services.db_logging_service import DbLoggingService
        from nanobot.providers.base import LLMResponse

        service = DbLoggingService(
            dsn="", table_name="x", question_runs_table="y", min_level="DEBUG",
        )
        hook = DatabaseLoggingHook(service, session_key="cli:1", request_id="m1")
        ctx = SimpleNamespace(
            session_key="cli:1",
            iteration=1,
            messages=[{"role": "user", "content": "привет"}],
            usage={"total_tokens": 7},
            response=LLMResponse(content="ответ", finish_reason="stop"),
        )
        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.after_iteration(ctx))
        return list(service._queue.queue)

    def test_llm_exchange_is_published_as_debug(self) -> None:
        """Хук оборота публикует ``llm.exchanged`` именно как ``DEBUG``.

        Проверяется не литерал в исходнике, а событие, построенное обычным
        прогоном ``after_iteration``: возврат к ``INFO`` роняет тест.
        """
        exchanges = [
            event for event in self._llm_exchange_events()
            if event.event_type == "llm.exchanged"
        ]
        assert len(exchanges) == 1, (
            "хук не опубликовал ровно одно событие обмена с моделью: "
            f"{[e.event_type for e in self._llm_exchange_events()]}"
        )
        assert exchanges[0].level == "DEBUG", (
            "решение заказчика «логировать вызов LLM с типом DEBUG» нарушено: "
            f"опубликован уровень {exchanges[0].level!r}"
        )

    def test_every_canon_entry_holds_for_its_event(self) -> None:
        """Канон не расходится с тем, что публикует хук.

        Отдельная проверка от ``test_llm_exchange_is_published_as_debug``
        нужна, чтобы канон нельзя было «подправить» под фактическое
        поведение, не увидев самого поведения: тест выше утверждает
        опубликованный уровень, этот — что он равен канону.
        """
        published = {
            event.event_type: event.level for event in self._llm_exchange_events()
        }
        for event_type, expected in EVENT_LEVEL_CANON.items():
            assert published.get(event_type) == expected, (
                f"{event_type} опубликован как {published.get(event_type)!r}, "
                f"канон требует {expected!r}"
            )

    def test_canon_levels_are_accepted_by_the_check(self) -> None:
        """Канон не может назначить уровень, который ``CHECK`` отвергнет.

        Страховка на «уровень канона» в самой шкале: отказ касается всего
        батча, поэтому значение из ``EVENT_LEVEL_CANON`` обязано быть
        членом ``valid_level`` — иначе страж выше проверял бы пустоту.
        """
        accepted = _accepted_levels()
        offenders = {
            event_type: level
            for event_type, level in EVENT_LEVEL_CANON.items()
            if level not in accepted
        }
        assert not offenders, f"CHECK valid_level отвергнет канон: {offenders}"

    def test_debug_is_actually_filtered_at_the_production_threshold(self) -> None:
        """Явное следствие размена: при ``min_level=INFO`` строка не пишется.

        Канон ``llm.exchanged → DEBUG`` не означает, что обмен с моделью
        виден в боевой таблице. Фильтр агента отбрасывает ``DEBUG`` ДО
        постановки в очередь, то есть 62 КБ не доезжают до таблицы вовсе —
        и одновременно обмен НЕ логируется, пока порог не опущен до
        ``DEBUG``.

        Тест зафиксирован как поведение, а не как пожелание: он держит
        открытым вопрос «логируем ли мы обмен на самом деле», чтобы смена
        ``min_level`` была осознанным решением с проверкой с двух сторон.
        """
        from lib.services.db_logging_service import DbLoggingService

        service = SimpleNamespace(_min_level="INFO")
        assert DbLoggingService._should_log(service, EVENT_LEVEL_CANON["llm.exchanged"]) is (
            False
        ), (
            "порог боевого контура опущен до DEBUG — тест перестал быть "
            "описанием реальности, перечитайте его и решение заказчика"
        )
