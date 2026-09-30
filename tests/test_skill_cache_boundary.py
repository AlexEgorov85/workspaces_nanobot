"""Граница «навык ↔ базовый интерфейс кэша».

Change ``drop-local-cache-read-from-pg``, задача 3.5.

Навык общается с кэшем через единый базовый интерфейс ``CacheProvider`` и
не знает, что за ним стоит. Это структурное свойство, поэтому оно
проверяется, а не декларируется: ниже гард запрещает в рабочем коде навыков
конкретные реализации хранилища, путь к файлу кэша и прямой импорт движка.

Осознанное исключение: собственные тесты навыка могут собирать in-memory
базу движка как фикстуру. Это тестовый doubles, а не знание навыка о
хранилище, поэтому каталог ``tests/`` внутри skill'а не проверяется.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SKILLS = _ROOT / "workspace" / "skills"

#: Каталоги внутри skill'а, которые проверяются.
def _skill_code_files() -> list[Path]:
    out: list[Path] = []
    for skill in sorted(_SKILLS.iterdir()) if _SKILLS.is_dir() else []:
        if not skill.is_dir():
            continue
        scripts = skill / "scripts"
        if scripts.is_dir():
            out.extend(sorted(scripts.rglob("*.py")))
    return out


def _skill_doc_files() -> list[Path]:
    return sorted(p for p in _SKILLS.glob("*/SKILL.md"))


#: Имена concrete-реализаций хранилища. Навык о них знать не должен.
CONCRETE_IMPLEMENTATIONS = (
    "DuckDbCacheStore",
    "PostgresDuckDbProvider",
    "CacheStore",
)

#: Признаки прямого доступа к файлу кэша вместо интерфейса.
FILE_PATH_MARKERS = (
    "cache.duckdb",
    "data_store/duckdb",
    ".cache/nanobot/duckdb",
    "resolve_cache_path",
)

#: Идентификаторы рабочего кода, названные по реализации.
IMPL_NAMED_IDENTIFIERS = re.compile(r"\bDuckDB\w*")


def test_skills_exist() -> None:
    """Сторож бессмысленен, если каталог навыков не найден."""
    assert _SKILLS.is_dir(), f"не найден каталог навыков: {_SKILLS}"
    assert _skill_code_files(), "не найден ни одного файла кода навыков"


@pytest.mark.parametrize(
    "name", CONCRETE_IMPLEMENTATIONS, ids=lambda n: n
)
def test_skill_code_does_not_name_concrete_implementation(name: str) -> None:
    offenders = [
        f"{p.relative_to(_ROOT)}: {name}"
        for p in _skill_code_files()
        if name in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "Код навыка называет конкретную реализацию кэша. Навык общается с "
        f"базовым интерфейсом CacheProvider. Найдено: {offenders}"
    )


@pytest.mark.parametrize("marker", FILE_PATH_MARKERS, ids=lambda m: m)
def test_skill_code_does_not_reach_cache_file(marker: str) -> None:
    offenders = [
        f"{p.relative_to(_ROOT)}: {marker}"
        for p in _skill_code_files()
        if marker in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "Код навыка ссылается на файл кэша напрямую вместо интерфейса. "
        f"Найдено: {offenders}"
    )


def test_skill_code_does_not_import_engine() -> None:
    offenders = [
        f"{p.relative_to(_ROOT)}"
        for p in _skill_code_files()
        if re.search(r"^\s*import duckdb\b|^\s*from duckdb\b", p.read_text(encoding="utf-8"), re.M)
    ]
    assert not offenders, (
        f"Код навыка импортирует движок напрямую: {offenders}"
    )


def test_skill_code_has_no_implementation_named_identifiers() -> None:
    """В идентификаторах рабочего кода навыка не должно быть имён движка.

    Проверяются только идентификаторы (дефиниции и ``__all__``), а не
    проза в доктрингах: проза может описывать диалект запросов, а имя в
    идентификаторе — это публичный контракт навыка, названный по хранилищу.
    """
    offenders: list[str] = []
    for path in _skill_code_files():
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not (stripped.startswith("def ") or stripped.startswith("class ")):
                continue
            if IMPL_NAMED_IDENTIFIERS.search(line):
                offenders.append(f"{path.relative_to(_ROOT)}:{num}: {stripped}")
            quoted = re.findall(r'"([^"]+)"', line)
            for name in quoted:
                if IMPL_NAMED_IDENTIFIERS.match(name):
                    offenders.append(f"{path.relative_to(_ROOT)}:{num}: __all__ -> {name}")
    assert not offenders, (
        "Идентификатор навыка назван по реализации хранилища: " f"{offenders}"
    )


def test_skill_docs_do_not_name_concrete_implementation() -> None:
    offenders = [
        f"{p.relative_to(_ROOT)}: {name}"
        for p in _skill_doc_files()
        for name in CONCRETE_IMPLEMENTATIONS
        if name in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"Документация навыка называет конкретную реализацию кэша: {offenders}"
    )


def test_skill_exports_generic_query_protocol() -> None:
    """Навык торгует срезом интерфейса, а не именем хранилища."""
    from workspace.skills.audit_analyzer.scripts.predefined import mode

    assert hasattr(mode, "CacheQueryService")
    assert not hasattr(mode, "DuckDBServiceProtocol")
