"""Тесты для StructureTreeBuilder (Этап 12 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate,
)
from workspace.skills.legal_summarizer.scripts.structure.hierarchy import (
    StructureTreeBuilderConfig,
    build_document_structure,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentTitle,
)


def _hc(block_index: int, text: str, source: str = "regex_numbered_1",
        level: int = 1, score: float = 0.7, raw_number: str | None = None):
    return HeadingCandidate(
        block_index=block_index, text=text, score=score, source=source,
        level=level, raw_number=raw_number,
    )


def test_build_structure_empty():
    s = build_document_structure([], total_blocks=10, document_id="test")
    assert s.root_id == "n_0000"
    assert len(s.nodes) == 1
    assert s.coverage_ratio == 1.0


def test_build_structure_with_one_candidate():
    cs = [_hc(0, "1. Общие положения", score=0.7, raw_number="1")]
    s = build_document_structure(cs, total_blocks=10, document_id="test")
    assert len(s.nodes) == 2
    assert s.root_id in s.nodes
    section_ids = s.nodes[s.root_id].children
    assert len(section_ids) == 1
    sec = s.nodes[section_ids[0]]
    assert sec.start_block == 0
    assert sec.end_block == 9
    assert sec.title == "1. Общие положения"


def test_build_structure_multiple_sections():
    cs = [
        _hc(0, "1. Первая", score=0.7),
        _hc(5, "2. Вторая", score=0.7),
    ]
    s = build_document_structure(cs, total_blocks=10, document_id="test")
    section_ids = s.nodes[s.root_id].children
    sec1 = s.nodes[section_ids[0]]
    sec2 = s.nodes[section_ids[1]]
    assert sec1.start_block == 0
    assert sec1.end_block == 4
    assert sec2.start_block == 5
    assert sec2.end_block == 9


def test_build_structure_skips_outline_unmapped():
    cs = [
        _hc(0, "1. Первая", score=0.7),
        _hc(-1, "PDF outline entry", score=0.95, source="pdf_outline"),
    ]
    s = build_document_structure(cs, total_blocks=10, document_id="test")
    section_ids = s.nodes[s.root_id].children
    assert len(section_ids) == 1


def test_build_structure_docx_style_evidence_high():
    cs = [_hc(0, "Heading 1", score=0.95, source="docx_style", level=1)]
    s = build_document_structure(cs, total_blocks=5, document_id="test")
    sec_id = s.nodes[s.root_id].children[0]
    sec = s.nodes[sec_id]
    assert sec.evidence[0].weight == 0.95


def test_build_structure_legal_article_semantic():
    cs = [_hc(0, "Статья 1. Права", score=0.85, source="regex_statiya")]
    s = build_document_structure(cs, total_blocks=5, document_id="test")
    sec_id = s.nodes[s.root_id].children[0]
    sec = s.nodes[sec_id]
    assert sec.semantic_type == "article"
    assert sec.number is not None
    assert sec.number.scheme == "legal_article"


def test_build_structure_title_kept():
    title = DocumentTitle(value="T", source="metadata", confidence=1.0)
    s = build_document_structure([], total_blocks=5, document_id="test", title=title)
    assert s.title == title


def test_build_structure_document_id_from_config():
    s = build_document_structure(
        [], total_blocks=5, document_id="test", config=StructureTreeBuilderConfig(document_id="my-doc"),
    )
    assert s.document_id == "my-doc"


def test_build_structure_sorted_by_block_index():
    cs = [
        _hc(5, "2.", score=0.7),
        _hc(0, "1.", score=0.7),
        _hc(10, "3.", score=0.7),
    ]
    s = build_document_structure(cs, total_blocks=15, document_id="test")
    section_ids = s.nodes[s.root_id].children
    assert [s.nodes[id].start_block for id in section_ids] == [0, 5, 10]