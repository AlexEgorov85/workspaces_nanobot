"""Тесты для DocumentAnalysis (Этап 39 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.identity import (
    DocumentIdentity,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.semantic_record import (
    SemanticRecord,
)


def _b(ord: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="paragraph", content="x",
        char_count=1, page_index=1, page_start=1, page_end=1,
        paragraph_index=None, table_index=None, ordinal=ord,
        block_metadata={},
    )


def _doc(tmp_path_factory=None) -> PhysicalDocument:
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        f.write("test content")
        tmp_path = f.name
    return PhysicalDocument(
        path=tmp_path, format="txt", title=None, size_bytes=12,
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
            children=(), start_block=0, end_block=2, confidence=1.0,
        )},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=3,
    )


def test_build_creates_identity():
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=(_c("001", "x"),),
    )
    assert analysis.identity.document_id != ""


def test_build_includes_retrieval_index_by_default():
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=(_c("001", "оплата"),),
    )
    assert analysis.retrieval_index is not None


def test_build_skips_retrieval_index_when_disabled():
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=(_c("001", "x"),),
        include_retrieval_index=False,
    )
    assert analysis.retrieval_index is None


def test_get_chunk_by_id():
    chunks = (_c("001", "x"), _c("002", "y"))
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
    )
    assert analysis.get_chunk("001").text == "x"
    assert analysis.get_chunk("999") is None


def test_get_record_by_id():
    chunks = (_c("001", "x"),)
    records = {"001": SemanticRecord.from_minimal("001", "s1", "summary")}
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
        semantic_records=records,
    )
    assert analysis.get_record("001").summary == "summary"
    assert analysis.get_record("999") is None


def test_to_dict_roundtrip():
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=(_c("001", "x"),),
        created_at="2026-01-01T00:00:00Z",
    )
    d = analysis.to_dict()
    assert d["chunk_count"] == 1
    assert d["version"] == 1
    assert d["has_retrieval_index"] is True


def test_retrieve_uses_index():
    chunks = (_c("001", "оплата по договору"), _c("002", "другой текст"))
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
    )
    hits = analysis.retrieve("оплата")
    assert len(hits) >= 1
    assert hits[0].chunk_id == "001"


def test_retrieve_falls_back_when_no_index():
    chunks = (_c("001", "оплата"),)
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
        include_retrieval_index=False,
    )
    hits = analysis.retrieve("оплата")
    assert len(hits) >= 1


def test_custom_identity_used():
    chunks = (_c("001", "x"),)
    identity = DocumentIdentity.from_path_with_mtime(
        "C:/tmp/x.txt", size_bytes=0, mtime_ns=0,
    )
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
        identity=identity,
    )
    assert analysis.identity == identity