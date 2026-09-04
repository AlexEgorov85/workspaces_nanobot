"""Zero-reference audit: найти все legacy references в коде.

Скрипт (не pytest-тест) — обходит всю кодовую базу и печатает
production references на legacy-символы. Полезен для отслеживания
оставшихся legacy-зависимостей в canonical-пути.

Запуск: ``python workspace/skills/legal_summarizer/scripts/legacy_audit.py``
"""

from __future__ import annotations

import ast
import pathlib
from collections import defaultdict

_LEGACY_MODULES = frozenset({
    "workspace.skills.legal_summarizer.scripts.fingerprint",
    "workspace.skills.legal_summarizer.scripts.reducer_strategy",
    "workspace.skills.legal_summarizer.scripts.document_cache",
    "workspace.skills.legal_summarizer.scripts.document_cleanup",
    "workspace.skills.legal_summarizer.scripts.structure.sections",
    "workspace.skills.legal_summarizer.scripts.structure.tree",
    "workspace.skills.legal_summarizer.scripts.structure.compatibility",
    "workspace.skills.legal_summarizer.scripts.brief_strategy",
    "workspace.skills.legal_summarizer.scripts.brief_representation",
    "workspace.skills.legal_summarizer.scripts.packing",
    "workspace.skills.legal_summarizer.scripts.packing_impl",
    "workspace.skills.legal_summarizer.scripts.packing_models",
    "workspace.skills.legal_summarizer.scripts.token_budget",
})

_LEGACY_SYMBOLS = frozenset({
    "SectionTree",
    "DocumentSection",
    "StructureAwareChunker",
    "build_section_tree",
    "merge_short_sections",
    "extract_local_structure_label",
    "count_meaningful_sections",
    "should_use_hierarchical_reduce",
    "select_reduce_strategy",
    "load_physical_document",
    "section_tree_from_structure",
    "structure_from_section_tree",
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


def _module_to_path(mod_name: str, root: pathlib.Path) -> pathlib.Path | None:
    parts = mod_name.split(".")
    path = root.joinpath(*parts).with_suffix(".py")
    if path.is_file():
        return path
    init = root.joinpath(*parts, "__init__.py")
    if init.is_file():
        return init
    return None


def audit_legacy_in_module(
    py_file: pathlib.Path,
    project_root: pathlib.Path,
) -> dict[str, list[str]]:
    """Найти legacy references в одном .py файле (через AST).

    Returns:
        dict: ``{module_or_symbol: [hit_description, ...]}``.
    """
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
            if node.module in _LEGACY_MODULES:
                for n in node.names:
                    hits[node.module].append(
                        f"{rel}:{node.lineno} from {node.module} import {n.name}",
                    )
        elif isinstance(node, ast.Import):
            for n in node.names:
                if n.name in _LEGACY_MODULES:
                    hits[n.name].append(
                        f"{rel}:{node.lineno} import {n.name}",
                    )
        elif isinstance(node, ast.Name):
            if node.id in _LEGACY_SYMBOLS:
                hits[node.id].append(
                    f"{rel}:{node.lineno} name: {node.id}",
                )
        elif isinstance(node, ast.Attribute):
            if node.attr in _LEGACY_SYMBOLS:
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


if __name__ == "__main__":
    main()
