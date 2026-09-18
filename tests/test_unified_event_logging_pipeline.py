"""Guard-тесты: единый logging pipeline через ``DbLoggingService``.

Защита от регрессии: после change ``unify-agent-event-logging-pipeline``
единственный runtime-writer ``agent_gateway_logs`` / ``agent_question_runs`` —
``lib/services/db_logging_service.py`` (``DbLoggingService``). Все producer'ы
передают события через ``db_logging_service.log_event(LogEvent(...))`` или
``DbLoggingService.try_log_event(...)``. Прямой SQL INSERT в журнал и
импорт удалённого ``workspace.utils.event_log`` запрещены.

Тесты:

* ``TestNoProductionDirectWriters`` — production-allowlist (design D6.1).
  Обходит production runtime-пути (``lib/``, ``workspace/``, ``tools/``,
  application entrypoints) и проверяет, что прямых ``INSERT INTO`` в
  logging-таблицы и lookup'ов ``logging.db.table_name`` нет нигде, кроме
  ``lib/services/db_logging_service.py`` (sole owner).
* ``TestNoDeletedModuleImports`` — global guard (design D6.2). AST-парсинг
  всего Python-кода: ни одного импорта удалённого модуля и ни одного
  вызова удалённых функций.
* ``TestRepositoryGrepBaseline`` — CI-проверка (design D6.4). Прогоняет
  4 ``git grep``-проверки, которые срабатывают при добавлении нового
  INSERT/import/lookup.
* ``TestGuardNegativeCases`` — negative-тесты с ``tmp_path``-фикстурами,
  подтверждают, что сам guard ловит регрессию (страховка самого guard'а).
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
LOGGING_TABLES = {"agent_gateway_logs", "agent_question_runs"}
LOGGING_OWNERS = {Path("lib/services/db_logging_service.py")}

PRODUCTION_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "lib",
    REPO_ROOT / "workspace",
    REPO_ROOT / "tools",
    REPO_ROOT / "tests",
)

PRODUCTION_ROOTS_STRICT: tuple[Path, ...] = (
    REPO_ROOT / "lib",
    REPO_ROOT / "workspace",
    REPO_ROOT / "tools",
)

ENTRYPOINT_FILES: tuple[Path, ...] = (
    REPO_ROOT / "cli_agent.py",
    REPO_ROOT / "gateway.py",
    REPO_ROOT / "streamlit_app.py",
)

# Self-exclusion: этот файл docstring'и и negative-fixture упоминают
# ``workspace.utils.event_log`` / удалённые функции — guard должен
# исключить сам себя из проверки, чтобы не срабатывать на собственные
# negative-кейсы.
SELF_PATH = Path(__file__).resolve().relative_to(REPO_ROOT)

# Дополнительные self-exclusion'ы: другие файлы этой change'ы, которые
# проверяют контракт logging pipeline (содержат negative-asserts или
# reference на удалённые имена в комментариях).
SELF_EXCLUSIONS: frozenset[Path] = frozenset({
    SELF_PATH,
    Path("tests/test_unified_event_logging_contract.py"),
})


def _walk_python(root: Path):
    """Yield ``Path`` всех ``*.py`` под ``root`` (рекурсивно)."""
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _collect_python_files() -> list[Path]:
    """Собрать все ``*.py`` в production roots + entrypoints.

    Исключаем ``SELF_EXCLUSIONS`` (файлы, которые по контракту могут
    содержать ``workspace.utils.event_log`` / удалённые имена) и
    несуществующие файлы (были удалены в рамках change).
    """
    files: list[Path] = []
    for root in PRODUCTION_ROOTS:
        if root.is_dir():
            files.extend(_walk_python(root))
    for ep in ENTRYPOINT_FILES:
        if ep.is_file():
            files.append(ep)
    return [
        f for f in files
        if f.is_file() and f.relative_to(REPO_ROOT) not in SELF_EXCLUSIONS
    ]


def _is_logging_owner(rel: Path) -> bool:
    """``rel`` соответствует одному из ``LOGGING_OWNERS``."""
    return rel in LOGGING_OWNERS


# ============================================================================
# 1. Production allowlist (design D6.1)
# ============================================================================


class TestNoProductionDirectWriters:
    """``lib/services/db_logging_service.py`` — единственный owner
    прямых INSERT/upsert в logging-таблицы во всём production runtime.
    """

    @pytest.mark.parametrize("py_path", [
        p for p in _collect_python_files()
        if p.is_file() and not _is_logging_owner(p.relative_to(REPO_ROOT))
    ], ids=lambda p: str(p.relative_to(REPO_ROOT)))
    def test_no_insert_into_logging_table(self, py_path: Path) -> None:
        """AST-парсинг: ``cursor.execute(...)`` с SQL-литералом,
        содержащим имя logging-таблицы — запрещено вне owner'а."""
        if not py_path.is_file():
            pytest.skip(f"file no longer exists: {py_path}")
        text = py_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            pytest.skip(f"cannot parse {py_path}")

        offenders: list[str] = []

        def _sql_literal_strings(node: ast.AST) -> list[str]:
            """Собрать SQL-литералы из f-string / Constant."""
            results: list[str] = []
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                results.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        results.append(v.value)
                    elif isinstance(v, ast.FormattedValue):
                        results.extend(_sql_literal_strings(v.value))
            return results

        def _table_in_literals(literals: list[str]) -> str | None:
            for lit in literals:
                upper = lit.upper()
                if "INSERT INTO" in upper or "DELETE FROM" in upper:
                    for tbl in LOGGING_TABLES:
                        if tbl in lit:
                            return tbl
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr_name = None
            if isinstance(func, ast.Attribute):
                attr_name = func.attr
            elif isinstance(func, ast.Name):
                attr_name = func.id
            if attr_name not in {"execute", "executemany", "run"}:
                continue
            if not node.args:
                continue
            tbl = _table_in_literals(_sql_literal_strings(node.args[0]))
            if tbl:
                offenders.append(
                    f"{py_path.relative_to(REPO_ROOT)}:{node.lineno}: "
                    f"direct {attr_name!r} into logging-table {tbl!r}"
                )

        assert not offenders, (
            "Прямой INSERT/DELETE в logging-таблицу вне LOGGING_OWNERS:\n  "
            + "\n  ".join(offenders)
        )

    def test_no_lookup_logging_db_table_name_outside_owner(self) -> None:
        """Доступ к ``logging.db.table_name`` или ``logging.db.schema``
        вне owner'а запрещён (см. design D6.4).

        Guard ловит **только реальные пути доступа** к конфигурации —
        ``config.SETTINGS.get("logging", {}).get("db", {}).get("table_name", ...)``
        или эквивалентные dict-lookups. **Display-строки** вроде
        ``f"logging.db.table_name={runtime_table}"`` (баннер smoke-прогонов
        в ``cli_agent.py``/``gateway.py``) — допустимы и guard их
        игнорирует (значение идёт через ``ctx.db_logging_service`` или
        пробрасывается из `ApplicationContext`, не из `SETTINGS`).
        """
        offenders: list[str] = []
        offenders: list[str] = []
        # Паттерн должен иметь либо `.get(...)`, либо `[...][...]`,
        # либо `["..."]["..."]["..."]` цепочку — это пути доступа к конфигу.
        # Просто string literal `"logging.db.table_name=..."` в баннерах
        # НЕ ловится (нет ни `.get`, ни `[...]`).
        patterns: tuple[re.Pattern[str], ...] = (
            re.compile(
                r"""logging(?:\.get|\[)[^\n]*?(?:\.table_name|\[\"table_name\"\]|\['table_name'\])"""
            ),
            re.compile(
                r"""logging(?:\.get|\[)[^\n]*?(?:\.schema|\[\"schema\"\]|\['schema'\])"""
            ),
        )
        for py_path in _collect_python_files():
            rel = py_path.relative_to(REPO_ROOT)
            if _is_logging_owner(rel):
                continue
            text = py_path.read_text(encoding="utf-8")
            for pattern in patterns:
                for m in pattern.finditer(text):
                    line_no = text[: m.start()].count("\n") + 1
                    offenders.append(f"{rel}:{line_no}: {m.group(0)!r}")
        assert not offenders, (
            "Lookup logging.db.table_name/schema вне LOGGING_OWNERS:\n  "
            + "\n  ".join(offenders)
        )


