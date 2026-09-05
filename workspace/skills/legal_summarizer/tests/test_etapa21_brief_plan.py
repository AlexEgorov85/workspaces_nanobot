"""Этап 21: brief mode → plan содержит только выбранные brief-chunks.

Инвариант: brief выборка (c1, c5, c10 из 20) → plan содержит ТОЛЬКО
эти chunks, остальные 17 НЕ попадают в plan.
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


def _build_twenty_section_doc(tmp_path: Path) -> str:
    """20 sections, each ~50000 chars → many chunks."""
    sections = []
    for i in range(1, 21):
        sections.append(
            f"{i}. Пункт {i}\n\n"
            + ("Слово. " * 60) * 100
            + "\n\n"
        )
    return "".join(sections)


def test_brief_plan_subset_of_all_chunks(tmp_path, monkeypatch):
    """selected_chunks=[c1,c5,c10] → plan содержит ТОЛЬКО эти chunks."""
    import summarizer

    text = _build_twenty_section_doc(tmp_path)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    assert len(insp.chunks) >= 10, (
        f"ожидали >=10 chunks, получили {len(insp.chunks)}"
    )

    # Выбираем 3 chunks из 20 (симуляция brief selection).
    selected = [insp.chunks[0], insp.chunks[4], insp.chunks[9]]
    selected_ids = {c.chunk_id for c in selected}

    ctx = summarizer._build_execution_context(insp, selected_chunks=selected)
    assert ctx.plan is not None

    # Plan содержит только выбранные chunks.
    plan_ids: set[str] = set()
    for batch in ctx.plan.batches:
        plan_ids.update(batch.chunk_ids)

    assert plan_ids == selected_ids, (
        f"plan={plan_ids} != selected={selected_ids}; "
        f"extra={plan_ids - selected_ids}"
    )

    # Brief selection строже полного документа.
    all_ids = {c.chunk_id for c in insp.chunks}
    assert len(selected_ids) < len(all_ids), (
        "brief должен выбирать subset, а не все chunks"
    )


def test_brief_plan_no_extra_chunks(tmp_path, monkeypatch):
    """Plan НЕ содержит chunks, не вошедших в selected_chunks."""
    import summarizer

    text = _build_twenty_section_doc(tmp_path)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = [insp.chunks[0], insp.chunks[4], insp.chunks[9]]
    ctx = summarizer._build_execution_context(insp, selected_chunks=selected)

    all_ids = {c.chunk_id for c in insp.chunks}
    selected_ids = {c.chunk_id for c in ctx.chunks}
    excluded_ids = all_ids - selected_ids

    plan_ids: set[str] = set()
    for batch in ctx.plan.batches:
        plan_ids.update(batch.chunk_ids)

    # Ни один excluded chunk не должен быть в plan.
    leaked = plan_ids & excluded_ids
    assert not leaked, f"plan contains non-selected chunks: {leaked}"
