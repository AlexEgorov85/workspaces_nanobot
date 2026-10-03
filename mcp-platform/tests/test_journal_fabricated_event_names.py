"""В тестах журнала не бывает выдуманных имён событий — ни на одной стороне.

Тест механики — буфера, плейсхолдеров, счётчиков, отказов — проверяет писателя.
Выдуманное имя (``turn.completed``, ``a``, ``b``) его не проверяет: под
``platform.json → data.log_unknown_event_type_policy = strict`` партия с таким
именем отказывается целиком ещё до записи, и красный тест говорит о словаре, а
не о том механизме, который задуман. Заглушка от этого выглядит как поломка
писателя, и чинить её начинают не с того места.

Имя вне словаря — законный предмет отдельной проверки, и она есть:
``tests/test_journal_contract_visibility.py`` (``TestUnknownEventTypesAreVisible``).
Здесь запрещено другое: подставлять выдуманное имя в тест, который проверяет не
словарь.

Почему обход, а не список
-------------------------
Страж прежней формы держал ``GUARDED`` — жёсткий список из двух файлов, которые
предыдущий воркер починил. Сканер честно отрабатывал на каждом элементе списка и
**не смотрел больше никуда**: страж проходил, ничего не проверяя, а выглядел при
этом работающей защитой. Хуже отсутствия стража — он усыпляет: следующий
воркер, увидев зелёный страж, не станет искать, где ещё остались выдуманные
имена, а они оставались (см. ``EXPECTED_EXCEPTION_OFFENDERS`` и историю правок).

Поэтому предмет объявлен как **оба дерева тестов целиком** —
``REPO_ROOT/tests`` и ``PLATFORM_ROOT/tests``, обходом каталога, а не
перечислением. Список исключений отсюда не следует и не может быть длинным:
исключение — это файл, в котором выдуманное имя является **предметом** проверки,
и каждое такое имя перечислено поимённо, чтобы новое имя в том же файле не
прошло молча (см. ``TestNarrowExceptions``).

Обе стороны — не только платформа
--------------------------------
Агент пишет журнал мимо платформенной проверки словаря: ``LogEvent`` уходит
прямо в PostgreSQL, и операция ``log_events`` с её ``strict``-отказом агента не
касается. Поэтому агентский прогон был зелёным при живых выдуманных именах в
агентских тестах, и «прогон зелёный» ничего не говорил об этой стороне. Скан
поэтому видит обе формы записи:

* платформенную — ``log_event``/``log_events``/``accept`` и словарь
  ``{"event_type": ...}``;
* агентскую — конструктор ``LogEvent(event_type=...)``, то есть единственное
  место, где агент задаёт имя события.

Правило
-------
Имя в позиции записи журнала обязано быть одним из трёх:

* **каноническое имя из словаря** — строковым литералом из
  ``libs/enterprise_common/eventing/types.py``, именем, импортированным оттуда
  же, или значением именованной константы модуля с таким значением; последнее
  проверяется отдельно, потому что иначе локальная
  ``TURN_COMPLETED = "turn.completed"`` прошла бы как «имя, взятое у словаря»;
* **пробное имя** (``smoke.``, ``probe_``, ``live.db_probe``) — тогда тест
  проверяет подавление пробы, и это законно: проба не пишется в таблицу;
* **пустое имя** — тогда тест проверяет отказ по контракту.

Что скан принципиально не видит (осознанная граница, а не дыра)
-------------------------------------------------------------
* **имя, пришедшее переменной**: ``service.log_event(name)``, где ``name`` —
  параметр ``parametrize`` или значение, собранное в другом модуле. Статически
  проверить нечего, а выдумывать значение нельзя. Выдуманное имя, спрятанное
  в такой переменной, страж не увидит — это цена разбора по AST, и она названа
  здесь, а не спрятана. Строка-константа, наоборот, разворачивается, поэтому
  ``EVENT_TYPE = "что-то"`` проверяется;
* **имя, вычисляемое на месте** (``f"q_{i}"``): по той же причине;
* **аргумент чужой операции** с полем ``event_type``: в ``history_search`` это
  фильтр чтения, а не запись. Такой словарь отбрасывается, когда он подан прямо
  в вызов не-журнальной функции;
* **ожидаемое значение в утверждении** (``assert x == {"event_type": ...}``) —
  это утверждение о том, что должно получиться, а не запись.

Проверка самого правила
-----------------------
Страж, который ничего не находит, зелёный всегда. Поэтому здесь же проверяется,
что скан действительно читает дерево — на подставном файле, на реальном файле
каждого контура и на области покрытия: сужение до «проверенного» списка
обязано ронять ``TestCoverage``, а не проходить тихо.
"""

