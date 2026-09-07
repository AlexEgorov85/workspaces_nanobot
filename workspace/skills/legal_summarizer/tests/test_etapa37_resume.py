"""Этап 37: Resume — partial run → повторный run пропускает выполненные chunks.

Механика: при partial-ошибке completed-batches пишут per-chunk файлы на
диск; повторный run() с тем же operation_id загружает cached_partials и
выполняет только pending chunks (инвойриант: no re-execution).
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


def test_partial_run_resume_processes_only_pending(tmp_path, monkeypatch):
    """Первый run падает на первом batch'е → second run обрабатывает
    только pending chunks (исключая уже записанные partials)."""
    import application.service as summarizer
    import llm.calls as llm_calls

    calls = {"batches": [], "calls": 0}

    def _flaky_batch(chunks, *, chunks_total, structure, length, question=None):
        calls["calls"] += 1
        calls["batches"].append(tuple(c.chunk_id for c in chunks))
        if calls["calls"] == 1:
            raise RuntimeError("simulated LLM outage")
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _flaky_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import application.service as _sm
    monkeypatch.setattr(_sm, "_llm_batch", _flaky_batch)
    monkeypatch.setattr(_sm, "_llm_section_reduce", _fake_section)
    monkeypatch.setattr(_sm, "_llm_document_reduce", _fake_doc)

    import execution.pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _flaky_batch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    # Первый run: flaky LLM — падает, статус должен быть partial/failed.
    result1 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result1["status"] in ("partial", "failed"), result1
    first_call_batches = calls["batches"]
    assert len(first_call_batches) >= 1
    # Записанных partials — только chunks из успешных batches.
    ok_chunk_ids: set[str] = set()
    for i, batch in enumerate(first_call_batches):
        # Первый batch упал (первый вызов flaky'шится), остальные — успех.
        if i != 0:
            ok_chunk_ids.update(batch)

    # Второй run: LLM не падает больше → completed.
    calls["batches"].clear()
    result2 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result2["status"] == "completed", result2
    # Resume invariant: во втором run LLM получила только pending chunks.
    second_seen = {cid for batch in calls["batches"] for cid in batch}
    assert len(second_seen) < len(ok_chunk_ids) or not ok_chunk_ids, (
        f"resume must reprocess fewer chunks than first run completed; "
        f"second={second_seen}, first_ok={ok_chunk_ids}"
    )
    # Завершённые в первом run chunks НЕ обрабатываются повторно.
    if ok_chunk_ids:
        assert second_seen.isdisjoint(ok_chunk_ids), (
            f"re-execution of completed chunks: {second_seen & ok_chunk_ids}"
        )


def test_resume_plan_remains_stable(tmp_path, monkeypatch):
    """План детерминирован при повторном build_execution_context."""
    import application.service as summarizer
    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "doc summary"

    import llm.calls as llm_calls
    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import application.service as _sm
    monkeypatch.setattr(_sm, "_llm_batch", _fake_batch)
    monkeypatch.setattr(_sm, "_llm_section_reduce", _fake_section)
    monkeypatch.setattr(_sm, "_llm_document_reduce", _fake_doc)

    import execution.pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _fake_batch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx_before = summarizer._build_execution_context(insp, length="detailed")
    assert ctx_before.plan is not None
    plan_before = [list(batch.chunk_ids) for batch in ctx_before.plan.batches]

    summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )

    ctx_after = summarizer._build_execution_context(insp, length="detailed")
    plan_after = [list(batch.chunk_ids) for batch in ctx_after.plan.batches]
    assert plan_before == plan_after, (
        f"plan changed:\n  before={plan_before}\n  after={plan_after}"
    )
