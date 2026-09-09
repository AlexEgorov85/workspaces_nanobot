"""Zero-reference audit: regression guard для legacy symbols (PLAN §34).

Это **regression guard**, не просто print. Три режима:

* ``audit()`` — возвращает structured result (dict с hits). Сканирует
  **весь проект** (не только ``legal_summarizer``): ``lib/``, ``workspace/``,
  ``tools/``, ``gateway.py``, ``streamlit_app.py``, ``cli_agent.py``,
  ``tests/``, ``sql/``, ``benchmarks/``. Каталоги ``__pycache__``, ``.venv``,
  ``data_store``, ``.pytest_cache``, ``.ruff_cache``, ``.benchmarks``,
  ``node_modules`` исключены.
* ``assert_no_legacy()`` — поднимает ``AssertionError`` при production hit,
  при наличии запрещённых файлов или при наличии legacy секций в
  ``project.json`` (Этап 11/18).
* ``main()`` — печать отчёта (production vs test разделение).

**Один canonical registry** для всего проекта (Этап B+C remediation,
2026-09-09):

* ``_FORBIDDEN_MODULES`` — модули, запрещённые в production. Расширен
  на полный набор из ``test_legal_summarizer_no_legacy.py`` —
  единый source of truth.
* ``_FORBIDDEN_SYMBOLS`` — символы, запрещённые в production.
* ``_FORBIDDEN_FILES`` — файлы, которые были удалены и не должны быть
  воссозданы.
* ``_LEGACY_CONFIG_KEYS`` — legacy-ключи в ``project.json``.
* ``_ALLOWED_LEGACY_TESTS`` — **test-level allow-list** (Этап C):
  словарь ``{file.py::test_function_name: rationale}``. Allow-list
  применяется **только к конкретным тестовым функциям**, а не ко
  всему файлу. Это позволяет правильным characterization-тестам
  вроде ``test_legacy_module_removed`` (с ``try: import X except
  ImportError: pass``) проходить guard, но **блокирует любой новый
  код**, который попытается импортировать удалённый модуль в
  production-логике внутри test-файла.

Config-level guard: ``project.json::gateway.vector_index`` (legacy →
``gateway.vector.index``). Это Type E — fail-fast, без нормализации.
"""

from __future__ import annotations

import ast
import json
import pathlib
from collections import defaultdict

_FORBIDDEN_MODULES = frozenset({
    # Из исходного реестра legacy_audit (минимальный набор):
    "workspace.skills.legal_summarizer.scripts.document_cleanup",
    "workspace.skills.legal_summarizer.scripts.packing",
    "workspace.skills.legal_summarizer.scripts.packing_impl",
    "workspace.skills.legal_summarizer.scripts.packing_models",
    "workspace.skills.legal_summarizer.scripts.structure.compatibility",
    "workspace.skills.legal_summarizer.scripts.brief_representation",
    "workspace.skills.legal_summarizer.scripts.document_stats",
    # Добавлено в Этап B для унификации с test_legal_summarizer_no_legacy:
    "workspace.skills.legal_summarizer.scripts.fingerprint",
    "workspace.skills.legal_summarizer.scripts.reducer_strategy",
    "workspace.skills.legal_summarizer.scripts.cached_retrieval",
    "workspace.skills.legal_summarizer.scripts.document_cache",
    "workspace.skills.legal_summarizer.scripts.structure.sections",
    "workspace.skills.legal_summarizer.scripts.structure.tree",
    "workspace.skills.legal_summarizer.scripts.structure.cleanup",
    "workspace.skills.legal_summarizer.scripts.brief_strategy",
    "workspace.skills.legal_summarizer.scripts.provenance_reconstruction",
    "workspace.skills.legal_summarizer.scripts.token_budget",
    "workspace.skills.legal_summarizer.scripts.execution_strategy",
    "workspace.skills.legal_summarizer.scripts.reducer_models",
    "workspace.skills.legal_summarizer.scripts.reducer_impl",
})

