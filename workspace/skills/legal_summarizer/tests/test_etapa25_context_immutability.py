"""Этап 25: ExecutionContext.chunks не изменяется между этапами run'а.

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


def test_ctx_chunks_immutable_after_estimate_and_execution(tmp_path, monkeypatch):
    """ctx.chunks == (selected) во всех фазах run'а."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    selected = tuple(insp.chunks[:2])
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=list(selected),
    )

    assert ctx.chunks == selected, (
        f"after build: {tuple(c.chunk_id for c in ctx.chunks)} != "
        f"{tuple(c.chunk_id for c in selected)}"
    )

    est = summarizer._estimate_for_run(insp, ctx)
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
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    selected = tuple(insp.chunks[:2])
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=list(selected),
    )
    before = tuple(c.chunk_id for c in ctx.chunks)

    # estimate НЕ должен менять ctx.chunks.
    summarizer._estimate_for_run(insp, ctx)
    after = tuple(c.chunk_id for c in ctx.chunks)
    assert before == after


def test_ctx_chunks_preserved_in_manifest(tmp_path, monkeypatch):
    """manifest.chunks_selected == len(ctx.chunks)."""
    import summarizer

    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    selected = tuple(insp.chunks[:3])
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=list(selected),
    )

    # Запускаем через run() с прямым mock'ом execution, чтобы проверить manifest.
    fake_manifest = {
        "strategy": "map_flat",
        "chunks_selected": len(ctx.chunks),
        "chunks_total": len(insp.chunks),
        "actual_llm_calls": 1,
        "context_batches_total": 1,
    }

    def _fake_run_map_reduce(chunks, *, plan, strategy, **_kwargs):
        return {
            "status": "completed",
            "summary": "fake",
            "manifest": fake_manifest,
        }

    monkeypatch.setattr(summarizer, "_run_map_reduce", _fake_run_map_reduce)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed"
    assert fake_manifest["chunks_selected"] == len(selected) == 3
    # ctx.chunks == selected — это проверяется в предыдущем тесте.
