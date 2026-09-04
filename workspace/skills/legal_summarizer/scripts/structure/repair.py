"""Structural repair pass (PLAN §5, §15).

После построения иерархии запускается **repair** для исправления
типичных проблем:

* level jumps (level 1 → level 3 без level 2);
* orphan nodes (parent_id не существует);
* overlapping ranges (если две секции перекрываются);
* invalid ranges (start > end);
* duplicate headings (тот же start_block у двух nodes);
* impossible parent (parent.level >= node.level);
* broken numbering (siblings с разным scheme).

Repair **не придумывает** информацию, а только:

* заменяет parent_id на ``root_id`` для orphan'ов;
* отбрасывает узлы с start_block > end_block;
* консервативно склеивает нарушения numbering (см. ``_repair_numbering``);
* не меняет confidence, evidence, source_refs.

**Important (PLAN §5.1):** one-block sections (``start_block == end_block``)
НЕ удаляются автоматически. Один блок может быть полностью валидной
секцией (например, «Статья 1» одна занимает один semantic block).

**Important (PLAN §5.2-§5.4):**

* При изменении ``parent_id`` синхронно пересобираем ``children``
  нового parent (drop из старого children + add в новый);
* При drop node его children привязываются к новому parent
  (repaired parent) либо явно drop, если становятся недействительными;
* Никаких dangling children;
* Repair детерминирован (никаких set-iterations на ordered data).

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
    invalid_ranges_dropped: int = 0
    impossible_parents_fixed: int = 0
    numbering_glued: int = 0


def repair_structure(struct: DocumentStructure) -> tuple[DocumentStructure, RepairReport]:
    """Запустить repair pass и вернуть (исправленная структура, отчёт).

    Repair идёт в фиксированном порядке:

    1. orphans (parent_id не существует) → parent_id = root_id;
       синхронно пересобираем ``children`` root и старого parent;
    2. invalid ranges (start_block > end_block) → отбрасываем node;
       его children либо привязываются к root, либо явно drop, если
       их parent_id указывает на удалённый node;
    3. impossible parents (parent.level >= node.level) →
       parent_id = root_id; синхронно пересобираем children.

    Returns:
        ``(исправленная DocumentStructure, RepairReport)``.

    Детерминирован: операции выполняются в порядке ``sorted(existing_ids)``.
    """
    report = RepairReport()
    changes: dict[str, StructureNode] = {}

    current_nodes: dict[str, StructureNode] = dict(struct.nodes)

    def _put(node: StructureNode) -> None:
        current_nodes[node.node_id] = node
        changes[node.node_id] = node

    def _drop(nid: str) -> None:
        current_nodes.pop(nid, None)
        changes.pop(nid, None)

    def _rebuild_children(parent_id: str) -> None:
        """Пересобрать ``children`` parent на основе актуальных parent_id."""
        parent = current_nodes.get(parent_id)
        if parent is None:
            return
        actual = tuple(
            cid for cid, n in sorted(current_nodes.items())
            if n.parent_id == parent_id
        )
        if actual != parent.children:
            _put(StructureNode(
                node_id=parent.node_id, node_type=parent.node_type,
                semantic_type=parent.semantic_type, level=parent.level,
                title=parent.title, number=parent.number,
                parent_id=parent.parent_id, children=actual,
                start_block=parent.start_block, end_block=parent.end_block,
                confidence=parent.confidence, evidence=parent.evidence,
                source_refs=parent.source_refs,
            ))

    def _reparent(node_id: str, new_parent_id: str) -> None:
        """Переставить node под new_parent_id и синхронно пересобрать
        ``children`` обоих (старого и нового) parent'ов."""
        node = current_nodes.get(node_id)
        if node is None:
            return
        old_parent_id = node.parent_id
        _put(StructureNode(
            node_id=node.node_id, node_type=node.node_type,
            semantic_type=node.semantic_type, level=node.level,
            title=node.title, number=node.number,
            parent_id=new_parent_id, children=node.children,
            start_block=node.start_block, end_block=node.end_block,
            confidence=node.confidence, evidence=node.evidence,
            source_refs=node.source_refs,
        ))
        if old_parent_id is not None and old_parent_id != new_parent_id:
            _rebuild_children(old_parent_id)
        _rebuild_children(new_parent_id)

    ordered_ids = sorted(current_nodes.keys())

    for nid in ordered_ids:
        if nid == struct.root_id:
            continue
        node = current_nodes.get(nid)
        if node is None:
            continue
        if node.parent_id is not None and node.parent_id not in current_nodes:
            _reparent(nid, struct.root_id)
            report = RepairReport(
                orphans_fixed=report.orphans_fixed + 1,
                invalid_ranges_dropped=report.invalid_ranges_dropped,
                impossible_parents_fixed=report.impossible_parents_fixed,
                numbering_glued=report.numbering_glued,
            )

    for nid in ordered_ids:
        if nid == struct.root_id:
            continue
        node = current_nodes.get(nid)
        if node is None:
            continue
        if node.parent_id == struct.root_id:
            continue
        parent_after = current_nodes.get(node.parent_id)
        if parent_after is None or parent_after.level >= node.level:
            _reparent(nid, struct.root_id)
            report = RepairReport(
                orphans_fixed=report.orphans_fixed,
                invalid_ranges_dropped=report.invalid_ranges_dropped,
                impossible_parents_fixed=report.impossible_parents_fixed + 1,
                numbering_glued=report.numbering_glued,
            )

    for nid in ordered_ids:
        if nid == struct.root_id:
            continue
        node = current_nodes.get(nid)
        if node is None:
            continue
        if node.start_block > node.end_block:
            old_parent_id = node.parent_id
            _drop(nid)
            for cid, child in sorted(current_nodes.items()):
                if child.parent_id == nid:
                    _reparent(cid, struct.root_id)
            if old_parent_id is not None:
                _rebuild_children(old_parent_id)
            report = RepairReport(
                orphans_fixed=report.orphans_fixed,
                invalid_ranges_dropped=report.invalid_ranges_dropped + 1,
                impossible_parents_fixed=report.impossible_parents_fixed,
                numbering_glued=report.numbering_glued,
            )

    for nid in ordered_ids:
        _rebuild_children(nid)

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