_FORBIDDEN_SYMBOLS = frozenset({
    "SectionTree",
    "DocumentSection",
    "StructureAwareChunker",
    "build_section_tree",
    "merge_short_sections",
    "extract_local_structure_label",
    "count_meaningful_sections",
    "should_use_hierarchical_reduce",
    "select_reduce_strategy",
    "section_tree_from_structure",
    "structure_from_section_tree",
    "reduce_strategy_for_legacy",
    "execution_strategy_for_legacy",
    # Этап 12: load_physical_document — legacy loader; canonical —
    # DocumentLoader.load().
    "load_physical_document",
})

# Файлы, которые были удалены и не должны появиться снова (Этап 8).
_FORBIDDEN_FILES = frozenset({
    "workspace/skills/legal_summarizer/scripts/structure/cleanup.py",
    "workspace/skills/legal_summarizer/scripts/_legacy_run_map_reduce.py",
    "workspace/skills/legal_summarizer/scripts/document_cleanup.py",
    "workspace/skills/legal_summarizer/scripts/fingerprint.py",
    "workspace/skills/legal_summarizer/scripts/token_budget.py",
    "workspace/skills/legal_summarizer/scripts/reducer_strategy.py",
    "workspace/skills/legal_summarizer/scripts/document_cache.py",
    "workspace/skills/legal_summarizer/scripts/brief_strategy.py",
    "workspace/skills/legal_summarizer/scripts/brief_representation.py",
    "workspace/skills/legal_summarizer/scripts/provenance_reconstruction.py",
    "workspace/skills/legal_summarizer/scripts/document_stats.py",
    "workspace/skills/legal_summarizer/scripts/execution_strategy.py",
    "workspace/skills/legal_summarizer/scripts/reducer_models.py",
    "workspace/skills/legal_summarizer/scripts/reducer_impl.py",
    "workspace/skills/legal_summarizer/scripts/packing.py",
    "workspace/skills/legal_summarizer/scripts/packing_impl.py",
    "workspace/skills/legal_summarizer/scripts/packing_models.py",
    "workspace/skills/legal_summarizer/scripts/structure/sections.py",
    "workspace/skills/legal_summarizer/scripts/structure/tree.py",
    "workspace/skills/legal_summarizer/scripts/structure/compatibility.py",
    "workspace/skills/legal_summarizer/scripts/cached_retrieval.py",
    "workspace/skills/legal_summarizer/scripts/chunking/block_ownership.py",
    "workspace/skills/legal_summarizer/scripts/summarizer.py",
    "workspace/skills/legal_summarizer/scripts/manifest.py",
    "workspace/skills/legal_summarizer/scripts/output.py",
    "workspace/skills/legal_summarizer/scripts/skill_config.py",
})

# Legacy-ключи в project.json (Type E — fail-fast).
_LEGACY_CONFIG_KEYS = frozenset({
    "gateway.vector_index",
})

