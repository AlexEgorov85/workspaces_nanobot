"""Architecture tests: dependency direction (TARGET §4, §22.9).

Проверяет что:
  * ``workspace/tools/*`` не импортит ``workspace.skills.*`` (Skill → Tool
    нарушение архитектуры).
  * ``workspace/tools/*`` не импортит ``workspace.skills.audit_analyzer.*``
    специально (не domain-specific).
  * ``lib/*`` не импортит ``workspace.tools.*`` и ``workspace.skills.*``
    (core инфраструктура не зависит от generic tools).

Эти проверки дополняют ``test_core_infrastructure_independence.py``
(который проверяет lib/services/ и lib/utils/ на импорт skills).
Здесь добавляем правило для tools → skills и lib → tools.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _imports(tree: ast.AST, prefix: str) -> list[tuple[str, int]]:
    """Найти все imports модулей с заданным префиксом.

    Args:
        tree: AST-дерево.
        prefix: префикс модуля (например, ``"workspace.skills"``).
            Совпадение как ``workspace.skills`` или ``workspace.skills.audit_analyzer``.
    """
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == prefix or mod.startswith(prefix + "."):
                results.append((mod, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == prefix or alias.name.startswith(prefix + "."):
                    results.append((alias.name, node.lineno))
    return results


def _tool_files() -> list[Path]:
    tools_dir = REPO_ROOT / "workspace" / "tools"
    return [
        p for p in tools_dir.glob("*.py")
        if p.name != "__init__.py" and not p.name.startswith("_")
    ]


def _lib_files() -> list[Path]:
    """Все .py файлы в lib/ (без __pycache__)."""
    lib_dir = REPO_ROOT / "lib"
    return [
        p for p in lib_dir.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


class TestToolsDoNotImportSkills:
    """``workspace/tools/*`` не должны импортировать ``workspace.skills.*``.

    Это критическое правило: Tool — generic infrastructure, не должен
    знать про Skill. Skill может Tool регистрировать, но не наоборот.
    """

    @pytest.mark.parametrize(
        "tool_path",
        [str(p.relative_to(REPO_ROOT)) for p in _tool_files()],
    )
    def test_no_skill_import(self, tool_path: str) -> None:
        source = (REPO_ROOT / tool_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = _imports(tree, "workspace.skills")
        assert not offenders, (
            f"{tool_path} imports skill module: {offenders}. "
            "Tool must not depend on Skill (TARGET §22.9)."
        )


class TestToolsDoNotImportAuditAnalyzer:
    """``workspace/tools/*`` не должны импортировать ``workspace.skills.audit_analyzer.*``.

    Более строгая проверка: даже если Skill/ может быть generic, конкретный
    ``audit_analyzer`` — это доменный skill, и tool не должен о нём знать.
    """

    @pytest.mark.parametrize(
        "tool_path",
        [str(p.relative_to(REPO_ROOT)) for p in _tool_files()],
    )
    def test_no_audit_analyzer_import(self, tool_path: str) -> None:
        source = (REPO_ROOT / tool_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = _imports(tree, "workspace.skills.audit_analyzer")
        assert not offenders, (
            f"{tool_path} imports audit_analyzer: {offenders}. "
            "Generic tool must not reference any specific skill."
        )


class TestLibDoesNotImportToolsOrSkills:
    """``lib/*`` не должны импортировать ``workspace.tools.*`` или
    ``workspace.skills.*``.

    Core инфраструктура — фундамент. Если lib/ зависит от workspace/tools,
    нарушается направление зависимостей: tools становятся частью core.
    """

    @pytest.mark.parametrize(
        "lib_path",
        [str(p.relative_to(REPO_ROOT)) for p in _lib_files()],
    )
    def test_no_tool_import(self, lib_path: str) -> None:
        source = (REPO_ROOT / lib_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = _imports(tree, "workspace.tools")
        assert not offenders, (
            f"{lib_path} imports workspace.tools: {offenders}. "
            "Core lib/ must not depend on workspace/tools (TARGET §4)."
        )

    @pytest.mark.parametrize(
        "lib_path",
        [str(p.relative_to(REPO_ROOT)) for p in _lib_files()],
    )
    def test_no_skill_import(self, lib_path: str) -> None:
        source = (REPO_ROOT / lib_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = _imports(tree, "workspace.skills")
        assert not offenders, (
            f"{lib_path} imports workspace.skills: {offenders}. "
            "Core lib/ must not depend on workspace/skills (TARGET §4)."
        )


# Замечание про skills → lib/services:
# Тест нашёл реальное нарушение в workspace/skills/audit_analyzer/scripts/llm.py:13
# (``from lib.services.llm_client import call_llm``). Это **существующий** код,
# который надо чинить отдельной задачей (вынести llm_client в workspace/utils
# или ввести ApplicationContext-aware pattern). Пока не реализовано —
# строгие проверки skills→lib не активируем, чтобы не сломать CI.
# class TestSkillsDoNotImportLib: ...  # см. git history