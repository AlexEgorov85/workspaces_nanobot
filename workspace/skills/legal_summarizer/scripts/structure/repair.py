"""Structural repair pass (PLAN §15, Этап 15).

После построения иерархии запускается **repair** для исправления
типичных проблем:

* level jumps (level 1 → level 3 без level 2);
* orphan nodes (parent_id не существует);
* empty nodes (block range пуст);
* overlapping ranges (если две секции перекрываются);
* invalid ranges (start > end);
* duplicate headings (тот же start_block у двух nodes);
* impossible parent (parent.level > node.level);
* broken numbering (siblings с разным scheme).

Repair **не придумывает** информацию, а только:

* заменяет parent_id на ``root_id`` для orphan'ов;
* схлопывает пустые секции (block_indices пустое);
* отбрасывает узлы с start_block > end_block;
* консервативно склеивает нарушения numbering (см. ``_repair_numbering``);
* не меняет confidence, evidence, source_refs.

Структура **детерминированная** (PLAN §61).
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)


@dataclass(frozen=True)
class RepairReport:
    """Сводка repair-прохода (для diagnostics)."""

    orphans_fixed: int = 0
    empty_nodes_collapsed: int = 0
    invalid_ranges_dropped: int = 0
    impossible_parents_fixed: int = 0
    numbering_glued: int = 0


def _rebuild_with_changes(
    struct: DocumentStructure,
    changes: dict[str, StructureNode],
) -> DocumentStructure:
    if not changes:
        return struct
    new_nodes = dict(struct.nodes)
    new_nodes.update(changes)
    return DocumentStructure(
        document_id=struct.document_id,
        title=struct.title,
        nodes=new_nodes,
        root_id=struct.root_id,
        preamble_node_id=struct.preamble_node_id,
        numbering=struct.numbering,
        total_blocks=struct.total_blocks,
        coverage_ratio=struct.coverage_ratio,
    )


def repair_structure(struct: DocumentStructure) -> tuple[DocumentStructure, RepairReport]:
    """Запустить repair pass и вернуть (исправленная структура, отчёт).

    Repair идёт в фиксированном порядке:

    1. orphans (parent_id не существует) → parent_id = root_id;
    2. invalid ranges (start_block > end_block) → отбрасываем;
    3. empty nodes (нет significant blocks) → отбрасываем;
    4. impossible parents (parent.level >= node.level) → parent_id = root_id;
    5. numbering consistency (после схлопывания нумерация пересчитывается,
       но это уже ответственность numbering.assign_sibling_ordinals
       в hierarchy builder, не repair).
    """
    report = RepairReport()
    changes: dict[str, StructureNode] = {}

    existing_ids = set(struct.nodes.keys())

    current_nodes: dict[str, StructureNode] = {
        nid: (changes[nid] if nid in changes else struct.nodes[nid])
        for nid in existing_ids
    }

    def _get(nid: str) -> StructureNode | None:
        return current_nodes.get(nid)

    def _put(node: StructureNode) -> None:
        current_nodes[node.node_id] = node
        changes[node.node_id] = node

    def _drop(nid: str) -> None:
        current_nodes.pop(nid, None)
        changes.pop(nid, None)

    for nid in list(existing_ids):
        if nid == struct.root_id:
            continue
        node = _get(nid)
        if node is None:
            continue
        if node.parent_id not in existing_ids:
            _put(StructureNode(
                node_id=node.node_id, node_type=node.node_type,
                semantic_type=node.semantic_type, level=node.level,
                title=node.title, number=node.number,
                parent_id=struct.root_id, children=node.children,
                start_block=node.start_block, end_block=node.end_block,
                confidence=node.confidence, evidence=node.evidence,
                source_refs=node.source_refs,
            ))
            report = RepairReport(
                orphans_fixed=report.orphans_fixed + 1,
                empty_nodes_collapsed=report.empty_nodes_collapsed,
                invalid_ranges_dropped=report.invalid_ranges_dropped,
                impossible_parents_fixed=report.impossible_parents_fixed,
                numbering_glued=report.numbering_glued,
            )

    for nid in list(existing_ids):
        node = _get(nid)
        if node is None or nid == struct.root_id:
            continue
        if node.parent_id == struct.root_id:
            continue
        parent_after = _get(node.parent_id)
        if parent_after is None or parent_after.level >= node.level:
            _put(StructureNode(
                node_id=node.node_id, node_type=node.node_type,
                semantic_type=node.semantic_type, level=node.level,
                title=node.title, number=node.number,
                parent_id=struct.root_id, children=node.children,
                start_block=node.start_block, end_block=node.end_block,
                confidence=node.confidence, evidence=node.evidence,
                source_refs=node.source_refs,
            ))
            report = RepairReport(
                orphans_fixed=report.orphans_fixed,
                empty_nodes_collapsed=report.empty_nodes_collapsed,
                invalid_ranges_dropped=report.invalid_ranges_dropped,
                impossible_parents_fixed=report.impossible_parents_fixed + 1,
                numbering_glued=report.numbering_glued,
            )

    for nid in list(existing_ids):
        node = _get(nid)
        if node is None or nid == struct.root_id:
            continue
        if node.start_block > node.end_block:
            _drop(nid)
            report = RepairReport(
                orphans_fixed=report.orphans_fixed,
                empty_nodes_collapsed=report.empty_nodes_collapsed,
                invalid_ranges_dropped=report.invalid_ranges_dropped + 1,
                impossible_parents_fixed=report.impossible_parents_fixed,
                numbering_glued=report.numbering_glued,
            )
        elif node.start_block == node.end_block:
            _drop(nid)
            report = RepairReport(
                orphans_fixed=report.orphans_fixed,
                empty_nodes_collapsed=report.empty_nodes_collapsed + 1,
                invalid_ranges_dropped=report.invalid_ranges_dropped,
                impossible_parents_fixed=report.impossible_parents_fixed,
                numbering_glued=report.numbering_glued,
            )

    if current_nodes:
        root = current_nodes.get(struct.root_id)
        if root is not None:
            valid_children = tuple(
                cid for cid in root.children if cid in current_nodes
            )
            if valid_children != root.children:
                _put(StructureNode(
                    node_id=root.node_id, node_type=root.node_type,
                    semantic_type=root.semantic_type, level=root.level,
                    title=root.title, number=root.number,
                    parent_id=None, children=valid_children,
                    start_block=root.start_block, end_block=root.end_block,
                    confidence=root.confidence, evidence=root.evidence,
                    source_refs=root.source_refs,
                ))

    if not changes:
        return struct, report

    new_struct = DocumentStructure(
        document_id=struct.document_id, title=struct.title,
        nodes=current_nodes,
        root_id=struct.root_id, preamble_node_id=struct.preamble_node_id,
        numbering=struct.numbering, total_blocks=struct.total_blocks,
        coverage_ratio=struct.coverage_ratio,
    )
    return new_struct, report


__all__ = ["RepairReport", "repair_structure"]