"""Запрет SQL-конструкций, которых нет в Greenplum 6.5.

Целевая БД — Greenplum 6.5 (ядро PostgreSQL 9.4). В ней отсутствуют:

* ``make_interval()`` — функция появилась в PostgreSQL 9.4, но в Greenplum
  не реализована; из-за named-аргументов (``secs => %s``) парсер отдаёт
  ``UndefinedColumn: column "secs" does not exist``;
* ``INSERT ... ON CONFLICT`` — появился в PostgreSQL 9.5 / Greenplum 7.

Unit-тесты такую ошибку не ловят: SQL уходит в мокнутый курсор, и падение
впервые всплывает на старте gateway. Тест обходит AST и ищет обе
конструкции в строковых литералах — не в docstring'ах и не в комментариях,
где их упоминание корректно (там зафиксировано само ограничение).

Каталог ``tests/`` намеренно не сканируется: например
``tests/test_db_logging_service.py`` проверяет ``"ON CONFLICT" not in sql``,
то есть упоминает запрещённую строку в проверочном литерале.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_SCAN_DIRS = ("lib", "workspace", "tools", "benchmarks")
_SCAN_ROOT_FILES = ("config.py", "gateway.py", "cli_agent.py", "streamlit_app.py")

_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("make_interval", re.compile(r"\bmake_interval\s*\(", re.IGNORECASE)),
    ("ON CONFLICT", re.compile(r"\bON\s+CONFLICT\b", re.IGNORECASE)),
)


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for name in _SCAN_DIRS:
        files.extend(sorted((_ROOT / name).rglob("*.py")))
    files.extend(_ROOT / name for name in _SCAN_ROOT_FILES if (_ROOT / name).is_file())
    return files


def _source(path: Path) -> str:
    # utf-8-sig: часть исходников несёт BOM, а ast.parse на строке с
    # U+FEFF падает с SyntaxError.
    return path.read_bytes().decode("utf-8-sig")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() строковых констант, являющихся docstring'ами."""
    ids: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _violations(path: Path) -> list[str]:
    tree = ast.parse(_source(path))
    docstrings = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        for label, pattern in _FORBIDDEN:
            if pattern.search(node.value):
                rel = path.relative_to(_ROOT).as_posix()
                snippet = node.value.strip().replace("\n", " ")[:80]
                found.append(f"{rel}:{node.lineno}: {label} → {snippet}")
    return found


class TestGreenplumCompatibleSql:
    """Runtime-SQL обязан быть исполнимым на Greenplum 6.5."""

    def test_no_incompatible_sql_in_runtime_code(self) -> None:
        violations: list[str] = []
        for path in _iter_python_files():
            violations.extend(_violations(path))
        assert not violations, (
            "Greenplum 6.5 не поддерживает эти конструкции; "
            "используйте переносимые аналоги:\n" + "\n".join(sorted(violations))
        )

    def test_scan_covers_expected_modules(self) -> None:
        names = {p.relative_to(_ROOT).as_posix() for p in _iter_python_files()}
        assert "lib/services/cache_ownership.py" in names
        assert "tools/migrate.py" in names
        assert "workspace/tools/duckdb_query_tool.py" in names

    def test_cache_ownership_uses_portable_interval(self) -> None:
        """Конкретная регрессия: expires_at через make_interval ронял старт."""
        text = _source(_ROOT / "lib" / "services" / "cache_ownership.py")
        assert "make_interval" not in text
        assert text.count("(%s || ' seconds')::interval") == 3

    def test_migrate_stamp_has_no_on_conflict(self) -> None:
        # AST-вариант, а не поиск по тексту: в docstring'е stamp_migration
        # упоминание ON CONFLICT оставлено намеренно — там зафиксировано,
        # почему используется existence-check.
        assert _violations(_ROOT / "tools" / "migrate.py") == []
