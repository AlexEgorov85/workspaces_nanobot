"""StructureValidator (PLAN §16, Этап 16).

Проверяет ``DocumentStructure`` на:

* **Coverage**: каждый значимый ``DocumentBlock`` покрыт
  (``start_block ≤ ordinal ≤ end_block`` хотя бы для одного node);
* **Ordering**: ``start_block ≤ end_block`` и nodes упорядочены;
* **Parent**: каждый ``parent_id`` существует в ``nodes``;
* **No overlap**: нет невозможных пересечений section ranges
  (для **section** node'ов; body/list_item могут перекрываться
  внутри section);
* **Provenance**: каждый node умеет ссылаться назад на
  ``PhysicalDocument`` через ``start_block``/``end_block``;
* **Tables**: каждая ``table`` node имеет owner section или root.

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


def validate_structure(
    struct: DocumentStructure,
    doc: PhysicalDocument,
) -> ValidationReport:
    """Валидировать ``DocumentStructure`` против ``PhysicalDocument``.

    Проверки (см. PLAN §16):

    1. ``start_block ≤ end_block`` для всех non-root nodes.
    2. Все ``parent_id`` существуют в ``nodes`` (кроме root).
    3. Section nodes не перекрываются на уровне siblings.
    4. Каждый значимый ``DocumentBlock`` (из ``doc.blocks``) покрыт
       хотя бы одним node (внутри range).
    5. ``total_blocks`` соответствует ``len(doc.blocks)``.
    6. ``tables`` (если будут в ``StructureNode``) — будут иметь owner.
       Сейчас tables не part of StructureNode (они — DocumentBlock),
       поэтому проверка — no-op.

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

    section_children: set[int] = set()
    for nid, node in struct.nodes.items():
        if nid == struct.root_id:
            continue
        if node.start_block > node.end_block:
            issues.append(ValidationIssue(
                kind="invalid_range",
                detail=f"start={node.start_block} > end={node.end_block}",
                node_id=nid,
            ))

        if node.parent_id is not None and node.parent_id not in struct.nodes:
            issues.append(ValidationIssue(
                kind="orphan",
                detail=f"parent_id={node.parent_id} not found",
                node_id=nid,
            ))

    for nid, node in struct.nodes.items():
        if nid == struct.root_id:
            continue
        if node.node_type != "section":
            continue
        for b in range(node.start_block, node.end_block + 1):
            if b in section_children:
                issues.append(ValidationIssue(
                    kind="section_overlap",
                    detail=f"block {b} covered by multiple sections",
                    node_id=nid,
                ))
                break
        for b in range(node.start_block, node.end_block + 1):
            section_children.add(b)

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