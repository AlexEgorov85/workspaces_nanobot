"""Этап 22: _run_map_reduce safety invariants.

Инварианты:
- Unknown chunk_id in plan → RuntimeError (не тихий пропуск).
- Batch order == plan order (queued batch_ids в порядке plan.batches).
- Duplicate chunks across batches → RuntimeError.
- plan=None в _run_map_reduce → RuntimeError.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _install_llm_mocks(monkeypatch):
    from workspace.skills.legal_summarizer.scripts import llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import summarizer as _summarizer
    monkeypatch.setattr(_summarizer, "_llm_batch", _fake_batch)
    monkeypatch.setattr(_summarizer, "_llm_section_reduce", _fake_section)
    monkeypatch.setattr(_summarizer, "_llm_document_reduce", _fake_doc)

    from workspace.skills.legal_summarizer.scripts import pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _fake_batch)


def test_unknown_cid_raises(tmp_path, monkeypatch):
    """plan ссылается на неизвестный chunk_id → RuntimeError."""
    from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
        ExecutionPlan, PlannedBatch,
    )
    import summarizer

    text = ("1. Раздел\n\n" + ("Текст. " * 50) * 300 + "\n\n") * 3
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    assert len(insp.chunks) >= 2

    real_chunks = list(insp.chunks[:2])
    plan = ExecutionPlan(
        document_id="test_doc",
        strategy="map_flat",
        chunks=tuple(real_chunks),
        batches=(
            PlannedBatch(batch_id="cb_000", chunk_ids=(real_chunks[0].chunk_id, "UNKNOWN_ID"),
                         token_estimate=1000, section_ids=(), is_question_batch=False),
            PlannedBatch(batch_id="cb_001", chunk_ids=(real_chunks[1].chunk_id,),
                         token_estimate=1000, section_ids=(), is_question_batch=False),
        ),
        total_chunks=2, total_batches=2, total_input_tokens=2000,
        estimated_llm_calls=2, estimated_total_sec=0.0,
    )

    with pytest.raises(RuntimeError, match="unknown chunk_id"):
        summarizer._map_plan_to_chunk_batches(plan, real_chunks)


def test_order_invariant(tmp_path, monkeypatch):
    """queued batch_ids совпадают с порядком plan.batches."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    text = ("1. Раздел\n\n" + ("Текст. " * 50) * 300 + "\n\n") * 3
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # batch_ids в manifest в порядке plan order.
    manifest = summarizer.load_manifest(result["operation_id"], tmp_path)
    if manifest.batches_done:
        assert manifest.batches_done == sorted(manifest.batches_done)


def test_duplicate_chunks_raises(tmp_path, monkeypatch):
    """Дублирующиеся chunk_id в разных batches → RuntimeError."""
    from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
        ExecutionPlan, PlannedBatch,
    )
    import summarizer

    text = ("1. Раздел\n\n" + ("Текст. " * 50) * 300 + "\n\n") * 3
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    assert len(insp.chunks) >= 2

    real_chunks = list(insp.chunks[:2])
    cid = real_chunks[0].chunk_id
    plan = ExecutionPlan(
        document_id="test_doc",
        strategy="map_flat",
        chunks=tuple(real_chunks),
        batches=(
            PlannedBatch(batch_id="cb_000", chunk_ids=(cid,),
                         token_estimate=1000, section_ids=(), is_question_batch=False),
            PlannedBatch(batch_id="cb_001", chunk_ids=(cid,),
                         token_estimate=1000, section_ids=(), is_question_batch=False),
        ),
        total_chunks=2, total_batches=2, total_input_tokens=2000,
        estimated_llm_calls=2, estimated_total_sec=0.0,
    )

    with pytest.raises(RuntimeError, match="duplicate"):
        summarizer._map_plan_to_chunk_batches(plan, real_chunks)


def test_plan_none_raises_in_map_reduce(tmp_path, monkeypatch):
    """_run_map_reduce с plan=None → RuntimeError."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    text = ("1. Раздел\n\n" + ("Текст. " * 50) * 300 + "\n\n") * 3
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    with pytest.raises(RuntimeError, match="non-None ExecutionPlan"):
        summarizer._run_map_reduce(
            list(insp.chunks[:2]),
            plan=None,
            strategy="map_flat",
            length="detailed",
            focus=None,
            question=None,
            structure=None,
            analysis=insp.analysis,
            document_path=str(p),
            operation_id="test_op",
            workspace_root=tmp_path,
            chars_in=insp.chars_in,
            estimated_llm_calls=1,
            article_count=0,
            existing_manifest=None,
        )