from __future__ import annotations

import ast
import sys
from functools import lru_cache
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLATFORM_ROOT.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.eventing.types import (  # noqa: E402
    EVENT_TYPES,
    is_probe_event_type,
)

#: Деревья тестов, которые страж обязан видеть целиком. Оба: платформа и агент.
TEST_ROOTS: tuple[Path, ...] = (REPO_ROOT / "tests", PLATFORM_ROOT / "tests")

#: Модуль, из которого имя события обязано прийти.
DICTIONARY_MODULE = "eventing.types"

#: Входы журнала платформы, первый позиционный аргумент которых — имя события.
ENTRY_POINTS = frozenset({"log_event", "log_events", "accept"})

#: Конструктор события агента: имя задаётся одноимённым аргументом.
AGENT_EVENT_BUILDERS = frozenset({"LogEvent"})

#: Файл стража. Исключение не из правила, а из самоссылки: строковые литералы
#: выдуманных имён лежат в самом коде скана (см. ``TestTheRuleItself``), и без
#: этого исключения скан поймал бы собственный пример разбора.
SELF = Path(__file__).resolve()

#: Файлы, где выдуманное имя — ПРЕДМЕТ проверки, а не её фикстура.
#: Ключ — файл, значение — (причина, обязательный маркер в его тексте).
#: Причина обязана быть непустой: исключение без объяснения не проходит.
REJECTION_SUBJECT_FILES: dict[Path, tuple[str, str]] = {
    PLATFORM_ROOT / "tests" / "test_journal_contract_visibility.py": (
        "Выдуманное имя здесь и есть предмет проверки: файл доказывает, что имя "
        "вне словаря видно (счётчик, строка в логе) и что strict отказывает "
        "батч. Заменить имя на каноническое здесь нельзя — проверять было бы "
        "нечего.",
        "вне объявленного словаря",
    ),
}

#: Имена вне словаря, которые объяснены исключением выше — поимённо.
#: Не список «что тут разрешено», а затычка: новое выдуманное имя в
#: исключённом файле меняет множество и роняет ``test_exception_names_exactly``
#: с требованием объяснить именно его.
EXPECTED_EXCEPTION_OFFENDERS: dict[Path, frozenset[str]] = {
    PLATFORM_ROOT / "tests" / "test_journal_contract_visibility.py": frozenset(
        {"tool_call", "tool.complted", "inbound"}
    ),
}


# --- обход дерева ----------------------------------------------------------


