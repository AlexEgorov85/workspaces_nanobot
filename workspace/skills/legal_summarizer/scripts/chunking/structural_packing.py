"""Hierarchical structural packing для legal_summarizer.

Алгоритм (Phase 2):

1. Tables → atomic chunks (каждый table block — свой chunk).
2. Oversized blocks → split через ``_split_block_with_offsets``.
3. Recursive descent по DocumentStructure:
   - если subtree помещается в ``target_chunk_chars`` AND не содержит
     tables/oversized AND все блоки owned → один ``PackableUnit`` на
     всё subtree;
   - иначе → раскрываем на children + gaps (direct blocks родителя,
     не покрытые children).
4. Greedy packing unit'ов с strong boundary check.
5. Emit ``Chunk[]``.

Ключевое отличие от flat greedy: subtree может стать одним chunk'ом,
если оно semantically coherent и помещается. Это уменьшает количество
chunks в 5-10x для документов с nested sections.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from chunking.chunks import Chunk
from document.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from document.structure import (
    DocumentStructure,
    StructureNode,
)


_MAJOR_SEMANTIC_TYPES = frozenset({"chapter", "section", "appendix", "razdel"})


@dataclass(frozen=True)
class PackableUnit:
    """Внутренняя единица packing'а. Не экспортируется в public API.

    Attributes:
        kind: "structural" | "table" | "oversized_part" | "root".
        block_indices: конкретные ordinals блоков (отсортированы).
        section_ids: все section node_id, чьи structural ranges
            пересекаются с блоками unit'а.
        primary_section_id: section_id первой секции в unit'е
            (для backward-compat с Chunk.section_id).
        char_count: суммарный char_count блоков.
        table_id: для kind="table".
        source_block: для kind="oversized_part" — ordinal исходного блока.
        source_char_start / source_char_end: для oversized_part.
    """

    kind: Literal["structural", "table", "oversized_part", "root"]
    block_indices: tuple[int, ...]
    section_ids: tuple[str, ...]
    primary_section_id: str
    char_count: int
    table_id: str | None = None
    source_block: int | None = None
    source_char_start: int | None = None
    source_char_end: int | None = None


def _node_subtree_range(
    node_id: str,
    struct: DocumentStructure,
) -> tuple[int, int]:
    """Subtree physical range для node'а.

    Учитывает direct range самого node + диапазоны всех descendants.
    """
    node = struct.nodes[node_id]
    start = node.start_block
    end = node.end_block
    for child in struct.iter_children(node_id):
        c_start, c_end = _node_subtree_range(child.node_id, struct)
        if c_start < start:
            start = c_start
        if c_end > end:
            end = c_end
    return start, end


def _node_has_specials(
    node_id: str,
    struct: DocumentStructure,
    by_ord: dict[int, DocumentBlock],
    max_chunk_chars: int,
) -> bool:
    """True, если subtree содержит oversized block (>max_chunk_chars).

    Tables — обычные блоки (atomic, но не special); обрабатываются
    наравне с текстовыми в ``_build_units_for_node``.
    """
    start, end = _node_subtree_range(node_id, struct)
    for ord_i in range(start, end + 1):
        b = by_ord.get(ord_i)
        if b is None:
            continue
        if b.char_count > max_chunk_chars:
            return True
    return False


def _direct_blocks_for_node(
    node_id: str,
    struct: DocumentStructure,
    by_ord: dict[int, DocumentBlock],
    max_chunk_chars: int,
) -> tuple[int, ...]:
    """Direct blocks node'а, не покрытые ни одним descendant.

    Это blocks, у которых owner == node_id, но которые не входят в
    subtree range ни одного child'а. Исключаем tables и oversized —
    они обрабатываются отдельно как packing barriers.
    """
    node = struct.nodes[node_id]
    start, end = _node_subtree_range(node_id, struct)

    child_ranges: list[tuple[int, int]] = []
    for child in struct.iter_children(node_id):
        child_ranges.append(_node_subtree_range(child.node_id, struct))

    direct: list[int] = []
    for b in range(start, end + 1):
        if any(cs <= b <= ce for cs, ce in child_ranges):
            continue
        b_obj = by_ord.get(b)
        if b_obj is None:
            continue
        if b_obj.char_count > max_chunk_chars:
            continue
        direct.append(b)
    return tuple(direct)


def _collect_section_ids_for_range(
    block_indices: tuple[int, ...],
    ownership: dict[int, str],
    root_id: str,
) -> tuple[str, ...]:
    """Уникальные owner'ы blocks в document order, исключая root_id."""
    seen: list[str] = []
    for b in block_indices:
        o = ownership.get(b, root_id)
        if o == root_id:
            continue
        if o not in seen:
            seen.append(o)
    return tuple(seen)


