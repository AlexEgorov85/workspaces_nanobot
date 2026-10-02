"""Страж раскладки домена ``legal_summarizer`` после переноса в платформу (п. 11.1/11.4).

Раньше страж смотрел на каталог Skill агента (``workspace/skills/legal_summarizer``)
и требовал ``scripts/`` с девятью слоями. Домен живёт в платформе, и расклад
разделся на три части:

* ``mcp-platform/libs/legal_summarizer/`` - код: девять слоёв, ``cli.py``,
  ``cli_query.py``;
* ``mcp-platform/servers/enterprise/capabilities/legal_summarizer/skill/`` -
  ``SKILL.md``, ``README.md``, ``prompts/``, ``references/``;
* ``mcp-platform/tests/legal_summarizer/`` - тесты.

Страж по-прежнему запрещает старые раскладки: плоский ``src/``, ``domain/``,
nested-пакет ``legal_summarizer/`` внутри кода и legacy-файлы
(``summarizer.py``, ``manifest.py``, ``output.py``, ``skill_config.py``).

Проверка «в агенте не осталось импортов legal» здесь намеренно **не** стоит:
она проверяет tombstone оригинала, который сносится на 11.3, вместе с
переключением агента на MCP-вызов. Держать её здесь означало бы держать
красный страж до конца фазы.
"""
from __future__ import annotations

from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_LIB_DIR = _PLATFORM_ROOT / "libs" / "legal_summarizer"
_SKILL_DIR = (
    _PLATFORM_ROOT
    / "servers"
    / "enterprise"
    / "capabilities"
    / "legal_summarizer"
    / "skill"
)
_TESTS_DIR = _PLATFORM_ROOT / "tests" / "legal_summarizer"

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
    assert (_SKILL_DIR / "SKILL.md").is_file()


def test_readme_exists() -> None:
    assert (_SKILL_DIR / "README.md").is_file()


def test_prompts_dir_exists() -> None:
    prompts = _SKILL_DIR / "prompts"
    assert prompts.is_dir()
    assert (prompts / "summarize_system.md").is_file()
    assert (prompts / "reduce_system.md").is_file()
    assert (prompts / "section_reduce_system.md").is_file()


def test_references_dir_exists() -> None:
    refs = _SKILL_DIR / "references"
    assert refs.is_dir()
    assert (refs / "architecture.md").is_file()
    assert (refs / "contracts.md").is_file()
    assert (refs / "testing.md").is_file()


def test_tests_dir_exists() -> None:
    assert _TESTS_DIR.is_dir()


def test_lib_dir_exists() -> None:
    assert _LIB_DIR.is_dir()


def test_entrypoints_exist() -> None:
    assert (_LIB_DIR / "cli.py").is_file()
    assert (_LIB_DIR / "cli_query.py").is_file()


def test_runtime_layers_in_lib() -> None:
    for layer in _RUNTIME_LAYERS:
        assert (_LIB_DIR / layer).is_dir(), f"libs/legal_summarizer/{layer}/ не существует"


def test_no_src_dir() -> None:
    assert not (_LIB_DIR / "src").exists()


def test_no_domain_dir() -> None:
    assert not (_LIB_DIR / "domain").exists()


def test_no_nested_package_in_lib() -> None:
    """Вложенный ``legal_summarizer/`` внутри кода - признак плоского переноса."""
    assert not (_LIB_DIR / "legal_summarizer").exists()


def test_no_legacy_summarizer_py() -> None:
    assert not (_LIB_DIR / "summarizer.py").exists()


def test_no_legacy_manifest_py() -> None:
    assert not (_LIB_DIR / "manifest.py").exists()


def test_no_legacy_output_py() -> None:
    assert not (_LIB_DIR / "output.py").exists()


def test_no_legacy_skill_config_py() -> None:
    assert not (_LIB_DIR / "skill_config.py").exists()


def test_lib_does_not_import_agent() -> None:
    """Домен в платформе не должен тянуть слои агента.

    Это главный страж шва, который рушится первым при обратной миграции:
    ``workspace.*`` и ``lib.*`` существуют в другом проекте.
    """
    offenders: list[str] = []
    for py in _LIB_DIR.rglob("*.py"):
        for lineno, line in enumerate(
            py.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                continue
            for banned in ("import workspace", "from workspace", "import lib.", "from lib."):
                if stripped.startswith(banned):
                    offenders.append(f"{py.relative_to(_PLATFORM_ROOT)}:{lineno}: {stripped}")
    assert not offenders, "домен тянет слои агента:\n" + "\n".join(offenders)