def _test_files() -> list[Path]:
    """Каждый файл тестов обоих контуров. Обходом, а не перечислением."""
    found: set[Path] = set()
    for root in TEST_ROOTS:
        if not root.is_dir():  # pragma: no cover - корень тестов обязателен
            continue
        found.update(
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return sorted(found)


def _excepted(path: Path) -> Path | None:
    """Файл исключён — вернуть причину, иначе ``None``."""
    if path == SELF:
        return "сам файл стража: здесь разбираются выдуманные имена как примеры"
    for candidate, (reason, _marker) in REJECTION_SUBJECT_FILES.items():
        if path == candidate:
            return reason
    return None


def _scanned_files() -> list[Path]:
    return [path for path in _test_files() if _excepted(path) is None]


def _parse(path: Path) -> ast.Module:
    """Разобрать исходник или упасть с внятной причиной.

    Обход ``SyntaxError`` недопустим: страж, который не смог прочитать свой
    предмет, не имеет права сообщать «выдуманных имён нет». ``utf-8-sig`` —
    потому что в дереве есть файлы с BOM, и они читаются, а не выпадают молча.
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except SyntaxError as exc:
        raise AssertionError(
            f"{path}: файл не разбирается ({exc.msg}). Страж не имеет права "
            "сказать «выдуманных имён нет», не прочитав файл."
        ) from exc


# --- разбор одной позиции записи ------------------------------------------


def _func_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return getattr(node, "id", None)


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    return {
        id(child): parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _assigned(tree: ast.AST) -> tuple[dict[str, str], frozenset[str]]:
    """Значения именованных констант и имена объектов ``LogEvent``.

    Строка-константа модуля разворачивается в своё значение, а имя объекта
    события запоминается отдельно: ``svc.log_event(event)`` передаёт в журнал не
    имя, а событие, и имя задано в ``LogEvent(event_type=...)`` — там оно и
    проверяется. Без этого различения страж ругался бы на имя переменной.
    """
    values: dict[str, str] = {}
    events: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        value = node.value
        if not names or not isinstance(value, ast.AST):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.update(dict.fromkeys(names, value.value))
        elif isinstance(value, ast.Call) and _func_name(value.func) in AGENT_EVENT_BUILDERS:
            events |= names
    return values, frozenset(events)


def _name_of(
    node: ast.AST | None,
    *,
    constants: dict[str, str],
    from_dictionary: frozenset[str],
) -> tuple[str, bool] | None:
    """Имя события и признак «взято из словаря»; ``None`` — не имя события.

    Имя, пришедшее переменной без строкового значения, именем не считается: его
    нечем проверить, а выдумывать значение страж не вправе (граница названа в
    докстринге модуля).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.Name):
        if node.id in from_dictionary:
            return node.id, True
        if node.id in constants:
            return constants[node.id], False
    return None


def _is_argument_payload(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Словарь не является событием журнала — и почему.

    Два случая, оба по месту, а не по списку имён:

    * **подан прямо в вызов НЕ-журнальной функции** — это аргумент операции.
      Так выглядит ``event_type`` фильтра чтения ``history_search`` и белый
      список полей аргументов ``ENTERPRISE_EXEC_LOG_ARG_FIELDS``: имя поля то
      же, а писателя за ним нет;
    * **стоит операндом сравнения** — это ожидаемое значение утверждения
      ``assert x == {...}``, а не запись.
    """
    parent = parents.get(id(node))
    if isinstance(parent, ast.Compare):
        return True
    current: ast.AST = node
    parent = parents.get(id(current))
    while isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
        current = parent
        parent = parents.get(id(current))
    if not isinstance(parent, ast.Call):
        return False
    if _func_name(parent.func) in ENTRY_POINTS:
        return False
    return any(
        item is current
        for item in [*parent.args, *(kw.value for kw in parent.keywords)]
    )


def _event_names(path: Path) -> list[tuple[int, str, bool]]:
    """Имена событий в позиции записи: ``(строка, значение, взят ли из словаря)``.

    Три синтаксические формы — потому что ровно три есть в тестах двух контуров:
    ``service.log_event("...")`` — вызовом платформенного входа,
    ``LogEvent(event_type="...")`` — конструктором агентского события и
    ``{"event_type": "..."}`` — словарём события на проводе.
    """
    tree = _parse(path)
    constants, event_objects = _assigned(tree)
    from_dictionary = _dictionary_names(tree)
    parents = _parents(tree)
    found: list[tuple[int, str, bool]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = _func_name(node.func)
            if func in AGENT_EVENT_BUILDERS:
                for keyword in node.keywords:
                    if keyword.arg != "event_type":
                        continue
                    resolved = _name_of(
                        keyword.value,
                        constants=constants,
                        from_dictionary=from_dictionary,
                    )
                    if resolved is not None:
                        found.append((node.lineno, resolved[0], resolved[1]))
                continue
            if func not in ENTRY_POINTS or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in event_objects:
                continue  # имя задано в LogEvent(...) и проверяется там
            resolved = _name_of(
                first,
                constants=constants,
                from_dictionary=from_dictionary,
            )
            if resolved is not None:
                found.append((node.lineno, resolved[0], resolved[1]))
        elif isinstance(node, ast.Dict) and not _is_argument_payload(node, parents):
            for key, value in zip(node.keys, node.values, strict=True):
                if not (isinstance(key, ast.Constant) and key.value == "event_type"):
                    continue
                resolved = _name_of(
                    value,
                    constants=constants,
                    from_dictionary=from_dictionary,
                )
                if resolved is not None:
                    found.append((node.lineno, resolved[0], resolved[1]))
    return found


def _dictionary_names(tree: ast.AST) -> frozenset[str]:
    """Имена, импортированные из словаря типов, — где угодно в модуле.

    Импорт бывает и на верхнем уровне, и внутри теста; оба случая одинаково
    означают «имя взято у словаря», поэтому обходятся все узлы дерева.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and DICTIONARY_MODULE in node.module
        ):
            names.update(alias.asname or alias.name for alias in node.names)
    return frozenset(names)