def _build_units_for_node(
    node_id: str,
    struct: DocumentStructure,
    by_ord: dict[int, DocumentBlock],
    ownership: dict[int, str],
    target_chunk_chars: int,
    max_chunk_chars: int,
) -> list[PackableUnit]:
    """Recursive descent: subtree целиком или раскрытие на children.

    Tables — обычные блоки (atomic по построению: один table = один
    block, не делится). Обрабатываются наравне с текстовыми блоками;
    ``has_specials`` отражает только oversized (>max_chunk_chars).
    """
    node = struct.nodes[node_id]

    start, end = _node_subtree_range(node_id, struct)

    structural_blocks: list[DocumentBlock] = []
    has_specials = False
    for ord_i in range(start, end + 1):
        b = by_ord.get(ord_i)
        if b is None:
            continue
        if b.char_count > max_chunk_chars:
            has_specials = True
            continue
        structural_blocks.append(b)

    sub_chars = sum(b.char_count for b in structural_blocks)

    if (
        sub_chars <= max_chunk_chars
        and sub_chars <= target_chunk_chars
        and not has_specials
        and node_id != struct.root_id
    ):
        block_indices = tuple(b.ordinal for b in structural_blocks)
        section_ids = _collect_section_ids_for_range(
            block_indices, ownership, struct.root_id,
        )
        if not block_indices:
            return []
        return [
            PackableUnit(
                kind="structural",
                block_indices=block_indices,
                section_ids=section_ids,
                primary_section_id=node_id,
                char_count=sub_chars,
            ),
        ]

    if not node.children:
        if not structural_blocks:
            return []
        if sub_chars <= max_chunk_chars:
            block_indices = tuple(b.ordinal for b in structural_blocks)
            section_ids = _collect_section_ids_for_range(
                block_indices, ownership, struct.root_id,
            )
            primary = (
                node_id if node_id != struct.root_id
                else (section_ids[0] if section_ids else struct.root_id)
            )
            return [
                PackableUnit(
                    kind="structural",
                    block_indices=block_indices,
                    section_ids=section_ids,
                    primary_section_id=primary,
                    char_count=sub_chars,
                ),
            ]

        leaf_units: list[PackableUnit] = []
        current_blocks: list[DocumentBlock] = []
        current_chars = 0
        primary = (
            node_id if node_id != struct.root_id
            else struct.root_id
        )

        for b in structural_blocks:
            if current_chars + b.char_count > max_chunk_chars and current_blocks:
                leaf_indices = tuple(x.ordinal for x in current_blocks)
                leaf_section_ids = _collect_section_ids_for_range(
                    leaf_indices, ownership, struct.root_id,
                )
                leaf_units.append(
                    PackableUnit(
                        kind="structural",
                        block_indices=leaf_indices,
                        section_ids=leaf_section_ids,
                        primary_section_id=primary,
                        char_count=current_chars,
                    ),
                )
                current_blocks = []
                current_chars = 0
            current_blocks.append(b)
            current_chars += b.char_count

        if current_blocks:
            tail_indices = tuple(x.ordinal for x in current_blocks)
            tail_section_ids = _collect_section_ids_for_range(
                tail_indices, ownership, struct.root_id,
            )
            leaf_units.append(
                PackableUnit(
                    kind="structural",
                    block_indices=tail_indices,
                    section_ids=tail_section_ids,
                    primary_section_id=primary,
                    char_count=current_chars,
                ),
            )

        return leaf_units

    units: list[PackableUnit] = []

    direct = _direct_blocks_for_node(node_id, struct, by_ord, max_chunk_chars)
    if direct:
        direct_blocks = [by_ord[b] for b in direct if b in by_ord]
        direct_chars = sum(b.char_count for b in direct_blocks)
        if direct:
            direct_block_indices = tuple(b.ordinal for b in direct_blocks)
            direct_section_ids = _collect_section_ids_for_range(
                direct_block_indices, ownership, struct.root_id,
            )
            if direct_section_ids:
                primary = direct_section_ids[0]
            else:
                primary = node_id
            units.append(
                PackableUnit(
                    kind="structural",
                    block_indices=direct_block_indices,
                    section_ids=direct_section_ids,
                    primary_section_id=primary,
                    char_count=direct_chars,
                ),
            )

    for child in struct.iter_children(node_id):
        units.extend(
            _build_units_for_node(
                child.node_id, struct, by_ord, ownership,
                target_chunk_chars, max_chunk_chars,
            ),
        )

    return units


