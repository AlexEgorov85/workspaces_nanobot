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


def test_direct_estimated_equals_actual(tmp_path: Path, monkeypatch):
    """Direct: estimated=1, actual=1."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    text = "1. Пункт\n\nКороткий текст."
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    assert insp.strategy == "direct"
    assert insp.estimated_llm_calls == 1

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert result["status"] == "completed", result
    actual = result["stats"]["total_llm_calls"]
    assert actual == insp.estimated_llm_calls, (
        f"estimated={insp.estimated_llm_calls}, actual={actual}"
    )


def test_map_flat_estimated_equals_actual(tmp_path: Path, monkeypatch):
    """Map-flat: estimated=len(batches)+1, actual=map+doc."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    text = (
        "1. Общие положения\n\n"
        + ("Текст длинный. " * 50) * 100
        + "\n\n2. Предмет\n\n"
        + ("Текст предмета. " * 50) * 100
    )
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    assert insp.strategy in ("map_flat", "map_hierarchical"), insp.strategy
    estimated = insp.estimated_llm_calls

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    actual = result["stats"]["total_llm_calls"]
    assert actual == estimated, (
        f"strategy={result['stats']['strategy']}, "
        f"estimated={estimated}, actual={actual}"
    )