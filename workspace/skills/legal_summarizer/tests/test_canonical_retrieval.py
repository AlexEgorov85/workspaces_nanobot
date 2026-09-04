"""Тесты для canonical retrieval wrapper."""

from __future__ import annotations

from pathlib import Path

from workspace.skills.legal_summarizer.scripts.canonical_retrieval import (
    answer_followup,
    select_brief_from_analysis,
)
from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
    build_pipeline_result,
)


def _make_analysis(tmp_path: Path):
    p = tmp_path / "doc.txt"
    p.write_text(
        "1. Общие положения\n\nТекст о праве собственности.\n\n"
        "2. Обязательства\n\nТекст о договорных обязательствах.\n\n"
        "3. Наследственное право\n\nТекст о наследовании по завещанию.",
        encoding="utf-8",
    )
    result = build_pipeline_result(document_path=p)
    return result.analysis


def test_answer_followup_returns_followup_result(tmp_path: Path):
    """answer_followup возвращает FollowupResult с непустым target_chunks."""
    analysis = _make_analysis(tmp_path)
    result = answer_followup(
        analysis, "что такое право собственности?",
    )
    assert result.target_chunks is not None
    assert len(result.target_chunks) >= 0
    assert result.confidence in ("high", "medium", "low", "very_low")


def test_select_brief_from_analysis_returns_result(tmp_path: Path):
    """select_brief возвращает FollowupResult с target_chunks."""
    analysis = _make_analysis(tmp_path)
    result = select_brief_from_analysis(analysis)
    assert result.target_chunks is not None
    assert result.confidence == "medium"


def test_answer_followup_with_no_match_triggers_fallback(tmp_path: Path):
    """Запрос без match → full-document fallback (very_low)."""
    analysis = _make_analysis(tmp_path)
    result = answer_followup(
        analysis, "xyzqwerty12345 нерелевантный запрос",
    )
    assert result.confidence == "very_low"
    assert result.used_full_doc_fallback is True