def _is_strong_boundary_units(
    prev: PackableUnit,
    nxt: PackableUnit,
    struct: DocumentStructure,
) -> bool:
    """True, если prev→nxt — major→major переход между разными primary section."""
    if prev.primary_section_id == nxt.primary_section_id:
        return False
    if prev.primary_section_id == struct.root_id:
        return False
    if nxt.primary_section_id == struct.root_id:
        return False
    prev_node = struct.nodes.get(prev.primary_section_id)
    nxt_node = struct.nodes.get(nxt.primary_section_id)
    if prev_node is None or nxt_node is None:
        return False
    prev_major = prev_node.semantic_type in _MAJOR_SEMANTIC_TYPES
    nxt_major = nxt_node.semantic_type in _MAJOR_SEMANTIC_TYPES
    return prev_major and nxt_major


def _merge_units(prev: PackableUnit, nxt: PackableUnit) -> PackableUnit:
    """Объединить два соседних unit'а (structural или table inline).

    primary_section_id остаётся от prev (back-compat).
    section_ids объединяются через dict.fromkeys (document order).
    """
    merged_blocks = prev.block_indices + nxt.block_indices
    merged_section_ids = tuple(
        dict.fromkeys(prev.section_ids + nxt.section_ids),
    )
    return PackableUnit(
        kind="structural",
        block_indices=merged_blocks,
        section_ids=merged_section_ids,
        primary_section_id=prev.primary_section_id,
        char_count=prev.char_count + nxt.char_count,
    )


def _is_consecutive(prev: PackableUnit, nxt: PackableUnit) -> bool:
    """True, если nxt находится ВНУТРИ диапазона prev или сразу после.

    Используется для small_table inline: таблица inline'нутая в current
    должна физически находиться в том же непрерывном диапазоне блоков
    (или сразу после, если prev — хвост subtree).

    Возвращает True если:
    - nxt.block_indices[0] находится внутри [prev.block_indices[0], prev.block_indices[-1]] (nxt внутри subtree prev)
    - или nxt.block_indices[0] == prev.block_indices[-1] + 1 (сосед после subtree)
    """
    if not prev.block_indices or not nxt.block_indices:
        return False
    prev_start = prev.block_indices[0]
    prev_end = prev.block_indices[-1]
    nxt_start = nxt.block_indices[0]
    return prev_start <= nxt_start <= prev_end + 1


def greedy_pack_units(
    units: list[PackableUnit],
    *,
    target_chunk_chars: int,
    max_chunk_chars: int,
    preferred_min_before_strong_boundary: float,
    struct: DocumentStructure,
) -> list[PackableUnit]:
    """Greedy packing unit'ов с плотным заполнением chunks.

    Алгоритм:

    1. **Oversized_part**: всегда atomic.

    2. **Разные kinds** (после oversized_part): emit current, current = nxt.

    3. **Structural + structural**:
       - max overflow → emit, start new
       - оба < min_target → force merge
       - primary_section_id = root → emit current
       - strong boundary при current ≥ target * preferred_min → emit
       - default: merge
    """
    if not units:
        return []

    min_target = int(max_chunk_chars * 0.4)

    packed: list[PackableUnit] = []
    current = units[0]

    for nxt in units[1:]:
        if nxt.kind == "oversized_part":
            packed.append(current)
            current = nxt
            continue

        if current.kind != "structural" or nxt.kind != "structural":
            packed.append(current)
            current = nxt
            continue

        if current.char_count + nxt.char_count > max_chunk_chars:
            packed.append(current)
            current = nxt
            continue

        if current.char_count < min_target and nxt.char_count < min_target:
            current = _merge_units(current, nxt)
            continue

        if current.primary_section_id == struct.root_id:
            packed.append(current)
            current = nxt
            continue

        if nxt.primary_section_id == struct.root_id:
            packed.append(current)
            current = nxt
            continue

        if (
            _is_strong_boundary_units(current, nxt, struct)
            and current.char_count >= target_chunk_chars * preferred_min_before_strong_boundary
        ):
            packed.append(current)
            current = nxt
            continue

        current = _merge_units(current, nxt)

    packed.append(current)
    return packed


def build_packable_units(
    doc: PhysicalDocument,
    struct: DocumentStructure,
    ownership: dict[int, str],
    *,
    target_chunk_chars: int,
    max_chunk_chars: int,
) -> list[PackableUnit]:
    """Public entry point: построить упорядоченные packable units.

    Использует hierarchical descent по DocumentStructure, начиная с root.
    Tables обрабатываются как обычные блоки (atomic по построению).
    Oversized blocks (включая tables > max_chunk_chars) caller должен
    собрать в PackableUnit'ы с kind="oversized_part" и вставить в
    список перед greedy_pack_units.
    """
    by_ord = doc.blocks_by_ord

    units = _build_units_for_node(
        struct.root_id, struct, by_ord, ownership,
        target_chunk_chars, max_chunk_chars,
    )

    units.sort(key=lambda u: u.block_indices[0] if u.block_indices else 0)
    return units
