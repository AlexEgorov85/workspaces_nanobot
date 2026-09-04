"""Benchmark DIRECT threshold (PLAN §28, Этап 28).

Сравнивает метрики разных размеров документов для unified execution
planner'а:

* small (≤ direct_threshold_tokens) → ``direct``, 1 LLM-вызов.
* medium (> direct_threshold, < sections threshold) → ``map_flat``.
* large (> direct_threshold, ≥ sections threshold) → ``map_hierarchical``.

Метрики:

* ``llm_calls`` (map + reduce).
* ``input_tokens`` (сумма токенов для map-вызовов).
* ``batches`` (число map-вызовов).

Сравнение с legacy baseline (из SKILL.md Acceptance matrix):

* Small (≤12000 chars) → 1 LLM-вызов (direct).
* Medium default → 2 LLM-вызова.
* Large default → 17 LLM-вызовов.
"""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator, TokenEstimatorConfig,
)
from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
    ExecutionPolicy,
    build_execution_plan,
    select_strategy,
)


def _root(children: tuple[str, ...] = ()) -> StructureNode:
    return StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=children, start_block=0, end_block=10,
        confidence=1.0,
    )


def _sec(nid: str, title: str = "Section") -> StructureNode:
    return StructureNode(
        node_id=nid, node_type="section", semantic_type=None,
        level=1, title=title, number=None, parent_id="n_0000",
        children=(), start_block=0, end_block=10,
        confidence=0.7,
    )


def _chunk(cid: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text=text, char_count=len(text),
        token_estimate=10, page_start=1, page_end=1,
        section_id="s1", section_path="1", section_heading="x",
        block_indices=(0,), block_types=("paragraph",),
    )


def _build_small_doc():
    """~3000 chars (≤12000 threshold) → direct."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": _root(("n_0001",)), "n_0001": _sec("n_0001")},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    chunks = (_chunk("001", "x" * 3000),)
    return s, chunks


def _build_medium_doc():
    """~50k chars (> direct_threshold, 1 section) → map_flat."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": _root(("n_0001",)), "n_0001": _sec("n_0001")},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    chunks = tuple(_chunk(f"{i:03d}", "x" * 1000) for i in range(50))
    return s, chunks


def _build_large_doc():
    """~200k chars, 6 sections → map_hierarchical."""
    nodes = {"n_0000": _root(tuple(f"n_{i:04d}" for i in range(1, 7)))}
    for i in range(1, 7):
        nodes[f"n_{i:04d}"] = _sec(f"n_{i:04d}", f"Section {i}")
    s = DocumentStructure(
        document_id="d", title=None, nodes=nodes,
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    chunks = tuple(_chunk(f"{i:03d}", "x" * 1000) for i in range(200))
    return s, chunks


def test_small_doc_direct_strategy():
    s, chunks = _build_small_doc()
    strategy = select_strategy(s, chunks)
    assert strategy == "direct"


def test_medium_doc_map_flat():
    s, chunks = _build_medium_doc()
    strategy = select_strategy(s, chunks)
    assert strategy == "map_flat"


def test_large_doc_map_hierarchical():
    s, chunks = _build_large_doc()
    strategy = select_strategy(s, chunks)
    assert strategy == "map_hierarchical"


def test_direct_call_count_one():
    """Small doc → exactly 1 LLM call."""
    s, chunks = _build_small_doc()
    plan = build_execution_plan(s, chunks, document_id="d")
    assert plan.estimated_llm_calls == 1
    assert plan.total_batches == 1


def test_medium_doc_call_count():
    s, chunks = _build_medium_doc()
    plan = build_execution_plan(s, chunks, document_id="d")
    assert plan.estimated_llm_calls >= 2
    assert plan.strategy == "map_flat"


def test_large_doc_hierarchical():
    s, chunks = _build_large_doc()
    plan = build_execution_plan(s, chunks, document_id="d")
    assert plan.strategy == "map_hierarchical"


def test_token_estimator_consistent():
    """Estimator даёт одинаковые значения для одного текста."""
    estimator = TokenEstimator(TokenEstimatorConfig(chars_per_token=3.5))
    text = "x" * 1000
    assert estimator.estimate(text) == estimator.estimate(text)


def test_execution_plan_budget_constant_for_repeated_plans():
    """Один и тот же документ → один план (PLAN §75 deterministic)."""
    s, chunks = _build_medium_doc()
    p1 = build_execution_plan(s, chunks, document_id="d")
    p2 = build_execution_plan(s, chunks, document_id="d")
    assert p1.total_batches == p2.total_batches
    assert p1.total_chunks == p2.total_chunks
    assert p1.strategy == p2.strategy


def test_direct_threshold_lower_reduces_call_count():
    """Снижение direct_threshold должно переводить medium в map."""
    s, chunks = _build_medium_doc()
    high_policy = ExecutionPolicy(direct_threshold_tokens=50_000)
    low_policy = ExecutionPolicy(direct_threshold_tokens=1000)
    plan_high = build_execution_plan(s, chunks, document_id="d", policy=high_policy)
    plan_low = build_execution_plan(s, chunks, document_id="d", policy=low_policy)
    assert plan_high.strategy == "direct"
    assert plan_low.strategy in ("map_flat", "map_hierarchical")


def test_adjacent_packing_reduces_batches_vs_legacy():
    """Adjacent packing уменьшает число batches vs naive section-locality."""
    from workspace.skills.legal_summarizer.scripts.structure.adjacent_packing import (
        AdjacentPackingConfig, pack_chunks_with_adjacent,
    )

    chunks = tuple(_chunk(f"{i:03d}", "x" * 1000) for i in range(20))
    batches = pack_chunks_with_adjacent(
        chunks, config=AdjacentPackingConfig(
            max_sections_per_batch=2,
            per_batch_token_budget=4000,
        ),
    )
    assert len(batches) < len(chunks)