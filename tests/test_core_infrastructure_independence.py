"""Architecture tests: core infrastructure независим от skills.

Цель — зафиксировать TARGET_ARCHITECTURE.md §4, §22.1, §22.9.
Любое падение — архитектурная регрессия.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

LIB_DIRS = ["lib/services", "lib/utils"]


def _core_services() -> list[str]:
    """Ядро, к которому применяются доменные запреты, — выводится из дерева.

    Раньше список был зашит в константу ``CORE_SERVICES``. После того как из
    агента уехал кластер снимка, записи из списка начали указывать на
    несуществующие файлы, а фильтр ``if (REPO_ROOT / p).exists()`` молча их
    отбрасывал: тесты оставались зелёными, проверяя всё меньше, и никто не
    видел, что половина стражей выродилась в ``skip`` по несуществующему пути.

    Теперь список выводится: новый модуль в ядре попадает под запрет
    автоматически, а удалённый не оставляет после себя пустую запись.
    Tombstone'ы (имя с подчёркиванием) исключены — они не исполняются.
    """
    found: list[str] = []
    for directory in LIB_DIRS:
        base = REPO_ROOT / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT)
            if any(part.startswith("_") for part in rel.parts):
                continue
            if "__pycache__" in rel.parts:
                continue
            found.append(str(rel))
    return sorted(found)


FORBIDDEN_TOKENS = {"audit", "violations", "audits_index", "audit_analyzer"}


def _imports_skill(tree: ast.AST) -> list[tuple[str, int]]:
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("workspace.skills") or mod.startswith("skills."):
                results.append((mod, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("workspace.skills") or alias.name.startswith("skills."):
                    results.append((alias.name, node.lineno))
    return results


class TestCoreDoesNotImportSkills:
    """lib/ не должен импортировать workspace.skills/* (TARGET §4, §22.9)."""

    @pytest.mark.parametrize(
        "path",
        [str(p.relative_to(REPO_ROOT))
         for d in LIB_DIRS
         for p in (REPO_ROOT / d).rglob("*.py")
         if "__pycache__" not in p.parts],
    )
    def test_no_skill_import(self, path: str) -> None:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = _imports_skill(tree)
        assert not offenders, (
            f"{path} imports skill: {offenders}. "
            "Core infrastructure must be Skill-independent."
        )


class TestCoreNoDomainRouting:
    """Core services не должны иметь caller/skill/domain routing (TARGET §22.9)."""

    @pytest.mark.parametrize(
        "path",
        _core_services(),
    )
    def test_no_routing(self, path: str) -> None:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        forbidden_patterns = [
            "if caller ==",
            "if caller in",
            "if skill ==",
            "if skill in",
            "if domain ==",
            "if domain in",
        ]
        for pattern in forbidden_patterns:
            assert pattern not in source, (
                f"{path} contains forbidden routing pattern {pattern!r}. "
                "Generic core service must not branch on caller/skill/domain."
            )


class TestCoreNoAuditStringsInCode:
    """Core code не должен содержать audit-domain в коде (TARGET §22.3)."""

    @pytest.mark.parametrize(
        "path",
        _core_services(),
    )
    def test_no_audit_identifiers(self, path: str) -> None:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in FORBIDDEN_TOKENS:
                    offenders.append((node.name, node.lineno))
            elif isinstance(node, ast.Name):
                if node.id in FORBIDDEN_TOKENS:
                    offenders.append((node.id, node.lineno))
            elif isinstance(node, ast.Attribute):
                if node.attr in FORBIDDEN_TOKENS:
                    offenders.append((node.attr, node.lineno))
        assert not offenders, (
            f"{path} contains forbidden domain identifiers: {offenders}. "
            "Core must be domain-free (TARGET §22.3)."
        )


class TestDefaultSchemaIsGeneric:
    """Дефолтная схема в ядре — 'main', а не 'oarb' (TARGET §22.3).

    Список модулей раньше был зашит и указывал на ``duckdb_cache_store.py`` и
    ``cache_load_service.py`` — оба уехали на платформу вместе с кластером
    снимка. Проверка на них стала бессмысленной, но исключение по отсутствию
    файла скрыло бы это: тест молча превратился бы в «проверять нечего».

    Теперь обход идёт по **всему** ядру, а утверждение сформулировано как
    запрет, а не как проверка двух мест: запрет переживает и удаление модуля,
    и появление нового. Схема по умолчанию — свойство домена, которое легко
    протащить новым сервисом, и раньше оно протаскивалось именно так.
    """

    #: Подстроки, которыми в ядре может быть выражен дефолт схемы.
    #:
    #: Только ``oarb``. ``public`` здесь **не** запрещён и добавлять его
    #: нельзя: это схема собственных runtime-таблиц агента
    #: (``agent_gateway_logs``, ``agent_session_meta``), а не домен аудита.
    #: Первая версия стража запрещала и его — и падала на
    #: ``db_logging_service.py`` и ``lib/gateway/mirror/``, где
    #: ``schema="public"`` корректен. Запрет доменной схемы не должен
    #: запрещать обычную.
    _OARB_DEFAULTS = (
        'schema: str = "oarb"',
        'schema = "oarb"',
        "schema = 'oarb'",
        'default_schema: str = "oarb"',
    )

    def test_no_oarb_default_anywhere_in_core(self) -> None:
        offenders: list[tuple[str, str]] = []
        for rel in _core_services():
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for needle in self._OARB_DEFAULTS:
                if needle in source:
                    offenders.append((rel, needle))
        assert not offenders, (
            f"дефолтная схема домена в ядре: {offenders}. "
            "Схема по умолчанию — 'main' (TARGET §22.3)."
        )