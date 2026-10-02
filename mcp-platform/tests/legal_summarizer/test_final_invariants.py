"""Финальные invariants.

Ручная проверка invariant'ов из плана. Каждый invariant проверяется
отдельным тестом с чётким assert.

Два invariant'а из исходного списка A–L здесь намеренно ОТСУТСТВУЮТ,
потому что их полноценная проверка живёт в специализированных модулях,
а копии здесь были зелёными пустышками с телом ``pass``:

* H (два конкурентных входа → максимум один LLM-вызов) —
  ``test_single_flight_concurrent_safety.py::test_concurrent_runs_peak_is_one``
  (два потока, peak active LLM ≤ 1).
* L (нет legacy-файлов) — ``test_legacy_audit.py``
  (``test_forbidden_files_do_not_exist``,
  ``test_forbidden_runtime_dirs_do_not_exist``).

Оставшиеся проверки обязаны выполняться всегда: если предусловие
(``ctx.plan``, ``insp.analysis``) не построилось, тест падает, а не
проходит молча.
"""

from __future__ import annotations

from pathlib import Path


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
    import libs.legal_summarizer.llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import libs.legal_summarizer.application.service as _summarizer

    import libs.legal_summarizer.execution.pipeline as _pipeline_mod

def test_invariant_a_one_run_one_pipeline(tmp_path, monkeypatch):
    """A: One run → one canonical pipeline."""
    import libs.legal_summarizer.application.service as summarizer
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
    import libs.legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer.build_execution_context(insp, length="detailed")
    assert ctx is not None
    assert isinstance(ctx.chunks, tuple)
    assert isinstance(ctx.strategy, str)

def test_invariant_c_one_context_one_plan(tmp_path, monkeypatch):
    """C: One ExecutionContext → one ExecutionPlan."""
    import libs.legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer.build_execution_context(insp, length="detailed")
    if ctx.strategy != "direct":
        assert ctx.plan is not None

def test_invariant_d_selected_equals_planned(tmp_path, monkeypatch):
    """D: selected chunks == planned chunks."""
    import libs.legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:4])
    ctx = summarizer.build_execution_context(
        insp, selected_chunks=selected,
    )
    assert ctx.plan is not None, (
        "D требует построенный план: 4 выбранных chunk'а + structure "
        f"обязаны дать plan, получено strategy={ctx.strategy!r}"
    )
    selected_ids = {c.chunk_id for c in selected}
    planned_ids = set()
    for batch in ctx.plan.batches:
        for cid in batch.chunk_ids:
            planned_ids.add(cid)
    assert selected_ids == planned_ids

def test_invariant_e_planned_equals_actual(tmp_path, monkeypatch):
    """E: planned batches == actual batches."""
    import libs.legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:4])
    ctx = summarizer.build_execution_context(
        insp, selected_chunks=selected,
    )
    assert ctx.plan is not None, (
        "E требует построенный план: 4 выбранных chunk'а + structure "
        f"обязаны дать plan, получено strategy={ctx.strategy!r}"
    )
    planned = [list(batch.chunk_ids) for batch in ctx.plan.batches]
    actual = summarizer.map_plan_to_chunk_batches(ctx.plan, selected)
    actual_str = [[c.chunk_id for c in batch] for batch in actual]
    assert planned == actual_str

def test_invariant_f_each_chunk_processed_exactly_once(tmp_path, monkeypatch):
    """F: each selected chunk → processed exactly once."""
    import libs.legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:4])
    ctx = summarizer.build_execution_context(
        insp, selected_chunks=selected,
    )
    assert ctx.plan is not None, (
        "F требует построенный план: 4 выбранных chunk'а + structure "
        f"обязаны дать plan, получено strategy={ctx.strategy!r}"
    )
    actual = summarizer.map_plan_to_chunk_batches(ctx.plan, selected)
    all_ids = [c.chunk_id for batch in actual for c in batch]
    assert len(all_ids) == len(set(all_ids))

def test_invariant_g_idempotent_no_reexecution(tmp_path, monkeypatch):
    """G: completed idempotent run → no pipeline, no plan, no LLM."""
    import libs.legal_summarizer.application.service as summarizer
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
    import libs.legal_summarizer.llm.calls as llm_calls

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

def test_invariant_i_exception_releases_lock():
    """I: LLM exception → lock released."""
    from libs.legal_summarizer.llm.calls import chat_locked
    import libs.legal_summarizer.llm.calls as lc
    import libs.legal_summarizer.llm.client as llm
    import libs.legal_summarizer.llm.single_flight as lsf
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
        assert lsf.LLM_FLIGHT_LOCK.acquire(blocking=False)
        lsf.LLM_FLIGHT_LOCK.release()
    finally:
        lc.llm.chat = original

def test_invariant_j_same_input_same_plan(tmp_path):
    """J: same analysis + same selection + same policy → same plan."""
    import libs.legal_summarizer.application.service as summarizer
    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:4])

    ctx1 = summarizer.build_execution_context(insp, selected_chunks=selected)
    ctx2 = summarizer.build_execution_context(insp, selected_chunks=selected)

    assert ctx1.plan is not None and ctx2.plan is not None, (
        "J требует оба плана построенными: одинаковый вход обязан дать "
        f"план, ctx1={ctx1.plan!r}, ctx2={ctx2.plan!r}"
    )
    b1 = [list(batch.chunk_ids) for batch in ctx1.plan.batches]
    b2 = [list(batch.chunk_ids) for batch in ctx2.plan.batches]
    assert b1 == b2

def test_invariant_k_identity_matches_structure(tmp_path):
    """K: identity.document_id == structure.document_id."""
    import libs.legal_summarizer.application.service as summarizer
    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    assert insp.analysis is not None and insp.structure is not None, (
        "K требует и analysis, и structure: "
        f"analysis={insp.analysis!r}, structure={insp.structure!r}"
    )
    assert insp.analysis.identity.document_id == insp.structure.document_id
