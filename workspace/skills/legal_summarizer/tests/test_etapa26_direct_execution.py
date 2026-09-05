"""Этап 26: Direct execution semantics.

Проверяем, что для direct-run:
- strategy == "direct"
- ctx.plan is None
- build_execution_plan НЕ вызывается
- llm_batch (map) НЕ вызывается
- llm_document_reduce вызывается ровно 1 раз
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _build_tiny_doc() -> str:
    """Документ из 1 chunk'а (маленький)."""
    return "Только один абзац текста, без секций."


def test_direct_run_no_plan_no_map(tmp_path, monkeypatch):
    """Direct run: strategy=='direct', plan is None, нет map calls."""
    import summarizer
    from workspace.skills.legal_summarizer.scripts.structure import unified_execution

    calls = {"plan_build": 0, "map": 0, "doc": 0}

    def _fake_plan(*args, **kwargs):
        calls["plan_build"] += 1
        from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
            ExecutionPlan,
            PlannedBatch,
        )
        return ExecutionPlan(
            document_id="d",
            strategy="map_flat",
            chunks=(),
            batches=(PlannedBatch(batch_id="cb_000", chunk_ids=(), token_estimate=0),),
            total_chunks=0,
            total_batches=1,
            total_input_tokens=0,
            estimated_llm_calls=1,
            estimated_total_sec=0.0,
        )

    monkeypatch.setattr(unified_execution, "build_execution_plan", _fake_plan)
    monkeypatch.setattr(summarizer, "build_execution_plan", _fake_plan)

    from workspace.skills.legal_summarizer.scripts import llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        calls["map"] += 1
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_doc(text, *, length, focus, structure, question=None):
        calls["doc"] += 1
        return "doc summary"

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    monkeypatch.setattr(summarizer, "_llm_batch", _fake_batch)
    monkeypatch.setattr(summarizer, "_llm_section_reduce", _fake_section)
    monkeypatch.setattr(summarizer, "_llm_document_reduce", _fake_doc)

    from workspace.skills.legal_summarizer.scripts import pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _fake_batch)

    text = _build_tiny_doc()
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)

    assert ctx.strategy == "direct", f"expected direct, got {ctx.strategy}"
    assert ctx.plan is None, f"direct ctx should have plan=None, got {ctx.plan}"

    # Reset map/doc counters.
    calls["plan_build"] = 0
    calls["map"] = 0
    calls["doc"] = 0

    result = summarizer.run(
        text,
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result

    # plan_build должен быть вызван только в inspect() если бы strategy != direct,
    # но для direct inspect не вызывает build_execution_plan (см. summarizer.py:608).
    # Проверяем: для tiny doc — 0 вызовов.
    assert calls["plan_build"] == 0, (
        f"direct run must not call build_execution_plan; got {calls['plan_build']}"
    )
    assert calls["map"] == 0, (
        f"direct run must not call llm_batch (map); got {calls['map']}"
    )
    assert calls["doc"] == 1, (
        f"direct run must call llm_document_reduce exactly once; "
        f"got {calls['doc']}"
    )


def test_direct_run_through_inspect_no_plan(tmp_path):
    """Inspection direct run: insp.execution_plan is None."""
    import summarizer

    text = _build_tiny_doc()
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    assert insp.strategy == "direct", f"expected direct, got {insp.strategy}"
    # insp.execution_plan может быть None или not None — для direct это None.
    assert insp.execution_plan is None, (
        f"direct Inspection must have execution_plan=None; "
        f"got {insp.execution_plan!r}"
    )


def test_direct_strategy_via_ctx_plan_none(tmp_path):
    """ctx.plan is None для direct."""
    import summarizer

    text = _build_tiny_doc()
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)
    assert ctx.strategy == "direct"
    assert ctx.plan is None
