"""Regression: existing legacy API продолжает работать (Этап 50).

Этап 50 PLAN: после миграции consumers на DocumentStructure,
существующие тесты должны продолжать проходить. Этот модуль
подтверждает, что legacy API (``HeadingCandidate``, ``SectionTree``,
``DocumentSection``) не сломан.
"""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate, detect_heading_candidates,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    DocumentSection, SectionTree, detect_sections,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)


def _b(ord: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="paragraph", content="x",
        char_count=1, page_index=None, page_start=None, page_end=None,
        paragraph_index=None, table_index=None, ordinal=ord,
        block_metadata={},
    )


def _doc() -> PhysicalDocument:
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        path = f.name
    return PhysicalDocument(
        path=path, format="txt", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(5)), page_count=1,
    )


def test_heading_candidate_dataclass_still_works():
    hc = HeadingCandidate(
        block_index=0, text="1. First", score=0.7, source="regex_numbered_1",
        level=1, raw_number="1",
    )
    assert hc.block_index == 0
    assert hc.score == 0.7


def test_detect_heading_candidates_legacy_api():
    blocks = (
        _b(0)._replace(content="1. Общие положения"),
    )
    blocks = tuple(
        DocumentBlock(
            block_id=f"b_{i:04d}", block_type="paragraph",
            content=("1. Общие положения" if i == 0 else f"body {i}"),
            char_count=10, page_index=None, page_start=None, page_end=None,
            paragraph_index=None, table_index=None, ordinal=i,
            block_metadata={},
        )
        for i in range(3)
    )
    candidates = detect_heading_candidates(blocks, pdf_path=None)
    assert any(c.text == "1. Общие положения" for c in candidates)


def test_section_tree_dataclass_still_works():
    tree = SectionTree(
        sections={"s_root": DocumentSection(
            section_id="s_root", level=0, heading="", section_path="",
            block_indices=(0, 1, 2), children=(),
        )},
        root_id="s_root",
        block_to_section={0: "s_root", 1: "s_root", 2: "s_root"},
    )
    assert tree.root_id == "s_root"
    assert "s_root" in tree.sections


def test_detect_sections_legacy_api():
    blocks = tuple(
        DocumentBlock(
            block_id=f"b_{i:04d}", block_type="paragraph",
            content=("1. Заголовок" if i == 0 else f"text {i}"),
            char_count=10, page_index=None, page_start=None, page_end=None,
            paragraph_index=None, table_index=None, ordinal=i,
            block_metadata={},
        )
        for i in range(3)
    )
    doc = PhysicalDocument(
        path="placeholder", format="txt", title=None, size_bytes=0,
        blocks=blocks, page_count=1,
    )
    tree = detect_sections(doc)
    assert tree.root_id == "s_root"


def _replace(self, **kwargs):
    from dataclasses import replace
    return replace(self, **kwargs)

DocumentBlock._replace = _replace  # type: ignore