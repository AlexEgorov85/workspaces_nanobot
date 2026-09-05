"""Этап 21: ExecutionPlan строится ТОЛЬКО из selected chunks.

Инвариант: plan конкретного запуска = subset chunks, выбранных для этого
запуска. Нет дубликатов, нет пропусков. question-режим → plan содержит
только релевантные chunks.
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
    """Подменяем llm_* во всех namespace."""
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


def _build_six_section_doc(tmp_path: Path) -> str:
    """6 sections, each ~105000 chars → 7+ chunks."""
    sections = []
    for i in range(1, 7):
        sections.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 300
            + "\n\n"
        )
    return "".join(sections)


def test_plan_covers_only_selected_chunks(tmp_path, monkeypatch):
    """selected_chunks=[c2, c4] → plan содержит ТОЛЬКО c2, c4."""
    import summarizer

    text = _build_six_section_doc(tmp_path)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    assert len(insp.chunks) >= 4, f"ожидали >=4 chunks, получили {len(insp.chunks)}"

    # Выбираем 2й и 4й chunks (0-indexed: 1, 3).
    selected = [insp.chunks[1], insp.chunks[3]]
    selected_ids = {c.chunk_id for c in selected}

    ctx = summarizer._build_execution_context(insp, selected_chunks=selected)
    assert len(ctx.chunks) == 2
    assert {c.chunk_id for c in ctx.chunks} == selected_ids

    # Plan должен покрывать ТОЛЬКО выбранные chunks.
    assert ctx.plan is not None
    plan_ids: set[str] = set()
    for batch in ctx.plan.batches:
        plan_ids.update(batch.chunk_ids)

    assert plan_ids == selected_ids, (
        f"plan={plan_ids} != selected={selected_ids}; "
        f"extra={plan_ids - selected_ids}, missing={selected_ids - plan_ids}"
    )


def test_plan_no_duplicates(tmp_path, monkeypatch):
    """plan не содержит дублирующихся chunk_id."""
    import summarizer

    text = _build_six_section_doc(tmp_path)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = [insp.chunks[0], insp.chunks[2], insp.chunks[4]]
    ctx = summarizer._build_execution_context(insp, selected_chunks=selected)

    all_ids: list[str] = []
    for batch in ctx.plan.batches:
        all_ids.extend(batch.chunk_ids)

    assert len(all_ids) == len(set(all_ids)), f"duplicate chunk_ids: {all_ids}"


def test_plan_no_omissions(tmp_path, monkeypatch):
    """plan покрывает ВСЕ выбранные chunks без пропусков."""
    import summarizer

    text = _build_six_section_doc(tmp_path)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = list(insp.chunks[:5])
    selected_ids = {c.chunk_id for c in selected}
    ctx = summarizer._build_execution_context(insp, selected_chunks=selected)

    plan_ids: set[str] = set()
    for batch in ctx.plan.batches:
        plan_ids.update(batch.chunk_ids)

    assert plan_ids == selected_ids
