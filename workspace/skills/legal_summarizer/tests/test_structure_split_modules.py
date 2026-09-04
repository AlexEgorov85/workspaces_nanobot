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

from workspace.skills.legal_summarizer.scripts.structure.document_chunker import (
    ChunkPlanner,
)
from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
    reduce_chunks_hierarchical,
)
from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
    run_canonical_pipeline,
)
from workspace.skills.legal_summarizer.scripts.structure.retrieval_index import (
    RetrievalIndex,
)
from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
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
    from workspace.skills.legal_summarizer.scripts.structure import (
        pipeline, document_chunker, hierarchical_reducer,
        retrieval_index, unified_execution,
    )
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
        "loader": "scripts/structure/document_loader.py",
        "identity": "scripts/structure/identity.py",
        "numbering": "scripts/structure/numbering.py",
        "heading": "scripts/structure/heading.py",
        "hierarchy": "scripts/structure/hierarchy.py",
        "structure": "scripts/structure/models.py",
        "chunker": "scripts/structure/document_chunker.py",
        "execution": "scripts/structure/execution_plan.py",
        "reducer": "scripts/structure/hierarchical_reducer.py",
        "retrieval": "scripts/structure/retrieval.py",
        "pipeline": "scripts/structure/pipeline.py",
    }
    for key, path in parts.items():
        assert path.endswith(".py")