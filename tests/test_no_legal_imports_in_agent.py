"""Страж приёмки фазы 11: в репозитории агента нет ни одного импорта legal.

Проверяется **импорт**, а не упоминание. Слово ``legal_summarizer`` в
докстринге или в имени tool'а ``legal_summarizer_query`` — не зависимость
агента от домена; зависимостью был и остаётся любой ``import`` доменного
модуля из кода агента.

Домен живёт в ``mcp-platform/libs/legal_summarizer/``, агент ходит в него
операцией ``query_operation`` у процесса ``enterprise-mcp`` через tool
``workspace/tools/legal_summarizer_query.py``. Пока агент импортировал
``workspace.skills.legal_summarizer.scripts.*``, у него был второй
экземпляр домена, и обе копии разъезжались молча.

Сканируются ``lib/``, ``workspace/``, ``tools/``. Каталоги и файлы, любой
компонент пути которых начинается с подчёркивания, — tombstone'ы
(см. ``workspace/skills/_legal_summarizer/REMOVED.md``), они инертны и
перебор пропускает; второе утверждение ниже следит, чтобы tombstone'ами
были **все** остатки legal, а не выборочно.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Корни кода агента. ``mcp-platform/`` здесь нет: это другая кодовая база
#: со своим pytest и своей границей, там импорт ``libs.legal_summarizer`` —
#: норма, а не нарушение.
SCAN_ROOTS = ("lib", "workspace", "tools")

#: Маркеры домена. По одному ``legal`` ловить нельзя: слово встречается в
#: чужих строках, а ``legal_summarizer`` — точное имя навыка и пакета.
LEGAL_MARKERS = ("legal_summarizer", "legal-summarizer")

#: Единственный активный файл агента, названный по домену, — tool, который
#: ходит в capability. Это sanctioned-точка входа, а не часть домена: её
#: имя обязано называть домен, иначе агент не поймёт, куда обращаться.
SANCTIONED = frozenset({Path("workspace/tools/legal_summarizer_query.py")})


def _is_tombstone(path: Path) -> bool:
    """Tombstone — любой компонент пути с именем на подчёркивании.

    Исключение — ``__init__.py`` и ``__pycache__``: служебные имена, а не
    маркер захоронения.
    """
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
    return [f for f in files if "__pycache__" not in f.parts]


def _is_legal(name: str) -> bool:
    return any(marker in name for marker in LEGAL_MARKERS)


def _imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Все имена, которые модуль импортирует, включая динамические.

    Ловится четыре формы: ``import x``, ``from x import y``,
    ``importlib.import_module("x")`` и ``__import__("x")``. Динамические
    берутся литералами: собирать их через константы нельзя, такой импорт
    всё равно нечитаем для ревью и должен считаться нарушением.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            is_dynamic = (
                isinstance(func, ast.Name) and func.id == "__import__"
            ) or (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
            if is_dynamic and node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    found.append((value, node.lineno))
    return found


@pytest.mark.parametrize("path", _iter_agent_sources(), ids=lambda p: str(p))
def test_agent_source_does_not_import_legal(path: Path) -> None:
    """Ни один модуль агента не импортирует домен legal_summarizer."""
    if _is_tombstone(path):
        pytest.skip("tombstone: код вырезан и не подключается")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        pytest.fail(f"{path.relative_to(REPO_ROOT)} не разбирается: {exc}")

    offenders = [
        f"{name} (строка {lineno})"
        for name, lineno in _imported_names(tree)
        if _is_legal(name)
    ]
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} импортирует домен legal: "
        f"{'; '.join(offenders)}. Домен принадлежит платформе "
        f"(mcp-platform/libs/legal_summarizer/), агент ходит в него "
        f"операцией query_operation."
    )


def test_every_legal_remainder_in_agent_is_a_tombstone() -> None:
    """Ни один остаток legal в коде агента не активен.

    Сторожит дыру, которую создало бы исключение tombstone'ов из первого
    теста: если кто-то завёрнёт домен в не-tombstone-каталог, перебор его
    не увидит, и «импортов нет» окажется правдой только потому, что
    смотреть было не на что.
    """
    strays = [
        str(p.relative_to(REPO_ROOT))
        for p in _iter_agent_sources()
        if not _is_tombstone(p)
        and _is_legal(p.name)
        and p.relative_to(REPO_ROOT) not in SANCTIONED
    ]
    assert not strays, (
        f"активные файлы legal в коде агента: {strays}. Домен переехал на "
        f"платформу; остатки обязаны быть tombstone'ами (подчёркивание)."
    )
