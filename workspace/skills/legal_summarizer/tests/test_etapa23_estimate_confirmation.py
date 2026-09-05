"""Этап 23: estimate/confirmation работает по selected chunks, не по полному документу.

Инварианты:
- confirmation_required содержит chunks_selected (не только chunks_total).
- estimate min/max основан на selected chunks, не на полном документе.
- requires_continuation использует len(selected_chunks), не len(all_chunks).
- brief mode с малым числом selected chunks НЕ требует confirmation
  (даже если полный документ большой).
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


def _build_large_doc(tmp_path: Path, sections: int = 8) -> str:
    """Large doc → multiple chunks."""
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 300
            + "\n\n"
        )
    return "".join(parts)


def test_confirmation_contains_chunks_selected(tmp_path, monkeypatch):
    """confirmation_required содержит chunks_selected (run-level)."""
    import summarizer

    monkeypatch.setattr(
        summarizer, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 100.0,
            "max_chunks_for_execution": 100,
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
        },
    )

    text = _build_large_doc(tmp_path)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="brief",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert result["status"] == "confirmation_required"
    summary = result["summary"]
    assert "chunks_selected" in summary
    assert summary["chunks_selected"] <= summary["chunks_total"]


def test_estimate_uses_selected_chunks(tmp_path, monkeypatch):
    """estimate min/max основан на selected chunks."""
    import summarizer

    monkeypatch.setattr(
        summarizer, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 100.0,
            "max_chunks_for_execution": 100,
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
        },
    )

    text = _build_large_doc(tmp_path)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    # Полный документ: 8+ chunks → estimate > threshold.
    full_ctx = summarizer._build_execution_context(insp)
    full_est = summarizer._estimate_for_run(insp, full_ctx)
    assert full_est.estimated_duration_max_sec > 0

    # Selected 2 chunks → estimate меньше.
    selected = list(insp.chunks[:2])
    sel_ctx = summarizer._build_execution_context(insp, selected_chunks=selected)
    sel_est = summarizer._estimate_for_run(insp, sel_ctx)
    assert sel_est.estimated_duration_max_sec <= full_est.estimated_duration_max_sec
    assert sel_est.context_batches <= full_est.context_batches


def test_requires_continuation_uses_selected_count(tmp_path, monkeypatch):
    """requires_continuation проверяет len(selected_chunks), не len(all_chunks)."""
    import summarizer

    monkeypatch.setattr(
        summarizer, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 999999,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 2,
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
        },
    )

    text = _build_large_doc(tmp_path, sections=8)
    p = _write_doc(tmp_path, text)

    # brief → selected ≤2 chunks → НЕ requires_continuation.
    result = summarizer.run(
        text, length="brief",
        document_path=str(p), workspace_root=tmp_path,
    )
    # brief selection строже, чем max_chunks_for_execution=2.
    assert result["status"] in ("confirmation_required", "completed")


def test_brief_no_confirmation_when_small_selection(tmp_path, monkeypatch):
    """Brief mode с малым числом selected chunks → НЕ confirmation."""
    import summarizer

    # Порог очень высокий → confirmation только если max > threshold.
    # Brief selection: 1-2 chunks × 0.001s = tiny → no confirmation.
    monkeypatch.setattr(
        summarizer, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 999999,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 100,
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
        },
    )
    _install_llm_mocks(monkeypatch)

    text = _build_large_doc(tmp_path, sections=4)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="brief",
        document_path=str(p), workspace_root=tmp_path,
    )
    # brief selection маленький → max_duration < threshold → no confirmation.
    assert result["status"] == "completed", result
