"""Тест для sibling numbering в hierarchy builder."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate,
)
from workspace.skills.legal_summarizer.scripts.structure.hierarchy import (
    build_document_structure,
)


def _hc(block_index: int, text: str, source: str = "regex_numbered_1"):
    return HeadingCandidate(
        block_index=block_index, text=text, score=0.7, source=source,
        level=1, raw_number=None,
    )


def test_sibling_ordinals_in_hierarchy_flat():
    cs = [
        _hc(0, "1. Первая"),
        _hc(5, "2. Вторая"),
        _hc(10, "3. Третья"),
    ]
    s = build_document_structure(cs, total_blocks=15)
    section_ids = s.nodes[s.root_id].children
    ordinals = [s.nodes[nid].number.ordinal for nid in section_ids]
    assert ordinals == [1, 2, 3]


def test_sibling_ordinals_in_hierarchy_resets_per_parent():
    """Под каждым родителем ordinals начинаются заново, не глобально.

    ``s.root_id.children`` содержит только top-level nodes (1, 2).
    Чтобы проверить ordinals для nested children — итерируем все
    секции в document order через ``s.iter_sections()``.
    """
    cs = [
        _hc(0, "1. Первая"),
        _hc(2, "1.1. Под первая"),
        _hc(4, "1.2. Под вторая"),
        _hc(6, "2. Вторая"),
        _hc(8, "2.1. Под первой второй"),
        _hc(10, "2.2. Под второй второй"),
    ]
    s = build_document_structure(cs, total_blocks=15)
    sections = s.iter_sections()
    ordinals = [sec.number.ordinal for sec in sections]
    assert ordinals == [1, 1, 2, 1, 1, 2]