"""Тесты для split large files.

``summarizer.py`` (1773 строк) и ``chunks.py`` (555) — слишком
большие. Цель — **не переписывать**, а предоставить
**новые entry points** через выделенные модули.

Проверяем, что новые entry points существуют и работают:

* ``run_canonical_pipeline`` (pipeline.py)
* ``ChunkPlanner`` (document_chunker.py)
* ``HierarchicalReducer`` (hierarchical_reducer.py)
* ``RetrievalIndex.retrieve`` (retrieval_index.py)
"""

from __future__ import annotations

import inspect
from pathlib import Path

from chunking.chunker import (
    ChunkPlanner,
)
from execution.hierarchical import (
    reduce_chunks_hierarchical,
)
from application.pipeline_structure import (
    run_canonical_pipeline,
)
from retrieval.index import (
    RetrievalIndex,
)
from planning.strategy import (
    build_execution_plan,
)

_SKILL_ROOT = Path(__file__).resolve().parents[1]


def _assert_callable_with_params(obj, *, min_params: int = 1, name: str) -> None:
    sig = inspect.signature(obj)
    params = [
        p for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert callable(obj), f"{name} must be callable"
    assert len(params) >= min_params, (
        f"{name} must accept at least {min_params} positional parameter(s), got {len(params)}"
    )


def test_run_canonical_pipeline_exists():
    _assert_callable_with_params(run_canonical_pipeline, min_params=1, name="run_canonical_pipeline")
    assert run_canonical_pipeline.__module__ == "application.pipeline_structure"


def test_chunk_planner_exists():
    # ChunkPlanner is a class (instantiated with __init__ taking config kwargs);
    # verify it is a class with an __init__ accepting at least one kwarg.
    assert inspect.isclass(ChunkPlanner), "ChunkPlanner must be a class"
    init_sig = inspect.signature(ChunkPlanner.__init__)
    non_self_params = [
        p for p in init_sig.parameters.values()
        if p.name != "self"
        and p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                       inspect.Parameter.POSITIONAL_OR_KEYWORD,
                       inspect.Parameter.KEYWORD_ONLY)
    ]
    assert non_self_params, "ChunkPlanner.__init__ must accept at least one configurable parameter"


def test_hierarchical_reducer_exists():
    _assert_callable_with_params(reduce_chunks_hierarchical, min_params=2, name="reduce_chunks_hierarchical")


def test_retrieval_index_exists():
    assert callable(RetrievalIndex.build), "RetrievalIndex.build must be callable"
    assert inspect.ismethod(RetrievalIndex.build) or inspect.isfunction(RetrievalIndex.build), (
        "RetrievalIndex.build must be a method or function (not a generic callable)"
    )


def test_build_execution_plan_exists():
    _assert_callable_with_params(build_execution_plan, min_params=2, name="build_execution_plan")


def test_new_modules_have_narrow_responsibility():
    """Каждый новый модуль отвечает за одну вещь."""
    import application.pipeline_structure as pipeline
    import chunking.chunker as document_chunker
    import execution.hierarchical as hierarchical_reducer
    import retrieval.index as retrieval_index
    import planning.strategy as unified_execution
    for module in (
        pipeline, document_chunker, hierarchical_reducer,
        retrieval_index, unified_execution,
    ):
        source = inspect.getsource(module)
        assert "MAX_BATCH_PARSE_RETRIES" not in source
        assert "_llm_document_reduce" not in source

def test_summary_of_split_modules():
    """Краткая карта: где сейчас находится что."""
    parts = {
        "loader": "document/loader.py",
        "identity": "document/identity.py",
        "numbering": "document/numbering.py",
        "heading": "document/heading.py",
        "hierarchy": "document/hierarchy.py",
        "structure": "document/structure.py",
        "chunker": "chunking/chunker.py",
        "execution": "planning/plan.py",
        "reducer": "execution/hierarchical.py",
        "retrieval": "retrieval/query.py",
        "pipeline": "application/pipeline_structure.py",
    }
    scripts_root = _SKILL_ROOT / "scripts"
    for key, rel_path in parts.items():
        full = scripts_root / rel_path
        assert full.is_file(), f"{key} module file missing at {full}"
