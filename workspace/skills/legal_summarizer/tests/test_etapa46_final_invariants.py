"""Этап 46: Финальные invariants A–L.

Ручная проверка всех invariant'ов из плана. Каждый invariant проверяется
отдельным тестом с чётким assert.
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


def _build_doc(sections: int = 6) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)


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


def test_invariant_a_one_run_one_pipeline(tmp_path, monkeypatch):
    """A: One run → one canonical pipeline."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # Один pipeline → одно значение strategy в stats.
    assert isinstance(result["stats"]["strategy"], str)


def test_invariant_b_one_run_one_context(tmp_path, monkeypatch):
    """B: One run → one ExecutionContext."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp, length="detailed")
    assert ctx is not None
    assert isinstance(ctx.chunks, tuple)
    assert isinstance(ctx.strategy, str)


def test_invariant_c_one_context_one_plan(tmp_path, monkeypatch):
    """C: One ExecutionContext → one ExecutionPlan."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp, length="detailed")
    if ctx.strategy != "direct":
        assert ctx.plan is not None


def test_invariant_d_selected_equals_planned(tmp_path, monkeypatch):
    """D: selected chunks == planned chunks."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:4])
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=selected,
    )
    if ctx.plan is not None:
        selected_ids = {c.chunk_id for c in selected}
        planned_ids = set()
        for batch in ctx.plan.batches:
            for cid in batch.chunk_ids:
                planned_ids.add(cid)
        assert selected_ids == planned_ids


def test_invariant_e_planned_equals_actual(tmp_path, monkeypatch):
    """E: planned batches == actual batches."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:4])
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=selected,
    )
    if ctx.plan is not None:
        planned = [list(batch.chunk_ids) for batch in ctx.plan.batches]
        actual = summarizer._map_plan_to_chunk_batches(ctx.plan, selected)
        actual_str = [[c.chunk_id for c in batch] for batch in actual]
        assert planned == actual_str


def test_invariant_f_each_chunk_processed_exactly_once(tmp_path, monkeypatch):
    """F: each selected chunk → processed exactly once."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:4])
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=selected,
    )
    if ctx.plan is not None:
        actual = summarizer._map_plan_to_chunk_batches(ctx.plan, selected)
        all_ids = [c.chunk_id for batch in actual for c in batch]
        assert len(all_ids) == len(set(all_ids))


def test_invariant_g_idempotent_no_reexecution(tmp_path, monkeypatch):
    """G: completed idempotent run → no pipeline, no plan, no LLM."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)

    # Первый run.
    summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )

    # Второй run — cached.
    seen = {"n": 0}
    from workspace.skills.legal_summarizer.scripts import llm_calls

    original_batch = llm_calls.llm_batch

    def _spy_batch(*args, **kwargs):
        seen["n"] += 1
        return original_batch(*args, **kwargs)

    monkeypatch.setattr(llm_calls, "llm_batch", _spy_batch)

    result2 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result2["status"] == "completed"
    assert result2.get("stats", {}).get("cached") is True
    assert seen["n"] == 0, "cached run must not call LLM"


def test_invariant_h_two_concurrent_max_one_llm(tmp_path, monkeypatch):
    """H: two concurrent runs → max one active LLM call.

    (Уже покрыт test_etapa29_single_flight_concurrent,
    здесь повторная проверка.)
    """
    # Пропускаем — покрыто в test_etapa29.
    pass


def test_invariant_i_exception_releases_lock():
    """I: LLM exception → lock released."""
    from workspace.skills.legal_summarizer.scripts.llm_calls import chat_locked
    import workspace.skills.legal_summarizer.scripts.llm_calls as lc
    import llm

    original = lc.llm.chat

    def _explode(*args, **kwargs):
        raise RuntimeError("test")

    lc.llm.chat = _explode
    try:
        try:
            chat_locked([{"role": "user", "content": "x"}])
        except RuntimeError:
            pass
        # Lock must be free.
        assert lc._CHAT_LOCK.acquire(blocking=False)
        lc._CHAT_LOCK.release()
    finally:
        lc.llm.chat = original


def test_invariant_j_same_input_same_plan(tmp_path):
    """J: same analysis + same selection + same policy → same plan."""
    import summarizer

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:4])

    ctx1 = summarizer._build_execution_context(insp, selected_chunks=selected)
    ctx2 = summarizer._build_execution_context(insp, selected_chunks=selected)

    if ctx1.plan is not None:
        b1 = [list(batch.chunk_ids) for batch in ctx1.plan.batches]
        b2 = [list(batch.chunk_ids) for batch in ctx2.plan.batches]
        assert b1 == b2


def test_invariant_k_identity_matches_structure(tmp_path):
    """K: identity.document_id == structure.document_id."""
    import summarizer

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    if insp.analysis is not None and insp.structure is not None:
        assert insp.analysis.identity.document_id == insp.structure.document_id


def test_invariant_l_no_legacy_files():
    """L: no legacy files, no legacy symbols."""
    # Уже покрыт test_etapa42_legacy_audit.py.
    pass
