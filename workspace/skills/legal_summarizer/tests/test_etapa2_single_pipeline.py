"""Acceptance tests для Этапа 2: один canonical pipeline на запуск.

Invariant: ``run_canonical_pipeline`` вызывается ровно один раз на
``run()``. ``_run_map_reduce`` не имеет права самостоятельно запускать
pipeline — он получает готовый ``DocumentAnalysis`` из ``Inspection``.
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


def _long_text() -> str:
    return (
        "1. Общие положения\n\n"
        + ("Текст. " * 60) * 80
        + "\n\n2. Раздел Б\n\n"
        + ("Текст Б. " * 60) * 80
        + "\n\n3. Раздел В\n\n"
        + ("Текст В. " * 60) * 80
    )


def _install_pipeline_counter(monkeypatch):
    """Подменяем ``run_canonical_pipeline`` счётчиком вызовов."""
    from workspace.skills.legal_summarizer.tests import _etapa2_recorder
    from workspace.skills.legal_summarizer.scripts.structure import pipeline as _pipeline_mod

    original = _pipeline_mod.run_canonical_pipeline

    def _counting_run(*args, **kwargs):
        _etapa2_recorder.record_pipeline_call()
        return original(*args, **kwargs)

    monkeypatch.setattr(_pipeline_mod, "run_canonical_pipeline", _counting_run)
    import summarizer as _summarizer
    monkeypatch.setattr(_summarizer, "run_canonical_pipeline", _counting_run)


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

    # Map path: pipeline._llm_batch.
    from workspace.skills.legal_summarizer.scripts import pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _fake_batch)


def _reset_pipeline_counter():
    from workspace.skills.legal_summarizer.tests import _etapa2_recorder
    _etapa2_recorder.reset()


def test_run_calls_pipeline_exactly_once_map(tmp_path: Path, monkeypatch):
    """Map-reduce: ``run_canonical_pipeline`` вызывается один раз."""
    _reset_pipeline_counter()
    _install_pipeline_counter(monkeypatch)
    _install_llm_mocks(monkeypatch)

    import summarizer

    text = _long_text()
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text,
        length="detailed",
        document_path=str(p),
        workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] in ("completed", "partial"), result

    from workspace.skills.legal_summarizer.tests import _etapa2_recorder
    assert _etapa2_recorder.PIPELINE_CALLS == 1, (
        f"expected exactly 1 pipeline call, got {_etapa2_recorder.PIPELINE_CALLS}"
    )


def test_run_calls_pipeline_exactly_once_direct(tmp_path: Path, monkeypatch):
    """Direct: ``run_canonical_pipeline`` вызывается один раз."""
    _reset_pipeline_counter()
    _install_pipeline_counter(monkeypatch)
    _install_llm_mocks(monkeypatch)

    import summarizer

    text = "Короткий текст. Без секций. Просто содержание."
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text,
        length="detailed",
        document_path=str(p),
        workspace_root=tmp_path,
    )
    assert result["status"] == "completed", result

    from workspace.skills.legal_summarizer.tests import _etapa2_recorder
    assert _etapa2_recorder.PIPELINE_CALLS == 1, (
        f"expected exactly 1 pipeline call, got {_etapa2_recorder.PIPELINE_CALLS}"
    )


def test_run_map_reduce_does_not_re_run_pipeline(tmp_path: Path, monkeypatch):
    """Внутри ``run()`` — ровно один pipeline call (Этап 2 invariant)."""
    _reset_pipeline_counter()
    _install_pipeline_counter(monkeypatch)
    _install_llm_mocks(monkeypatch)

    import summarizer

    text = _long_text()
    p = _write_doc(tmp_path, text)

    summarizer.run(
        text,
        length="detailed",
        document_path=str(p),
        workspace_root=tmp_path,
        confirmed=True,
    )

    from workspace.skills.legal_summarizer.tests import _etapa2_recorder
    assert _etapa2_recorder.PIPELINE_CALLS == 1, (
        f"expected exactly 1 pipeline call inside run(), got {_etapa2_recorder.PIPELINE_CALLS}"
    )