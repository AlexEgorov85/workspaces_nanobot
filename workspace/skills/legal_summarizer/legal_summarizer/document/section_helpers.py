"""Canonical section helpers для DocumentStructure.

Чистые функции на ``DocumentStructure``: индекс секций (ids, headings,
hierarchical paths), подсчёт meaningful sections, подсчёт sections.

Раньше жили в ``application/section_index.py`` — переехали сюда
(в ``document/``), потому что это чистые утилиты над DocumentStructure,
а не orchestration-логика. ``application`` импортирует их отсюда.
"""

from __future__ import annotations

from legal_summarizer.document.structure import DocumentStructure


def section_index(
    struct: DocumentStructure,
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Build canonical section index: (ids, headings, paths) из DocumentStructure.

    ``paths`` — иерархические path вида ``"1 > 2 > 3"`` (root → leaf).
    """
    section_ids: list[str] = []
    section_headings: dict[str, str] = {}
    section_paths: dict[str, str] = {}
    for node in struct.iter_sections():
        section_ids.append(node.node_id)
        section_headings[node.node_id] = node.title
        parts: list[str] = []
        cur = node
        while cur is not None and cur.node_id != struct.root_id:
            if cur.number is not None and cur.number.ordinal is not None:
                parts.append(str(cur.number.ordinal))
            else:
                parts.append(str(cur.level))
            if cur.parent_id is None:
                break
            cur = struct.nodes.get(cur.parent_id)
        section_paths[node.node_id] = " > ".join(reversed(parts))
    return section_ids, section_headings, section_paths


def count_meaningful_sections_canonical(struct: DocumentStructure) -> int:
    """Meaningful sections: section nodes с non-empty title или >1 block."""
    return sum(
        1 for n in struct.iter_sections()
        if n.title.strip() or n.end_block > n.start_block
    )


def count_sections(struct: DocumentStructure | None) -> int:
    """Число section-узлов в DocumentStructure (0 если None)."""
    if struct is None:
        return 0
    return len(struct.iter_sections())


__all__ = [
    "section_index",
    "count_meaningful_sections_canonical",
    "count_sections",
]