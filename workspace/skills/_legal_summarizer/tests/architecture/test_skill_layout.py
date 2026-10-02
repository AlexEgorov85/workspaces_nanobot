"""Skill layout guard для ``legal_summarizer`` после миграции runtime в ``scripts/``.

Проверяет целевую структуру каталога Skill согласно § 19 плана миграции:

* ``SKILL.md`` существует.
* ``README.md`` существует.
* ``prompts/``, ``references/``, ``scripts/``, ``tests/`` существуют.
* ``scripts/cli.py`` и ``scripts/cli_query.py`` существуют.
* 9 runtime-каталогов существуют непосредственно в ``scripts/``.
* ``src/``, ``domain/``, ``legal_summarizer/``, ``scripts/legal_summarizer/``
  НЕ существуют.
* legacy shim-файлы в ``scripts/`` НЕ существуют.
"""
from __future__ import annotations

from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"

_RUNTIME_LAYERS = (
    "application",
    "cache",
    "chunking",
    "document",
    "execution",
    "llm",
    "output",
    "planning",
    "retrieval",
)

def test_skill_md_exists() -> None:
    """``SKILL.md`` существует."""
    assert (_SKILL_ROOT / "SKILL.md").is_file()

def test_readme_exists() -> None:
    """``README.md`` существует."""
    assert (_SKILL_ROOT / "README.md").is_file()

def test_prompts_dir_exists() -> None:
    """``prompts/`` существует со всеми prompt-файлами."""
    prompts = _SKILL_ROOT / "prompts"
    assert prompts.is_dir()
    assert (prompts / "summarize_system.md").is_file()
    assert (prompts / "reduce_system.md").is_file()
    assert (prompts / "section_reduce_system.md").is_file()

def test_references_dir_exists() -> None:
    """``references/`` существует с обязательными документами."""
    refs = _SKILL_ROOT / "references"
    assert refs.is_dir()
    assert (refs / "architecture.md").is_file()
    assert (refs / "contracts.md").is_file()
    assert (refs / "testing.md").is_file()

def test_tests_dir_exists() -> None:
    """``tests/`` существует."""
    assert (_SKILL_ROOT / "tests").is_dir()

def test_scripts_dir_exists() -> None:
    """``scripts/`` существует с entry-points ``cli.py`` / ``cli_query.py``."""
    assert _SCRIPTS_DIR.is_dir()
    assert (_SCRIPTS_DIR / "cli.py").is_file()
    assert (_SCRIPTS_DIR / "cli_query.py").is_file()

def test_runtime_layers_in_scripts() -> None:
    """Все 9 runtime-каталогов лежат непосредственно в ``scripts/``."""
    for layer in _RUNTIME_LAYERS:
        assert (_SCRIPTS_DIR / layer).is_dir(), (
            f"scripts/{layer}/ не существует"
        )

def test_no_src_dir() -> None:
    """``src/`` не должен существовать."""
    assert not (_SKILL_ROOT / "src").exists()

def test_no_domain_dir() -> None:
    """``domain/`` не должен существовать в Skill root."""
    assert not (_SKILL_ROOT / "domain").exists()

def test_no_legacy_runtime_package() -> None:
    """``legal_summarizer/`` runtime-каталог НЕ должен существовать в Skill root."""
    assert not (_SKILL_ROOT / "legal_summarizer").exists(), (
        "legal_summarizer/ runtime package не должен существовать — "
        "runtime переехал в scripts/"
    )

def test_no_nested_legal_summarizer_in_scripts() -> None:
    """``scripts/legal_summarizer/`` (nested package) НЕ должен существовать."""
    assert not (_SCRIPTS_DIR / "legal_summarizer").exists()

def test_no_legacy_summarizer_py() -> None:
    """``scripts/summarizer.py`` НЕ должен существовать."""
    assert not (_SCRIPTS_DIR / "summarizer.py").exists()

def test_no_legacy_manifest_py() -> None:
    """``scripts/manifest.py`` НЕ должен существовать."""
    assert not (_SCRIPTS_DIR / "manifest.py").exists()

def test_no_legacy_output_py() -> None:
    """``scripts/output.py`` НЕ должен существовать."""
    assert not (_SCRIPTS_DIR / "output.py").exists()

def test_no_legacy_skill_config_py() -> None:
    """``scripts/skill_config.py`` НЕ должен существовать."""
    assert not (_SCRIPTS_DIR / "skill_config.py").exists()
