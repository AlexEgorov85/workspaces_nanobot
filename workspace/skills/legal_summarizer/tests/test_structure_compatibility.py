"""Тесты для back-compat adapter (Этап 58 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.compatibility import (
    section_tree_from_structure,
    structure_from_section_tree,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    ROOT_SECTION_ID, DocumentSection, SectionTree,
)


def _b(ord: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="paragraph", content="x",
        char_count=1, page_index=None, page_start=None, page_end=None,
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
        blocks=tuple(_b(i) for i in range(5)), page_count=1,
    )


def _struct() -> DocumentStructure:
    return DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": StructureNode(
            node_id="n_0000", node_type="document", semantic_type=None,
            level=0, title="", number=None, parent_id=None,
            children=("n_0001",), start_block=0, end_block=4,
            confidence=1.0,
        ), "n_0001": StructureNode(
            node_id="n_0001", node_type="section", semantic_type=None,
            level=1, title="Section A", number=None,
            parent_id="n_0000", children=(), start_block=0, end_block=4,
            confidence=0.7,
        )},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )


def test_section_tree_from_structure():
    doc_blocks = tuple(_b(i) for i in range(5))
    tree = section_tree_from_structure(_struct(), doc_blocks)
    assert tree.root_id == ROOT_SECTION_ID
    assert any(
        sec.heading == "Section A"
        for sec in tree.sections.values()
    )


def test_section_tree_preserves_block_to_section():
    doc_blocks = tuple(_b(i) for i in range(5))
    tree = section_tree_from_structure(_struct(), doc_blocks)
    assert tree.block_to_section[0] != ROOT_SECTION_ID


def test_structure_from_section_tree():
    tree = SectionTree(
        sections={ROOT_SECTION_ID: DocumentSection(
            section_id=ROOT_SECTION_ID, level=0, heading="",
            section_path="", block_indices=(0, 1, 2),
            children=("s_0001",), parent_id=None,
        ), "s_0001": DocumentSection(
            section_id="s_0001", level=1, heading="Section B",
            section_path="1", block_indices=(0, 1, 2),
            children=(), parent_id=ROOT_SECTION_ID,
        )},
        root_id=ROOT_SECTION_ID,
        block_to_section={0: "s_0001", 1: "s_0001", 2: "s_0001"},
    )
    struct = structure_from_section_tree(tree, total_blocks=3)
    assert struct.root_id == "n_0000"
    assert any(
        n.title == "Section B"
        for n in struct.nodes.values()
    )


def test_round_trip_preserves_section_count():
    doc_blocks = tuple(_b(i) for i in range(5))
    struct1 = _struct()
    tree = section_tree_from_structure(struct1, doc_blocks)
    struct2 = structure_from_section_tree(tree, total_blocks=5)
    sec1 = sum(1 for n in struct1.nodes.values() if n.node_type == "section")
    sec2 = sum(1 for n in struct2.nodes.values() if n.node_type == "section")
    assert sec1 == sec2 == 1


def test_section_tree_from_empty_structure():
    empty = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": StructureNode(
            node_id="n_0000", node_type="document", semantic_type=None,
            level=0, title="", number=None, parent_id=None,
            children=(), start_block=0, end_block=4, confidence=1.0,
        )},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    tree = section_tree_from_structure(empty, tuple(_b(i) for i in range(5)))
    assert tree.root_id == ROOT_SECTION_ID
    assert len(tree.sections) == 1