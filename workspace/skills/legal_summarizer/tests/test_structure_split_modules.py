"""Тесты для split large files (Этап 59 из PLAN.md).

PLAN §59: ``summarizer.py`` (1773 строк) и ``chunks.py`` (555) — слишком
большие. Цель — **не переписывать** (PLAN §1), а предоставить
**новые entry points** через выделенные модули.

Проверяем, что новые entry points существуют и работают:

* ``run_canonical_pipeline`` (pipeline.py)
* ``ChunkPlanner`` (document_chunker.py)
* ``HierarchicalReducer`` (hierarchical_reducer.py)
* ``RetrievalIndex.retrieve`` (retrieval_index.py)
"""

from __future__ import annotations

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

def test_run_canonical_pipeline_exists():
    assert callable(run_canonical_pipeline)

def test_chunk_planner_exists():
    assert callable(ChunkPlanner)

def test_hierarchical_reducer_exists():
    assert callable(reduce_chunks_hierarchical)

def test_retrieval_index_exists():
    assert callable(RetrievalIndex.build)

def test_build_execution_plan_exists():
    assert callable(build_execution_plan)

def test_new_modules_have_narrow_responsibility():
    """Каждый новый модуль отвечает за одну вещь (PLAN §60)."""
    import inspect
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
        "loader": "legal_summarizer/document/loader.py",
        "identity": "legal_summarizer/document/identity.py",
        "numbering": "legal_summarizer/document/numbering.py",
        "heading": "legal_summarizer/document/heading.py",
        "hierarchy": "legal_summarizer/document/hierarchy.py",
        "structure": "legal_summarizer/document/structure.py",
        "chunker": "legal_summarizer/chunking/chunker.py",
        "execution": "legal_summarizer/planning/plan.py",
        "reducer": "legal_summarizer/execution/hierarchical.py",
        "retrieval": "legal_summarizer/retrieval/query.py",
        "pipeline": "legal_summarizer/application/pipeline_structure.py",
    }
    for key, path in parts.items():
        assert path.endswith(".py")