"""Страж границы: код агента не импортирует платформенные пакеты напрямую.

Платформа (``mcp-platform/``) — отдельная кодовая база со своим pytest, своим
``pyproject.toml`` и своей раскладкой. Агент ходит в неё **операциями** процесса
``enterprise-mcp``, а не импортами: единственный клиент к платформе — процесс
сервера, а всё, что домен делает доступным агенту, обязано быть операцией.

Этот страж закрывает дыру, которую не закрывает
``tests/test_no_legal_imports_in_agent.py``: тот ищет маркеры
``legal_summarizer``/``legal-summarizer``, то есть конкретный домен. Любой
**другой** импорт платформенного пакета из кода агента — например
``from libs.enterprise_data import ...`` — прошёл бы мимо него незамеченным.
Направление зависимостей должно быть правильным для **всей** платформы, а не
для одного её домена.

Проверяются два утверждения:

1. **Импорты.** Ни один модуль ``lib/``, ``workspace/``, ``tools/`` не импортирует
   пакет платформы (``libs.*``, ``servers.*``, ``mcp_platform*``) — ни прямой
   формой, ни через ``importlib.import_module``/``__import__``.
2. **``sys.path``.** Ни один модуль агента не добавляет каталог ``mcp-platform``
   в ``sys.path``: это второй путь к той же зависимости, только невидимый для
   первого утверждения.

Единственное исключение — ``workspace/tools/document_read.py``, которому
разрешено импортировать ``libs.office``: он читает локальный пользовательский
файл по пути, известному только агенту (сам файл лежит на диске выбранного
каталога), и отдельная операция ради «прочитать этот файл» была бы вторым
путём к тому же разбору. Решение осознанное и зафиксировано в докстринге модуля;
страж его не ломает, а лишь держит единственным — чтобы исключение не
разъехалось на другие модули.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Корни кода агента. ``mcp-platform/`` здесь намеренно нет: это другая
#: кодовая база, там ``import libs.*`` — норма, а не нарушение.
SCAN_ROOTS = ("lib", "workspace", "tools")

#: Корни пакетов платформы. Импорт любого из них из кода агента означает, что
#: зависимость направлена не туда.
PLATFORM_ROOTS = ("libs", "servers", "mcp_platform", "mcp-platform")

#: Разрешённая точка входа: ``libs.office`` (парсер офисных форматов) и только
#: он. Ключ — путь относительно корня репозитория, значение — единственный
#: разрешённый модуль. Всё остальное платформенное в этом файле по-прежнему
#: запрещено.
SANCTIONED: dict[Path, frozenset[str]] = {
    Path("workspace/tools/document_read.py"): frozenset({"libs.office"}),
}

#: Каталоги, исключённые из обхода. ``workspace/data_store/`` — не код, а
#: рантайм-хранилище: туда сессии складывают свои бэкапы, а бэкап — это ПОЛНАЯ
#: копия репозитория. Обходя её, страж проверял бы копии и падал бы на
#: нарушениях, которые надо чинить в оригинале. Каталог единственный и
#: игнорируется git (``.gitignore``: ``data_store/``), так что исходников там
#: быть не может по определению.
EXCLUDED_DIRS = ("data_store",)


#: Пометка каталога платформы в исходнике. Платформа в коде агента может быть
#: названа в строке (путь к каталогу) — именно этим собирается целевой путь.
_PLATFORM_MARK = "mcp-platform"


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
        if not base.is_dir():
            continue
        files.extend(sorted(base.rglob("*.py")))
    return [
        f
        for f in files
        if "__pycache__" not in f.parts
        and not any(part in EXCLUDED_DIRS for part in f.relative_to(REPO_ROOT).parts)
    ]


def _is_platform(name: str) -> bool:
    """Модуль принадлежит платформе?"""
    return name.split(".")[0] in PLATFORM_ROOTS


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


def _platform_imports(source: str, *, rel: Path) -> list[str]:
    """Запрещённые импорты платформенных пакетов в исходнике (со строками)."""
    try:
        tree = ast.parse(source, filename=str(rel))
    except (SyntaxError, UnicodeDecodeError) as exc:
        pytest.fail(f"{rel} не разбирается: {exc}")
    allowed = SANCTIONED.get(rel, frozenset())
    return [
        f"{name} (строка {lineno})"
        for name, lineno in _imported_names(tree)
        if _is_platform(name) and name not in allowed
    ]


@pytest.mark.parametrize("path", _iter_agent_sources(), ids=lambda p: str(p))
def test_agent_source_does_not_import_platform_packages(path: Path) -> None:
    """Ни один модуль агента не импортирует пакет платформы напрямую."""
    if _is_tombstone(path):
        pytest.skip("tombstone: код вырезан и не подключается")
    rel = path.relative_to(REPO_ROOT)
    offenders = _platform_imports(path.read_text(encoding="utf-8"), rel=rel)
    assert not offenders, (
        f"{rel} импортирует платформенный пакет: {'; '.join(offenders)}. "
        f"Платформа (mcp-platform/) — отдельная кодовая база; агент ходит в "
        f"неё операциями процесса enterprise-mcp, а не импортом."
    )


def _sys_path_platform_inserts(source: str, *, rel: Path) -> list[tuple[int, str]]:
    """Строки, где в ``sys.path`` кладётся именно каталог платформы.

    Проверяется не «есть ли в файле слово mcp-platform», а **что именно**
    добавляется: иначе ложное срабатывание. ``application_context.py``, например,
    добавляет в ``sys.path`` каталог ``workspace`` (для ``utils.db``), а
    ``mcp-platform`` упоминает в прозе — проверка «мутация + упоминание» ловила бы
    его несправедливо.

    Путь к платформе собирается в переменную до вызова
    (``_PLATFORM_ROOT = ... / "mcp-platform"``), поэтому вставленное выражение
    разворачивается на все имена, которые в модуле вообще вычисляются из строки с
    ``mcp-platform``.
    """
    try:
        tree = ast.parse(source, filename=str(rel))
    except (SyntaxError, UnicodeDecodeError) as exc:
        pytest.fail(f"{rel} не разбирается: {exc}")

    platform_vars: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is None:
                continue
            segment = ast.get_source_segment(source, value) or ""
            if _PLATFORM_MARK in segment:
                platform_vars.update(
                    t.id for t in targets if isinstance(t, ast.Name)
                )

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not (
            node.func.attr in {"insert", "append", "extend"}
            and isinstance(owner, ast.Attribute)
            and owner.attr == "path"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "sys"
        ):
            continue
        for arg in node.args:
            segment = ast.get_source_segment(source, arg) or ""
            names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
            if _PLATFORM_MARK in segment or (names & platform_vars):
                hits.append((node.lineno, segment))
    return hits


@pytest.mark.parametrize("path", _iter_agent_sources(), ids=lambda p: str(p))
def test_agent_source_does_not_add_platform_to_sys_path(path: Path) -> None:
    """Ни один модуль агента не прописывает ``mcp-platform`` в ``sys.path``.

    Это второй путь к той же зависимости: импорт не виден в первом утверждении,
    а код при этом исполняется в процессе агента. Разрешён ровно один файл —
    тот же, что и в первом утверждении.
    """
    if _is_tombstone(path):
        pytest.skip("tombstone: код вырезан и не подключается")
    rel = path.relative_to(REPO_ROOT)
    if rel in SANCTIONED:
        return
    source = path.read_text(encoding="utf-8")
    hits = _sys_path_platform_inserts(source, rel=rel)
    assert not hits, (
        f"{rel} добавляет mcp-platform в sys.path (строка {hits[0][0]}: "
        f"sys.path.insert(0, {hits[0][1]})). Платформа подключается процессом "
        f"enterprise-mcp, а не правкой пути импорта в агенте."
    )


def test_sys_path_scanner_detects_a_planted_violation() -> None:
    """Проверка самой проверки: на заведомо плохом файле она обязана срабатывать.

    Подставлен именно реальный паттерн ``document_read.py`` (путь собирается в
    переменную до вызова). Иначе её зелёный результат ничего не значит — ровно
    тот случай, из-за которого зелёный страж хуже отсутствующего.
    """
    planted = (
        "import sys\n"
        "from pathlib import Path\n"
        "_PLATFORM_ROOT = Path(__file__).resolve().parents[2] / 'mcp-platform'\n"
        "if str(_PLATFORM_ROOT) not in sys.path:\n"
        "    sys.path.insert(0, str(_PLATFORM_ROOT))\n"
    )
    hits = _sys_path_platform_inserts(planted, rel=Path("planted.py"))
    assert hits, "сканер обязан ловить подставное добавление mcp-platform в sys.path"


def test_sys_path_scanner_ignores_unrelated_sys_path_edits() -> None:
    """Обратная сторона: правка ``sys.path`` не про платформу — не нарушение.

    ``application_context.py`` добавляет в ``sys.path`` каталог ``workspace``,
    чтобы дотянуться до ``utils.db``, и упоминает платформу в докстринге.
    Страж, который цепляется за любое упоминание, свалил бы такой файл — и его
    пришлось бы «чинить», ломая рабочий код.
    """
    source = (
        "import sys\n"
        "from pathlib import Path\n"
        "#: см. также mcp-platform — отдельная кодовая база.\n"
        "_ws = Path(__file__).resolve().parents[2] / 'workspace'\n"
        "if str(_ws) not in sys.path:\n"
        "    sys.path.insert(0, str(_ws))\n"
    )
    assert not _sys_path_platform_inserts(source, rel=Path("application_context.py"))


def test_import_scanner_detects_a_planted_platform_import() -> None:
    """Сканер импортов ловит тот самый импорт, ради которого страж и написан.

    Существующий ``test_no_legal_imports_in_agent.py`` ищет маркеры
    ``legal_summarizer``, поэтому ``from libs.enterprise_data import ...`` прошёл
    бы мимо него. Этот тест фиксирует, что новый страж такое видит.
    """
    assert _platform_imports(
        "from libs.enterprise_data import load_snapshot\n", rel=Path("lib/x.py")
    )
    assert _platform_imports(
        "import importlib\nimportlib.import_module('servers.enterprise')\n",
        rel=Path("lib/x.py"),
    )
    assert not _platform_imports("from lib.core import ctx\n", rel=Path("lib/x.py"))


def test_sanctioned_exception_stays_narrow() -> None:
    """Исключение держится ровно на ``libs.office`` и не расползается.

    Разрешённый модуль проходит, а любой другой платформенный импорт в том же
    файле — по-прежнему запрещён: иначе «одна санкция» тихо превратилась бы в
    «весь файл может импортировать платформу».
    """
    allowed = Path("workspace/tools/document_read.py")
    assert not _platform_imports(
        "from libs.office import extract_text\n", rel=allowed
    ), "санкционированный libs.office обязан проходить"
    assert _platform_imports(
        "from libs.office import extract_text\nfrom libs.enterprise_data import x\n",
        rel=allowed,
    ), "санкция не должна покрывать другие модули платформы в этом же файле"
