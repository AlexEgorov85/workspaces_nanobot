"""Тесты для ``prompts.py`` (build_batch_user_message + parse_batch_response).

Покрывает:
    * Prompt содержит section_path для каждого chunk
    * Prompt содержит page range
    * Parser: валидный JSON → dict[chunk_id, summary]
    * Parser: missing chunk_id → ChunkResultParseError
    * Parser: markdown fence ```json → парсится
    * Parser: невалидный JSON → ChunkResultParseError
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PROJ = _REPO
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from workspace.skills.legal_summarizer.scripts.packing import (  # noqa: E402
    ContextBatch,
    TokenBudget,
    pack_chunks,
)
from workspace.skills.legal_summarizer.scripts.prompts import (  # noqa: E402
    ChunkResultParseError,
    build_batch_user_message,
    parse_batch_response,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk  # noqa: E402


def _chunk(
    chunk_id: str,
    text: str,
    *,
    section_id: str = "s_0001",
    section_path: str = "1",
    section_heading: str = "Heading",
    page_start: int | None = 1,
    page_end: int | None = 2,
    chars: int | None = None,
) -> Chunk:
    text_len = chars if chars is not None else len(text)
    return Chunk(
        chunk_id=chunk_id,
        index=int(chunk_id),
        text=text,
        char_count=text_len,
        token_estimate=max(1, text_len // 3),
        page_start=page_start,
        page_end=page_end,
        section_id=section_id,
        section_path=section_path,
        section_heading=section_heading,
        block_indices=(int(chunk_id),),
        block_types=("paragraph",),
    )


def _budget() -> TokenBudget:
    return TokenBudget(
        context_window_tokens=65536,
        system_prompt_tokens=1200,
        instruction_tokens=200,
        output_reserve_tokens=8192,
        safety_margin=0.85,
        chars_per_token=3.5,
    )


def test_prompt_contains_section_paths():
    chunks = [
        _chunk("000", "Текст 1", section_path="1", section_heading="Раздел один"),
        _chunk("001", "Текст 2", section_path="1 > 1.1", section_heading="Подраздел"),
    ]
    batches = pack_chunks(chunks, _budget())
    msg = build_batch_user_message(batches[0], chunks_total=2)
    assert "Раздел один" in msg
    assert "Подраздел" in msg
    assert "1" in msg
    assert "1 > 1.1" in msg


def test_prompt_contains_page_ranges():
    chunks = [
        _chunk("000", "Текст 1", page_start=1, page_end=3),
        _chunk("001", "Текст 2", page_start=3, page_end=5),
    ]
    batches = pack_chunks(chunks, _budget())
    msg = build_batch_user_message(batches[0], chunks_total=2)
    assert "1–3" in msg
    assert "3–5" in msg


def test_prompt_contains_chunk_ids():
    chunks = [
        _chunk("000", "A"),
        _chunk("001", "B"),
    ]
    batches = pack_chunks(chunks, _budget())
    msg = build_batch_user_message(batches[0], chunks_total=2)
    assert "DOCUMENT CHUNK 000" in msg
    assert "DOCUMENT CHUNK 001" in msg


def test_prompt_contains_total_chunks():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    msg = build_batch_user_message(batches[0], chunks_total=42)
    assert "Всего чанков в документе: 42" in msg


def test_prompt_contains_format_instruction():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    msg = build_batch_user_message(batches[0], chunks_total=1)
    assert "ФОРМАТ ОТВЕТА" in msg
    assert "chunks" in msg


def test_parse_valid_json_response():
    chunks = [_chunk("000", "A"), _chunk("001", "B")]
    batches = pack_chunks(chunks, _budget())
    llm_text = '{"chunks":[{"chunk_id":"000","summary":"S1","section":"1"},{"chunk_id":"001","summary":"S2","section":"1"}]}'
    result = parse_batch_response(batches[0], llm_text)
    assert result == {"000": "S1", "001": "S2"}


def test_parse_markdown_fence_response():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    llm_text = '```json\n{"chunks":[{"chunk_id":"000","summary":"S1"}]}\n```'
    result = parse_batch_response(batches[0], llm_text)
    assert result == {"000": "S1"}


def test_parse_missing_chunk_id_raises():
    chunks = [_chunk("000", "A"), _chunk("001", "B")]
    batches = pack_chunks(chunks, _budget())
    llm_text = '{"chunks":[{"chunk_id":"000","summary":"S1"}]}'
    with pytest.raises(ChunkResultParseError, match="отсутствуют"):
        parse_batch_response(batches[0], llm_text)


def test_parse_invalid_json_raises():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    llm_text = "Это просто текст без JSON"
    with pytest.raises(ChunkResultParseError):
        parse_batch_response(batches[0], llm_text)


def test_parse_chunks_not_list_raises():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    llm_text = '{"chunks": "not a list"}'
    with pytest.raises(ChunkResultParseError):
        parse_batch_response(batches[0], llm_text)


def test_parse_extra_chunk_id_ignored():
    """LLM может вернуть chunk_id, которого нет в batch — игнорируется."""
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    llm_text = '{"chunks":[{"chunk_id":"000","summary":"S1"},{"chunk_id":"999","summary":"S9"}]}'
    result = parse_batch_response(batches[0], llm_text)
    assert "999" not in result
    assert result["000"] == "S1"


def test_parse_strips_whitespace():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    llm_text = '{"chunks":[{"chunk_id":"000","summary":"   S1   "}]}'
    result = parse_batch_response(batches[0], llm_text)
    assert result["000"] == "S1"


def test_parse_empty_summary_skipped():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    llm_text = '{"chunks":[{"chunk_id":"000","summary":""}]}'
    with pytest.raises(ChunkResultParseError):
        parse_batch_response(batches[0], llm_text)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))