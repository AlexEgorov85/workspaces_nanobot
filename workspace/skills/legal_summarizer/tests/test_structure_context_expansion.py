"""Тесты для context expansion (Этап 37 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.context_expansion import (
    ContextExpansionConfig, expand_context,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator, TokenEstimatorConfig,
)


def _b() -> StructureNode:
    return StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=("n_0001", "n_0002"), start_block=0, end_block=10,
        confidence=1.0,
    )


def _sec(nid: str, title: str, parent: str = "n_0000") -> StructureNode:
    return StructureNode(
        node_id=nid, node_type="section", semantic_type=None,
        level=1, title=title, number=None, parent_id=parent,
        children=(), start_block=0, end_block=5,
        confidence=0.7,
    )


def _c(cid: str, section: str, text: str = "x", idx: int = 0) -> Chunk:
    return Chunk(
        chunk_id=cid, index=idx, text=text, char_count=len(text),
        token_estimate=10, page_start=1, page_end=1,
        section_id=section, section_path="1", section_heading=section,
        block_indices=(0,), block_types=("paragraph",),
    )


def _struct() -> DocumentStructure:
    return DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _b(),
            "n_0001": _sec("n_0001", "Section 1"),
            "n_0002": _sec("n_0002", "Section 2"),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )


def test_expand_context_returns_target_and_metadata():
    chunks = (
        _c("001", "n_0001", text="target text"),
        _c("002", "n_0001", text="neighbour"),
        _c("003", "n_0002", text="other section"),
    )
    struct = _struct()
    target = chunks[0]
    result = expand_context(target, chunks, struct)
    assert result.target_chunk == target
    assert result.section_title == "Section 1"
    assert "002" in [c.chunk_id for c in result.neighbour_chunks]


def test_expand_context_respects_token_limit():
    chunks = (
        _c("001", "n_0001", text="x"),
        _c("002", "n_0001", text="x" * 10000),
        _c("003", "n_0001", text="y" * 10000),
    )
    struct = _struct()
    cfg = ContextExpansionConfig(max_context_tokens=500)
    result = expand_context(chunks[0], chunks, struct, config=cfg)
    assert result.truncated is True


def test_expand_context_respects_neighbour_limit():
    chunks = tuple(_c(f"{i:03d}", "n_0001", text="x") for i in range(10))
    struct = _struct()
    cfg = ContextExpansionConfig(max_neighbour_blocks=2)
    result = expand_context(chunks[0], chunks, struct, config=cfg)
    assert len(result.neighbour_chunks) <= 2


def test_expand_context_includes_parent_heading():
    chunks = (
        _c("001", "n_0001", text="x"),
        _c("002", "n_0001", text="y"),
    )
    struct = _struct()
    result = expand_context(chunks[0], chunks, struct)
    assert result.parent_heading == ""


def test_expand_context_no_chunks():
    chunks = (_c("001", "n_0001"),)
    struct = _struct()
    result = expand_context(chunks[0], chunks, struct)
    assert result.neighbour_chunks == ()


def test_expand_context_uses_custom_estimator():
    chunks = (
        _c("001", "n_0001", text="x"),
        _c("002", "n_0001", text="y"),
    )
    struct = _struct()
    estimator = TokenEstimator(TokenEstimatorConfig(chars_per_token=2.0))
    result = expand_context(chunks[0], chunks, struct, estimator=estimator)
    assert result.total_tokens > 0


def test_expand_context_target_not_found():
    chunks = (_c("001", "n_0001"),)
    struct = _struct()
    fake_target = _c("999", "n_0001")
    result = expand_context(fake_target, chunks, struct)
    assert result.target_chunk == fake_target
    assert result.neighbour_chunks == ()