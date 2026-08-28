"""Тесты для ``prompts.py`` (build_batch_user_message + parse_batch_response).

Новый формат: текст с маркерами ``DOC CHUNK N: <саммари>`` (N = 1..K,
порядковый номер чанка в батче). Без JSON, без chunk_id/section в ответе.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PROJECT_ROOT = _REPO
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from workspace.skills.legal_summarizer.scripts.prompts import (
    ChunkResultParseError,
    build_batch_user_message,
    parse_batch_response,
)
from workspace.skills.legal_summarizer.scripts.packing import (
    ContextBatch,
    TokenBudget,
    pack_chunks,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk  # noqa: E402


def _chunk(chunk_id: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        index=int(chunk_id),
        text=text,
        char_count=len(text),
        page_start=1,
        page_end=1,
        block_indices=(int(chunk_id),),
        section_id="1",
        section_path="1",
        section_heading="",
        token_estimate=max(1, len(text) // 4),
        block_types=("paragraph",),
    )


def _budget() -> TokenBudget:
    # Большой budget → один батч с всеми чанками (тестам проще).
    return TokenBudget(
        context_window_tokens=1_000_000,
        system_prompt_tokens=0,
        instruction_tokens=0,
        output_reserve_tokens=1000,
        safety_margin=0.99,
        chars_per_token=3.5,
    )


# ---------------------------------------------------------------------------
# build_batch_user_message
# ---------------------------------------------------------------------------


def test_prompt_numbers_chunks_by_position():
    """Чанки нумеруются 1..K по позиции в батче (НЕ по chunk_id)."""
    chunks = [_chunk("000", "A"), _chunk("001", "B")]
    batches = pack_chunks(chunks, _budget())
    msg = build_batch_user_message(batches[0], chunks_total=2)
    assert "DOCUMENT CHUNK 1" in msg
    assert "DOCUMENT CHUNK 2" in msg
    # chunk_id НЕ должен фигурировать в теле чанка — LLM не знает
    # внутренние id, оперирует позицией.
    assert "000" not in msg
    assert "001" not in msg


def test_prompt_contains_total_chunks():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    msg = build_batch_user_message(batches[0], chunks_total=42)
    assert "Всего чанков в документе: 42" in msg


def test_prompt_contains_doc_chunk_marker_format_instruction():
    """Промпт просит модель писать ``DOCUMENT CHUNK N: ...`` (не JSON)."""
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    msg = build_batch_user_message(batches[0], chunks_total=1)
    assert "DOCUMENT CHUNK 1:" in msg
    assert "Никакого JSON" in msg


# ---------------------------------------------------------------------------
# parse_batch_response — текстовый формат
# ---------------------------------------------------------------------------


def test_parse_text_response_maps_position_to_chunk_id():
    """Саммари в порядке 1..K → chunk_id по позиции в батче."""
    chunks = [_chunk("000", "A"), _chunk("001", "B"), _chunk("002", "C")]
    batches = pack_chunks(chunks, _budget())
    llm_text = (
        "DOC CHUNK 1: Саммари чанка A.\n"
        "\n"
        "DOC CHUNK 2: Саммари чанка B. Подробнее.\n"
        "\n"
        "DOC CHUNK 3: Саммари чанка C.\n"
    )
    result = parse_batch_response(batches[0], llm_text)
    assert result == {
        "000": "Саммари чанка A.",
        "001": "Саммари чанка B. Подробнее.",
        "002": "Саммари чанка C.",
    }


def test_parse_handles_extra_blank_lines_and_fluff():
    """LLM может добавить пустые строки, заголовки, болтовню — парсер
    вытаскивает только блоки DOC CHUNK N:..."""
    chunks = [_chunk("000", "A"), _chunk("001", "B")]
    batches = pack_chunks(chunks, _budget())
    llm_text = (
        "Вот моё саммари:\n"
        "\n"
        "DOC CHUNK 1: Первое.\n"
        "\n"
        "Немного вступления между чанками.\n"
        "\n"
        "DOC CHUNK 2: Второе с переносом\nстроки внутри.\n"
        "\n"
        "Готово.\n"
    )
    result = parse_batch_response(batches[0], llm_text)
    assert result["000"] == "Первое."
    assert result["001"] == "Второе с переносом\nстроки внутри."


def test_parse_missing_chunk_raises():
    """Пропущен номер → ChunkResultParseError."""
    chunks = [_chunk("000", "A"), _chunk("001", "B"), _chunk("002", "C")]
    batches = pack_chunks(chunks, _budget())
    llm_text = (
        "DOC CHUNK 1: A\n"
        "\n"
        "DOC CHUNK 3: C\n"  # №2 пропущен
    )
    with pytest.raises(ChunkResultParseError, match="missing"):
        parse_batch_response(batches[0], llm_text)


def test_parse_no_markers_raises():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    with pytest.raises(ChunkResultParseError):
        parse_batch_response(batches[0], "просто текст без маркеров")


def test_parse_empty_body_skipped():
    """Пустой summary для конкретного номера — пропускается (модель может
    ошибиться), потом отсутствующие номера дают ChunkResultParseError."""
    chunks = [_chunk("000", "A"), _chunk("001", "B")]
    batches = pack_chunks(chunks, _budget())
    llm_text = "DOC CHUNK 1:    \n\nDOC CHUNK 2: ok\n"
    with pytest.raises(ChunkResultParseError):
        parse_batch_response(batches[0], llm_text)


def test_parse_strips_whitespace():
    chunks = [_chunk("000", "A")]
    batches = pack_chunks(chunks, _budget())
    llm_text = "   DOC CHUNK 1:    саммари с пробелами    \n   "
    result = parse_batch_response(batches[0], llm_text)
    assert result["000"] == "саммари с пробелами"


def test_parse_duplicate_number_uses_first_when_all_present():
    """Дубликат маркера без пропусков не фатален — берётся первое вхождение.

    Особенно важно для одночанковых батчей, где модель может повторить
    маркер «DOC CHUNK 1» внутри тела или при перечислении.
    """
    chunks = [_chunk("000", "A"), _chunk("001", "B")]
    batches = pack_chunks(chunks, _budget())
    llm_text = "DOC CHUNK 1: first\n\nDOC CHUNK 1: duplicate\n\nDOC CHUNK 2: B\n"
    result = parse_batch_response(batches[0], llm_text)
    assert result == {"000": "first", "001": "B"}


def test_parse_duplicate_with_missing_raises():
    """Дубликат + реально пропущенный номер — всё ещё ChunkResultParseError."""
    chunks = [_chunk("000", "A"), _chunk("001", "B"), _chunk("002", "C")]
    batches = pack_chunks(chunks, _budget())
    llm_text = "DOC CHUNK 1: a\n\nDOC CHUNK 1: dup\n\nDOC CHUNK 3: c"
    with pytest.raises(ChunkResultParseError, match="missing"):
        parse_batch_response(batches[0], llm_text)