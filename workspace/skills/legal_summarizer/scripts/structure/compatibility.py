"""SectionTreeAdapter (PLAN §58, Этап 58).

Back-compat layer между legacy ``SectionTree`` и новым
``DocumentStructure`` (PLAN §45, §58).

После миграции consumers на ``DocumentStructure`` legacy API может
быть удалён (Этап 78), но до тех пор нужен адаптер.

* ``section_tree_from_structure(struct)`` — ``DocumentStructure`` → ``SectionTree``.
* ``structure_from_section_tree(tree, blocks, doc_id)`` — ``SectionTree`` →
  ``DocumentStructure``.
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    DocumentSection, ROOT_SECTION_ID, SectionTree,
)


def section_tree_from_structure(
    struct: DocumentStructure,
    blocks: tuple[DocumentBlock, ...],
) -> SectionTree:
    """``DocumentStructure`` → ``SectionTree``.

    Используется legacy consumers (chunks.py, reducer.py), которые
    ещё не перешли на новый pipeline.
    """
    sections: dict[str, DocumentSection] = {
        ROOT_SECTION_ID: DocumentSection(
            section_id=ROOT_SECTION_ID,
            level=0,
            heading="",
            section_path="",
            block_indices=tuple(b.ordinal for b in blocks),
            children=(),
            parent_id=None,
        ),
    }
    block_to_section: dict[int, str] = {
        b.ordinal: ROOT_SECTION_ID for b in blocks
    }

    sec_counter = 0

    def _next_id() -> str:
        nonlocal sec_counter
        sid = f"s_{sec_counter:04d}"
        sec_counter += 1
        return sid

    for nid, node in struct.nodes.items():
        if nid == struct.root_id:
            continue
        if node.node_type != "section":
            continue
        sid = _next_id()
        parent_id = node.parent_id if node.parent_id else ROOT_SECTION_ID
        sections[sid] = DocumentSection(
            section_id=sid,
            level=node.level,
            heading=node.title,
            section_path=str(node.number.ordinal) if node.number and node.number.ordinal else "",
            block_indices=tuple(range(node.start_block, node.end_block + 1)),
            children=(),
            parent_id=parent_id,
        )
        for b in range(node.start_block, node.end_block + 1):
            block_to_section[b] = sid

    for sid, sec in sections.items():
        if sec.parent_id and sec.parent_id in sections:
            parent = sections[sec.parent_id]
            sections[sec.parent_id] = DocumentSection(
                section_id=parent.section_id,
                level=parent.level,
                heading=parent.heading,
                section_path=parent.section_path,
                block_indices=parent.block_indices,
                children=parent.children + (sid,),
                parent_id=parent.parent_id,
            )

    return SectionTree(
        sections=sections,
        root_id=ROOT_SECTION_ID,
        block_to_section=block_to_section,
    )


def structure_from_section_tree(
    tree: SectionTree,
    *,
    total_blocks: int,
    document_id: str = "doc",
    title=None,
) -> DocumentStructure:
    """``SectionTree`` → ``DocumentStructure``.

    Используется при обратной миграции (если нужно снова сконвертировать).
    """
    from workspace.skills.legal_summarizer.scripts.structure.models import (
        DocumentStructure, StructureNode, _make_node_id,
    )

    nodes: dict[str, StructureNode] = {}
    root = StructureNode(
        node_id="n_0000",
        node_type="document",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=(),
        start_block=0,
        end_block=max(0, total_blocks - 1),
        confidence=1.0,
    )
    nodes[root.node_id] = root

    section_nodes = [
        (sid, sec) for sid, sec in tree.sections.items()
        if sid != ROOT_SECTION_ID
    ]
    section_nodes.sort(key=lambda kv: (min(kv[1].block_indices) if kv[1].block_indices else 0))

    child_ids_by_parent: dict[str | None, list[str]] = {}
    for i, (sid, sec) in enumerate(section_nodes, start=1):
        nid = _make_node_id(i)
        block_indices = sec.block_indices
        start = min(block_indices) if block_indices else 0
        end = max(block_indices) if block_indices else 0
        nodes[nid] = StructureNode(
            node_id=nid,
            node_type="section",
            semantic_type=None,
            level=sec.level,
            title=sec.heading,
            number=None,
            parent_id=root.node_id,
            children=(),
            start_block=start,
            end_block=end,
            confidence=0.7,
            source_refs=("legacy",),
        )
        child_ids_by_parent.setdefault(root.node_id, []).append(nid)

    if child_ids_by_parent:
        new_root = StructureNode(
            node_id=root.node_id,
            node_type=root.node_type,
            semantic_type=root.semantic_type,
            level=root.level,
            title=root.title,
            number=root.number,
            parent_id=None,
            children=tuple(child_ids_by_parent[root.node_id]),
            start_block=root.start_block,
            end_block=root.end_block,
            confidence=root.confidence,
            evidence=root.evidence,
            source_refs=root.source_refs,
        )
        nodes[root.node_id] = new_root

    return DocumentStructure(
        document_id=document_id,
        title=title,
        nodes=nodes,
        root_id=root.node_id,
        preamble_node_id=root.node_id,
        numbering=(),
        total_blocks=total_blocks,
        coverage_ratio=1.0,
    )


__all__ = [
    "section_tree_from_structure",
    "structure_from_section_tree",
]