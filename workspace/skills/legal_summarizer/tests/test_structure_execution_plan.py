"""Тесты для ExecutionPlan (Этап 21 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
    ExecutionPlan, PlannedBatch, build_direct_plan, build_map_plan,
)
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator, TokenEstimatorConfig,
)


def _chunk(cid: str, text: str, section_id: str = "s1") -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text=text, char_count=len(text),
        token_estimate=1, page_start=1, page_end=1,
        section_id=section_id, section_path="1", section_heading="x",
        block_indices=(0,), block_types=("paragraph",),
    )


def test_build_direct_plan_single_batch():
    chunks = (_chunk("001", "hello world"), _chunk("002", "another text"))
    plan = build_direct_plan(
        chunks, document_id="d1",
        token_estimator=TokenEstimator(TokenEstimatorConfig(chars_per_token=4.0)),
    )
    assert plan.strategy == "direct"
    assert plan.total_batches == 1
    assert plan.total_chunks == 2
    assert plan.estimated_llm_calls == 1
    assert plan.batches[0].chunk_ids == ("001", "002")
    assert plan.batches[0].token_estimate > 0


def test_build_map_plan_multiple_batches():
    chunks = tuple(_chunk(f"{i:03d}", f"text-{i}") for i in range(6))
    batches_input = [("001", "002", "003"), ("004", "005", "006")]
    plan = build_map_plan(
        chunks, document_id="d1", strategy="map_flat",
        batches_input=batches_input,
        token_estimator=TokenEstimator(TokenEstimatorConfig(chars_per_token=4.0)),
    )
    assert plan.strategy == "map_flat"
    assert plan.total_batches == 2
    assert plan.batches[0].chunk_ids == ("001", "002", "003")
    assert plan.batches[1].chunk_ids == ("004", "005", "006")
    assert plan.estimated_llm_calls == 2


def test_build_map_plan_skips_missing_chunk_ids():
    chunks = (_chunk("001", "a"), _chunk("002", "b"))
    batches_input = [("001", "999", "002")]
    plan = build_map_plan(
        chunks, document_id="d1", strategy="map_flat",
        batches_input=batches_input,
        token_estimator=TokenEstimator(),
    )
    assert plan.batches[0].chunk_ids == ("001", "999", "002")
    assert plan.batches[0].token_estimate > 0


def test_plan_get_batch():
    chunks = (_chunk("001", "a"),)
    plan = build_direct_plan(chunks, document_id="d", token_estimator=TokenEstimator())
    assert plan.get_batch("cb_000") is not None
    assert plan.get_batch("cb_999") is None


def test_plan_to_dict():
    chunks = (_chunk("001", "a"),)
    plan = build_direct_plan(
        chunks, document_id="d",
        token_estimator=TokenEstimator(),
        metadata={"mode": "brief"},
    )
    d = plan.to_dict()
    assert d["document_id"] == "d"
    assert d["strategy"] == "direct"
    assert d["metadata"] == {"mode": "brief"}
    assert len(d["batches"]) == 1


def test_plan_immutable():
    chunks = (_chunk("001", "a"),)
    plan = build_direct_plan(chunks, document_id="d", token_estimator=TokenEstimator())
    import dataclasses
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        plan.strategy = "map_flat"


def test_execution_plan_includes_hierarchical_strategy():
    chunks = (_chunk("001", "x"),)
    plan = build_map_plan(
        chunks, document_id="d", strategy="map_hierarchical",
        batches_input=[("001",)],
        token_estimator=TokenEstimator(),
    )
    assert plan.strategy == "map_hierarchical"