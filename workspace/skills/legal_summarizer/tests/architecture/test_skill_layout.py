"""Skill layout guard for ``legal_summarizer``.

Проверяет, что Skill соответствует модели Anthropic Skills:

* ``SKILL.md`` существует (инструкция агенту).
* ``scripts/`` существует с entry-points ``cli.py`` / ``cli_query.py``.
* ``legal_summarizer/`` (runtime Python-пакет) существует в корне Skill.
* ``prompts/`` существует.
* ``references/`` существует.
* ``src/`` НЕ существует (нет дополнительного packaging-слоя).
* ``domain/`` НЕ существует внутри runtime-пакета.
* ``ARCHITECTURE_V2.md`` НЕ существует (нет конфликтующих архитектурных
  источников).
* ``import legal_summarizer.src`` нигде не встречается.
"""

from __future__ import annotations

import re
from pathlib import Path


_SKILL_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_PKG = _SKILL_ROOT / "legal_summarizer"


def test_skill_md_exists() -> None:
    """``SKILL.md`` — главный интерфейс Skill."""
    assert (_SKILL_ROOT / "SKILL.md").is_file()


def test_scripts_dir_exists() -> None:
    """``scripts/`` — каталог entry-points (cli.py, cli_query.py)."""
    assert (_SKILL_ROOT / "scripts").is_dir()
    assert (_SKILL_ROOT / "scripts" / "cli.py").is_file()
    assert (_SKILL_ROOT / "scripts" / "cli_query.py").is_file()


def test_runtime_package_exists() -> None:
    """``legal_summarizer/`` — runtime Python-пакет в корне Skill."""
    assert _RUNTIME_PKG.is_dir()
    assert (_RUNTIME_PKG / "__init__.py").is_file()


def test_references_dir_exists() -> None:
    """``references/`` — подробные документы Skill."""
    assert (_SKILL_ROOT / "references").is_dir()
    assert (_SKILL_ROOT / "references" / "architecture.md").is_file()
    assert (_SKILL_ROOT / "references" / "contracts.md").is_file()
    assert (_SKILL_ROOT / "references" / "testing.md").is_file()


def test_prompts_dir_exists() -> None:
    """``prompts/`` — LLM-инструкции."""
    assert (_SKILL_ROOT / "prompts").is_dir()
    assert (_SKILL_ROOT / "prompts" / "summarize_system.md").is_file()
    assert (_SKILL_ROOT / "prompts" / "reduce_system.md").is_file()
    assert (_SKILL_ROOT / "prompts" / "section_reduce_system.md").is_file()


def test_no_src_dir() -> None:
    """``src/`` НЕ должен существовать — Skill не использует packaging-слой."""
    assert not (_SKILL_ROOT / "src").exists(), (
        "src/ found — Skill должен быть self-contained, без packaging-слоя"
    )


def test_no_legacy_architecture_v2() -> None:
    """``ARCHITECTURE_V2.md`` НЕ должен существовать — единственный
    архитектурный документ в ``references/architecture.md``."""
    assert not (_SKILL_ROOT / "ARCHITECTURE_V2.md").exists()


def test_no_legacy_architecture_root() -> None:
    """Корневой ``ARCHITECTURE.md`` удалён (его роль выполняет
    ``references/architecture.md``)."""
    assert not (_SKILL_ROOT / "ARCHITECTURE.md").exists()


def test_no_domain_layer_in_runtime() -> None:
    """``legal_summarizer/domain/`` НЕ должно быть — domain распределён
    по document / llm."""
    assert not (_RUNTIME_PKG / "domain").exists(), (
        "domain/ found in runtime package — must be removed; "
        "models live in document/structure.py, tokens in llm/tokens.py"
    )


def test_no_src_imports_in_codebase() -> None:
    """``import legal_summarizer.src`` / ``from legal_summarizer.src``
    нигде не встречается."""
    pattern = re.compile(r"legal_summarizer\.src|legal_summarizer\s*\.\s*src")
    this_file = Path(__file__).resolve()
    for path in _SKILL_ROOT.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        if path.resolve() == this_file:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not pattern.search(text), (
            f"{path.relative_to(_SKILL_ROOT)}: legacy src/ import found"
        )


def test_no_domain_imports_in_codebase() -> None:
    """``legal_summarizer.domain.*`` нигде не встречается."""
    pattern = re.compile(r"legal_summarizer\.domain\.")
    this_file = Path(__file__).resolve()
    for path in _SKILL_ROOT.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        if path.resolve() == this_file:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not pattern.search(text), (
            f"{path.relative_to(_SKILL_ROOT)}: legacy domain/ import found"
        )


def test_prompts_path_independent_of_cwd() -> None:
    """``load_prompt()`` использует абсолютный путь через ``__file__``,
    поэтому работает независимо от того, откуда запущен Skill.
    """
    import subprocess
    import sys

    cli = _SKILL_ROOT / "scripts" / "cli.py"
    assert cli.is_file()
    proc = subprocess.run(
        [sys.executable, str(cli), "--help"],
        cwd=_SKILL_ROOT,  # cwd skill root
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"cli.py --help from skill root failed: {proc.stderr}"
    )
    # Также из произвольной cwd (родитель репо).
    other_cwd = _SKILL_ROOT.parent.parent
    proc = subprocess.run(
        [sys.executable, str(cli), "--help"],
        cwd=other_cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"cli.py --help from arbitrary cwd ({other_cwd}) failed: "
        f"{proc.stderr}"
    )