"""Тесты для question from analysis (Этап 64 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.question_from_analysis import (
    answer_question_from_analysis,
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
            children=(), start_block=0, end_block=2, confidence=1.0,
        )},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=3,
    )


def test_answer_question_with_hits():
    chunks = (
        _c("001", "оплата по договору"),
        _c("002", "другое"),
        _c("003", "оплата налогов"),
    )
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
    )
    response = answer_question_from_analysis(analysis, "оплата")
    assert response.confidence in ("high", "medium", "low")


def test_answer_question_no_match_uses_fallback():
    chunks = (_c("001", "текст"),)
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
    )
    response = answer_question_from_analysis(analysis, "xyzabc")
    assert response.used_full_doc_fallback is True


def test_answer_question_does_not_reparse():
    chunks = (_c("001", "оплата"),)
    analysis = DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
    )
    doc_id_before = analysis.identity.document_id
    answer_question_from_analysis(analysis, "оплата")
    assert analysis.identity.document_id == doc_id_before