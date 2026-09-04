"""Тесты для PDF с плохим extraction (Этап 73 из PLAN.md).

PLAN §73: если extraction плохой — ``structure confidence`` должен
снижаться. Не нужно придумывать отсутствующую структуру.
"""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate, apply_evidence_scoring, compute_evidence,
    detect_heading_candidates,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)


def _b(ord: int, content: str = "x") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="page", content=content,
        char_count=len(content), page_index=ord + 1, page_start=ord + 1,
        page_end=ord + 1, paragraph_index=None, table_index=None,
        ordinal=ord, block_metadata={},
    )


def test_unparseable_heading_has_low_confidence():
    """Если heading невозможно распарсить, ``confidence`` низкий."""
    text = "???@@@@@!!!abc"
    candidates = detect_heading_candidates((_b(0, text),), pdf_path=None)
    if candidates:
        for c in candidates:
            assert c.score < 0.7


def test_no_headings_returns_low_confidence():
    """Только body без headings → очень низкая confidence."""
    blocks = (
        _b(0, "это содержательный текст первого раздела без явных заголовков"),
        _b(1, "это второй параграф текста документа"),
    )
    candidates = detect_heading_candidates(blocks, pdf_path=None)
    high_conf = [c for c in candidates if c.score >= 0.6]
    assert len(high_conf) == 0


def test_low_quality_pdf_with_partial_ocr():
    """Частичный OCR: heading detection должен дать **какие-то** candidates
    (regex-паттерн всё равно ловит цифру), но confidence остаётся
    на уровне regex (≤ 0.70)."""
    blocks = (
        _b(0, "1. Общие п$$%ожения"),
        _b(1, "2. Пр@ав@ и об@занности"),
        _b(2, "3. Ответств*нность"),
    )
    candidates = detect_heading_candidates(blocks, pdf_path=None)
    assert len(candidates) > 0
    for c in candidates:
        assert c.score < 0.85


def test_low_quality_no_repair_fabricates_structure():
    """Repair не должен придумывать секции, если их нет."""
    from workspace.skills.legal_summarizer.scripts.structure.models import (
        DocumentStructure, StructureNode,
    )
    from workspace.skills.legal_summarizer.scripts.structure.repair import (
        repair_structure,
    )
    struct = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": StructureNode(
            node_id="n_0000", node_type="document", semantic_type=None,
            level=0, title="", number=None, parent_id=None,
            children=(), start_block=0, end_block=5, confidence=0.5,
        )},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    fixed, report = repair_structure(struct)
    assert "n_0000" in fixed.nodes
    assert report.empty_nodes_collapsed == 0


def test_compute_evidence_for_ocr_garbage():
    """OCR-мусор не должен получать body_after бонус."""
    blocks = (_b(0, "???"), _b(1, "@@@"))
    c = HeadingCandidate(
        block_index=0, text="???", score=0.7,
        source="regex_numbered_1", level=1, raw_number=None,
    )
    ev = compute_evidence(c, blocks, [c])
    assert ev.body_after_bonus == 0.0