"""Этап 25: ExecutionContext.plan не изменяется во время execution.

Главный invariant: после ``_build_execution_context()`` план фиксируется.
Execution читает из плана (как контракт), не модифицирует его.
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


def _build_doc(sections: int = 6) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)


def _canonical_batches(plan):
    """Каноническое представление плана для сравнения."""
    return tuple(tuple(batch.chunk_ids) for batch in plan.batches)


def test_plan_batches_immutable_during_run(tmp_path, monkeypatch):
    """Snapshot плана до и после run() одинаков."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp, length="detailed")

    assert ctx.plan is not None
    before = _canonical_batches(ctx.plan)

    # Полный run.
    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result

    # После run ctx.plan не должен измениться.
    after = _canonical_batches(ctx.plan)
    assert before == after, (
        f"plan changed during run:\n  before={before}\n  after={after}"
    )


def test_plan_preserves_exact_chunk_order(tmp_path, monkeypatch):
    """План сохраняет порядок chunks внутри batch'ей (exact, не set)."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    # Берём selected в определённом порядке.
    selected = list(insp.chunks[:4])
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=selected,
    )

    assert ctx.plan is not None
    # Каждый batch хранит chunk_ids как tuple (immutable).
    for batch in ctx.plan.batches:
        assert isinstance(batch.chunk_ids, tuple)
        # Все chunk_ids — строки (immutable).
        for cid in batch.chunk_ids:
            assert isinstance(cid, str)


def test_plan_chunks_are_subset_of_selection(tmp_path, monkeypatch):
    """Все chunk_ids в плане — из selected chunks."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    selected_ids = {c.chunk_id for c in insp.chunks[:4]}
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=list(insp.chunks[:4]),
    )

    assert ctx.plan is not None
    planned_ids = set()
    for batch in ctx.plan.batches:
        for cid in batch.chunk_ids:
            planned_ids.add(cid)

    assert planned_ids == selected_ids, (
        f"plan includes chunks outside selection: "
        f"plan - sel = {planned_ids - selected_ids}; "
        f"sel - plan = {selected_ids - planned_ids}"
    )
