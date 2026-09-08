"""Smoke-тест для chunking на синтетическом НК РФ.

Подробный план — Этап 7: после исправления heading classification
(Этапы 2-3) chunking должен давать разумное количество chunks для
юридического документа.

Контракт:

* 30 статей × (5 пунктов + 15 подпунктов + 20 body blocks) = 1230 blocks.
* Ожидаем **ровно 30 chunks** — по одному на статью (chunker
  объединяет body/list_item под одной section-веткой).
* ``section_density`` (sections / total_blocks) < 10%.
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


def _build_synth_nk(num_articles: int = 30) -> tuple[DocumentBlock, ...]:
    """Синтетический НК РФ: статьи + пункты + подпункты + body."""
    body_text = (
        "Подробное описание пункта статьи нормативного правового акта, "
        "содержащее существенный объём юридического текста. "
    )
    body_text = body_text * 5  # ~600 chars per block

    blocks: list[DocumentBlock] = []
    for art in range(1, num_articles + 1):
        blocks.append(_b(len(blocks), f"Статья {art}. Заголовок статьи номер {art}."))
        for p in range(1, 6):
            blocks.append(_b(len(blocks), f"{p}. Содержание пункта {p} статьи {art}."))
            for sub in range(1, 4):
                blocks.append(_b(len(blocks), f"{p}.{sub}. Содержание подпункта."))
                blocks.append(_b(len(blocks), body_text))
            blocks.append(_b(len(blocks), body_text))
    return tuple(blocks)


def test_synth_nk_chunk_count():
    """1230 blocks → 30 chunks (по статьям)."""
    blocks = _build_synth_nk(num_articles=30)

    raw = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(raw, blocks)
    scored = apply_evidence_scoring(penalized, blocks)
    accepted = filter_above_threshold(scored)

    assert len(accepted) == 30, (
        f"Ожидалось 30 headings (30 статей); получено {len(accepted)}: "
        f"{[(c.block_index, c.source) for c in accepted]}"
    )

    struct = build_document_structure(
        accepted, total_blocks=len(blocks), document_id="synth_nk",
    )
    assert len(struct.iter_sections()) == 30

    doc = PhysicalDocument(
        path="<synth>", format="txt", title="Synth NK", size_bytes=0,
        blocks=blocks, page_count=1,
    )
    chunks = chunk_from_structure(doc, struct)

    assert len(chunks) < 30, (
        f"Ожидалось < 30 chunks (structural packing объединяет соседние "
        f"sections); получено {len(chunks)}"
    )
    assert len(chunks) >= 1, (
        f"Ожидалось ≥ 1 chunk; получено {len(chunks)}"
    )


def test_synth_nk_no_over_fragmentation():
    """``chunks / blocks < 5%`` (на реалистичном НК РФ).

    До фикса heading classification chunks/blocks было бы ~100% (каждый
    нумерованный блок — отдельный chunk). После фикса — по числу sections.
    """
    blocks = _build_synth_nk(num_articles=30)

    raw = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(raw, blocks)
    scored = apply_evidence_scoring(penalized, blocks)
    accepted = filter_above_threshold(scored)

    struct = build_document_structure(
        accepted, total_blocks=len(blocks), document_id="synth_nk",
    )
    doc = PhysicalDocument(
        path="<synth>", format="txt", title="Synth NK", size_bytes=0,
        blocks=blocks, page_count=1,
    )
    chunks = chunk_from_structure(doc, struct)

    ratio = len(chunks) / len(blocks)
    assert ratio < 0.05, (
        f"chunks/blocks = {ratio:.2%} слишком высокий "
        f"({len(chunks)} chunks / {len(blocks)} blocks); "
        f"ожидалось < 5% (по числу sections)"
    )


def test_synth_nk_section_density_under_sanity_threshold():
    """Section density < 50% (Этап 6: sanity warning threshold)."""
    blocks = _build_synth_nk(num_articles=30)

    raw = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(raw, blocks)
    scored = apply_evidence_scoring(penalized, blocks)
    accepted = filter_above_threshold(scored)

    density = len(accepted) / len(blocks)
    assert density < 0.50, (
        f"section_density = {density:.2%} > 50% — должна сработать "
        f"suspicious density warning (см. _log_structure_diagnostics)"
    )


def test_synth_nk_no_garbage_sources():
    """Все headings на синтетическом НК РФ — explicit legal markers."""
    blocks = _build_synth_nk(num_articles=10)

    raw = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(raw, blocks)
    scored = apply_evidence_scoring(penalized, blocks)
    accepted = filter_above_threshold(scored)

    sources = {c.source for c in accepted}
    assert sources == {"regex_statiya"}, (
        f"Ожидались только regex_statiya headings; получили {sources}. "
        f"Это означает, что голые '1./2./3.' ошибочно проходят как headings."
    )
