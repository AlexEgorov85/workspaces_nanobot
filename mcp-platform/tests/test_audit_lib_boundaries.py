"""Границы библиотеки аудита: в ``libs/audit`` нет чужих зависимостей.

Миграция ``enterprise-mcp-platform``, фаза 4. Основной страж
(``test_architecture_boundaries.py``) проверяет импорты агента и владение
драйверами; этот файл добавляет проверку, специфичную для библиотеки
аудита, и делает её явной: список запрещённого здесь виден целиком, а не
размазан по правилам стража.

Проверяется, что в ``libs/audit``:

* нет ``httpx``/``requests``/``duckdb``/``psycopg2`` (владельцы — ``libs/llm``
  и ``libs/enterprise_data``);
* нет ``urllib``/``socket``/``http.client`` — HTTP-клиент вообще не
  поднимается библиотекой;
* нет ``importlib.import_module``/``__import__`` (обход запрета импорта);
* нет импортов агента;
* **ни одна функция не принимает параметр с именем из
  ``SQL_PARAM_NAMES``** — набор взят из основного стража. Это проверка на
  будущее: когда capability ``audit`` (фаза 4, шаг 2) станет обёрткой над
  этими функциями, подпись операции унаследует эти имена, а такая подпись
  означала бы, что операция принимает SQL от вызывающей стороны.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

__all__ = [
    "SQL_PARAM_NAMES",
    "audit_module_files",
    "iter_imports",
    "iter_function_params",
]

AUDIT_DIR = Path(__file__).resolve().parent.parent / "libs" / "audit"

#: Те же имена, что и в ``test_architecture_boundaries.SQL_PARAM_NAMES``.
#: Дублируются, а не импортируются: страж — эталон, который этот тест
#: проверяет по смыслу, а не переиспользует.
SQL_PARAM_NAMES = frozenset({"sql", "query_sql", "statement", "raw_sql", "sql_text", "ddl"})

#: Драйверы и HTTP-клиенты, принадлежащие другим слоям платформы.
FORBIDDEN_MODULES = frozenset(
    {
        "httpx",
        "requests",
        "duckdb",
        "psycopg2",
        "urllib",
        "socket",
        "http",
        "aiohttp",
        "openai",
    }
)

#: Корни агента, которых в платформе быть не может.
FORBIDDEN_ROOTS = frozenset(
    {
        "nanobot",
        "lib",
        "workspace",
        "tools",
        "config",
        "benchmarks",
        "cli_agent",
        "gateway",
        "streamlit_app",
    }
)


def audit_module_files() -> list[Path]:
    """Все модули ``libs/audit`` — непустой список (иначе тест проходит вхолостую)."""
    return sorted(p for p in AUDIT_DIR.rglob("*.py"))


def iter_imports(source: str) -> list[str]:
    """Имена модулей, импортируемых модулем (обычные, относительные, ``from``)."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # ``from . import x`` / ``from .mod import y``
                base = f".{base}" if base else "."
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names if base)
    return names


def iter_function_params(tree: ast.AST) -> list[tuple[str, str]]:
    """``(имя_функции, имя_параметра)`` по всем функциям модуля, включая методы."""
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            found.append((node.name, arg.arg))
        if args.vararg is not None:
            found.append((node.name, args.vararg.arg))
        if args.kwarg is not None:
            found.append((node.name, args.kwarg.arg))
    return found


class TestAuditPackageShape:
    def test_package_is_not_empty(self) -> None:
        """Страж, проверяющий пустой пакет, ничего не проверяет."""
        files = audit_module_files()
        assert files, "libs/audit не найден — тест прошёл бы вхолостую"
        assert len(files) >= 8, files

    def test_every_module_is_importable(self) -> None:
        import importlib

        for path in audit_module_files():
            rel = path.relative_to(AUDIT_DIR.parent.parent)
            dotted = ".".join(rel.with_suffix("").parts)
            importlib.import_module(dotted)


class TestNoForeignDependencies:
    @pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_MODULES))
    def test_no_foreign_module_imported(self, forbidden: str) -> None:
        for path in audit_module_files():
            for name in iter_imports(path.read_text(encoding="utf-8")):
                root = name.lstrip(".").split(".")[0]
                assert root != forbidden, f"{path.name} импортирует {name}"

    @pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_ROOTS))
    def test_no_agent_imports(self, forbidden: str) -> None:
        for path in audit_module_files():
            for name in iter_imports(path.read_text(encoding="utf-8")):
                root = name.lstrip(".").split(".")[0]
                assert root != forbidden, f"{path.name} импортирует {name}"

    def test_no_textual_driver_mentions_in_imports(self) -> None:
        """Драйвер может встретиться в тексте комментария — это не запрещено.

        Проверяется именно импорт, поэтому утверждение про текст было бы
        ложным (и заставило бы переписывать комментарии, объясняющие, почему
        этих импортов тут нет).
        """
        for path in audit_module_files():
            for name in iter_imports(path.read_text(encoding="utf-8")):
                assert "duckdb" not in name, f"{path.name}: {name}"
                assert "psycopg2" not in name, f"{path.name}: {name}"

    def test_no_dynamic_import_escape_hatch(self) -> None:
        for path in audit_module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "__import__":
                        raise AssertionError(f"{path.name}: вызов __import__")
                    if isinstance(func, ast.Attribute) and func.attr == "import_module":
                        raise AssertionError(
                            f"{path.name}: вызов importlib.import_module"
                        )


class TestNoSqlParametersInSignatures:
    def test_no_sql_param_names(self) -> None:
        """Ни одна функция библиотеки не принимает SQL от вызывающей стороны."""
        offenders: list[str] = []
        for path in audit_module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for func_name, param in iter_function_params(tree):
                if param in SQL_PARAM_NAMES:
                    offenders.append(f"{path.name}: {func_name}({param}=...)")
        assert not offenders, "параметры с именами из SQL_PARAM_NAMES: " + "; ".join(
            offenders
        )

    def test_sql_param_names_are_the_guard_ones(self) -> None:
        """Набор не должен молча разойтись со стражем."""
        guard = Path(__file__).resolve().parent / "test_architecture_boundaries.py"
        tree = ast.parse(guard.read_text(encoding="utf-8"))
        declared: set[str] | None = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "SQL_PARAM_NAMES"
                for target in node.targets
            ):
                # ``frozenset({...})`` — сам литерал лежит в аргументе.
                value = node.value
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "frozenset"
                ):
                    value = value.args[0]
                declared = set(ast.literal_eval(value))
                break
        assert declared is not None, "страж больше не объявляет SQL_PARAM_NAMES"
        assert declared == set(SQL_PARAM_NAMES), (
            f"расхождение со стражем: {sorted(declared ^ set(SQL_PARAM_NAMES))}"
        )
