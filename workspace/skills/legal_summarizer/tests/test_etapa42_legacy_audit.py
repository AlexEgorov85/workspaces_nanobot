"""Этап 42: Legacy audit — forbidden files не существуют."""

from __future__ import annotations

from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"

FORBIDDEN_FILES = [
    "token_budget.py",
    "reducer_strategy.py",
    "brief_strategy.py",
    "document_cache.py",
    "fingerprint.py",
    "structure/sections.py",
    "structure/tree.py",
    "cleanup.py",
    "_legacy_run_map_reduce.py",
    # Phase: filesystem migration к scripts/ (§ 18 плана)
    "summarizer.py",
    "manifest.py",
    "output.py",
    "skill_config.py",
]

FORBIDDEN_DIRS = [
    # Phase: filesystem migration к scripts/ (§ 18 плана)
    "legal_summarizer",
    "src",
    "legal_summarizer",  # scripts/legal_summarizer/ (nested package)
]

def test_forbidden_files_do_not_exist():
    """Legacy-файлы не должны существовать."""
    for fname in FORBIDDEN_FILES:
        p = _SCRIPTS_DIR / fname
        assert not p.exists(), (
            f"forbidden legacy file exists: {p}"
        )

def test_forbidden_runtime_dirs_do_not_exist():
    """Legacy runtime-каталоги не должны существовать в Skill root."""
    for dname in FORBIDDEN_DIRS:
        p = _SKILL_ROOT / dname
        assert not p.exists(), (
            f"forbidden legacy directory exists: {p}"
        )
