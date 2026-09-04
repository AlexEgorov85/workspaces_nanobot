"""Safety net merge для микро-секций (PLAN §17, Этап 17).

После хорошего heading detection + repair (Этапы 11–16) остаются
**крайние случаи** — например, микро-секции из одной строки heading +
одной строки body (false micro-section из-за агрессивного heading
detector).

Этот модуль предоставляет **safety net**, который:

* срабатывает только если в структуре много** коротких siblings
  одного level (по умолчанию < 200 chars total);
* схлопывает их в соседнюю секцию того же level;
* **не** является основным механизмом исправления структуры.

Отличие от ``merge_short_sections`` в ``sections.py``:

* работает с новым ``DocumentStructure`` (не ``SectionTree``);
* не меняет block_indices (только mark'ит как merged);
* не убирает из дерева, а помечает ``confidence = 0.0`` и parent = merged.

Реальная миграция consumers на ``DocumentStructure`` — Этап 45.
Сейчас это **дополнительный** инструмент.
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)


@dataclass(frozen=True)
class SafetyMergeConfig:
    """Параметры safety merge."""

    min_section_chars: int = 200
    max_level: int = 2
    dry_run: bool = False


def _section_total_chars(
    node: StructureNode,
    chars_by_ord: dict[int,
    int],
) -> int:
    return sum(
        chars_by_ord.get(b, 0)
        for b in range(node.start_block, node.end_block + 1)
    )


def safety_merge(
    struct: DocumentStructure,
    blocks: tuple[DocumentBlock, ...],
    *,
    config: SafetyMergeConfig | None = None,
) -> DocumentStructure:
    """Safety net: схлопнуть микро-секции в соседнюю секцию того же level.

    Алгоритм:

    1. Для каждого level (1, 2) пройти siblings;
    2. Если section короткая (< ``min_section_chars``), найти соседа
       того же level и «слить» в него (расширить его range);
    3. Merged section получает ``confidence = 0.0`` (помечена как
       merged), чтобы downstream не считал её самостоятельной.

    Returns:
        Новый ``DocumentStructure`` (immutable — исходный не меняется).
    """
    cfg = config or SafetyMergeConfig()
    chars_by_ord = {b.ordinal: b.char_count for b in blocks}
    changes: dict[str, StructureNode] = {}

    for level in range(1, cfg.max_level + 1):
        siblings_at_level = sorted(
            [n for n in struct.nodes.values()
             if n.node_type == "section" and n.level == level],
            key=lambda n: n.start_block,
        )
        if len(siblings_at_level) < 2:
            continue
        i = 0
        while i < len(siblings_at_level):
            cur = siblings_at_level[i]
            if cur.node_id in changes:
                cur = changes[cur.node_id]
            total = _section_total_chars(cur, chars_by_ord)
            if total < cfg.min_section_chars and i + 1 < len(siblings_at_level):
                target = siblings_at_level[i + 1]
                if target.level == level:
                    new_end = cur.end_block
                    changes[target.node_id] = StructureNode(
                        node_id=target.node_id,
                        node_type=target.node_type,
                        semantic_type=target.semantic_type,
                        level=target.level, title=target.title,
                        number=target.number,
                        parent_id=target.parent_id,
                        children=target.children,
                        start_block=target.start_block,
                        end_block=max(target.end_block, new_end),
                        confidence=target.confidence,
                        evidence=target.evidence,
                        source_refs=target.source_refs,
                    )
                    changes[cur.node_id] = StructureNode(
                        node_id=cur.node_id,
                        node_type=cur.node_type,
                        semantic_type=cur.semantic_type,
                        level=cur.level, title=cur.title,
                        number=cur.number,
                        parent_id=cur.parent_id,
                        children=cur.children,
                        start_block=cur.start_block,
                        end_block=cur.end_block,
                        confidence=0.0,
                        evidence=cur.evidence,
                        source_refs=cur.source_refs,
                    )
                    i += 2
                    continue
            i += 1

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


__all__ = ["SafetyMergeConfig", "safety_merge"]