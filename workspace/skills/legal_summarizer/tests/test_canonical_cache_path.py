"""Cache-path test для canonical DocumentAnalysis (Этап 29).

Проверяет, что DocumentAnalysis переиспользуется между запросами
без повторного parsing/structure/chunking.
"""

from __future__ import annotations

from pathlib import Path


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def test_first_run_builds_analysis(tmp_path: Path):
    """Первый запуск строит DocumentAnalysis с semantic_records=[]."""
    from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
        build_pipeline_result,
    )

    p = _write_doc(
        tmp_path,
        "1. First\n\nContent here.\n\n2. Second\n\nMore content.",
    )
    result = build_pipeline_result(document_path=p)
    assert result.analysis is not None
    assert result.analysis.semantic_records == {}
    assert result.analysis.retrieval_index is not None


def test_second_run_returns_same_identity(tmp_path: Path):
    """Два запуска на одном файле → один и тот же DocumentIdentity."""
    from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
        build_pipeline_result,
    )

    p = _write_doc(
        tmp_path,
        "1. First\n\nContent.\n\n2. Second\n\nMore.",
    )
    result1 = build_pipeline_result(document_path=p)
    result2 = build_pipeline_result(document_path=p)

    assert result1.analysis.identity.document_id == (
        result2.analysis.identity.document_id
    )
    assert result1.analysis.identity.fingerprint == (
        result2.analysis.identity.fingerprint
    )


def test_modified_file_creates_new_identity(tmp_path: Path):
    """Изменённый файл → новый DocumentIdentity."""
    from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
        build_pipeline_result,
    )

    p1 = _write_doc(tmp_path, "First content.")
    result1 = build_pipeline_result(document_path=p1)

    p2 = tmp_path / "doc2.txt"
    p2.write_text("Completely different content.", encoding="utf-8")
    result2 = build_pipeline_result(document_path=p2)

    assert (
        result1.analysis.identity.document_id
        != result2.analysis.identity.document_id
    )


def test_followup_uses_cached_analysis(tmp_path: Path, monkeypatch):
    """Follow-up запрос использует cached DocumentAnalysis, без повторного parsing."""
    from workspace.skills.legal_summarizer.scripts import summarizer_canonical
    from workspace.skills.legal_summarizer.scripts import canonical_retrieval

    pipeline_calls = {"n": 0}
    original_pipeline = summarizer_canonical.run_canonical_pipeline

    def counting_pipeline(*args, **kwargs):
        pipeline_calls["n"] += 1
        return original_pipeline(*args, **kwargs)

    monkeypatch.setattr(
        summarizer_canonical, "run_canonical_pipeline", counting_pipeline,
    )

    p = _write_doc(
        tmp_path,
        "1. Право собственности\n\n"
        "Собственник владеет, пользуется и распоряжается имуществом.\n\n"
        "2. Обязательства\n\n"
        "Должник обязан совершить действие.",
    )

    result = summarizer_canonical.build_pipeline_result(document_path=p)
    pipeline_calls["n"] = 0

    canonical_retrieval.answer_followup(
        result.analysis, "что такое собственность?",
    )
    canonical_retrieval.answer_followup(
        result.analysis, "что такое обязательство?",
    )

    assert pipeline_calls["n"] == 0


def test_document_analysis_immutable(tmp_path: Path):
    """DocumentAnalysis — frozen dataclass."""
    from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
        build_pipeline_result,
    )

    p = _write_doc(tmp_path, "Test content.")
    result = build_pipeline_result(document_path=p)
    try:
        result.analysis.identity = None
    except (AttributeError, Exception):
        return
    raise AssertionError(
        "DocumentAnalysis should be frozen",
    )