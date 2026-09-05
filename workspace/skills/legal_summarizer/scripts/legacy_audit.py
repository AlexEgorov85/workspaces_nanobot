"""Zero-reference audit: regression guard для legacy symbols (PLAN §34).

Это **regression guard**, не просто print. Два режима:

* ``audit()`` — возвращает structured result (dict с hits).
* ``assert_no_legacy()`` — поднимает ``AssertionError`` при production hit
  или при наличии запрещённых файлов (Этап 18).

Разделение:

* ``_FORBIDDEN_MODULES`` — модули, запрещённые в production:
  ``document_cleanup``, ``fingerprint``, ``document_cache``,
  ``token_budget``, ``packing``, ``structure.sections``, ``structure.tree``.

* ``_FORBIDDEN_SYMBOLS`` — символы, запрещённые в production:
  ``SectionTree``, ``DocumentSection``, ``StructureAwareChunker``,
  ``build_section_tree``, ``merge_short_sections``,
  ``extract_local_structure_label``, ``count_meaningful_sections``,
  ``should_use_hierarchical_reduce``, ``select_reduce_strategy``,
  ``section_tree_from_structure``, ``structure_from_section_tree``,
  ``load_physical_document`` (Этап 12 — canonical: ``DocumentLoader.load()``).

* ``_FORBIDDEN_FILES`` — файлы, которые были удалены и не должны быть
  воссозданы (Этап 8, Этап 18).
"""

from __future__ import annotations

import ast
import pathlib
from collections import defaultdict

_FORBIDDEN_MODULES = frozenset({
    "workspace.skills.legal_summarizer.scripts.document_cleanup",
    "workspace.skills.legal_summarizer.scripts.packing",
    "workspace.skills.legal_summarizer.scripts.packing_impl",
    "workspace.skills.legal_summarizer.scripts.packing_models",
    "workspace.skills.legal_summarizer.scripts.structure.compatibility",
    "workspace.skills.legal_summarizer.scripts.brief_representation",
    "workspace.skills.legal_summarizer.scripts.document_stats",
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
})

_CANONICAL_PRODUCTION = frozenset({
    "workspace.skills.legal_summarizer.scripts.summarizer",
    "workspace.skills.legal_summarizer.scripts.summarizer_canonical",
    "workspace.skills.legal_summarizer.scripts.cli",
    "workspace.skills.legal_summarizer.scripts.cli_query",
})


def _iter_python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return [
        p for p in root.rglob("*.py")
        if "__pycache__" not in str(p)
    ]


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


def audit() -> dict[str, list[str]]:
    """Запустить audit по всему проекту.

    Returns:
        dict: ``{module_or_symbol: [hit_description, ...]}``.
    """
    project_root = pathlib.Path.cwd()
    skill_root = project_root / "workspace" / "skills" / "legal_summarizer"
    all_hits: dict[str, list[str]] = defaultdict(list)

    for py_file in _iter_python_files(skill_root):
        hits = audit_legacy_in_module(py_file, project_root)
        for k, v in hits.items():
            all_hits[k].extend(v)

    return dict(all_hits)


def _is_production_file(rel: str) -> bool:
    return "/tests/" not in f"/{rel}" and "/test_" not in rel


def assert_no_legacy() -> None:
    """Поднять AssertionError если production содержит legacy hits.

    Returns:
        None если всё чисто.

    Raises:
        AssertionError: список production hits или наличие
        запрещённых файлов.
    """
    hits = audit()
    production_hits: dict[str, list[str]] = {}
    for k, locations in hits.items():
        for loc in locations:
            rel = loc.split(":", 1)[0]
            if _is_production_file(rel):
                production_hits.setdefault(k, []).append(loc)

    # Проверка _FORBIDDEN_FILES (Этап 8 / Этап 18): файл не должен
    # существовать на диске.
    project_root = pathlib.Path.cwd()
    forbidden_present: list[str] = []
    for rel_path in _FORBIDDEN_FILES:
        if (project_root / rel_path).is_file():
            forbidden_present.append(rel_path)

    if production_hits:
        details = "\n".join(
            f"  {k}: {len(v)} refs\n    " + "\n    ".join(v[:3])
            for k, v in sorted(production_hits.items())
        )
        raise AssertionError(
            f"Found {sum(len(v) for v in production_hits.values())} "
            f"production legacy references:\n{details}"
        )
    if forbidden_present:
        raise AssertionError(
            f"Forbidden files present:\n  "
            + "\n  ".join(sorted(forbidden_present))
        )


def main() -> None:
    hits = audit()
    if not hits:
        print("No legacy references found.")
        return

    production_hits: dict[str, list[str]] = {}
    test_hits: dict[str, list[str]] = {}
    for k, locations in hits.items():
        for loc in locations:
            rel = loc.split(":", 1)[0]
            if _is_production_file(rel):
                production_hits.setdefault(k, []).append(loc)
            else:
                test_hits.setdefault(k, []).append(loc)

    print("=" * 70)
    print("LEGACY REFERENCE AUDIT")
    print("=" * 70)
    print()
    print(f"Production legacy references: {sum(len(v) for v in production_hits.values())}")
    print(f"Test-only legacy references:   {sum(len(v) for v in test_hits.values())}")
    print()
    if production_hits:
        print("-" * 70)
        print("PRODUCTION REFERENCES (must be migrated):")
        print("-" * 70)
        for k in sorted(production_hits):
            print(f"\n{k}:")
            for loc in production_hits[k]:
                print(f"  {loc}")
    if test_hits:
        print()
        print("-" * 70)
        print("TEST REFERENCES (informational):")
        print("-" * 70)
        for k in sorted(test_hits):
            print(f"\n{k}: {len(test_hits[k])} references")


__all__ = [
    "audit",
    "audit_legacy_in_module",
    "assert_no_legacy",
]