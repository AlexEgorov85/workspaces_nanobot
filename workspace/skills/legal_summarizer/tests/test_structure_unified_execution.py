"""Тесты для unified execution strategy (Этап 23 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
    ExecutionPolicy, build_execution_plan, select_strategy,
)


def _root(children: tuple[str, ...] = ()) -> StructureNode:
    return StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=children, start_block=0, end_block=10,
        confidence=1.0,
    )


def _sec(nid: str, title: str, *, start: int = 0, end: int = 5) -> StructureNode:
    return StructureNode(
        node_id=nid, node_type="section", semantic_type=None,
        level=1, title=title, number=None, parent_id="n_0000",
        children=(), start_block=start, end_block=end,
        confidence=0.7,
    )


def _chunk(cid: str, text: str = "x" * 100) -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text=text, char_count=len(text),
        token_estimate=10, page_start=1, page_end=1,
        section_id="s1", section_path="1", section_heading="x",
        block_indices=(0,), block_types=("paragraph",),
    )


def test_select_strategy_direct_small_doc():
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", "short"),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    chunks = (_chunk("001", "short"),)
    assert select_strategy(s, chunks) == "direct"


def test_select_strategy_map_flat_medium():
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", "x" * 200),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    chunks = tuple(_chunk(f"{i:03d}", "x" * 1000) for i in range(50))
    assert select_strategy(s, chunks) == "map_flat"


def test_select_strategy_hierarchical_many_sections():
    nodes = {"n_0000": _root(tuple(f"n_{i:04d}" for i in range(1, 6)))}
    for i in range(1, 6):
        nodes[f"n_{i:04d}"] = _sec(f"n_{i:04d}", f"Section {i} " + "x" * 200)
    s = DocumentStructure(
        document_id="d", title=None, nodes=nodes,
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    chunks = tuple(_chunk(f"{i:03d}", "x" * 1000) for i in range(50))
    assert select_strategy(s, chunks) == "map_hierarchical"


def test_select_strategy_custom_threshold():
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", "long " * 1000),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    chunks = tuple(_chunk(f"{i:03d}", "x" * 1000) for i in range(50))
    policy = ExecutionPolicy(direct_threshold_tokens=100_000)
    assert select_strategy(s, chunks, policy=policy) == "direct"


def test_build_execution_plan_direct():
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", "tiny"),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    chunks = (_chunk("001", "x" * 100),)
    plan = build_execution_plan(s, chunks, document_id="d")
    assert plan.strategy == "direct"
    assert plan.total_batches == 1


def test_build_execution_plan_map():
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", "long " * 200),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    chunks = tuple(_chunk(f"{i:03d}", "x" * 1000) for i in range(50))
    plan = build_execution_plan(s, chunks, document_id="d")
    assert plan.strategy in ("map_flat", "map_hierarchical")