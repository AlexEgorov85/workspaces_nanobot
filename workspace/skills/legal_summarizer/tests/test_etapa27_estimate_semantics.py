"""Этап 27: estimate semantics — estimated != actual.

Главный invariant: ``manifest.estimated_llm_calls`` — это прогноз,
``manifest.actual_llm_calls`` — это фактический runtime counter.

Система НЕ ДОЛЖНА писать ``actual_llm_calls == estimated_llm_calls`` как
гарантию. Это два разных поля с разной семантикой.
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


def test_estimate_and_actual_are_separate_fields(tmp_path, monkeypatch):
    """Manifest хранит estimated и actual как разные поля."""
    import summarizer

    _install_llm_mocks(monkeypatch)
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # run() возвращает stats с актуальным счётчиком.
    # total_llm_calls — это фактическое число LLM-вызовов.
    assert "total_llm_calls" in result["stats"]
    assert result["stats"]["total_llm_calls"] >= 1
    # estimate доступен через ctx/estimate.
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)
    est = summarizer._estimate_for_run(insp, ctx)
    assert est.estimated_llm_calls > 0


def test_estimate_is_a_forecast_not_a_guarantee(tmp_path, monkeypatch):
    """Estimate остаётся forecast'ом, не превращается в факт."""
    import summarizer

    _install_llm_mocks(monkeypatch)
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)
    est = summarizer._estimate_for_run(insp, ctx)

    # estimate.estimated_llm_calls основан на selected batches.
    # Реальное число LLM-вызовов зависит от hierarchical reducer (min/max bounds).
    # Минимум — это map_calls + 1 (document reduce).
    # Максимум — это map_calls + N (если hierarchical reducer разбивает много раз).
    # Главное: estimate это upper bound для простого случая.
    assert isinstance(est.estimated_llm_calls, int)
    assert est.estimated_llm_calls >= 1


def test_estimate_returns_min_max_bounds(tmp_path):
    """_estimate_execution возвращает (min_calls, max_calls)."""
    import summarizer

    text = _build_doc(sections=3)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    min_c, max_c = summarizer._estimate_execution(insp)
    # Для map стратегии: min = 1 + len(batches) + 1, max = min + section_doc_reduce.
    assert min_c >= 1
    assert max_c >= min_c


def test_estimate_for_run_returns_estimate_dataclass(tmp_path):
    """_estimate_for_run возвращает dataclass Estimate с min/max."""
    import summarizer

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)

    est = summarizer._estimate_for_run(insp, ctx)
    assert hasattr(est, "estimated_llm_calls")
    assert hasattr(est, "estimated_duration_min_sec")
    assert hasattr(est, "estimated_duration_max_sec")
    assert est.estimated_duration_min_sec <= est.estimated_duration_max_sec
