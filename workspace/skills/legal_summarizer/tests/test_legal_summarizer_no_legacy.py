"""Architecture guard: production-путь не должен использовать legacy symbols.

Тест импортирует ключевые production-модули и проверяет, что в их
**AST** нет legacy-зависимостей. Это **динамическая** проверка
(не grep), потому что grep не видит ленивые импорты внутри функций.

Комментарии и docstring игнорируются — анализируется только код.
"""

from __future__ import annotations

import ast


_LEGACY_MODULES = frozenset({
    "workspace.skills.legal_summarizer.scripts.fingerprint",
    "workspace.skills.legal_summarizer.scripts.reducer",
    "workspace.skills.legal_summarizer.scripts.reducer_impl",
    "workspace.skills.legal_summarizer.scripts.reducer_strategy",
    "workspace.skills.legal_summarizer.scripts.reducer_models",
    "workspace.skills.legal_summarizer.scripts.context_expansion",
    "workspace.skills.legal_summarizer.scripts.cached_retrieval",
    "workspace.skills.legal_summarizer.scripts.document_cache",
    "workspace.skills.legal_summarizer.scripts.document_cleanup",
    "workspace.skills.legal_summarizer.scripts.structure.sections",
    "workspace.skills.legal_summarizer.scripts.structure.tree",
    "workspace.skills.legal_summarizer.scripts.structure.compatibility",
    "workspace.skills.legal_summarizer.scripts.brief_strategy",
    "workspace.skills.legal_summarizer.scripts.brief_representation",
    "workspace.skills.legal_summarizer.scripts.cache_followup",
    "workspace.skills.legal_summarizer.scripts.provenance_reconstruction",
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


def _module_legacy_refs(module) -> list[str]:
    """Найти legacy-ссылки в AST модуля (не в комментариях/docstring)."""
    if module is None:
        return []
    try:
        source = _read_source(module)
    except (OSError, TypeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in _LEGACY_MODULES:
                hits.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for n in node.names:
                if n.name in _LEGACY_MODULES:
                    hits.append(f"import {n.name}")
        elif isinstance(node, ast.Name):
            if node.id in _LEGACY_SYMBOLS:
                hits.append(f"name: {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in _LEGACY_SYMBOLS:
                hits.append(f"attr: .{node.attr}")
    return hits


def _read_source(module) -> str:
    import inspect
    return inspect.getsource(module)


def test_summarizer_canonical_does_not_reference_legacy():
    """summarizer_canonical — единственная production-точка входа без legacy."""
    from workspace.skills.legal_summarizer.scripts import summarizer_canonical

    hits = _module_legacy_refs(summarizer_canonical)
    assert hits == [], (
        f"summarizer_canonical has unexpected legacy refs: {hits}"
    )


def test_canonical_pipeline_has_no_legacy_imports():
    """Все canonical-структурные модули не должны ссылаться на legacy."""
    from workspace.skills.legal_summarizer.scripts.structure import (
        document_loader,
        document_chunker,
        document_analysis,
        execution_plan,
        followup,
        hierarchical_reducer,
        pipeline,
        retrieval,
        retrieval_index,
        unified_execution,
    )

    for module in (
        document_loader,
        document_chunker,
        document_analysis,
        execution_plan,
        followup,
        hierarchical_reducer,
        pipeline,
        retrieval,
        retrieval_index,
        unified_execution,
    ):
        hits = _module_legacy_refs(module)
        assert hits == [], (
            f"{module.__name__} has unexpected legacy refs: {hits}"
        )


def test_compatibility_adapter_removed():
    """compatibility.py полностью удалён (Этап 20)."""
    try:
        from workspace.skills.legal_summarizer.scripts.structure import (
            compatibility,
        )
    except ImportError:
        return
    raise AssertionError("compatibility.py should be removed (Этап 20)")


def test_legacy_reducer_modules_still_present():
    """Legacy reducer_impl/reducer_strategy ещё существуют (миграция в процессе).

    Этот тест — **negative** assertion: показывает текущее состояние.
    ``reducer.py`` уже удалён.
    """
    from workspace.skills.legal_summarizer.scripts import reducer_impl
    from workspace.skills.legal_summarizer.scripts import reducer_strategy

    assert reducer_impl is not None
    assert reducer_strategy is not None
    assert reducer_strategy is not None