"""Этап 27: confirmation/continuation использует run-level ctx, не document-level.

Главные invariants:
- ``confirmation_required`` показывает chunks_selected (не chunks_total).
- ``requires_continuation`` проверяет ``len(ctx.chunks)``, не ``len(insp.chunks)``.
- Idempotency: повторный run не должен вызывать confirmation_required
  если operation completed в manifest.
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


def _build_doc(sections: int = 8) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)


def test_confirmation_uses_run_estimate_not_document(tmp_path, monkeypatch):
    """confirmation_required показывает run-level metrics."""
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

    text = _build_doc(sections=8)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=False,
    )
    assert result["status"] == "confirmation_required"
    summary = result["summary"]
    # Главные поля:
    assert "chunks_total" in summary
    assert "chunks_selected" in summary
    assert "context_batches_total" in summary
    assert "estimated_llm_calls" in summary
    assert "strategy" in summary


def test_continuation_uses_selected_chunks_not_document(tmp_path, monkeypatch):
    """requires_continuation проверяет selected, а не document chunks."""
    import summarizer

    monkeypatch.setattr(
        summarizer, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 999999,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 1,  # Только 1 chunk allowed → continuation
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
        },
    )

    text = _build_doc(sections=10)
    p = _write_doc(tmp_path, text)

    # brief selection ≤ 1 chunk → может быть completed.
    # detailed selection > 1 → requires_continuation.
    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    # selected > max_chunks_for_execution → requires_continuation.
    assert result["status"] == "requires_continuation", result
    summary = result["summary"]
    # Ключевое: chunks_selected > max_chunks_for_execution.
    assert summary["chunks_selected"] > 1


def test_brief_does_not_require_continuation_for_large_doc(tmp_path, monkeypatch):
    """Brief mode с малым selected — НЕ requires_continuation, даже если doc большой."""
    import summarizer

    monkeypatch.setattr(
        summarizer, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 999999,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 1,  # Очень строгий limit
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
        },
    )

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=10)  # 10 sections
    p = _write_doc(tmp_path, text)

    # brief → selected ≤ 1 chunk (или ≤ max_chunks) → completed.
    result = summarizer.run(
        text, length="brief",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert result["status"] == "completed", result


def test_confirmation_does_not_trigger_for_small_doc(tmp_path, monkeypatch):
    """Маленький документ (selected ≤ 1 batch) → без confirmation при высоком threshold."""
    import summarizer

    monkeypatch.setattr(
        summarizer, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 999999,  # высокий threshold
            "estimated_chunk_duration_sec": 0.001,  # очень быстрый estimate
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
    text = "Маленький текст, который создаст 1 chunk."
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text,
        document_path=str(p), workspace_root=tmp_path,
    )
    # 1 chunk → direct → max_duration < threshold → completed без confirmation.
    assert result["status"] == "completed", result