def _offenders(path: Path) -> list[str]:
    offenders: list[str] = []
    for lineno, value, from_dictionary in _event_names(path):
        if not value.strip():
            continue  # пустое имя — намеренный отказ по контракту
        if is_probe_event_type(value):
            continue  # проба — намеренное подавление
        if value in EVENT_TYPES:
            continue
        if from_dictionary:
            continue  # каноническое имя, взятое у словаря
        offenders.append(f"{path.name}:{lineno} {value!r}")
    return offenders


@lru_cache(maxsize=None)
def _event_names_cached(path: Path, mtime: float) -> tuple[tuple[int, str, bool], ...]:
    return tuple(_event_names(path))


@lru_cache(maxsize=None)
def _offenders_cached(path: Path, mtime: float) -> tuple[str, ...]:
    return tuple(_offenders(path))


def offenders_of(path: Path) -> list[str]:
    return list(_offenders_cached(path, path.stat().st_mtime))


def event_names_of(path: Path) -> list[tuple[int, str, bool]]:
    return list(_event_names_cached(path, path.stat().st_mtime))


def _id(path: Path) -> str:
    """Идентификатор параметра с контуром: имена тестов в двух деревьях совпадают."""
    return path.relative_to(REPO_ROOT).as_posix()


#: Файлы, в которых скан вообще что-то находит: предмет невакуумности.
#: Не «избранные», а найденные обходом — иначе проверка «скан не пуст» работала
#: бы на двух файлах, а страж молчал бы на остальных.
WRITE_FILES: list[Path] = [
    path for path in _scanned_files() if event_names_of(path)
]


# --- предмет: выдуманных имён нет ни в одном контуре ------------------------


class TestNoFabricatedEventNames:
    @pytest.mark.parametrize("path", _scanned_files(), ids=_id)
    def test_names_in_entry_position_are_declared(self, path: Path) -> None:
        offenders = offenders_of(path)
        assert not offenders, (
            f"выдуманные имена событий в {path.name}: {offenders}. Тест механики "
            "писателя обязан брать имя из "
            "libs/enterprise_common/eventing/types.py, иначе под "
            "log_unknown_event_type_policy=strict он падает на словаре и "
            "выглядит как поломка писателя. Проверка отказа на имя вне словаря "
            "живёт в tests/test_journal_contract_visibility.py."
        )

    @pytest.mark.parametrize("path", WRITE_FILES, ids=_id)
    def test_scan_is_not_vacuous(self, path: Path) -> None:
        """В файле обязано быть что проверять.

        Пустой результат скана означал бы, что страж молчит вхолостую: файл можно
        было бы переписывать целиком, и он остался бы зелёным. Тест работает на
        каждом файле предмета, найденном обходом, — а не на избранных.
        """
        found = event_names_of(path)
        assert found, f"{path.name}: скан не нашёл ни одного имени события в позиции записи"


