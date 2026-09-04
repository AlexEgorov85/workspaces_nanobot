"""Тесты для canonical pipeline wrapper (summarizer_canonical.py)."""

from __future__ import annotations

from pathlib import Path

from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
    build_pipeline_result,
    build_plan_from_pipeline,
    strategy_from_pipeline,
)


def _write_doc(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_build_pipeline_result_text(tmp_path: Path):
    """Pipeline на TXT даёт PipelineResult со всеми компонентами."""
    p = _write_doc(tmp_path, "doc.txt", "1. First\n\nContent.\n\n2. Second\n\nMore.")
    result = build_pipeline_result(document_path=p)
    assert result.analysis is not None
    assert result.analysis.physical is not None
    assert result.analysis.structure is not None
    assert result.chunks is not None
    assert result.validation is not None


def test_strategy_from_pipeline_short_doc(tmp_path: Path):
    """Короткий документ → DIRECT стратегия."""
    p = _write_doc(tmp_path, "short.txt", "Hello world. Это короткий текст.")
    result = build_pipeline_result(document_path=p)
    policy = strategy_from_pipeline(result)
    assert policy in ("direct", "map_flat")


def test_strategy_from_pipeline_long_doc(tmp_path: Path):
    """Длинный документ: strategy selector возвращает валидное значение."""
    sections = []
    for i in range(40):
        sections.append(
            f"{i+1}. Раздел {i+1}\n\n" + ("Текст раздела. " * 60) * 30,
        )
    text = "\n\n".join(sections)
    p = _write_doc(tmp_path, "long.txt", text)
    result = build_pipeline_result(document_path=p)
    policy = strategy_from_pipeline(result)
    assert policy in ("direct", "map_flat", "map_hierarchical")


def test_build_plan_from_pipeline_returns_plan(tmp_path: Path):
    """План строится из PipelineResult."""
    p = _write_doc(
        tmp_path, "doc.txt",
        "1. One\n\nContent.\n\n2. Two\n\nMore content here.",
    )
    result = build_pipeline_result(document_path=p)
    plan = build_plan_from_pipeline(
        result, document_id=result.analysis.identity.document_id,
    )
    assert plan is not None