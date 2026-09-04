"""DocumentStructure-aware chunker.

Новый chunker, который использует ``DocumentStructure`` как единственный
источник section info (а не переоткрывает headings заново, как старый
``StructureAwareChunker``).

Ключевые правила:

* ``DocumentStructure`` — единственный источник section boundaries;
* tables атомарны (как в старом chunker);
* chunk boundary предпочитает section boundary;
* split by rows для oversize tables (с сохранением ``table_id``);
* **каждый physical block имеет ровно одного semantic owner** —
  самый глубокий section, чей диапазон содержит block. Это решает
  проблему двойного ownership между parent и child nodes.

Это **не переписывание** старого chunker'а — новый класс.
Старый ``StructureAwareChunker`` остаётся для back-compat (но более
не нужен в production после миграции consumers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.chunks import (
    Chunk,
    ChunkConfig,
    _split_block_with_offsets,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)


@dataclass(frozen=True)
class DocumentStructureChunkerConfig:
    """Параметры chunker'а, работающего с DocumentStructure."""

    chunk_config: ChunkConfig = field(default_factory=lambda: ChunkConfig(
        max_chunk_chars=100000,
        chunk_overlap_chars=0,
    ))


def _make_chunk_id(idx: int) -> str:
    return f"{idx:03}"


def _depth_of(node_id: str, struct: DocumentStructure) -> int:
    """Глубина узла (root = 0). Используется для выбора owner."""
    depth = 0
    cur = struct.nodes.get(node_id)
    while cur is not None and cur.parent_id is not None:
        depth += 1
        cur = struct.nodes.get(cur.parent_id)
    return depth


def build_block_ownership(
    struct: DocumentStructure,
) -> dict[int, str]:
    """Построить ``block_ordinal -> owning_section_node_id``.

    Каждый block, попадающий в диапазон какого-либо section, принадлежит
    **самому глубокому** section, чей диапазон его содержит. Это даёт
    ровно одного owner на block (или ноль, если block не покрыт ни одним
    section — такие blocks принадлежат root preamble).

    Returns:
        ``dict[block_ordinal, owning_section_node_id]``.
        Blocks вне section ranges отсутствуют в dict (см.
        ``owner_for_block`` для fallback на root).
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
            Если ``None`` — будет построена на лету (детерминированно).
    """
    if ordinal < 0 or ordinal >= struct.total_blocks:
        return None
    if ownership is None:
        ownership = build_block_ownership(struct)
    return ownership.get(ordinal, struct.root_id)


