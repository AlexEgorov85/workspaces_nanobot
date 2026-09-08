"""Acceptance tests для Этапа 15: estimated_llm_calls == actual_llm_calls."""

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

def test_direct_estimated_equals_actual(tmp_path: Path, monkeypatch):
    """Direct: estimated=1, actual=1."""
    _install_llm_mocks(monkeypatch)
    import application.service as summarizer
    text = "1. Пункт\n\nКороткий текст."
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer.build_execution_context(insp, length="detailed")
    est = summarizer.estimate_for_run(insp, ctx)
    assert ctx.strategy == "direct"
    assert est.estimated_llm_calls == 1

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert result["status"] == "completed", result
    actual = result["stats"]["total_llm_calls"]
    assert actual == est.estimated_llm_calls, (
        f"estimated={est.estimated_llm_calls}, actual={actual}"
    )

def test_map_flat_estimated_bounds_actual(tmp_path: Path, monkeypatch):
    """Map-flat: estimate — верхняя граница (бatches + reduce + buffer);
    actual = map + doc.

    Semantic estimate (Этап 11): estimate != guarantee; для flat-map
    actual часто равен estimate - 1 (нет section-level reduce).
    """
    _install_llm_mocks(monkeypatch)
    import application.service as summarizer
    text = (
        "1. Общие положения\n\n"
        + ("Текст длинный. " * 50) * 100
        + "\n\n2. Предмет\n\n"
        + ("Текст предмета. " * 50) * 100
    )
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer.build_execution_context(insp, length="detailed")
    est = summarizer.estimate_for_run(insp, ctx)
    assert ctx.strategy in ("map_flat", "map_hierarchical"), ctx.strategy
    estimated = est.estimated_llm_calls
    n_batches = len(ctx.plan.batches)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    actual = result["stats"]["total_llm_calls"]
    # Estimate — верхняя граница: actual <= estimated.
    assert actual <= estimated, (
        f"strategy={result['stats']['strategy']}, "
        f"estimated={estimated}, actual={actual} (actual > estimate)"
    )
    # Для flat-map actual = batches + 1 (map + doc reduce).
    # Для hierarchical actual может быть больше (section reduce).
    expected_min = n_batches + 1
    assert actual >= expected_min, (
        f"map reduce: expected actual >= {expected_min}, got {actual}"
    )