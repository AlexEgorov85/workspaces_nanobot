"""Canonical block ownership (Этап 5).

Единственный механизм определения «какой section node владеет
данным DocumentBlock». Все consumers (DocumentStructure.block_to_node,
DocumentStructureChunker, retrieval, repair) должны использовать эти
функции — никаких альтернативных реализаций.

Семантика (Этап 4):

* каждый block принадлежит **ровно одному** section node — самому
  глубокому (deepest), чей диапазон ``[start_block, end_block]``
  содержит block. Это решает проблему двойного ownership между
  parent и child nodes.
* blocks вне section ranges принадлежат root preamble
  (``struct.root_id``).
* blocks с ordinal вне ``[0, total_blocks)`` — невалидны, возвращают
  ``None``.
"""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)


def _depth_of(node_id: str, struct: DocumentStructure) -> int:
    """Глубина узла в дереве (root = 0)."""
    depth = 0
    cur = struct.nodes.get(node_id)
    while cur is not None and cur.parent_id is not None:
        depth += 1
        cur = struct.nodes.get(cur.parent_id)
    return depth


def build_block_ownership(
    struct: DocumentStructure,
) -> dict[int, str]:
    """Построить ``block_ordinal → owning_section_node_id``.

    Каждый block, попадающий в диапазон какого-либо section, принадлежит
    **самому глубокому** section, чей диапазон его содержит. Это даёт
    ровно одного owner на block.

    Returns:
        ``dict[block_ordinal, owning_section_node_id]``. Blocks вне
        section ranges отсутствуют в dict (см. ``owner_for_block`` для
        fallback на ``struct.root_id``).
    """
    candidates = [n for n in struct.nodes.values() if n.node_type == "section"]
    candidates.sort(key=lambda n: _depth_of(n.node_id, struct), reverse=True)

    owner: dict[int, str] = {}
    for node in candidates:
        for b in range(node.start_block, node.end_block + 1):
            owner.setdefault(b, node.node_id)
    return owner


def owner_for_block(
    struct: DocumentStructure,
    ordinal: int,
    ownership: dict[int, str] | None = None,
) -> str | None:
    """Получить owner для block ``ordinal``.

    Returns:
        - ``owning_section_node_id`` если block принадлежит section;
        - ``struct.root_id`` если block не принадлежит ни одной section
          (preamble / orphan block);
        - ``None`` если ``ordinal`` вне диапазона документа.

    Args:
        struct: ``DocumentStructure``.
        ordinal: ``DocumentBlock.ordinal``.
        ownership: предвычисленная ``build_block_ownership(struct)``.
            Если ``None`` — будет построена на лету.
    """
    if ordinal < 0 or ordinal >= struct.total_blocks:
        return None
    if ownership is None:
        ownership = build_block_ownership(struct)
    return ownership.get(ordinal, struct.root_id)


def block_to_node(struct: DocumentStructure) -> dict[int, str]:
    """Mapping ``block_ordinal → node_id`` (Этап 5 — canonical delegate).

    Этот метод — единая точка для consumers, которым нужно
    ``block → node_id``. Реализация делегирует ``owner_for_block``
    и заполняет весь диапазон ``[0, total_blocks)`` (с root fallback).
    """
    ownership = build_block_ownership(struct)
    return {
        b: ownership.get(b, struct.root_id)
        for b in range(struct.total_blocks)
    }


__all__ = [
    "build_block_ownership",
    "owner_for_block",
    "block_to_node",
]