"""Тесты для structure-aware retrieval (Этап 65 из PLAN.md).

PLAN §65: при ranking учитывать:

* heading text (section_title_weight);
* section title;
* body text;
* caption;
* table content;
* semantic type;
* section importance.

Сейчас реализовано:

* ``section_title_weight=2.0`` (boost когда термин в section title).
* ``heading_weight=1.5`` (boost для коротких heading chunks).
* ``body_weight=1.0`` (baseline).
"""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.retrieval import (
    RetrievalConfig, retrieve_chunks,
)


def _b(ord: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="paragraph", content="x",
        char_count=1, page_index=1, page_start=1, page_end=1,
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
        blocks=tuple(_b(i) for i in range(3)), page_count=1,
    )


def _c(cid: str, text: str, section_heading: str = "") -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text=text, char_count=len(text),
        token_estimate=1, page_start=1, page_end=1,
        section_id="s1", section_path="1", section_heading=section_heading,
        block_indices=(0,), block_types=("paragraph",),
    )


def test_section_title_boost():
    chunks = (
        _c("001", "оплата", section_heading="Прочее"),
        _c("002", "оплата", section_heading="Раздел оплата"),
    )
    hits = retrieve_chunks(chunks, "оплата")
    assert hits[0].chunk_id == "002"
    assert hits[0].section_title_hit is True


def test_heading_boost_short_text():
    chunks = (
        _c("001", "оплата"),
        _c("002", "оплата длинное описание с большим количеством слов " * 10),
    )
    hits = retrieve_chunks(chunks, "оплата")
    titles = [h.title_hit for h in hits]
    assert True in titles


def test_body_weight_baseline():
    """Body weight = 1.0 (baseline)."""
    cfg = RetrievalConfig()
    assert cfg.body_weight == 1.0
    assert cfg.section_title_weight == 2.0
    assert cfg.heading_weight == 1.5


def test_combined_evidence_score():
    """Chunk в section title + body → max score."""
    chunks = (
        _c("001", "другое слово", section_heading="Оплата"),
        _c("002", "оплата длинное описание с большим количеством слов " * 10,
            section_heading="Срок"),
    )
    hits = retrieve_chunks(chunks, "оплата")
    assert hits[0].chunk_id == "001"
    assert hits[0].score >= 2.0


def test_structure_node_lookup_for_retrieval():
    """DocumentStructure можно использовать для context ranking."""
    struct = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": StructureNode(
            node_id="n_0000", node_type="document", semantic_type=None,
            level=0, title="", number=None, parent_id=None,
            children=("n_0001",), start_block=0, end_block=2,
            confidence=1.0,
        ), "n_0001": StructureNode(
            node_id="n_0001", node_type="section", semantic_type=None,
            level=1, title="Срок оплаты", number=None,
            parent_id="n_0000", children=(), start_block=0, end_block=2,
            confidence=0.7,
        )},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=3,
    )
    section = struct.nodes["n_0001"]
    assert "оплаты" in section.title.lower()
    assert section.node_type == "section"
    assert section.parent_id == "n_0000"