def chunk_from_structure(
    doc: PhysicalDocument,
    struct: DocumentStructure,
    *,
    config: DocumentStructureChunkerConfig | None = None,
) -> list[Chunk]:
    """Создать ``Chunk``-и из ``PhysicalDocument`` + ``DocumentStructure``.

    Алгоритм (PLAN §7 — chunks в **physical document order**):

    1. Строим ``block_ownership``: каждый block → ровно один section
       (deepest, см. ``owner_for_block``);
    2. Итерируем **physical blocks** в ``ordinal`` order (НЕ sections);
    3. Группируем последовательные blocks с одним owner в один chunk
       (если влезает в ``max_chunk_chars``);
    4. tables атомарны (каждый table block — свой chunk);
    5. chunk_id = zero-padded index (1-based).

    Returns:
        ``list[Chunk]`` в physical document order. ``chunks[i].index``
        строго возрастает.
    """
    cfg = config or DocumentStructureChunkerConfig()

    chars_per_token = cfg.chunk_config.chars_per_token
    max_chunk_chars = cfg.chunk_config.max_chunk_chars
    chunk_overlap = cfg.chunk_config.chunk_overlap_chars

    by_ord = doc.blocks_by_ord
    ownership = build_block_ownership(struct)

    chunks: list[Chunk] = []
    chunk_index = 0

    def _emit(
        text: str,
        block_indices: tuple[int, ...],
        block_types: tuple[str, ...],
        section_id: str,
        section_path: str,
        section_heading: str,
        page_start: int | None,
        page_end: int | None,
        table_id: str | None = None,
        table_row_start: int | None = None,
        table_row_end: int | None = None,
        source_char_start: int | None = None,
        source_char_end: int | None = None,
    ) -> None:
        nonlocal chunk_index
        chunk_index += 1
        token_est = max(1, len(text) // max(1, int(chars_per_token)))
        chunks.append(
            Chunk(
                chunk_id=_make_chunk_id(chunk_index),
                index=chunk_index - 1,
                text=text,
                char_count=len(text),
                token_estimate=token_est,
                page_start=page_start,
                page_end=page_end,
                section_id=section_id,
                section_path=section_path,
                section_heading=section_heading,
                block_indices=block_indices,
                block_types=block_types,
                table_id=table_id,
                table_row_start=table_row_start,
                table_row_end=table_row_end,
                source_char_start=source_char_start,
                source_char_end=source_char_end,
            )
        )

    def _section_path_for(node_id: str, struct: DocumentStructure) -> str:
        if node_id == struct.root_id:
            return ""
        path_parts: list[str] = []
        cur = struct.nodes.get(node_id)
        while cur is not None and cur.node_id != struct.root_id:
            if cur.number is not None and cur.number.ordinal is not None:
                path_parts.append(str(cur.number.ordinal))
            else:
                path_parts.append(str(cur.level))
            if cur.parent_id is None:
                break
            cur = struct.nodes.get(cur.parent_id)
        return " > ".join(reversed(path_parts))

    def _split_oversized_block(
        block: DocumentBlock,
        section_id: str,
        section_path: str,
        section_heading: str,
    ) -> None:
        parts = _split_block_with_offsets(
            block.content, chunk_size=max_chunk_chars, chunk_overlap=chunk_overlap,
        )
        for part_text, cs, ce in parts:
            _emit(
                text=part_text,
                block_indices=(block.ordinal,),
                block_types=(block.block_type,),
                section_id=section_id,
                section_path=section_path,
                section_heading=section_heading,
                page_start=block.page_index,
                page_end=block.page_end,
                source_char_start=cs,
                source_char_end=ce,
            )

    section_meta_cache: dict[str, tuple[str, str]] = {}

    def _meta(section_id: str) -> tuple[str, str]:
        if section_id not in section_meta_cache:
            node = struct.nodes.get(section_id)
            heading = node.title if node is not None else ""
            section_meta_cache[section_id] = (
                _section_path_for(section_id, struct), heading,
            )
        return section_meta_cache[section_id]

    ordered_ords = sorted(by_ord.keys())
    i = 0
    document_table_counter = 0

    while i < len(ordered_ords):
        ord_i = ordered_ords[i]
        block = by_ord.get(ord_i)
        if block is None:
            i += 1
            continue
        owner = ownership.get(ord_i, struct.root_id)
        section_path, section_heading = _meta(owner)

        if block.block_type == "table":
            document_table_counter += 1
            tid = f"t_{document_table_counter:03d}"
            _emit(
                text=block.content,
                block_indices=(block.ordinal,),
                block_types=(block.block_type,),
                section_id=owner,
                section_path=section_path,
                section_heading=section_heading,
                page_start=block.page_index,
                page_end=block.page_end,
                table_id=tid,
            )
            i += 1
            continue

        if block.char_count > max_chunk_chars:
            _split_oversized_block(block, owner, section_path, section_heading)
            i += 1
            continue

        collected_indices: list[int] = [block.ordinal]
        collected_types: list[str] = [block.block_type]
        collected_text_parts: list[str] = [block.content]
        collected_page_start = block.page_index
        collected_page_end = block.page_end
        j = i + 1
        current_chars = block.char_count
        while j < len(ordered_ords):
            ord_j = ordered_ords[j]
            next_block = by_ord.get(ord_j)
            if next_block is None:
                j += 1
                continue
            next_owner = ownership.get(ord_j, struct.root_id)
            if next_owner != owner:
                break
            if next_block.block_type == "table":
                break
            if next_block.char_count > max_chunk_chars:
                break
            new_chars = current_chars + next_block.char_count
            if new_chars > max_chunk_chars:
                break
            collected_indices.append(next_block.ordinal)
            collected_types.append(next_block.block_type)
            collected_text_parts.append(next_block.content)
            if next_block.page_index is not None:
                if collected_page_start is None or next_block.page_index < collected_page_start:
                    collected_page_start = next_block.page_index
            if next_block.page_end is not None:
                if collected_page_end is None or next_block.page_end > collected_page_end:
                    collected_page_end = next_block.page_end
            current_chars = new_chars
            j += 1

        _emit(
            text="\n\n".join(collected_text_parts),
            block_indices=tuple(collected_indices),
            block_types=tuple(collected_types),
            section_id=owner,
            section_path=section_path,
            section_heading=section_heading,
            page_start=collected_page_start,
            page_end=collected_page_end,
        )
        i = j

    return chunks


__all__ = [
    "DocumentStructureChunkerConfig",
    "ChunkPlanner",
    "chunk_from_structure",
    "build_block_ownership",
    "owner_for_block",
]


class ChunkPlanner:
    """ChunkPlanner использует ``DocumentStructure`` как SoT.

    Не переопределяет structure.
    """

    def __init__(self, *, config: DocumentStructureChunkerConfig | None = None) -> None:
        self.config = config or DocumentStructureChunkerConfig()

    def plan(
        self,
        doc: PhysicalDocument,
        struct: DocumentStructure,
    ) -> list[Chunk]:
        return chunk_from_structure(doc, struct, config=self.config)