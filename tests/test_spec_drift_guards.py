"""Стражи против повторного расхождения «спека ↔ код ↔ документация».

Появились после аудита 2026-10-08 (docs/spec-code-drift-audit.md), который
нашёл шесть категорий расхождений, каждая из которых молчала до правки:

  * ``close_mcp()`` — удалённый в nanobot 0.3.5 метод, вызывался в
    ``benchmarks/runner.py`` под пустым ``except``; shutdown-контракт
    (``openspec/specs/runtime/agent-hooks/spec.md:37-45``) требовал
    ``aclose()``.
  * Реестр ``COMPONENTS.md`` описывал 10 из 22 спек, хотя
    ``documentation/component-registry`` требует регистрации каждого.
  * ``AGENTS.md`` и docs ссылались на несуществующие
    ``RuntimePatcher.patch_project_tools`` / ``patch_compact_command`` —
    в том числе ``workspace/tools/example.py``, то есть reference для
    автора нового tool'а.
  * Указатели вида ``file.py:ClassName`` в спеке указывали на класс,
    которого в коде нет (``VectorIndexService`` вместо
    ``VectorIndexBuildService``).
  * Канон ссылался на несуществующую миграцию
    ``sql/migrations/V005__test_profile_tables.sql``, и это же имя было
    продублировано в комментариях шести DDL-файлов.
  * ``sql/README.md`` не содержал раздел, который требует
    ``infrastructure/test-profile-tables``.

Каждый страж обязан ловить подсаженный дефект, а не просто зелёнить на
текущем дереве: проверка существования файла регуляркой без якоря на
границе слова однажды дала ложное «файла нет» для ``profiles/test.jsonc``,
поэтому расширения здесь не вырезаются из общего класса символов.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_OPENSPEC = _REPO / "openspec"

# Корни проекта: указание на файл внутри них обязано существовать.
# ``nanobot/`` и прочие пакеты upstream сюда НЕ входят — это установленный
# пакет, а не файл репозитория.
_PROJECT_ROOTS = (
    "lib/", "workspace/", "tools/", "tests/", "benchmarks/", "sql/",
    "docs/", "scripts/", "profiles/",
)

# ``file.py:ClassName`` — указатель на символ. Границы слов обязательны:
# ``a.py:Class`` не должен матчить внутри ``a.py:ClassFactory``.
_POINTER_RE = re.compile(
    r"(?<![\w./\\-])"          # не внутри слова или более длинного пути
    r"((?:[A-Za-z0-9_.-]+[/\\])*[A-Za-z0-9_-]+\.py):"   # путь до .py
    r"([A-Za-z_][A-Za-z0-9_]*)"                          # имя символа
    r"(?![\w])"                # не префикс более длинного идентификатора
)

# ``openspec/...`` — ссылка на спеку/change. Хвост может быть каталогом.
_OPENSPEC_PATH_RE = re.compile(r"openspec/(?:specs|changes)/[A-Za-z0-9_./-]*[A-Za-z0-9_]")


def _read(rel: str) -> str:
    # utf-8-sig, а не utf-8: часть исходников в репозитории несёт BOM,
    # и ast.parse на строке с U+FEFF падает с SyntaxError — страж обязан
    # быть устойчивым к такому файлу, а не падать на нём сам.
    return (_REPO / rel).read_text(encoding="utf-8-sig")


def _iter_specs() -> list[Path]:
    return sorted(p for p in (_OPENSPEC / "specs").rglob("spec.md"))


def _markdown_targets() -> list[Path]:
    """Markdown-файлы, в которых ссылки обязаны быть живыми."""
    out = [_REPO / "AGENTS.md", _REPO / "README.md", _REPO / "docs" / "README.md"]
    out.extend(p for p in (_REPO / "docs").rglob("*.md") if "_archive" not in p.parts)
    return [p for p in out if p.exists()]


# ---------------------------------------------------------------------------
# 1. Shutdown-контракт: close_mcp удалён, aclose вызывается
# ---------------------------------------------------------------------------


def _python_sources(*roots: str) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        base = _REPO / root
        if not base.exists():
            continue
        out.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return out


def _attr_accesses(path: Path, attr: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == attr
    ]


def test_no_executable_close_mcp_calls() -> None:
    """``AgentLoop.close_mcp`` удалён в nanobot 0.3.5.

    Исполняемых обращений быть не должно нигде. Упоминания в комментариях
    (объяснение, почему вызов убран) разрешены — поэтому проверка идёт по
    AST, а не по подстроке.
    """
    sources = _python_sources(
        "lib", "workspace", "tools", "tests", "benchmarks", "scripts",
    ) + [_REPO / "gateway.py", _REPO / "cli_agent.py"]
    bad: list[str] = []
    for path in sources:
        if not path.exists():
            continue
        for lineno in _attr_accesses(path, "close_mcp"):
            bad.append(f"{path.relative_to(_REPO)}:{lineno}")
    assert not bad, (
        "close_mcp удалён в nanobot 0.3.5 — вызовы дают AttributeError "
        f"(контракт: openspec/specs/runtime/agent-hooks/spec.md:37-45). "
        f"Использовать await agent.aclose(). Найдено: {bad}"
    )


@pytest.mark.parametrize(
    "rel",
    ["gateway.py", "benchmarks/runner.py", "lib/cli/console_loop.py"],
)
def test_shutdown_points_call_aclose(rel: str) -> None:
    """Каждая точка shutdown обязана звать ``aclose()``."""
    lines = _attr_accesses(_REPO / rel, "aclose")
    assert lines, f"{rel} не вызывает aclose() — shutdown-контракт нарушен"


# ---------------------------------------------------------------------------
# 2. Реестр компонентов покрывает каждую спеку
# ---------------------------------------------------------------------------


def test_registry_covers_every_spec() -> None:
    """Каждая спека каталога обязана быть зарегистрирована в COMPONENTS.md."""
    registry = _read("openspec/specs/COMPONENTS.md")
    linked = {m.group(1) for m in re.finditer(r"\]\(([^)]*spec\.md)\)", registry)}
    specs = {
        str(p.relative_to(_OPENSPEC / "specs")).replace("\\", "/")
        for p in _iter_specs()
    }
    missing = sorted(specs - linked)
    assert not missing, (
        f"Спеки вне реестра COMPONENTS.md: {missing}. "
        "component-registry требует регистрации каждого production-компонента."
    )


# ---------------------------------------------------------------------------
# 3. Указатели ``file.py:ClassName`` в спеке указывают на существующее
# ---------------------------------------------------------------------------


def _defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
    return names


def test_spec_pointers_reference_existing_symbols() -> None:
    """``lib/services/x.py:ClassName`` в спеке — реальный символ реального файла.

    Ловит два класса расхождений: неверный путь и неверное имя класса.
    Пропускаются указания на upstream-пакет (``nanobot/...``) и на
    не-проектные корни.
    """
    problems: list[str] = []
    for spec in _iter_specs():
        text = spec.read_text(encoding="utf-8")
        for m in _POINTER_RE.finditer(text):
            raw_path, symbol = m.group(1), m.group(2)
            if raw_path.startswith(("nanobot/", "werkzeug/", "loguru/", "psycopg2/")):
                continue
            is_project_path = raw_path.startswith(_PROJECT_ROOTS) or raw_path in {
                "gateway.py", "cli_agent.py", "config.py", "streamlit_app.py",
            }
            if not is_project_path:
                continue
            target = _REPO / raw_path
            where = f"{spec.relative_to(_REPO)}:{text[: m.start()].count(chr(10)) + 1}"
            if not target.exists():
                problems.append(f"{where} — нет файла {raw_path}")
                continue
            if symbol not in _defined_names(target):
                problems.append(f"{where} — в {raw_path} нет символа {symbol}")
    assert not problems, (
        "Указатели вида file.py:ClassName в спеке не соответствуют коду:\n"
        + "\n".join(problems)
    )


# ---------------------------------------------------------------------------
# 4. Ссылки на openspec в живой документации ведут в существующее место
# ---------------------------------------------------------------------------


def _openspec_paths_exist() -> list[str]:
    known_files: set[str] = set()
    for path in _OPENSPEC.rglob("*"):
        rel = path.relative_to(_REPO).as_posix()
        known_files.add(rel)
    known_dirs: set[str] = set()
    for path in _OPENSPEC.rglob("*"):
        if path.is_dir():
            known_dirs.add(path.relative_to(_REPO).as_posix())

    dangling: list[str] = []
    for md in _markdown_targets():
        if md.name == "spec-code-drift-audit.md":
            # Сам отчёт аудита цитирует заведомо отсутствующие пути,
            # чтобы описать дефект; это не битая ссылка.
            continue
        text = md.read_text(encoding="utf-8-sig")
        for m in _OPENSPEC_PATH_RE.finditer(text):
            token = m.group(0).rstrip(".")
            # Ссылка валидна, если совпадает с существующим файлом/каталогом
            # либо является каталогом-префиксом существующего пути.
            if token in known_files or token in known_dirs:
                continue
            if any(f.startswith(token.rstrip("/") + "/") for f in known_files):
                continue
            lineno = text[: m.start()].count("\n") + 1
            dangling.append(f"{md.relative_to(_REPO)}:{lineno} -> {token}")
    return dangling


def test_openspec_references_in_docs_resolve() -> None:
    """Пути ``openspec/...`` из живой документации должны существовать.

    ``docs/_archive`` исключён намеренно: там лежат исторические ссылки на
    доархивные имена change'ов, и это не дефект.
    """
    dangling = _openspec_paths_exist()
    assert not dangling, (
        "Ссылки на openspec в документации ведут в никуда:\n"
        + "\n".join(dangling)
        + "\n(docs/_archive исключён — исторические ссылки допустимы)"
    )


# ---------------------------------------------------------------------------
# 5. Несуществующая миграция test-профиля не упоминается
# ---------------------------------------------------------------------------


def test_no_fake_test_profile_migration_reference() -> None:
    """``V005__test_profile_tables.sql`` не существует.

    Имя было продублировано в нормативной спеке и в комментариях шести
    DDL-файлов, хотя V005 в ``sql/migrations/`` — это
    ``create_agent_cache_ownership``.
    """
    offenders: list[str] = []
    targets = [_REPO / p for p in ("sql", "openspec/specs", "docs", "AGENTS.md", "README.md")]
    for base in targets:
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for path in files:
            if "changes/archive" in path.as_posix() or "_archive" in path.parts:
                continue
            if "spec-code-drift-audit" in path.name:
                continue  # отчёт описывает сам дефект
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "V005__test_profile_tables" in text:
                offenders.append(path.relative_to(_REPO).as_posix())
    assert not offenders, (
        "Упоминается несуществующая миграция test-профиля: "
        f"{offenders}. Применение — tools/apply_test_profile_tables.py "
        "или psql -f по sql/<domain>/create_public_agent_*_test.sql."
    )


def test_sql_readme_documents_test_profile() -> None:
    """``infrastructure/test-profile-tables`` требует раздел в ``sql/README.md``."""
    readme = _read("sql/README.md")
    assert "## Test-профиль" in readme, (
        "sql/README.md обязан содержать раздел «Test-профиль» с командой "
        "python tools/apply_test_profile_tables.py "
        "(требование: openspec/specs/infrastructure/test-profile-tables)"
    )
    assert "apply_test_profile_tables.py" in readme, (
        "Раздел «Test-профиль» не называет команду применения тест-таблиц"
    )


# ---------------------------------------------------------------------------
# 6. Удалённые методы RuntimePatcher не упоминаются как существующие
# ---------------------------------------------------------------------------


def test_removed_runtime_patcher_methods_stay_removed() -> None:
    """``patch_project_tools`` / ``patch_compact_command`` не должны появиться.

    Регистрация project tools живёт в
    ``lib/services/project_tool_loader.py::register_project_tools``,
    наблюдение за сжатием — в ``CompactionEventSubscriber``.
    """
    for name in ("patch_project_tools", "patch_compact_command"):
        found = [
            f"{p.relative_to(_REPO)}:{n}"
            for p in _python_sources("lib")
            for n in [
                ln
                for ln in _def_lines(p, name)
            ]
        ]
        assert not found, (
            f"RuntimePatcher.{name} не должен появляться снова: {found}. "
            "Регистрация tool'ов — project_tool_loader.register_project_tools; "
            "наблюдение за сжатием — CompactionEventSubscriber."
        )


def _def_lines(path: Path, name: str) -> list[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except SyntaxError:
        return []
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    ]