# Test-level allow-list (Этап C remediation): конкретные тестовые
# функции, которые НАМЕРЕННО импортируют удалённые legacy-модули
# (например, ``test_legacy_module_removed`` с ``try: import X except
# ImportError: ...``). Allow-list применяется к **конкретной тестовой
# функции**, а не ко всему файлу — это не позволяет добавлять
# production-логику с legacy-импортами в allow-listed файл.
#
# Формат: ``"tests/test_X.py::test_function_name"``.
# Значение — текстовое обоснование (для документации и аудита).
_ALLOWED_LEGACY_TESTS: dict[str, str] = {
    # Positive removal tests — проверяют, что удалённые модули
    # действительно недоступны. Это правильная форма characterization
    # для удалённой поверхности.
    "workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py::test_compatibility_adapter_removed":
        "позитивная проверка: модуль compatibility.py должен быть удалён",
    "workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py::test_legacy_reducer_strategy_removed":
        "позитивная проверка: модуль reducer_strategy должен быть удалён",
    "workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py::test_legacy_token_budget_removed":
        "позитивная проверка: модуль token_budget должен быть удалён",
    "workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py::test_legacy_brief_strategy_removed":
        "позитивная проверка: модуль brief_strategy должен быть удалён",
    "workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py::test_legacy_document_cache_removed":
        "позитивная проверка: модуль document_cache должен быть удалён",
    "workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py::test_legacy_fingerprint_removed":
        "позитивная проверка: модуль fingerprint должен быть удалён",
    "workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py::test_legacy_structure_sections_removed":
        "позитивная проверка: модуль structure.sections должен быть удалён",
    "workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py::test_legacy_structure_tree_removed":
        "позитивная проверка: модуль structure.tree должен быть удалён",
    # Characterization tests в tests/test_skill_legal_summarizer_characterization.py
    # (Этап E remediation, 2026-09-09) — positive removal tests после удаления
    # 75 orphaned characterization тестов.
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_should_use_hierarchical_removed":
        "позитивная проверка: модуль reducer должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_document_cleanup_removed":
        "позитивная проверка: модуль document_cleanup должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_packing_removed":
        "позитивная проверка: модуль packing должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_packing_impl_removed":
        "позитивная проверка: модуль packing_impl должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_packing_models_removed":
        "позитивная проверка: модуль packing_models должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_document_stats_removed":
        "позитивная проверка: модуль document_stats должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_token_budget_removed":
        "позитивная проверка: модуль token_budget должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_fingerprint_removed":
        "позитивная проверка: модуль fingerprint должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_document_cache_removed":
        "позитивная проверка: модуль document_cache должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_execution_strategy_removed":
        "позитивная проверка: модуль execution_strategy должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_reducer_strategy_removed":
        "позитивная проверка: модуль reducer_strategy должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_reducer_models_removed":
        "позитивная проверка: модуль reducer_models должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_reducer_impl_removed":
        "позитивная проверка: модуль reducer_impl должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_brief_strategy_removed":
        "позитивная проверка: модуль brief_strategy должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_brief_representation_removed":
        "позитивная проверка: модуль brief_representation должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_provenance_reconstruction_removed":
        "позитивная проверка: модуль provenance_reconstruction должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_cached_retrieval_removed":
        "позитивная проверка: модуль cached_retrieval должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_structure_sections_removed":
        "позитивная проверка: модуль structure.sections должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_structure_tree_removed":
        "позитивная проверка: модуль structure.tree должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_structure_compatibility_removed":
        "позитивная проверка: модуль structure.compatibility должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_structure_cleanup_removed":
        "позитивная проверка: модуль structure.cleanup должен быть удалён",
    "tests/test_skill_legal_summarizer_characterization.py::test_legacy_chunking_block_ownership_removed":
        "позитивная проверка: модуль chunking.block_ownership должен быть удалён",
}


def _iter_python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return [
        p for p in root.rglob("*.py")
        if "__pycache__" not in str(p)
    ]


def _is_test_file(rel: str) -> bool:
    """Является ли путь тестовым файлом (не production)."""
    return "/tests/" in f"/{rel}" or "/test_" in rel or rel.startswith("tests/")


def _is_allowed_legacy_test(rel: str, line_no: int | None = None) -> bool:
    """Является ли legacy reference в файле allow-listed для теста.

    Args:
        rel: относительный путь к .py файлу.
        line_no: номер строки legacy reference. Если None — проверяем
            только file-level (для file-level references вроде
            ``_FORBIDDEN_FILES``).

    Returns:
        True если файл+строка (или файл без line_no) — в allow-list.
    """
    if not _is_test_file(rel):
        return False
    # Без line_no — file-level allow-list (для _FORBIDDEN_FILES).
    if line_no is None:
        # File-level allow-list: проверяем, что в этом файле есть хотя бы
        # одна allow-listed тест-функция.
        rel_posix = rel.replace("\\", "/")
        for key in _ALLOWED_LEGACY_TESTS:
            if key.startswith(rel_posix + "::"):
                return True
        return False
    # С line_no — test-level allow-list.
    rel_posix = rel.replace("\\", "/")
    key = f"{rel_posix}::{line_no}"
    return key in _ALLOWED_LEGACY_TESTS