# --- предмет не сузить до «проверенного» списка ----------------------------


class TestCoverage:
    def test_both_contours_are_scanned(self) -> None:
        """Страж видит и платформенные, и агентские тесты.

        Агент пишет журнал мимо ``log_events``, поэтому его зелёный прогон ничего
        не говорит об именах в его тестах: проверять обе стороны можно только
        обходом обоих деревьев.
        """
        agent = [p for p in _scanned_files() if REPO_ROOT / "tests" in p.parents]
        platform = [p for p in _scanned_files() if PLATFORM_ROOT / "tests" in p.parents]
        assert len(agent) > 50, f"агентских файлов в предмете: {len(agent)}"
        assert len(platform) > 50, f"платформенных файлов в предмете: {len(platform)}"

    def test_both_contours_are_in_the_subject(self) -> None:
        """Предмет невакуумности — не пустой и лежит в обоих контурах.

        Именно на этом стоит «скан не пуст»: если разбор перестанет находить
        имена в позиции записи, множество опустеет, и проверка станет зелёной
        вслепую. Пустота предмета — падение, а не успех.
        """
        agent = [p for p in WRITE_FILES if REPO_ROOT / "tests" in p.parents]
        platform = [p for p in WRITE_FILES if PLATFORM_ROOT / "tests" in p.parents]
        assert agent, "в агентских тестах не найдено ни одного имени в позиции записи"
        assert platform, "в платформенных тестах не найдено ни одного имени в позиции записи"
        assert len(WRITE_FILES) == len({*agent, *platform})

    def test_nothing_outside_the_test_trees_is_scanned(self) -> None:
        """Страж читает тесты, а не рабочий код: предмет объявлен явно."""
        roots = tuple(root.resolve() for root in TEST_ROOTS)
        for path in _test_files():
            assert any(root in path.parents for root in roots), path

    def test_every_file_in_the_trees_is_either_scanned_or_excepted(self) -> None:
        """Объявленных исключений мало, и каждое — файл, а не каталог."""
        everything = len(_test_files())
        assert len(_scanned_files()) + len(REJECTION_SUBJECT_FILES) + 1 == everything

    def test_the_guard_reads_a_real_file_of_each_contour(self) -> None:
        """Скан читает настоящие файлы дерева, а не только подставные.

        Берутся файлы, которых в прежней форме стража (``GUARDED`` из двух
        пунктов) не было вовсе.
        """
        platform = PLATFORM_ROOT / "tests" / "test_data_log_events.py"
        agent = REPO_ROOT / "tests" / "test_turn_observability_events.py"
        assert event_names_of(platform), "платформенный файл не прочитан"
        assert event_names_of(agent), "агентский файл не прочитан"


# --- исключения: узкие и объяснённые ---------------------------------------


