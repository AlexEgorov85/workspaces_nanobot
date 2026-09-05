"""Этап 21: detailed mode → plan покрывает весь документ; direct → plan=None.

Инварианты:
- detailed → plan содержит ВСЕ chunks документа.
- direct (1 chunk) → plan=None, strategy='direct', map_calls=0,
  document_reduce_calls=1.
- execution_plan совпадает с selected chunks.
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


def test_detailed_plan_covers_all_chunks(tmp_path, monkeypatch):
    """detailed → plan содержит ВСЕ chunks документа."""
    import summarizer

    sections = []
    for i in range(1, 7):
        sections.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 300
            + "\n\n"
        )
    text = "".join(sections)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    assert len(insp.chunks) >= 3

    ctx = summarizer._build_execution_context(insp, length="detailed")

    all_ids = {c.chunk_id for c in insp.chunks}
    selected_ids = {c.chunk_id for c in ctx.chunks}
    assert selected_ids == all_ids, "detailed должен выбирать все chunks"

    if ctx.plan is not None:
        plan_ids: set[str] = set()
        for batch in ctx.plan.batches:
            plan_ids.update(batch.chunk_ids)
        assert plan_ids == all_ids


def test_direct_plan_is_none(tmp_path, monkeypatch):
    """Direct (1 chunk) → plan=None, strategy='direct', map_calls=0."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    text = "1. Пункт\n\nКороткий текст для direct."
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    assert len(insp.chunks) <= 1

    ctx = summarizer._build_execution_context(insp, length="detailed")
    assert ctx.plan is None
    assert ctx.strategy == "direct"


def test_direct_execution_stats(tmp_path, monkeypatch):
    """Direct → map_calls=0, document_reduce_calls=1."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    text = "1. Пункт\n\nКороткий текст для direct."
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert result["status"] == "completed", result
    assert result["result"]["strategy"] == "direct"
    assert result["stats"]["map_calls"] == 0
    assert result["stats"]["document_reduce_calls"] == 1
    assert result["stats"]["total_llm_calls"] == 1


def test_detailed_execution_uses_plan(tmp_path, monkeypatch):
    """detailed (multi-chunk) → plan не None, map_calls > 0."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    sections = []
    for i in range(1, 6):
        sections.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 300
            + "\n\n"
        )
    text = "".join(sections)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert result["stats"]["map_calls"] > 0
    assert result["stats"]["total_llm_calls"] >= 3