def audit_legacy_in_module(
    py_file: pathlib.Path,
    project_root: pathlib.Path,
) -> dict[str, list[str]]:
    """Найти legacy references в одном .py файле (через AST)."""
    try:
        source = py_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    hits: dict[str, list[str]] = defaultdict(list)
    rel = py_file.relative_to(project_root).as_posix()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in _FORBIDDEN_MODULES:
                for n in node.names:
                    hits[node.module].append(
                        f"{rel}:{node.lineno} from {node.module} import {n.name}",
                    )
        elif isinstance(node, ast.Import):
            for n in node.names:
                if n.name in _FORBIDDEN_MODULES:
                    hits[n.name].append(
                        f"{rel}:{node.lineno} import {n.name}",
                    )
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_SYMBOLS:
                hits[node.id].append(
                    f"{rel}:{node.lineno} name: {node.id}",
                )
        elif isinstance(node, ast.Attribute):
            if node.attr in _FORBIDDEN_SYMBOLS:
                hits[node.attr].append(
                    f"{rel}:{node.lineno} attr: .{node.attr}",
                )
    return dict(hits)


def audit(skill_root: pathlib.Path | None = None) -> dict[str, list[str]]:
    """Запустить audit по всему проекту.

    Сканирует ВСЕ .py файлы репозитория (не только legal_summarizer);
    каталоги ``__pycache__``, ``.venv``, ``data_store`` исключены.
    Это регрессионный guard для всех зарегистрированных forbidden
    modules/symbols/files — независимо от того, в какой подсистеме
    они встретились.

    Args:
        skill_root: legacy-параметр для обратной совместимости со
            старыми вызывающими (см. ``tests/test_legal_summarizer_no_legacy``).
            Игнорируется: audit покрывает весь проект.

    Returns:
        dict: ``{module_or_symbol: [hit_description, ...]}``.
    """
    project_root = pathlib.Path.cwd()
    all_hits: dict[str, list[str]] = defaultdict(list)

    for py_file in _iter_python_files(project_root):
        if any(part in py_file.parts for part in (
            "__pycache__", ".venv", "data_store",
            ".pytest_cache", ".ruff_cache", ".benchmarks",
            "node_modules",
        )):
            continue
        hits = audit_legacy_in_module(py_file, project_root)
        for k, v in hits.items():
            all_hits[k].extend(v)

    return dict(all_hits)


def _is_production_file(rel: str) -> bool:
    return not _is_test_file(rel)


def assert_no_legacy() -> None:
    """Поднять AssertionError если production содержит legacy hits.

    Учитывает **test-level allow-list** (Этап C remediation):
    production-файлы всегда блокируются; test-файлы блокируются,
    если legacy reference не входит в ``_ALLOWED_LEGACY_TESTS``
    для конкретной строки.

    Returns:
        None если всё чисто.

    Raises:
        AssertionError: список production hits, test-only hits вне
            allow-list, наличие запрещённых файлов или legacy config
            ключей.
    """
    hits = audit()
    production_hits: dict[str, list[str]] = {}
    test_blocking_hits: dict[str, list[str]] = {}
    for k, locations in hits.items():
        for loc in locations:
            rel, _, lineno_str = loc.partition(":")
            lineno_str2, _, _ = lineno_str.partition(":")
            try:
                line_no = int(lineno_str2)
            except ValueError:
                line_no = None
            if _is_production_file(rel):
                production_hits.setdefault(k, []).append(loc)
            elif not _is_allowed_legacy_test(rel, line_no):
                test_blocking_hits.setdefault(k, []).append(loc)

    # Проверка _FORBIDDEN_FILES (Этап 8 / Этап 18): файл не должен
    # существовать на диске. Применяется всегда (даже для test-файлов).
    project_root = pathlib.Path.cwd()
    forbidden_present: list[str] = []
    for rel_path in _FORBIDDEN_FILES:
        if (project_root / rel_path).is_file():
            forbidden_present.append(rel_path)

    # Config-level guard (Этап 11): project.json не должен содержать
    # legacy-секций.
    config_legacy: list[str] = []
    cfg_path = project_root / "project.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            gw = cfg.get("gateway") or {}
            if isinstance(gw, dict) and "vector_index" in gw:
                config_legacy.append(
                    "project.json::gateway.vector_index (legacy → gateway.vector.index)"
                )
        except (OSError, json.JSONDecodeError):
            pass

    errors: list[str] = []
    if production_hits:
        details = "\n".join(
            f"  {k}: {len(v)} refs\n    " + "\n    ".join(v[:3])
            for k, v in sorted(production_hits.items())
        )
        errors.append(
            f"Found {sum(len(v) for v in production_hits.values())} "
            f"production legacy references:\n{details}"
        )
    if test_blocking_hits:
        details = "\n".join(
            f"  {k}: {len(v)} refs\n    " + "\n    ".join(v[:3])
            for k, v in sorted(test_blocking_hits.items())
        )
        errors.append(
            f"Found {sum(len(v) for v in test_blocking_hits.values())} "
            f"test-only legacy references (NOT in allow-list):\n{details}"
        )
    if forbidden_present:
        errors.append(
            "Forbidden files present:\n  "
            + "\n  ".join(sorted(forbidden_present))
        )
    if config_legacy:
        errors.append(
            "Legacy config keys present:\n  "
            + "\n  ".join(sorted(config_legacy))
        )

    if errors:
        raise AssertionError("\n\n".join(errors))


