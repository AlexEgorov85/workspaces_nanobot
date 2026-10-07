"""Страж границы: продакшн агента не импортирует зону навыков ``workspace/``.

``workspace/`` — кастомное окружение навыков: плагины-хуки, инструменты,
переопределения шаблонов, память, навыки. Это **не** библиотека агента, и
зависимость направлена в одну сторону: ``lib/`` (продакшн) не должен брать
из зоны навыков ни код, ни путь импорта.

Нарушение было не умозрительным. Пакет ``workspace/utils/`` содержал
``db.py`` — пул PostgreSQL на 1143 строки, который импортировали
``lib/core/application_context.py``, ``lib/services/session_storage.py`` и
``gateway.py``. Пока каталог ``workspace/`` стоял в ``pythonpath``, на одном
``sys.path`` оказывались два пакета с именем ``utils``, и в позиции 0 зона
навыков перехватывала одноимённые импорты — в том числе ``import tools``.
6 модулей перенесены в ``lib/utils/`` 2026-10-07; этот страж не даёт
нарушению вернуться.

Проверяются три утверждения:

1. **Импорты.** Ни один модуль ``lib/``, ``tools/`` и ни одна точка входа
   (``gateway.py``, ``cli_agent.py``) не импортирует ``workspace.*`` и не
   импортирует top-level ``utils`` — тот самый пакет, который переехал.
2. **``sys.path``.** Ни один модуль агента не добавляет каталог ``workspace``
   в ``sys.path``: это второй путь к той же зависимости, невидимый для
   первого утверждения.
3. **Зона пуста.** В ``workspace/utils/`` не осталось кода — только
   ``__init__.py``. Пока там лежит модуль, правило держится только потому,
   что его никто не импортирует; как только появится потребитель, запрет
   снова станет кусающимся.

Что правило **не** запрещает, чтобы оно не съело нужное:

- Чтение файлов зоны **по пути** (``workspace/overrides`` в
  ``lib/services/consolidator_locale.py``, ``workspace/cron/jobs.json``,
  ``workspace/hooks/*.py`` в ``lib/cli/hook_loader.py``) — это данные и
  плагины, а не код агента; импорт там не нужен и не делается.
- Импорты **внутри** самой зоны (``workspace/``) — это её собственный код.
- ``tests/``: тестам ``workspace/`` в ``sys.path`` нужен для сценариев
  загрузки плагинов, поэтому тесты не обходятся стражем.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Корни продакшна агента. ``workspace/`` здесь нет намеренно: внутри зоны
#: импорт ``workspace.*`` — норма, это её собственный код.
SCAN_ROOTS = ("lib", "tools")

#: Точки входа. Отдельным списком, а не каталогом: это файлы в корне.
ENTRYPOINTS = ("gateway.py", "cli_agent.py")

#: Корни импортов, которые означают зависимость от зоны навыков. ``utils``
#: — top-level пакет, каким был ``workspace/utils/`` при ``workspace/`` в
#: ``sys.path``; отдельно он ничего не значит и в коде агента не встречается.
FORBIDDEN_ROOTS = ("workspace", "utils")

#: Пометка каталога зоны в исходнике.
_ZONE_MARK = "workspace"

#: Каталоги, исключённые из обхода: не код, а рантайм-хранилище (в
#: ``workspace/data_store/`` лежат бэкапы сессий — полные копии репозитория).
EXCLUDED_DIRS = ("data_store",)


def _is_tombstone(path: Path) -> bool:
    """Tombstone — компонент пути на подчёркивании (код вырезан, не подключается)."""
    return any(
        part.startswith("_") and part not in {"__init__.py", "__pycache__"}
        for part in path.relative_to(REPO_ROOT).parts
    )


def _iter_agent_sources() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    for name in ENTRYPOINTS:
        candidate = REPO_ROOT / name
        if candidate.is_file():
            files.append(candidate)
    return [
        f
        for f in files
        if "__pycache__" not in f.parts
        and not any(part in EXCLUDED_DIRS for part in f.relative_to(REPO_ROOT).parts)
    ]


def _imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Все имена, которые модуль импортирует, включая динамические.

    Ловятся четыре формы: ``import x``, ``from x import y``,
    ``importlib.import_module("x")`` и ``__import__("x")``. Динамические берутся
    литералами: импорт через константу всё равно нечитаем для ревью и должен
    считаться нарушением.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            is_dynamic = (isinstance(func, ast.Name) and func.id == "__import__") or (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
            if is_dynamic and node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    found.append((value, node.lineno))
    return found


def _zone_imports(source: str, *, rel: Path) -> list[str]:
    """Запрещённые импорты зоны навыков в исходнике (со строками)."""
    try:
        tree = ast.parse(source, filename=str(rel))
    except (SyntaxError, UnicodeDecodeError) as exc:
        pytest.fail(f"{rel} не разбирается: {exc}")
    return [
        f"{name} (строка {lineno})"
        for name, lineno in _imported_names(tree)
        if name.split(".")[0] in FORBIDDEN_ROOTS
    ]


@pytest.mark.parametrize("path", _iter_agent_sources(), ids=lambda p: str(p))
def test_agent_source_does_not_import_skills_zone(path: Path) -> None:
    """Ни один модуль продакшна агента не импортирует зону навыков."""
    if _is_tombstone(path):
        pytest.skip("tombstone: код вырезан и не подключается")
    rel = path.relative_to(REPO_ROOT)
    offenders = _zone_imports(path.read_text(encoding="utf-8"), rel=rel)
    assert not offenders, (
        f"{rel} импортирует зону навыков: {'; '.join(offenders)}. "
        f"workspace/ — кастомное окружение навыков, а не библиотека агента: "
        f"всё внутреннее живёт в lib/ и импортируется как lib.*. Общие "
        f"утилиты — lib/utils/* (перенесены из workspace/utils/ 2026-10-07)."
    )


def _sys_path_zone_inserts(source: str, *, rel: Path) -> list[tuple[int, str]]:
    """Строки, где в ``sys.path`` кладётся каталог зоны навыков.

    Проверяется не «есть ли в файле слово workspace», а **что именно**
    добавляется: ``consolidator_locale.py`` упоминает ``workspace/overrides``
    в докстринге и читает его по пути — это данные, а не импорт, и ловить
    такое словом нельзя.

    Путь к зоне собирается в переменную до вызова (``_ws = ... / "workspace"``),
    поэтому вставленное выражение разворачивается на все имена, которые в
    модуле вообще вычисляются из строки с ``workspace``.

    Ловятся три формы: ``sys.path.insert/append/extend(...)``, присваивание
    ``sys.path = ...``/``sys.path[:] = ...`` и ``sys.path += ...``. Вторая и
    третья — не экзотика: замена списка целиком выглядит безобидно и точно так
    же возвращает каталог в резолв имён.
    """
    try:
        tree = ast.parse(source, filename=str(rel))
    except (SyntaxError, UnicodeDecodeError) as exc:
        pytest.fail(f"{rel} не разбирается: {exc}")

    zone_vars: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is None:
                continue
            segment = ast.get_source_segment(source, value) or ""
            if _ZONE_MARK in segment:
                zone_vars.update(
                    t.id for t in targets if isinstance(t, ast.Name)
                )

    def _touches_zone(node: ast.AST) -> bool:
        segment = ast.get_source_segment(source, node) or ""
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        return _ZONE_MARK in segment or bool(names & zone_vars)

    def _is_sys_path(obj: ast.AST) -> bool:
        return (
            isinstance(obj, ast.Attribute)
            and obj.attr == "path"
            and isinstance(obj.value, ast.Name)
            and obj.value.id == "sys"
        )

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"insert", "append", "extend"}:
            continue
        if not _is_sys_path(node.func.value):
            continue
        for arg in node.args:
            if _touches_zone(arg):
                hits.append((node.lineno, ast.get_source_segment(source, arg) or ""))

    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            payload = node.value
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
            payload = node.value
        else:
            continue
        for target in targets:
            base = target.value if isinstance(target, ast.Subscript) else target
            if _is_sys_path(base) and _touches_zone(payload):
                hits.append((node.lineno, ast.get_source_segment(source, payload) or ""))

    return sorted(hits)


@pytest.mark.parametrize("path", _iter_agent_sources(), ids=lambda p: str(p))
def test_agent_source_does_not_add_skills_zone_to_sys_path(path: Path) -> None:
    """Ни один модуль агента не прописывает ``workspace/`` в ``sys.path``.

    Это второй путь к той же зависимости: сам импорт не виден в первом
    утверждении, а каталог в резолве имён — виден.
    """
    if _is_tombstone(path):
        pytest.skip("tombstone: код вырезан и не подключается")
    rel = path.relative_to(REPO_ROOT)
    hits = _sys_path_zone_inserts(path.read_text(encoding="utf-8"), rel=rel)
    assert not hits, (
        f"{rel} добавляет каталог workspace/ в sys.path (строка {hits[0][0]}: "
        f"{hits[0][1]}). Зона навыков не участвует в резолве имён агента: "
        f"в ней лежат плагины и данные, а код агента — в lib/. Каталог в "
        f"позиции 0 к тому же перехватывает одноимённые пакеты."
    )


def test_skills_utils_directory_holds_no_code() -> None:
    """``workspace/utils/`` обязан остаться пустым кодом.

    Пока модуль там лежит, запрет держится только тем, что его никто не
    импортирует. Появление файла — это новая зависимость, которую придётся
    переносить в ``lib/`` заново.
    """
    zone = REPO_ROOT / "workspace" / "utils"
    if not zone.is_dir():
        return
    offenders = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sorted(zone.rglob("*.py"))
        if "__pycache__" not in p.parts and p.name != "__init__.py"
    ]
    assert not offenders, (
        f"В зоне навыков снова появился код: {', '.join(offenders)}. "
        f"Продакшн агента не берёт код из workspace/: переносите модуль "
        f"в lib/ (вместе со всеми импортами) — отдельным коммитом."
    )


# --------------------------------------------------------------------------
# Проверка самих проверок. Страж, который не ловит подсаженный дефект,
# доказывает только то, что регулярное выражение написано.
# --------------------------------------------------------------------------


def test_import_scanner_detects_a_planted_zone_import() -> None:
    """Сканер импортов ловит подсаженное нарушение на живом модуле агента."""
    from lib.services import runtime_health

    rel = Path("lib/services/runtime_health.py")
    source = Path(runtime_health.__file__).read_text(encoding="utf-8")
    assert _zone_imports(source, rel=rel) == [], "чистый модуль не должен давать находок"

    planted = "from utils.db import fetch\n"
    assert _zone_imports(planted, rel=rel) == [
        "utils.db (строка 1)"
    ], "подсаженный импорт workspace-пакета обязан быть найден"

    planted_ws = "import workspace.hooks.session_file_redirect_hook\n"
    assert _zone_imports(planted_ws, rel=rel) == [
        "workspace.hooks.session_file_redirect_hook (строка 1)"
    ], "подсаженный импорт workspace.* обязан быть найден"

    dynamic = 'import importlib\nimportlib.import_module("utils.media")\n'
    assert _zone_imports(dynamic, rel=rel) == [
        "utils.media (строка 2)"
    ], "динамический импорт обязан быть найден"

    # Родной код агента — законная находка: он не должен ловиться.
    clean = "from lib.utils.db import fetch\nfrom lib.utils.media import encode\n"
    assert _zone_imports(clean, rel=rel) == [], (
        "импорт lib.utils.* — законный код агента, а не зависимость от зоны"
    )


def test_sys_path_scanner_detects_a_planted_zone_insert() -> None:
    """Сканер ``sys.path`` ловит подсаженную вставку и не ловит чтение по пути."""
    rel = Path("lib/core/application_context.py")

    planted = (
        "import sys\n"
        "from pathlib import Path\n"
        '_ws = Path(__file__).resolve().parents[2] / "workspace"\n'
        "if str(_ws) not in sys.path:\n"
        "    sys.path.insert(0, str(_ws))\n"
    )
    hits = _sys_path_zone_inserts(planted, rel=rel)
    assert len(hits) == 1, f"подсаженная вставка обязана быть найдена один раз: {hits}"

    literal = 'import sys\nsys.path.append("workspace")\n'
    assert len(_sys_path_zone_inserts(literal, rel=rel)) == 1, (
        "вставка строковым литералом обязана быть найдена"
    )

    rebind = 'import sys\nsys.path[:] = ["workspace", *sys.path]\n'
    assert len(_sys_path_zone_inserts(rebind, rel=rel)) == 1, (
        "замена sys.path целиком обязана быть найдена: это тот же резолв"
    )

    # Чтение файла зоны по пути — разрешено, ловить его нельзя.
    by_path = (
        "from pathlib import Path\n"
        "def overrides(root):\n"
        '    return root / "workspace" / "overrides"\n'
    )
    assert _sys_path_zone_inserts(by_path, rel=rel) == [], (
        "чтение каталога по пути — не вставка в sys.path"
    )