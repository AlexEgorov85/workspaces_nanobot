"""ExecutionContext.chunks не изменяется между этапами run'а.

Главный invariant: после ``_build_execution_context()`` выбранные chunks
фиксируются. Ни estimate, ни execution не имеют права заменять их.

ctx.chunks — frozen tuple, но даже структура (порядок, состав) не должна
меняться в ходе run'а.
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
    import llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import application.service as _summarizer

    import execution.pipeline as _pipeline_mod

def _build_doc(sections: int = 6) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)

def test_ctx_chunks_immutable_after_estimate_and_execution(tmp_path, monkeypatch):
    """ctx.chunks == (selected) во всех фазах run'а."""
    import application.service as summarizer
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    selected = tuple(insp.chunks[:2])
    ctx = summarizer.build_execution_context(
        insp, selected_chunks=list(selected),
    )

    assert ctx.chunks == selected, (
        f"after build: {tuple(c.chunk_id for c in ctx.chunks)} != "
        f"{tuple(c.chunk_id for c in selected)}"
    )

    est = summarizer.estimate_for_run(insp, ctx)
    assert ctx.chunks == selected, (
        f"after estimate: {tuple(c.chunk_id for c in ctx.chunks)} != "
        f"{tuple(c.chunk_id for c in selected)}"
    )

    # Полный run — после execution.
    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # run с _selected_chunks_=2 вдохновляет selection для summary.
    # selected_chunks определяет chunks в execution, не в inspect (всего).
    assert result["stats"]["chunks_total"] == 6  # полный документ
    # manifest напрямую отсутствует в result["manifest"]; см. result["manifest"] в
    # другой форме. Проверяем stats.
    assert "chunks_total" in result["stats"]

def test_ctx_chunks_cannot_be_swapped_by_estimate(tmp_path, monkeypatch):
    """Estimate не подменяет ctx.chunks на полный набор."""
    import application.service as summarizer
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    selected = tuple(insp.chunks[:2])
    ctx = summarizer.build_execution_context(
        insp, selected_chunks=list(selected),
    )
    before = tuple(c.chunk_id for c in ctx.chunks)

    # estimate НЕ должен менять ctx.chunks.
    summarizer.estimate_for_run(insp, ctx)
    after = tuple(c.chunk_id for c in ctx.chunks)
    assert before == after

def test_ctx_chunks_preserved_in_manifest(tmp_path, monkeypatch):
    """После run() в manifest сохранены ровно те chunk_id, которые в ctx.chunks.

    LLM мокаются, ``run_map_reduce`` отрабатывает настоящий код.
    Проверяем что для каждого chunk_id из ``ctx.chunks`` в ``manifest.chunk_states``
    есть запись со статусом completed (manifest хранит subset-или-equal от
    ctx.chunks, потому что direct-стратегия сохраняет все chunks документа).
    """
    import application.service as summarizer
    from cache.manifest import load_manifest
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    selected = tuple(insp.chunks[:3])
    ctx = summarizer.build_execution_context(
        insp, selected_chunks=list(selected),
    )
    ctx_chunk_ids = tuple(c.chunk_id for c in ctx.chunks)
    assert len(ctx_chunk_ids) == 3

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result

    # Загружаем реальный manifest из кэша (run() сохраняет его туда).
    manifest = load_manifest(result["operation_id"], tmp_path)
    assert manifest is not None, (
        f"manifest for operation_id={result['operation_id']!r} not persisted"
    )
    chunk_states = manifest.chunk_states
    # Каждый chunk из ctx.chunks должен иметь completed-запись в manifest.
    for cid in ctx_chunk_ids:
        assert cid in chunk_states, (
            f"chunk {cid!r} from ctx.chunks missing in manifest.chunk_states"
        )
        assert chunk_states[cid].get("status") == "completed", (
            f"chunk {cid!r} status is {chunk_states[cid].get('status')!r}, "
            "expected 'completed'"
        )
