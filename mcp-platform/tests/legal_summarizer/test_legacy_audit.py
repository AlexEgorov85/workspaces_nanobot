"""Legacy audit — forbidden files не существуют."""

from __future__ import annotations

from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parents[2]
_LIB_DIR = _PLATFORM_ROOT / "libs" / "legal_summarizer"
# Раньше скилл лежал в агенте и подключал себя в sys.path; в платформе
# корень и так на месте (mcp-platform/tests/conftest.py).

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
    "legal_summarizer",  # scripts/legal_summarizer/ — вложенный пакет
    "src",
]

def test_forbidden_files_do_not_exist():
    """Legacy-файлы не должны существовать."""
    for fname in FORBIDDEN_FILES:
        p = _LIB_DIR / fname
        assert not p.exists(), (
            f"forbidden legacy file exists: {p}"
        )

def test_forbidden_runtime_dirs_do_not_exist():
    """Legacy runtime-каталоги не должны существовать в Skill root."""
    for dname in FORBIDDEN_DIRS:
        p = _LIB_DIR / dname
        assert not p.exists(), (
            f"forbidden legacy directory exists: {p}"
        )