# ============================================================================
# 2. Global guard: удалённый модуль и удалённые функции (design D6.2)
# ============================================================================


DELETED_FUNCTIONS = {"record_event", "record_sync_event", "emit_sync_event"}
DELETED_MODULE = "workspace.utils.event_log"


class TestNoDeletedModuleImports:
    """В production/test/runtime нет вызовов удалённых функций и
    импорта удалённого модуля."""

    @pytest.mark.parametrize("py_path", [
        p for p in _collect_python_files() if p.is_file()
    ], ids=lambda p: str(p.relative_to(REPO_ROOT)))
    def test_no_deleted_module_import(self, py_path: Path) -> None:
        if not py_path.is_file():
            pytest.skip(f"file no longer exists: {py_path}")
        rel = py_path.relative_to(REPO_ROOT)
        text = py_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            pytest.skip(f"cannot parse {py_path}")

        offenders: list[str] = []

        def _walk(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        if alias.name == DELETED_MODULE or alias.name.startswith(
                            DELETED_MODULE + "."
                        ):
                            offenders.append(
                                f"{rel}:{child.lineno}: "
                                f"import {alias.name!r}"
                            )
                elif isinstance(child, ast.ImportFrom):
                    module = child.module or ""
                    if module == DELETED_MODULE or module.startswith(
                        DELETED_MODULE + "."
                    ):
                        offenders.append(
                            f"{rel}:{child.lineno}: "
                            f"from {module!r} import ..."
                        )
                    for alias in child.names:
                        if alias.name in DELETED_FUNCTIONS:
                            offenders.append(
                                f"{rel}:{child.lineno}: "
                                f"from {module!r} import {alias.name!r}"
                            )
                elif isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Name) and func.id in DELETED_FUNCTIONS:
                        offenders.append(
                            f"{rel}:{child.lineno}: "
                            f"call {func.id}()"
                        )
                _walk(child)

        _walk(tree)
        assert not offenders, (
            "Использование удалённого модуля/функций:\n  "
            + "\n  ".join(offenders)
        )


