"""Тесты для brief from analysis (Этап 63 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.brief_from_analysis import (
    select_brief_chunks_from_analysis,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.importance_brief import (
    BriefSelectionConfig,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
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


def _c(cid: str, text: str, idx: int = 0) -> Chunk:
    return Chunk(
        chunk_id=cid, index=idx, text=text, char_count=len(text),
        token_estimate=1, page_start=1, page_end=1,
        section_id="s1", section_path="1", section_heading="x",
        block_indices=(0,), block_types=("paragraph",),
    )


def _struct() -> DocumentStructure:
    return DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": StructureNode(
            node_id="n_0000", node_type="document", semantic_type=None,
            level=0, title="", number=None, parent_id=None,
            children=("n_0001", "n_0002"), start_block=0, end_block=2,
            confidence=1.0,
        ), "n_0001": StructureNode(
            node_id="n_0001", node_type="section", semantic_type=None,
            level=1, title="Section 1", number=None,
            parent_id="n_0000", children=(), start_block=0, end_block=1,
            confidence=0.7,
        ), "n_0002": StructureNode(
            node_id="n_0002", node_type="section", semantic_type=None,
            level=1, title="Section 2", number=None,
            parent_id="n_0000", children=(), start_block=2, end_block=2,
            confidence=0.7,
        )},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=3,
    )


def test_select_brief_chunks_from_analysis():
    chunks = (
        _c("001", "Section 1", idx=0),
        _c("002", "Section 1", idx=1),
        _c("003", "Section 2", idx=2),
    )
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
    )
    result = select_brief_chunks_from_analysis(analysis)
    assert len(result) > 0
    assert len(result) <= len(chunks)


def test_brief_does_not_reparse():
    chunks = (_c("001", "Section 1"),)
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
    )
    doc_id_before = analysis.identity.document_id
    _ = select_brief_chunks_from_analysis(analysis)
    assert analysis.identity.document_id == doc_id_before


def test_brief_respects_config():
    chunks = (_c("001", "x"), _c("002", "y"), _c("003", "z"))
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
    )
    config = BriefSelectionConfig(target_chunk_count=2, coverage_ratio=1.0)
    result = select_brief_chunks_from_analysis(analysis, config=config)
    assert len(result) <= 2