def main() -> None:
    hits = audit()
    if not hits:
        print("No legacy references found.")
        return

    production_hits: dict[str, list[str]] = {}
    test_blocking_hits: dict[str, list[str]] = {}
    test_allowed_hits: dict[str, list[str]] = {}
    for k, locations in hits.items():
        for loc in locations:
            rel, _, lineno_str = loc.partition(":")
            lineno_str2, _, _ = lineno_str.partition(":")
            try:
                line_no = int(lineno_str2)
            except ValueError:
                line_no = None
            if _is_production_file(rel):
                production_hits.setdefault(k, []).append(loc)
            elif _is_allowed_legacy_test(rel, line_no):
                test_allowed_hits.setdefault(k, []).append(loc)
            else:
                test_blocking_hits.setdefault(k, []).append(loc)

    print("=" * 70)
    print("LEGACY REFERENCE AUDIT (whole repo)")
    print("=" * 70)
    print()
    print(f"Production legacy references:           "
          f"{sum(len(v) for v in production_hits.values())}")
    print(f"Test-only blocking (NOT in allow-list): "
          f"{sum(len(v) for v in test_blocking_hits.values())}")
    print(f"Test-only allowed (characterization):   "
          f"{sum(len(v) for v in test_allowed_hits.values())}")
    print()
    if production_hits:
        print("-" * 70)
        print("PRODUCTION REFERENCES (must be migrated):")
        print("-" * 70)
        for k in sorted(production_hits):
            print(f"\n{k}:")
            for loc in production_hits[k]:
                print(f"  {loc}")
    if test_blocking_hits:
        print()
        print("-" * 70)
        print("TEST-ONLY BLOCKING (NOT in allow-list — must be fixed):")
        print("-" * 70)
        for k in sorted(test_blocking_hits):
            print(f"\n{k}:")
            for loc in test_blocking_hits[k]:
                print(f"  {loc}")
    if test_allowed_hits:
        print()
        print("-" * 70)
        print("TEST-ONLY ALLOWED (in _ALLOWED_LEGACY_TESTS):")
        print("-" * 70)
        for k in sorted(test_allowed_hits):
            print(f"\n{k}: {len(test_allowed_hits[k])} references")


__all__ = [
    "audit",
    "audit_legacy_in_module",
    "assert_no_legacy",
    "_FORBIDDEN_MODULES",
    "_FORBIDDEN_SYMBOLS",
    "_FORBIDDEN_FILES",
    "_LEGACY_CONFIG_KEYS",
    "_ALLOWED_LEGACY_TESTS",
]
