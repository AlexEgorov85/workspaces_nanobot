"""Integration test: realistic НК РФ → pipeline.

Подробный план:  regression на реальный сценарий.

Контракт:

* 3 статьи × ~3 пунктов каждая → ~10 blocks.
* headings = 3 (только 'Статья N.'), голые '1./2./3.' НЕ headings.
* chunks = 3 (по статьям), каждый с существенным содержимым.
* section_density = 30% (в пределах sanity threshold 50%).
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


_NK_TEXT = """Статья 1. Общие положения

1. Настоящий Кодекс устанавливает систему налогов и сборов в Российской Федерации.

2. Законодательство о налогах состоит из настоящего Кодекса и федеральных законов.

3. Действие настоящего Кодекса распространяется на отношения по установлению налогов.

Статья 2. Основные термины

1. Для целей настоящего Кодекса используются основные термины: налогоплательщик, сбор.

2. Понятия применяются в значении, в каком они используются в других отраслях.

Статья 3. Основные начала законодательства

1. Каждое лицо должно уплачивать законно установленные налоги.

2. Никакие налоги не могут быть установлены иначе как в порядке, установленном Кодексом.
"""


def _nk_blocks() -> tuple[DocumentBlock, ...]:
    paragraphs = [p.strip() for p in _NK_TEXT.split("\n\n") if p.strip()]
    return tuple(
        DocumentBlock(
            block_id=f"b_{i:04d}",
            block_type="paragraph",
            content=p,
            char_count=len(p),
            page_index=None,
            page_start=None,
            page_end=None,
            paragraph_index=None,
            table_index=None,
            ordinal=i,
            block_metadata={},
        )
        for i, p in enumerate(paragraphs)
    )


def _run_pipeline(blocks):
    raw = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(raw, blocks)
    scored = apply_evidence_scoring(penalized, blocks)
    accepted = filter_above_threshold(scored)
    struct = build_document_structure(
        accepted, total_blocks=len(blocks), document_id="nk_regression",
    )
    doc = PhysicalDocument(
        path="<nk_regression>", format="txt", title="НК РФ",
        size_bytes=0, blocks=blocks, page_count=1,
    )
    chunks = chunk_from_structure(doc, struct)
    return accepted, struct, chunks


def test_realistic_nk_three_articles_pack_into_few_chunks():
    """3 коротких статьи → ≤ 3 chunks (новое structural packing).

    До рефакторинга (owner-boundary): 3 chunks (по одной статье).
    После рефакторинга (structural packing): статьи объединяются,
    если суммарно не превышают max_chunk_chars. Для короткого НК
    РФ fixture все 3 статьи влезают в 1 chunk.
    """
    blocks = _nk_blocks()
    accepted, struct, chunks = _run_pipeline(blocks)

    assert len(accepted) == 3, (
        f"Ожидалось 3 headings; получено {len(accepted)}: "
        f"{[(c.block_index, c.source) for c in accepted]}"
    )
    assert len(chunks) <= 3, (
        f"Ожидалось ≤ 3 chunks (structural packing); получено {len(chunks)}"
    )
    assert len(chunks) >= 1, (
        f"Ожидалось ≥ 1 chunk; получено {len(chunks)}"
    )


def test_realistic_nk_only_articles_as_headings():
    """Только 'Статья N.' приняты как headings."""
    blocks = _nk_blocks()
    accepted, _, _ = _run_pipeline(blocks)

    sources = {c.source for c in accepted}
    assert sources == {"regex_statiya"}, (
        f"Только 'regex_statiya' ожидались; получили {sources}"
    )


def test_realistic_nk_chunks_have_meaningful_text():
    """Каждый chunk содержит значимый текст (≥ 100 chars)."""
    blocks = _nk_blocks()
    _, _, chunks = _run_pipeline(blocks)

    for chunk in chunks:
        assert chunk.char_count >= 100, (
            f"chunk {chunk.chunk_id} слишком короткий ({chunk.char_count} chars)"
        )
        assert chunk.text.strip(), f"chunk {chunk.chunk_id} пустой"


def test_realistic_nk_density_under_threshold():
    """section_density < 50% (sanity check не сработает)."""
    blocks = _nk_blocks()
    accepted, _, _ = _run_pipeline(blocks)

    density = len(accepted) / len(blocks)
    assert density < 0.50, (
        f"section_density = {density:.2%} > 50%; sanity check сработает"
    )
