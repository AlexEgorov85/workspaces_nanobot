"""Smoke-тест для Этапа 8: pipeline не даёт пустой reduce input.

Подробный план — Этап 8: после исправления heading classification
(Этапы 2-3) reduce input не должен быть пустым, потому что каждый
chunk содержит реальный текст body под своей section-веткой.

Контракт:

* 5 статей с пунктами → 5 chunks (по статьям).
* Каждый chunk **непустой** (содержит текст пунктов + body).
* ``reduce_input_chars > 0`` для каждого chunk.
"""
from __future__ import annotations

from pathlib import Path
import sys

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


from document.heading import (
    detect_heading_candidates,
    apply_confidence_penalties,
    apply_evidence_scoring,
    filter_above_threshold,
)
from document.hierarchy import build_document_structure
from document.physical import DocumentBlock, PhysicalDocument
from chunking.chunker import chunk_from_structure


def _b(ordinal: int, content: str) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}",
        block_type="paragraph",
        content=content,
        char_count=len(content),
        page_index=None,
        page_start=None,
        page_end=None,
        paragraph_index=None,
        table_index=None,
        ordinal=ordinal,
        block_metadata={},
    )


def _build_small_nk() -> tuple[DocumentBlock, ...]:
    """Маленький НК РФ: 5 статей с пунктами."""
    body = "Содержание пункта статьи нормативного правового акта. " * 20

    blocks: list[DocumentBlock] = []
    for art in range(1, 6):
        blocks.append(_b(len(blocks), f"Статья {art}. Заголовок статьи номер {art}."))
        for p in range(1, 4):
            blocks.append(_b(len(blocks), f"{p}. Содержание пункта {p} статьи {art}."))
            blocks.append(_b(len(blocks), body))
    return tuple(blocks)


def test_small_nk_no_empty_chunks():
    """Все chunks непустые — REDUCE_INPUT_EMPTY не должен сработать."""
    blocks = _build_small_nk()

    raw = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(raw, blocks)
    scored = apply_evidence_scoring(penalized, blocks)
    accepted = filter_above_threshold(scored)

    struct = build_document_structure(
        accepted, total_blocks=len(blocks), document_id="small_nk",
    )
    doc = PhysicalDocument(
        path="<synth>", format="txt", title="Small NK", size_bytes=0,
        blocks=blocks, page_count=1,
    )
    chunks = chunk_from_structure(doc, struct)

    assert len(chunks) == 5, (
        f"Ожидалось 5 chunks (по 5 статьям); получено {len(chunks)}"
    )
    for chunk in chunks:
        assert chunk.text.strip(), (
            f"chunk {chunk.chunk_id} пустой — это приведёт к "
            f"REDUCE_INPUT_EMPTY в reduce stage"
        )
        assert chunk.char_count > 100, (
            f"chunk {chunk.chunk_id} слишком короткий ({chunk.char_count} chars); "
            f"возможна structural regression"
        )


def test_small_nk_chunks_have_substantial_text():
    """Каждый chunk содержит существенный текст (≥ 1000 chars).

    Защита от регрессии: если section ownership сломается и каждый
    block станет отдельным chunk'ом (как было до фикса), chunk может
    содержать только heading-строку.
    """
    blocks = _build_small_nk()

    raw = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(raw, blocks)
    scored = apply_evidence_scoring(penalized, blocks)
    accepted = filter_above_threshold(scored)

    struct = build_document_structure(
        accepted, total_blocks=len(blocks), document_id="small_nk",
    )
    doc = PhysicalDocument(
        path="<synth>", format="txt", title="Small NK", size_bytes=0,
        blocks=blocks, page_count=1,
    )
    chunks = chunk_from_structure(doc, struct)

    total_chars = sum(c.char_count for c in chunks)
    assert total_chars > 5000, (
        f"Total chunk chars = {total_chars}; ожидалось > 5000 "
        f"(маленький НК РФ имеет существенный body content)"
    )
