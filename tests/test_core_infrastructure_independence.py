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
CORE_SERVICES = [
    "lib/services/cache_provider.py",
    "lib/services/cache_provider_impl.py",
    "lib/services/audit_memory_store.py",
    "lib/services/audit_sync_service.py",
    "lib/services/preload_service.py",
    "lib/services/table_registry.py",
    "lib/services/vector_index_service.py",
    "lib/utils/duckdb_query.py",
    "lib/utils/sql_safety.py",
    "lib/utils/text_utils.py",
    "lib/utils/table_utils.py",
]

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
        [p for p in CORE_SERVICES if (REPO_ROOT / p).exists()],
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
        [p for p in CORE_SERVICES if (REPO_ROOT / p).exists()],
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
    """Дефолтная схема в Core-сервисах должна быть 'main', не 'oarb' (TARGET §22.3)."""

    @pytest.mark.parametrize(
        "path",
        [
            "lib/services/audit_memory_store.py",
            "lib/services/audit_sync_service.py",
        ],
    )
    def test_no_oarb_default(self, path: str) -> None:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        # Ищем `schema: str = "oarb"` (default в сигнатуре __init__)
        assert 'schema: str = "oarb"' not in source, (
            f"{path} still has 'oarb' as default schema. "
            "Default must be 'main' (TARGET §22.3)."
        )