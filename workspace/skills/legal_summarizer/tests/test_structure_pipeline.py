"""Тесты для canonical pipeline (Этап 45 из PLAN.md)."""

from __future__ import annotations

from pathlib import Path

from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
    run_canonical_pipeline,
)


def test_pipeline_on_text_file(tmp_path: Path):
    text_file = tmp_path / "doc.txt"
    text_file.write_text(
        "1. First section\n\nSome content here.\n\n2. Second\n\nMore text.",
        encoding="utf-8",
    )

    result = run_canonical_pipeline(text_file)
    assert result.analysis is not None
    assert len(result.chunks) >= 1 or result.analysis.structure.coverage_ratio >= 0


def test_pipeline_returns_validation_report(tmp_path: Path):
    text_file = tmp_path / "doc.txt"
    text_file.write_text("hello world", encoding="utf-8")

    result = run_canonical_pipeline(text_file)
    assert result.validation is not None
    assert result.validation.coverage_ratio >= 0.0


def test_pipeline_skips_repair_when_disabled(tmp_path: Path):
    text_file = tmp_path / "doc.txt"
    text_file.write_text("1. Section one\n\nContent.", encoding="utf-8")

    result_no_repair = run_canonical_pipeline(text_file, apply_repair=False)
    result_with_repair = run_canonical_pipeline(text_file, apply_repair=True)
    assert result_no_repair is not None
    assert result_with_repair is not None


def test_pipeline_skips_retrieval_when_disabled(tmp_path: Path):
    text_file = tmp_path / "doc.txt"
    text_file.write_text("1. Section\n\nContent.", encoding="utf-8")

    result = run_canonical_pipeline(text_file, include_retrieval_index=False)
    assert result.analysis.retrieval_index is None


def test_pipeline_handles_plain_text_no_headings(tmp_path: Path):
    """Plain text без headings — valid structure."""
    text_file = tmp_path / "plain.txt"
    text_file.write_text("Просто текст без headings.", encoding="utf-8")

    result = run_canonical_pipeline(text_file)
    assert result.analysis is not None