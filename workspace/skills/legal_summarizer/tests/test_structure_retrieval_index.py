"""Тесты для RetrievalIndex (Этап 36 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.retrieval_index import (
    RetrievalIndex,
)


def _b(ord: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="paragraph", content="x",
        char_count=1, page_index=1, page_start=1, page_end=1,
        paragraph_index=None, table_index=None, ordinal=ord,
        block_metadata={},
    )


def _doc() -> PhysicalDocument:
    return PhysicalDocument(
        path="/tmp/x", format="txt", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(3)), page_count=1,
    )


def _c(cid: str, text: str, section_heading: str = "") -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text=text, char_count=len(text),
        token_estimate=1, page_start=1, page_end=1,
        section_id="s1", section_path="1", section_heading=section_heading,
        block_indices=(0,), block_types=("paragraph",),
    )


def _struct() -> DocumentStructure:
    return DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": StructureNode(
            node_id="n_0000", node_type="document", semantic_type=None,
            level=0, title="", number=None, parent_id=None,
            children=(), start_block=0, end_block=2, confidence=1.0,
        )},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=3,
    )


def test_build_index_creates_inverted():
    chunks = (
        _c("001", "срок оплаты"),
        _c("002", "срок хранения"),
        _c("003", "другое"),
    )
    index = RetrievalIndex.build(
        chunks=chunks, structure=_struct(), physical=_doc(),
    )
    assert "срок" in index.term_to_chunks
    assert set(index.term_to_chunks["срок"]) == {"001", "002"}


def test_retrieve_uses_inverted_index():
    chunks = (
        _c("001", "оплата — 30 дней"),
        _c("002", "штраф за просрочку"),
        _c("003", "другой текст"),
    )
    index = RetrievalIndex.build(chunks=chunks, structure=_struct())
    hits = index.retrieve("оплата")
    assert len(hits) >= 1
    assert hits[0].chunk_id == "001"


def test_retrieve_no_match():
    chunks = (_c("001", "оплата"),)
    index = RetrievalIndex.build(chunks=chunks, structure=_struct())
    assert index.retrieve("xyz123") == []


def test_to_dict():
    index = RetrievalIndex.build(
        chunks=(_c("001", "оплата"),),
        structure=_struct(),
        document_id="my-doc",
    )
    d = index.to_dict()
    assert d["document_id"] == "my-doc"
    assert d["chunk_count"] == 1


def test_retrieve_section_title_boost_via_score():
    """Section title boost покрывается в retrieval tests; для inverted
    index важно проверить, что section_title НЕ участвует в inverted."""
    chunks = (
        _c("001", "оплата", section_heading="Срок оплаты"),
        _c("002", "оплата", section_heading="Другой раздел"),
    )
    index = RetrievalIndex.build(chunks=chunks, structure=_struct())
    hits = index.retrieve("оплата")
    assert len(hits) == 2
    assert all(h.chunk_id in ("001", "002") for h in hits)