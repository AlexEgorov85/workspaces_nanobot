"""Acceptance tests для Этапа 7: ExecutionPolicy действительно влияет на план."""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _simple_struct_with_sections(n_sections: int):
    """Структура с N секциями."""
    from workspace.skills.legal_summarizer.scripts.structure.models import (
        DocumentStructure, StructureNode,
    )

    nodes: dict[str, StructureNode] = {}
    root = StructureNode(
        node_id="n_0000",
        node_type="document",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=tuple(f"n_{i + 1:04d}" for i in range(n_sections)),
        start_block=0,
        end_block=n_sections * 2,
        confidence=1.0,
    )
    nodes["n_0000"] = root
    for i in range(n_sections):
        nid = f"n_{i + 1:04d}"
        nodes[nid] = StructureNode(
            node_id=nid,
            node_type="section",
            semantic_type=None,
            level=1,
            title=f"S{i}",
            number=None,
            parent_id="n_0000",
            children=(),
            start_block=i * 2,
            end_block=i * 2 + 1,
            confidence=1.0,
        )
    return DocumentStructure(
        document_id="t",
        title=None,
        nodes=nodes,
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=n_sections * 2,
        coverage_ratio=1.0,
    )


class _FakeChunk:
    def __init__(self, chunk_id: str, section_id: str, text: str):
        self.chunk_id = chunk_id
        self.section_id = section_id
        self.section_path = ""
        self.section_heading = ""
        self.page_start = None
        self.page_end = None
        self.char_count = len(text)
        self.table_id = None
        self.text = text


def _chunks_for(struct):
    """Один chunk на каждую секцию."""
    chunks = []
    for n in struct.iter_sections():
        chunks.append(_FakeChunk(
            chunk_id=n.node_id.replace("n_", "c_"),
            section_id=n.node_id,
            text="x" * 3_500,
        ))
    return tuple(chunks)


def test_policy_max_sections_per_batch_changes_plan():
    """Изменение ``max_sections_per_batch`` меняет реальный план."""
    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        ExecutionPolicy,
        build_execution_plan,
    )

    # 6 sections, по 2 chunks каждый → 12 chunks.
    struct = _simple_struct_with_sections(6)
    chunks = tuple(
        _FakeChunk(
            f"c_{i}", f"n_{(i // 2) + 1:04d}", "x" * 3_500,
        )
        for i in range(12)
    )

    plan_small = build_execution_plan(
        struct, chunks, document_id="t",
        policy=ExecutionPolicy(
            direct_threshold_tokens=0,
            max_sections_per_batch=1,
            per_batch_token_budget=30_000,
        ),
    )
    plan_large = build_execution_plan(
        struct, chunks, document_id="t",
        policy=ExecutionPolicy(
            direct_threshold_tokens=0,
            max_sections_per_batch=6,
            per_batch_token_budget=30_000,
        ),
    )

    assert len(plan_small.batches) > len(plan_large.batches), (
        f"expected more batches with smaller max_sections_per_batch: "
        f"{len(plan_small.batches)} vs {len(plan_large.batches)}"
    )


def test_policy_per_batch_token_budget_changes_plan():
    """Изменение ``per_batch_token_budget`` меняет реальный план."""
    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        ExecutionPolicy,
        build_execution_plan,
    )

    struct = _simple_struct_with_sections(6)
    chunks = tuple(
        _FakeChunk(
            f"c_{i}", f"n_{(i // 2) + 1:04d}", "x" * 3_500,
        )
        for i in range(12)
    )

    plan_tight = build_execution_plan(
        struct, chunks, document_id="t",
        policy=ExecutionPolicy(
            direct_threshold_tokens=0,
            max_sections_per_batch=10,
            per_batch_token_budget=500,
        ),
    )
    plan_loose = build_execution_plan(
        struct, chunks, document_id="t",
        policy=ExecutionPolicy(
            direct_threshold_tokens=0,
            max_sections_per_batch=10,
            per_batch_token_budget=30_000,
        ),
    )

    assert len(plan_tight.batches) >= len(plan_loose.batches)


def test_policy_direct_threshold_changes_strategy():
    """Изменение ``direct_threshold_tokens`` меняет стратегию."""
    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        ExecutionPolicy,
        select_strategy,
    )

    struct = _simple_struct_with_sections(1)
    chunks = (_FakeChunk("c1", "n_0001", "x" * 100_000),)

    # 100_000 chars / 3.5 chars_per_token ≈ 28_571 tokens.
    s_low_threshold = select_strategy(struct, chunks, policy=ExecutionPolicy(
        direct_threshold_tokens=1_000,
    ))
    s_high_threshold = select_strategy(struct, chunks, policy=ExecutionPolicy(
        direct_threshold_tokens=100_000,
    ))

    # total_tokens > 1_000 → не direct.
    assert s_low_threshold != "direct", f"expected non-direct, got {s_low_threshold}"
    # total_tokens <= 100_000 → direct.
    assert s_high_threshold == "direct", f"expected direct, got {s_high_threshold}"