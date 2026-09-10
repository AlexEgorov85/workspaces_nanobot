"""Тесты #7: ``build_question_context`` — формирует LLM-вход для question.

Проверяется:

1. Блок содержит section heading, page range, cached section summary,
   cached chunk summary, source text — в указанном порядке.
2. Если chunk_summary отсутствует → вместо текста — ``[no cached chunk summary]``-
   эквивалент (конкретный формат: пропуск блока CACHED CHUNK SUMMARY).
3. Если section_summary отсутствует → блок CACHED SECTION SUMMARY пропускается.
4. Source text никогда не пропускается (lossless fallback).
5. Budget truncate обрезает source text сначала, сохраняя summaries.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_chunk(
    *, chunk_id: str, section_id: str = "s_0001",
    section_path: str = "1", section_heading: str = "Раздел 1",
    page_start: int = 1, page_end: int = 1,
    text: str = "Chunk text.",
) -> "Chunk":
    from chunking.chunks import Chunk
    return Chunk(
        chunk_id=chunk_id,
        index=int(chunk_id),
        text=text,
        char_count=len(text),
        token_estimate=len(text) // 4,
        page_start=page_start,
        page_end=page_end,
        section_id=section_id,
        section_path=section_path,
        section_heading=section_heading,
        block_indices=(int(chunk_id) - 1,),
        block_types=("paragraph",),
    )


def test_full_block_includes_all_three_levels(tmp_path):
    """Если section_summary + chunk_summary + text — все три присутствуют."""
    from application.question_context import build_question_context
    from cache.document_cache import DocumentCache

    document_id = "d_full"
    cache = DocumentCache(tmp_path)
    cache.write_section_summary(
        document_id=document_id,
        section_id="s_0001",
        summary="Раздел об обязанностях сторон",
    )
    cache.write_chunk_summary(
        document_id=document_id,
        chunk_id="001",
        summary="Chunk 1: пеня 0.1%",
        section_id="s_0001",
    )

    chunks = [_make_chunk(chunk_id="001", text="Срок 30 дней. Пеня 0.1%.")]
    ctx = build_question_context(
        chunks, document_id=document_id, workspace_root=tmp_path,
        budget_chars=10_000,
    )

    assert "CACHED SECTION SUMMARY" in ctx
    assert "Раздел об обязанностях сторон" in ctx
    assert "CACHED CHUNK SUMMARY" in ctx
    assert "Chunk 1: пеня 0.1%" in ctx
    assert "SOURCE TEXT" in ctx
    assert "Срок 30 дней. Пеня 0.1%." in ctx


def test_missing_chunk_summary_omits_block(tmp_path):
    """Нет chunk summary → блок CACHED CHUNK SUMMARY пропускается, source text есть."""
    from application.question_context import build_question_context

    chunks = [_make_chunk(chunk_id="001", text="Original text")]
    ctx = build_question_context(
        chunks, document_id="d_empty", workspace_root=tmp_path,
        budget_chars=10_000,
    )

    assert "CACHED CHUNK SUMMARY" not in ctx
    assert "SOURCE TEXT" in ctx
    assert "Original text" in ctx


def test_missing_section_summary_omits_block(tmp_path):
    """Нет section summary → блок CACHED SECTION SUMMARY пропускается."""
    from application.question_context import build_question_context
    from cache.document_cache import DocumentCache

    document_id = "d_no_sec"
    cache = DocumentCache(tmp_path)
    cache.write_chunk_summary(
        document_id=document_id,
        chunk_id="001",
        summary="chunk summary",
    )

    chunks = [_make_chunk(chunk_id="001", text="text")]
    ctx = build_question_context(
        chunks, document_id=document_id, workspace_root=tmp_path,
        budget_chars=10_000,
    )

    assert "CACHED SECTION SUMMARY" not in ctx
    assert "CACHED CHUNK SUMMARY" in ctx
    assert "SOURCE TEXT" in ctx


def test_no_summaries_at_all_still_includes_source(tmp_path):
    """Без cache summaries — только metadata + source text."""
    from application.question_context import build_question_context

    chunks = [_make_chunk(chunk_id="001", text="only source text")]
    ctx = build_question_context(
        chunks, document_id="d_none", workspace_root=tmp_path,
        budget_chars=10_000,
    )

    assert "CACHED SECTION SUMMARY" not in ctx
    assert "CACHED CHUNK SUMMARY" not in ctx
    assert "SOURCE TEXT" in ctx
    assert "only source text" in ctx


def test_budget_truncation_preserves_summaries(tmp_path):
    """При budget < source text — source text обрезается, summaries сохраняются,
    общий размер <= budget_chars."""
    from application.question_context import build_question_context
    from cache.document_cache import DocumentCache

    document_id = "d_budget"
    cache = DocumentCache(tmp_path)
    cache.write_section_summary(
        document_id=document_id,
        section_id="s_0001",
        summary="Important section summary that must survive truncation",
    )
    cache.write_chunk_summary(
        document_id=document_id,
        chunk_id="001",
        summary="Critical chunk summary",
    )

    long_text = "XYZUNIQUE_TOKEN_42. " * 1000  # токен, который легко найти
    chunks = [_make_chunk(chunk_id="001", text=long_text)]

    ctx_tight = build_question_context(
        chunks, document_id=document_id, workspace_root=tmp_path,
        budget_chars=500,
    )
    # Section summary и chunk summary ДОЛЖНЫ остаться.
    assert "Important section summary" in ctx_tight
    assert "Critical chunk summary" in ctx_tight
    # Source text ОБЯЗАН быть обрезан.
    assert len(ctx_tight) <= 500
    # Последний токен source text НЕ дойдёт до конца документа (было 1000 повторов).
    token_count = ctx_tight.count("XYZUNIQUE_TOKEN_42")
    assert token_count < 1000, (
        f"budget=500 должен обрезать source text, но встречается "
        f"{token_count} повторов XYZUNIQUE_TOKEN_42"
    )


def test_metadata_includes_section_and_page_range(tmp_path):
    """Метаданные (section heading + page range) присутствуют всегда."""
    from application.question_context import build_question_context

    chunks = [
        _make_chunk(
            chunk_id="001",
            section_heading="Раздел 4. Обязанности",
            section_path="4",
            page_start=12,
            page_end=18,
            text="x",
        ),
    ]
    ctx = build_question_context(
        chunks, document_id="d_meta", workspace_root=tmp_path,
        budget_chars=10_000,
    )
    assert "[CHUNK 001" in ctx
    assert "Раздел 4. Обязанности" in ctx
    assert "pages 12-18" in ctx


def test_multiple_chunks_concatenated(tmp_path):
    """Несколько chunks → блоки конкатенируются в выбранном порядке."""
    from application.question_context import build_question_context

    chunks = [
        _make_chunk(chunk_id="001", text="first chunk text"),
        _make_chunk(chunk_id="002", text="second chunk text"),
        _make_chunk(chunk_id="003", text="third chunk text"),
    ]
    ctx = build_question_context(
        chunks, document_id="d_multi", workspace_root=tmp_path,
        budget_chars=10_000,
    )

    # Порядок сохранён.
    pos1 = ctx.find("first chunk text")
    pos2 = ctx.find("second chunk text")
    pos3 = ctx.find("third chunk text")
    assert pos1 < pos2 < pos3


def test_empty_selection_returns_empty_string(tmp_path):
    """Пустой список chunks → пустой результат."""
    from application.question_context import build_question_context

    ctx = build_question_context(
        [], document_id="d_empty", workspace_root=tmp_path,
        budget_chars=10_000,
    )
    assert ctx == ""


def test_chunk_without_section_id(tmp_path):
    """Chunk без section_id (root) → section_summary пропускается, chunk_summary подгружается."""
    from application.question_context import build_question_context
    from cache.document_cache import DocumentCache

    document_id = "d_root"
    cache = DocumentCache(tmp_path)
    cache.write_chunk_summary(
        document_id=document_id,
        chunk_id="001",
        summary="preamble chunk summary",
    )

    chunks = [_make_chunk(chunk_id="001", section_id="", text="preamble text")]
    ctx = build_question_context(
        chunks, document_id=document_id, workspace_root=tmp_path,
        budget_chars=10_000,
    )

    assert "CACHED SECTION SUMMARY" not in ctx
    assert "preamble chunk summary" in ctx
    assert "preamble text" in ctx