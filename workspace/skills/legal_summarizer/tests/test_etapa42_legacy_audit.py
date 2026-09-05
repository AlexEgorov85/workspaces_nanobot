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
]


def test_forbidden_files_do_not_exist():
    """Legacy-файлы не должны существовать."""
    for fname in FORBIDDEN_FILES:
        p = _SCRIPTS_DIR / fname
        assert not p.exists(), (
            f"forbidden legacy file exists: {p}"
        )