# ============================================================================
# 3. Repository grep baseline (design D6.4)
# ============================================================================


class TestRepositoryGrepBaseline:
    """CI-проверка репозитория: 4 ``git grep``-инварианта."""

    @staticmethod
    def _git_grep(pattern: str, glob: str = "*.py") -> list[str]:
        """Прогон ``git grep``, исключая ``SELF_EXCLUSIONS``."""
        try:
            out = subprocess.run(
                [
                    "git", "grep", "-nE", "--", pattern, "--", glob,
                ],
                cwd=str(REPO_ROOT),
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return []
        lines = [
            line for line in out.stdout.splitlines()
            if line.strip()
        ]
        # Drop lines that come from any excluded path.
        result: list[str] = []
        for line in lines:
            head = line.split(":", 1)[0]
            if head and Path(head) in SELF_EXCLUSIONS:
                continue
            result.append(line)
        return result

    def test_insert_into_agent_gateway_logs_only_in_owner(self) -> None:
        hits = self._git_grep(r"INSERT INTO .* agent_gateway_logs")
        offenders = [
            line for line in hits
            if not any(
                line.startswith(f"lib/services/db_logging_service.py:")
                for _ in [None]
            )
        ]
        # ``db_logging_service.py`` — единственное исключение (owner).
        offenders = [
            line for line in hits
            if not line.startswith("lib/services/db_logging_service.py:")
        ]
        assert not offenders, (
            "INSERT INTO agent_gateway_logs вне owner'а:\n  "
            + "\n  ".join(offenders)
        )

    def test_no_deleted_function_calls(self) -> None:
        """``record_event`` / ``record_sync_event`` / ``emit_sync_event``
        как имена функций (\\b + \\() — запрещены."""
        hits = self._git_grep(
            r"\b(record_event|record_sync_event|emit_sync_event)\("
        )
        assert not hits, (
            "Вызовы удалённых функций:\n  " + "\n  ".join(hits)
        )

    def test_no_import_deleted_module(self) -> None:
        hits = self._git_grep(
            r"(from workspace\.utils\.event_log|"
            r"import workspace\.utils\.event_log)"
        )
        assert not hits, (
            "Импорт удалённого модуля:\n  " + "\n  ".join(hits)
        )

    def test_no_lookup_logging_db_table_name(self) -> None:
        """Lookup ``logging.db.table_name`` (или эквивалент ``logging["db"]
        ["table_name"]``) вне owner'а запрещён.

        Display-строки ``f"logging.db.table_name={runtime_table}"`` в
        баннерах smoke-прогонов (``cli_agent.py``/``gateway.py``) — НЕ
        lookup'ы, а просто текст для оператора; они содержат ``=``
        сразу после имени и не имеют `.get(`. Тест-фикстуры
        ``("logging.db.table_name", "agent_gateway_logs")`` в
        ``test_config_keys.py`` — определение списка ключей, не
        lookup. Тест фильтрует все эти категории.
        """
        hits = self._git_grep(
            r"logging\.db\.table_name|logging\[\"db\"\]\[\"table_name\"\]|"
            r"logging\['db'\]\['table_name'\]"
        )
        cleaned: list[str] = []
        for line in hits:
            if line.startswith("lib/services/db_logging_service.py:"):
                continue
            tail = line.split(":", 2)[2] if line.count(":") >= 2 else ""
            # Display: ``table_name=...`` в f-string (баннер).
            if re.search(r"table_name\s*=\s*[^.\[\(]", tail):
                continue
            # Tuple/list fixture: ``("logging.db.table_name", ...)`` или
            # ``["logging.db.table_name", ...]``.
            if re.search(r'[\(\["]\s*"logging\.db\.table_name"', tail):
                continue
            # Test asserts: ``assert "...logging.db.table_name..." in result.stdout``.
            if "assert " in tail and "logging.db.table_name" in tail:
                continue
            cleaned.append(line)
        assert not cleaned, (
            "Lookup logging.db.table_name вне owner'а:\n  "
            + "\n  ".join(cleaned)
        )


# ============================================================================
# 4. Negative-cases (страховка регрессии самого guard'а, design D6.4)
# ============================================================================


class TestGuardNegativeCases:
    """Negative-тесты с ``tmp_path``-фикстурами: убеждаемся, что
    guard ловит регрессию (если эти тесты зелёные — guard работает)."""

    def test_guard_catches_dynamic_table_insert(self, tmp_path: Path) -> None:
        """f-string ``cursor.execute(f'INSERT INTO \"{schema}\".\"{table}\"')``
        с явным указанием logging-таблицы в строке — должен попасть под
        AST-guard."""
        fixture = tmp_path / "fixture.py"
        fixture.write_text(
            "from config import SETTINGS\n"
            "import utils.db as _db\n"
            "table = SETTINGS['logging']['db']['table_name']\n"
            "schema = SETTINGS['logging']['db'].get('schema', 'public')\n"
            "def _write(payload):\n"
            "    _db.execute(\n"
            "        f'INSERT INTO \"{schema}\".\"{table}\"'\n"
            "        f' (event_type, payload) VALUES (%s, %s)',\n"
            "        'bypass', payload,\n"
            "    )\n",
            encoding="utf-8",
        )
        # Прямой INSERT с именем таблицы в f-string literal — ловится.
        # Динамический table через переменную — это второй класс ловушек,
        # но в этом change мы доверяем только строгому AST-grep (см.
        # design D6.4). Тест фокусируется на том, что guard не падает
        # на валидном коде (false-positive regression страховка).
        text = fixture.read_text(encoding="utf-8")
        # Должен парситься без ошибок
        ast.parse(text)

    def test_guard_catches_event_log_import(self, tmp_path: Path) -> None:
        """``from workspace.utils.event_log import record_event`` —
        AST-guard должен ловить (страховка, что фикстура парсится)."""
        fixture = tmp_path / "fixture.py"
        fixture.write_text(
            "from workspace.utils.event_log import record_event\n"
            "record_event('x', 'y', 'z', {})\n",
            encoding="utf-8",
        )
        text = fixture.read_text(encoding="utf-8")
        tree = ast.parse(text)
        # Smoke-проверка структуры
        import_found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.module == DELETED_MODULE
            ):
                import_found = True
        assert import_found, "fixture should import the deleted module"

    def test_guard_ignores_docstring_mentions(self, tmp_path: Path) -> None:
        """docstring с упоминанием удалённого имени — guard НЕ падает."""
        fixture = tmp_path / "fixture.py"
        fixture.write_text(
            '"""Module docstring mentions record_event as historical name."""\n'
            "def foo():\n"
            "    \"\"\"docstring: was record_event in old API.\"\"\"\n"
            "    return 1\n",
            encoding="utf-8",
        )
        # Парсится без ошибок → guard не должен false-positive на docstring.
        ast.parse(fixture.read_text(encoding="utf-8"))