class TestNarrowExceptions:
    def test_every_exception_explains_itself(self) -> None:
        for path, (reason, _marker) in REJECTION_SUBJECT_FILES.items():
            assert reason.strip(), f"{path.name}: исключение без причины"
            assert len(reason) > 80, (
                f"{path.name}: причина в одно предложение — исключение обязано "
                "объяснять, почему имя здесь законно"
            )

    def test_every_exception_still_matches_its_file(self) -> None:
        """Причина обязана оставаться правдой о файле, а не только о нём самой."""
        for path, (_reason, marker) in REJECTION_SUBJECT_FILES.items():
            assert path.is_file(), f"{path}: исключённого файла нет"
            assert marker in path.read_text(encoding="utf-8-sig"), (
                f"{path.name}: маркер {marker!r} из причины в файле не найден — "
                "причина исключения устарела, файл перестал быть проверкой отказа"
            )

    def test_exception_names_exactly(self) -> None:
        """Новое выдуманное имя в исключённом файле не пройдёт молча.

        Множество имён объявлено поимённо, поэтому добавить офендера в
        исключённый файл можно только вместе с объяснением именно его.
        """
        for path, expected in EXPECTED_EXCEPTION_OFFENDERS.items():
            found = {line.split(" ", 1)[1].strip("'\"") for line in offenders_of(path)}
            assert found == set(expected), (
                f"{path.name}: выдуманные имена изменились — {sorted(found)} вместо "
                f"{sorted(expected)}. Если новое имя здесь законно, объяви его в "
                "EXPECTED_EXCEPTION_OFFENDERS с причиной; если нет — замени на "
                "каноническое."
            )

    def test_the_guard_file_is_the_only_self_reference(self) -> None:
        assert SELF in {Path(__file__).resolve()}
        assert SELF not in _scanned_files()


# --- проверка правила, иначе страж может оказаться пустым -------------------


