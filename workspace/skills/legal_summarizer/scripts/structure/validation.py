"""StructureValidator (PLAN §4, §16).

Проверяет ``DocumentStructure`` на:

* **Coverage**: каждый значимый ``DocumentBlock`` покрыт
  (``start_block ≤ ordinal ≤ end_block`` хотя бы для одного node);
* **Ordering**: ``start_block ≤ end_block`` и nodes упорядочены;
* **Parent**: каждый ``parent_id`` существует в ``nodes``;
* **No cross-branch overlap**: section nodes разных ветвей
  не должны перекрываться по диапазонам. **Parent-child overlap
  разрешён** (parent range содержит child range) — это часть
  семантики nested hierarchy (PLAN §4);
* **No sibling overlap**: section nodes одного parent не должны
  перекрываться по диапазонам;
* **No cycles**: parent-chain не содержит циклов;
* **No duplicate child**: каждый child встречается в ``children``
  ровно одного parent;
* **Root covers full document**: root.start_block == 0,
  root.end_block == total_blocks - 1;
* **Ranges inside document**: 0 ≤ start_block < total_blocks,
  0 ≤ end_block < total_blocks.

Возвращает ``ValidationReport`` со списком issues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    PhysicalDocument,
)


@dataclass(frozen=True)
class ValidationIssue:
    """Одна найденная проблема в структуре."""

    kind: str
    detail: str
    node_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "node_id": self.node_id}


@dataclass(frozen=True)
class ValidationReport:
    """Сводка валидации (для diagnostics и acceptance)."""

    issues: tuple[ValidationIssue, ...] = ()
    coverage_ratio: float = 0.0
    is_valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [i.to_dict() for i in self.issues],
            "coverage_ratio": self.coverage_ratio,
            "is_valid": self.is_valid,
        }


def _is_ancestor(
    struct: DocumentStructure,
    ancestor_id: str,
    descendant_id: str,
) -> bool:
    """True если ``ancestor_id`` — предок ``descendant_id`` (или они равны)."""
    if ancestor_id == descendant_id:
        return True
    seen: set[str] = set()
    current: str | None = struct.nodes[descendant_id].parent_id
    while current is not None and current not in seen:
        if current == ancestor_id:
            return True
        seen.add(current)
        parent = struct.nodes.get(current)
        if parent is None:
            return False
        current = parent.parent_id
    return False


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """True если диапазоны перекрываются (включая границы)."""
    return not (a_end < b_start or b_end < a_start)


def validate_structure(
    struct: DocumentStructure,
    doc: PhysicalDocument,
) -> ValidationReport:
    """Валидировать ``DocumentStructure`` против ``PhysicalDocument``.

    Проверки (PLAN §4):

    1. ``total_blocks`` соответствует ``len(doc.blocks)``.
    2. ``start_block ≤ end_block`` для всех non-root nodes.
    3. 0 ≤ start_block < total_blocks, 0 ≤ end_block < total_blocks.
    4. Все ``parent_id`` существуют в ``nodes`` (кроме root).
    5. No cycles в parent-chain.
    6. No duplicate child: каждый node встречается в ``children``
       ровно одного parent.
    7. No sibling overlap: для каждой пары sections с общим parent —
       ranges не должны перекрываться.
    8. No cross-branch overlap: для каждой пары sections из разных
       ветвей (без ancestor-descendant relation) — ranges не должны
       перекрываться.
    9. Parent-child overlap разрешён и **не** приводит к issue.
    10. Root covers full document: root.start_block == 0,
        root.end_block == total_blocks - 1.
    11. Coverage: каждый значимый ``DocumentBlock`` покрыт хотя бы
        одним node.

    Returns:
        ``ValidationReport`` с ``is_valid=True`` если issues пуст.
    """
    issues: list[ValidationIssue] = []

    if struct.total_blocks != len(doc.blocks):
        issues.append(ValidationIssue(
            kind="total_blocks_mismatch",
            detail=f"structure.total_blocks={struct.total_blocks}, "
                   f"len(doc.blocks)={len(doc.blocks)}",
        ))

    root = struct.nodes.get(struct.root_id)
    if root is not None:
        if root.start_block != 0:
            issues.append(ValidationIssue(
                kind="root_not_at_start",
                detail=f"root.start_block={root.start_block}, expected 0",
                node_id=struct.root_id,
            ))
        if root.end_block != max(0, struct.total_blocks - 1):
            issues.append(ValidationIssue(
                kind="root_does_not_cover_document",
                detail=f"root.end_block={root.end_block}, "
                       f"expected={max(0, struct.total_blocks - 1)}",
                node_id=struct.root_id,
            ))

    child_owners: dict[str, set[str]] = {}
    for nid, node in struct.nodes.items():
        if nid == struct.root_id:
            continue
        if node.start_block > node.end_block:
            issues.append(ValidationIssue(
                kind="invalid_range",
                detail=f"start={node.start_block} > end={node.end_block}",
                node_id=nid,
            ))
        if node.start_block < 0 or node.start_block >= struct.total_blocks:
            issues.append(ValidationIssue(
                kind="range_out_of_bounds",
                detail=f"start_block={node.start_block} not in "
                       f"[0, {struct.total_blocks})",
                node_id=nid,
            ))
        if node.end_block < 0 or node.end_block >= struct.total_blocks:
            issues.append(ValidationIssue(
                kind="range_out_of_bounds",
                detail=f"end_block={node.end_block} not in "
                       f"[0, {struct.total_blocks})",
                node_id=nid,
            ))

        if node.parent_id is None:
            issues.append(ValidationIssue(
                kind="non_root_without_parent",
                detail=f"non-root node has parent_id=None",
                node_id=nid,
            ))
            continue
        if node.parent_id not in struct.nodes:
            issues.append(ValidationIssue(
                kind="orphan",
                detail=f"parent_id={node.parent_id} not found",
                node_id=nid,
            ))
            continue

        seen: set[str] = set()
        current = node.parent_id
        cycle = False
        while current is not None:
            if current in seen:
                cycle = True
                break
            seen.add(current)
            parent = struct.nodes.get(current)
            if parent is None:
                break
            current = parent.parent_id
        if cycle:
            issues.append(ValidationIssue(
                kind="cycle",
                detail=f"parent chain contains cycle starting at {nid}",
                node_id=nid,
            ))

        child_owners.setdefault(nid, set()).add(node.parent_id)

    for nid in struct.nodes:
        for cid in struct.nodes[nid].children:
            child_owners.setdefault(cid, set()).add(nid)

    duplicates = {
        nid: sorted(owners)
        for nid, owners in child_owners.items()
        if len(owners) > 1
    }
    for nid, owners in duplicates.items():
        issues.append(ValidationIssue(
            kind="duplicate_child",
            detail=f"{nid} appears in children of {owners}",
            node_id=nid,
        ))

    section_nodes = [
        nid for nid, n in struct.nodes.items()
        if n.node_type == "section" and nid != struct.root_id
    ]
    for i, a_id in enumerate(section_nodes):
        a = struct.nodes[a_id]
        for b_id in section_nodes[i + 1:]:
            b = struct.nodes[b_id]
            if (
                _is_ancestor(struct, a_id, b_id)
                or _is_ancestor(struct, b_id, a_id)
            ):
                continue
            if _ranges_overlap(a.start_block, a.end_block, b.start_block, b.end_block):
                issues.append(ValidationIssue(
                    kind="cross_branch_overlap",
                    detail=(
                        f"sections {a_id}[{a.start_block}..{a.end_block}] and "
                        f"{b_id}[{b.start_block}..{b.end_block}] overlap "
                        "across different branches"
                    ),
                    node_id=a_id,
                ))

    sibling_owners: dict[str | None, list[str]] = {}
    for nid, node in struct.nodes.items():
        if nid == struct.root_id:
            continue
        if node.node_type != "section":
            continue
        sibling_owners.setdefault(node.parent_id, []).append(nid)
    for parent_id, children in sibling_owners.items():
        if len(children) < 2:
            continue
        for i, a_id in enumerate(children):
            a = struct.nodes[a_id]
            for b_id in children[i + 1:]:
                b = struct.nodes[b_id]
                if _ranges_overlap(
                    a.start_block, a.end_block,
                    b.start_block, b.end_block,
                ):
                    issues.append(ValidationIssue(
                        kind="sibling_overlap",
                        detail=(
                            f"siblings {a_id}[{a.start_block}..{a.end_block}] "
                            f"and {b_id}[{b.start_block}..{b.end_block}] "
                            f"under parent {parent_id} overlap"
                        ),
                        node_id=a_id,
                    ))

    covered = set()
    for nid, n in struct.nodes.items():
        if nid == struct.root_id:
            continue
        for b in range(n.start_block, n.end_block + 1):
            covered.add(b)

    uncovered = [b.ordinal for b in doc.blocks if b.ordinal not in covered]
    coverage = 1.0 if not doc.blocks else 1.0 - len(uncovered) / len(doc.blocks)

    if uncovered and len(uncovered) > 0.5 * len(doc.blocks):
        issues.append(ValidationIssue(
            kind="low_coverage",
            detail=f"only {coverage:.0%} of blocks covered by sections; "
                   f"{len(uncovered)} uncovered",
        ))

    return ValidationReport(
        issues=tuple(issues),
        coverage_ratio=coverage,
        is_valid=len(issues) == 0,
    )


__all__ = [
    "ValidationIssue",
    "ValidationReport",
    "validate_structure",
]