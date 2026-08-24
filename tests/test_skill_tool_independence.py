"""Архитектурные тесты: Skill и Tool независимы.

Эти тесты фиксируют инвариант из TARGET_ARCHITECTURE.md §3, §22.1, §22.2, §22.3.
Любое падение — архитектурная регрессия.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _all_py_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imports_skill(tree: ast.AST) -> list[tuple[str, int]]:
    """Собрать все `from workspace.skills` импорты в файле."""
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("workspace.skills") or mod == "skills":
                results.append((mod, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("workspace.skills"):
                    results.append((alias.name, node.lineno))
    return results


def _imports_tool(tree: ast.AST) -> list[tuple[str, int]]:
    """Собрать все `from workspace.tools` импорты в файле."""
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("workspace.tools") or mod == "tools":
                results.append((mod, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("workspace.tools"):
                    results.append((alias.name, node.lineno))
    return results


class TestSkillsDoNotImportTools:
    """Skill не должен импортировать Tool (TARGET §3, §22.2)."""

    @pytest.mark.parametrize(
        "path",
        [str(p.relative_to(REPO_ROOT)) for p in _all_py_files(REPO_ROOT / "workspace" / "skills")],
    )
    def test_no_tool_import(self, path: str) -> None:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = _imports_tool(tree)
        assert not offenders, (
            f"{path} imports Tool from workspace.tools: {offenders}. "
            "Skill must not depend on Tool implementation."
        )


class TestToolsDoNotImportSkills:
    """Tool не должен импортировать Skill (TARGET §3, §22.1)."""

    @pytest.mark.parametrize(
        "path",
        [str(p.relative_to(REPO_ROOT)) for p in _all_py_files(REPO_ROOT / "workspace" / "tools")],
    )
    def test_no_skill_import(self, path: str) -> None:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = _imports_skill(tree)
        assert not offenders, (
            f"{path} imports Skill from workspace.skills: {offenders}. "
            "Tool must not depend on Skill implementation."
        )


class TestLibDoesNotImportSkills:
    """lib/ не должен импортировать workspace.skills (только shared infrastructure)."""

    @pytest.mark.parametrize(
        "path",
        [str(p.relative_to(REPO_ROOT)) for p in _all_py_files(REPO_ROOT / "lib")],
    )
    def test_no_skill_import(self, path: str) -> None:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = _imports_skill(tree)
        assert not offenders, (
            f"{path} imports Skill from workspace.skills: {offenders}. "
            "lib/ must remain Skill-independent."
        )


class TestNoDynamicSkillLoadingInTools:
    """Tool не должен использовать importlib для загрузки skill-модулей."""

    @pytest.mark.parametrize(
        "path",
        [str(p.relative_to(REPO_ROOT)) for p in _all_py_files(REPO_ROOT / "workspace" / "tools")],
    )
    def test_no_spec_from_file_location(self, path: str) -> None:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert "spec_from_file_location" not in source, (
            f"{path} uses spec_from_file_location; "
            "Tools must not dynamically load Skill modules (TARGET §22.8)."
        )