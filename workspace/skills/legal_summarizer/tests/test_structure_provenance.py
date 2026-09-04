"""Тесты для provenance (Этап 46 из PLAN.md)."""

from __future__ import annotations

import tempfile

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.provenance import (
    build_provenance_chain,
)


def _b(ord: int, page: int = 1) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="page", content="x",
        char_count=1, page_index=page, page_start=page, page_end=page,
        paragraph_index=None, table_index=None, ordinal=ord,
        block_metadata={},
    )


def _doc() -> PhysicalDocument:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        path = f.name
    return PhysicalDocument(
        path=path, format="txt", title=None, size_bytes=0,
        blocks=tuple(_b(i, page=i + 1) for i in range(3)), page_count=3,
    )


def _struct() -> DocumentStructure:
    return DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": StructureNode(
                node_id="n_0000", node_type="document", semantic_type=None,
                level=0, title="", number=None, parent_id=None,
                children=("n_0001",), start_block=0, end_block=2,
                confidence=1.0,
            ),
            "n_0001": StructureNode(
                node_id="n_0001", node_type="section", semantic_type=None,
                level=1, title="Section A", number=None,
                parent_id="n_0000", children=(),
                start_block=0, end_block=2, confidence=0.7,
            ),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=3,
    )


def _c(cid: str, idx: int = 0, ordinals: tuple[int, ...] = (0, 1)) -> Chunk:
    return Chunk(
        chunk_id=cid, index=idx, text="x", char_count=1, token_estimate=1,
        page_start=1, page_end=1, section_id="n_0001",
        section_path="1", section_heading="Section A",
        block_indices=ordinals, block_types=("page",),
    )


def test_build_provenance_chain_basic():
    doc = _doc()
    struct = _struct()
    chunk = _c("001")
    chain = build_provenance_chain(
        chunk, doc=doc, struct=struct, document_id="doc-1",
    )
    assert chain is not None
    assert chain.document_id == "doc-1"
    assert chain.section_title == "Section A"
    assert chain.chunk_id == "001"
    assert chain.page_start == 1
    assert chain.page_end == 2


def test_provenance_chain_is_complete():
    doc = _doc()
    struct = _struct()
    chunk = _c("001")
    chain = build_provenance_chain(
        chunk, doc=doc, struct=struct, document_id="doc-1",
    )
    assert chain.is_complete() is True


def test_provenance_chain_to_dict():
    doc = _doc()
    struct = _struct()
    chunk = _c("001")
    chain = build_provenance_chain(
        chunk, doc=doc, struct=struct, document_id="doc-1",
    )
    d = chain.to_dict()
    assert d["chunk_id"] == "001"
    assert d["section_title"] == "Section A"


def test_provenance_chain_with_invalid_block_indices():
    doc = _doc()
    struct = _struct()
    chunk = _c("001", ordinals=(999,))
    chain = build_provenance_chain(
        chunk, doc=doc, struct=struct, document_id="doc-1",
    )
    assert chain is None