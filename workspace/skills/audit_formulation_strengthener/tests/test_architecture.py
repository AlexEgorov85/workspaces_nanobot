"""Архитектурные инварианты skill'а.

Проверяет, что код соответствует заявленным границам (П5: строго 0
вхождений, включая комментарии и docstring).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"


def _all_py_files() -> list[Path]:
    return [
        p for p in SCRIPTS_DIR.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# Инвариант 1: legal_summarizer нигде в scripts/ (включая docstring и комментарии)
# -----------------------------------------------------------------------------


def test_no_legal_summarizer_anywhere_in_scripts() -> None:
    """П5: ноль вхождений 'legal_summarizer' во всех .py файлах scripts/."""
    violations: list[tuple[Path, int, str]] = []
    for p in _all_py_files():
        text = _read(p)
        for lineno, line in enumerate(text.splitlines(), 1):
            if "legal_summarizer" in line:
                violations.append((p, lineno, line.strip()))
    assert not violations, (
        "Запрещены любые упоминания 'legal_summarizer' в scripts/ "
        "(включая комментарии и docstring):\n"
        + "\n".join(f"{p}:{ln}: {line}" for p, ln, line in violations)
    )


# -----------------------------------------------------------------------------
# Инвариант 2: sys.path мутируется только в cli.py
# -----------------------------------------------------------------------------


def test_sys_path_only_in_cli_py() -> None:
    """``sys.path.insert`` или ``sys.path.append`` — только в cli.py."""
    violations: list[tuple[Path, int, str]] = []
    pattern = re.compile(r"sys\.path\.(insert|append)")
    for p in _all_py_files():
        text = _read(p)
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                if p.name != "cli.py":
                    violations.append((p, lineno, line.strip()))
    assert not violations, (
        "sys.path может мутироваться только в cli.py; нарушения:\n"
        + "\n".join(f"{p}:{ln}: {line}" for p, ln, line in violations)
    )


# -----------------------------------------------------------------------------
# Инвариант 3: ноль импортов workspace.tools в scripts/
# -----------------------------------------------------------------------------


def test_no_workspace_tools_imports() -> None:
    """Запрещены любые импорты из workspace.tools (skills не должны знать о tools)."""
    violations: list[tuple[Path, int, str]] = []
    pattern = re.compile(r"(from|import)\s+workspace\.tools")
    for p in _all_py_files():
        text = _read(p)
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                violations.append((p, lineno, line.strip()))
    assert not violations, (
        "Запрещены импорты из workspace.tools в scripts/:\n"
        + "\n".join(f"{p}:{ln}: {line}" for p, ln, line in violations)
    )


# -----------------------------------------------------------------------------
# Инвариант 4: skills импортируются только в tests/, не в scripts/
# -----------------------------------------------------------------------------


def test_no_skill_imports_in_scripts() -> None:
    """scripts/ не импортирует из workspace.skills.* (skill автономен)."""
    violations: list[tuple[Path, int, str]] = []
    pattern = re.compile(r"(from|import)\s+workspace\.skills")
    for p in _all_py_files():
        text = _read(p)
        for lineno, line in enumerate(text.splitlines(), 1):
            # ВАЖНО: исключаем self-imports (когда scripts/ ссылается на свой
            # собственный namespace, например
            # `from workspace.skills.audit_formulation_strengthener.scripts.llm import ...`).
            if "audit_formulation_strengthener" in line:
                continue
            if pattern.search(line):
                violations.append((p, lineno, line.strip()))
    assert not violations, (
        "Запрещены импорты из workspace.skills.* (кроме своего namespace) в scripts/:\n"
        + "\n".join(f"{p}:{ln}: {line}" for p, ln, line in violations)
    )