class TestTheRuleItself:
    """Проверка правила, иначе страж может оказаться пустым."""

    @pytest.mark.parametrize(
        "value", ["turn.completed", "turn.started", "a", "b", "tool.start", "agent.done"]
    )
    def test_fabricated_names_are_not_excused_by_the_rule(self, value: str) -> None:
        assert value not in EVENT_TYPES
        assert not is_probe_event_type(value)
        assert value.strip(), "имя не пустое, значит, это фикстура, а не проверка отказа"

    def test_the_three_allowed_shapes_are_still_allowed(self) -> None:
        """Правило не должно запрещать то, что законно.

        Проба и пустое имя — не «выдуманные имена», а предмет отдельных
        проверок; запретив их, страж стал бы требовать ослабления настоящих
        тестов.
        """
        assert is_probe_event_type("smoke.contract")
        assert "tool.started" in EVENT_TYPES
        assert not "".strip()

    def test_scanner_reports_a_planted_name(self, tmp_path: Path) -> None:
        """Скан действительно читает файл, а не возвращает пустоту."""
        planted = tmp_path / "planted_case.py"
        planted.write_text(
            "def check(service):\n"
            "    service.log_event('turn.completed')\n"
            "    return {'event_type': 'also.made.up'}\n",
            encoding="utf-8",
        )
        found = _event_names(planted)
        assert [value for _, value, _ in found] == ["turn.completed", "also.made.up"]
        assert _offenders(planted) == [
            "planted_case.py:2 'turn.completed'",
            "planted_case.py:3 'also.made.up'",
        ]

    def test_scanner_reads_the_agent_side_entry_point(self, tmp_path: Path) -> None:
        """Имя, заданное конструктором события агента, — тоже имя события.

        Этот вход прежний скан не видел вовсе, и на нём имя проверять нечем:
        агент пишет в PostgreSQL мимо ``log_events``, поэтому ``strict`` до
        агентских тестов не доходит.
        """
        planted = tmp_path / "planted_agent.py"
        planted.write_text(
            "def check(service):\n"
            "    service.log_event(LogEvent(event_type='tool.finished'))\n"
            "    service.log_event(LogEvent(event_type='turn_started'))\n",
            encoding="utf-8",
        )
        assert _offenders(planted) == [
            "planted_agent.py:2 'tool.finished'",
            "planted_agent.py:3 'turn_started'",
        ]

    def test_scanner_accepts_a_name_taken_from_the_dictionary(self, tmp_path: Path) -> None:
        planted = tmp_path / "planted_ok.py"
        planted.write_text(
            "from libs.enterprise_common.eventing.types import TOOL_STARTED\n"
            "def check(service):\n"
            "    service.log_event(TOOL_STARTED)\n",
            encoding="utf-8",
        )
        assert _offenders(planted) == []

    def test_scanner_rejects_a_locally_defined_fiction(self, tmp_path: Path) -> None:
        """Локальная константа не выдаёт себя за имя из словаря.

        Ровно этим приёмом выдуманное имя проскочило бы в «официальный» вид,
        поэтому оно проверяется отдельно. Разбор разворачивает константу в её
        значение — и «выдуманное имя, спрятанное в локальную константу» остаётся
        офендером, просто называется своим настоящим именем.
        """
        planted = tmp_path / "planted_alias.py"
        planted.write_text(
            "from libs.enterprise_common.eventing.types import TOOL_STARTED\n"
            "TURN_COMPLETED = 'turn.completed'\n"
            "def check(service):\n"
            "    service.log_event(TOOL_STARTED)\n"
            "    service.log_event(TURN_COMPLETED)\n",
            encoding="utf-8",
        )
        assert _offenders(planted) == ["planted_alias.py:5 'turn.completed'"]

    def test_scanner_follows_a_local_constant(self, tmp_path: Path) -> None:
        """Константа с каноническим значением — каноническое имя.

        Иначе страж требовал бы заменять ``EVENT_TYPE = "tool.started"`` на
        литерал в двадцати местах и мешал бы читать тест.
        """
        planted = tmp_path / "planted_constant.py"
        planted.write_text(
            "EVENT_TYPE = 'tool.started'\n"
            "SMOKE = 'smoke.e2e'\n"
            "def check(service):\n"
            "    service.log_event(EVENT_TYPE)\n"
            "    service.log_event(SMOKE)\n",
            encoding="utf-8",
        )
        assert _offenders(planted) == []

    def test_scanner_does_not_report_the_object_it_is_given(self, tmp_path: Path) -> None:
        """``log_event(event)`` передаёт событие, а не имя.

        Имя лежит в конструкторе и проверяется там; имя переменной ``event``
        выдуманным именем события не является.
        """
        planted = tmp_path / "planted_object.py"
        planted.write_text(
            "def check(service):\n"
            "    event = LogEvent(event_type='turn_started')\n"
            "    service.log_event(event)\n",
            encoding="utf-8",
        )
        assert _offenders(planted) == ["planted_object.py:2 'turn_started'"]

    def test_scanner_ignores_an_argument_that_is_not_a_journal_event(
        self, tmp_path: Path
    ) -> None:
        """Поле ``event_type`` в аргументе чужой операции — фильтр, не событие.

        Так объявлен параметр ``history_search`` и белый список полей аргументов
        ``ENTERPRISE_EXEC_LOG_ARG_FIELDS``: имя поля то же, а писателя за ним
        нет. Словарь отбрасывается по месту — подан прямо в вызов не-журнальной
        функции.
        """
        planted = tmp_path / "planted_argument.py"
        planted.write_text(
            "def check(layer, definition):\n"
            "    layer.pipeline.execute(definition, {'event_type': 'smoke'}, None)\n"
            "    return service.log_events([{'event_type': 'tool.finished'}])\n",
            encoding="utf-8",
        )
        assert _offenders(planted) == ["planted_argument.py:3 'tool.finished'"]

    def test_scanner_reads_a_file_with_a_bom(self, tmp_path: Path) -> None:
        """Файл с BOM читается, а не выпадает молча.

        В дереве такие файлы есть, и «не разобрался» не должно молча читаться
        как «выдуманных имён нет».
        """
        planted = tmp_path / "planted_bom.py"
        planted.write_bytes(
            "def check(service):\n"
            "    service.log_event('turn_started')\n".encode("utf-8-sig")
        )
        assert _offenders(planted) == ["planted_bom.py:2 'turn_started'"]

    def test_scanner_refuses_to_report_on_an_unreadable_file(self, tmp_path: Path) -> None:
        """Непрочитанный файл — падение стража, а не «нарушений нет»."""
        broken = tmp_path / "planted_broken.py"
        broken.write_text("def check(:\n", encoding="utf-8")
        with pytest.raises(AssertionError, match="не разбирается"):
            _event_names(broken)
