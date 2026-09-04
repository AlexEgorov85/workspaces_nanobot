"""Тесты для first-run/follow-up split (Этап 40 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.followup import (
    build_first_run_analysis, build_followup_response,
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
        f.write("test")
        path = f.name
    return PhysicalDocument(
        path=path, format="txt", title=None, size_bytes=4,
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


def _build_analysis(chunks_text: list[str]) -> DocumentAnalysis:
    chunks = tuple(
        _c(f"{i:03d}", text, idx=i)
        for i, text in enumerate(chunks_text)
    )
    return DocumentAnalysis.build(
        physical=_doc(), structure=_struct(), chunks=chunks,
    )


def test_first_run_returns_analysis():
    analysis = _build_analysis(["x" * 100])
    out = build_first_run_analysis(analysis=analysis)
    assert out is analysis


def test_followup_brief_mode():
    analysis = _build_analysis(["x" * 100, "y" * 100, "z" * 100])
    result = build_followup_response(analysis, mode="brief")
    assert result.confidence == "medium"
    assert not result.used_full_doc_fallback


def test_followup_question_with_hits():
    chunks_text = [
        "оплата по договору 30 дней",
        "другой текст",
        "оплата за услуги",
        "оплата налогов",
    ]
    analysis = _build_analysis(chunks_text)
    result = build_followup_response(
        analysis, query="оплата", mode="question",
    )
    assert result.confidence in ("high", "medium")
    assert not result.used_full_doc_fallback


def test_followup_question_no_hits_uses_fallback():
    chunks_text = ["some random text"] * 5
    analysis = _build_analysis(chunks_text)
    result = build_followup_response(
        analysis, query="xyzabc123", mode="question",
    )
    assert result.used_full_doc_fallback is True
    assert result.confidence == "very_low"


def test_followup_question_low_confidence_expands():
    chunks_text = [
        "общий текст 1",
        "общий текст 2",
        "оплата по факту",
        "другое 4",
        "другое 5",
    ]
    analysis = _build_analysis(chunks_text)
    result = build_followup_response(
        analysis, query="оплата", mode="question",
    )
    assert result.confidence in ("low", "medium", "high")


def test_followup_uses_cached_analysis_no_reparse():
    """PLAN §41: follow-up не должен перепарсивать документ."""
    analysis = _build_analysis(["x" * 100])
    analysis_id_before = analysis.identity.document_id
    _ = build_followup_response(analysis, query="x", mode="question")
    assert analysis.identity.document_id == analysis_id_before


def test_followup_result_to_dict():
    analysis = _build_analysis(["x" * 100])
    result = build_followup_response(analysis, mode="brief")
    d = result.to_dict()
    assert "chunk_count" in d
    assert "confidence" in d