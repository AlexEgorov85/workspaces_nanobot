"""Тесты для DocumentStructure контракта (Этап 2 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    DocumentTitle,
    NumberingInfo,
    StructureEvidence,
    StructureNode,
    _make_node_id,
)


def _make_node(
    *,
    node_id: str = "n_0001",
    node_type: str = "section",
    semantic_type: str | None = "article",
    level: int = 1,
    title: str = "Статья 1",
    parent_id: str | None = None,
    children: tuple[str, ...] = (),
    start_block: int = 0,
    end_block: int = 5,
    confidence: float = 0.95,
    evidence: tuple[StructureEvidence, ...] = (),
    number: NumberingInfo | None = None,
) -> StructureNode:
    return StructureNode(
        node_id=node_id,
        node_type=node_type,
        semantic_type=semantic_type,
        level=level,
        title=title,
        number=number,
        parent_id=parent_id,
        children=children,
        start_block=start_block,
        end_block=end_block,
        confidence=confidence,
        evidence=evidence,
    )


def test_node_id_generator_format():
    assert _make_node_id(0) == "n_0000"
    assert _make_node_id(123) == "n_0123"
    assert _make_node_id(9999) == "n_9999"


def test_structure_node_is_frozen():
    import dataclasses

    node = _make_node()
    try:
        node.title = "mutated"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("StructureNode must be frozen")


def test_structure_node_to_dict_roundtrip():
    node = _make_node(
        evidence=(StructureEvidence(source="docx_style", weight=0.95),),
        number=NumberingInfo(raw="1", scheme="decimal", components=(1,), level=1, ordinal=1),
    )
    d = node.to_dict()
    assert d["node_id"] == "n_0001"
    assert d["node_type"] == "section"
    assert d["semantic_type"] == "article"
    assert d["level"] == 1
    assert d["title"] == "Статья 1"
    assert d["number"]["scheme"] == "decimal"
    assert d["number"]["components"] == [1]
    assert d["evidence"][0]["source"] == "docx_style"


def test_document_structure_get_node_and_iter():
    root = _make_node(node_id="root", level=0, title="", semantic_type=None, node_type="document")
    child = _make_node(node_id="n1", parent_id="root", children=(), start_block=0, end_block=3)
    leaf = _make_node(node_id="n2", parent_id="n1", start_block=4, end_block=7, level=2)

    root_with_kids = StructureNode(
        node_id=root.node_id,
        node_type=root.node_type,
        semantic_type=root.semantic_type,
        level=root.level,
        title=root.title,
        number=None,
        parent_id=None,
        children=(child.node_id, leaf.node_id),
        start_block=0,
        end_block=7,
        confidence=1.0,
    )

    s = DocumentStructure(
        document_id="doc1",
        title=DocumentTitle(value="T", source="metadata", confidence=1.0),
        nodes={root_with_kids.node_id: root_with_kids, child.node_id: child, leaf.node_id: leaf},
        root_id="root",
        preamble_node_id="root",
        numbering=(),
        total_blocks=8,
    )
    assert s.get_node("n1") is child
    assert s.get_node("nope") is None
    assert len(s.iter_nodes()) == 3
    assert s.iter_nodes()[0].node_id == "root"


def test_document_structure_iter_sections():
    sec = _make_node(node_id="sec1", node_type="section", start_block=0, end_block=10)
    body = _make_node(node_id="b1", node_type="body", start_block=2, end_block=4)
    s = DocumentStructure(
        document_id="d",
        title=None,
        nodes={"sec1": sec, "b1": body},
        root_id="sec1",
        preamble_node_id="sec1",
        numbering=(),
        total_blocks=11,
    )
    sections = s.iter_sections()
    assert len(sections) == 1
    assert sections[0].node_id == "sec1"


def test_document_structure_block_to_node():
    root = _make_node(node_id="root", node_type="document", semantic_type=None, level=0, start_block=0, end_block=9)
    sec = _make_node(node_id="sec1", start_block=2, end_block=5)
    s = DocumentStructure(
        document_id="d",
        title=None,
        nodes={"root": root, "sec1": sec},
        root_id="root",
        preamble_node_id="root",
        numbering=(),
        total_blocks=10,
    )
    mapping = s.block_to_node()
    assert mapping[0] == "root"
    assert mapping[1] == "root"
    assert mapping[2] == "sec1"
    assert mapping[5] == "sec1"
    assert mapping[6] == "root"


def test_numbering_info_decimal():
    ni = NumberingInfo(raw="1.2.3", scheme="decimal", components=(1, 2, 3), level=3, ordinal=3)
    assert ni.scheme == "decimal"
    assert ni.components == (1, 2, 3)
    assert ni.level == 3


def test_document_title_sources():
    for source in ("metadata", "visual", "inferred"):
        t = DocumentTitle(value="X", source=source, confidence=0.5, block_ordinal=0)
        assert t.source == source