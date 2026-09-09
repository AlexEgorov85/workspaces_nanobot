"""DocumentStructure-aware chunker.

Chunker, который использует ``DocumentStructure`` как единственный
источник section info.

Ключевые правила (STRUCTURAL_PACKING_PLAN v3):

* ``DocumentStructure`` — единственный источник section boundaries;
* tables атомарны (каждый table block — свой chunk);
* chunk boundary предпочитает section boundary, но НЕ привязан к ней:
  соседние sections с одним parent и одинаковым уровнем могут быть
  объединены в один chunk, если их суммарный размер ≤ ``max_chunk_chars``;
* ``target_chunk_chars`` — мягкий ориентир (используется только как
  порог для strong boundary);
* ``max_chunk_chars`` — жёсткий лимит;
* split by rows для oversize tables (с сохранением ``table_id``);
* oversized physical block → split через ``_split_block_with_offsets``
  с сохранением offsets;
* **chunk boundary больше НЕ определяется owner-change** —
  hierarchical structural packing (см. STRUCTURAL_PACKING_PLAN.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chunking.chunks import (
    Chunk,
    ChunkConfig,
    _split_block_with_offsets,
)
from document.structure import (
    DocumentStructure,
    build_block_ownership,
    owner_for_block,
)
from document.physical import (
    DocumentBlock,
    PhysicalDocument,
)


_MAJOR_SEMANTIC_TYPES = frozenset({"chapter", "section", "appendix", "razdel"})


@dataclass(frozen=True)
class DocumentStructureChunkerConfig:
    """Параметры chunker'а, работающего с DocumentStructure."""

    chunk_config: ChunkConfig = field(default_factory=lambda: ChunkConfig(
        max_chunk_chars=100000,
        chunk_overlap_chars=0,
    ))


def build_chunk_config_from_runtime(
    context_window_tokens: int | None = None,
) -> ChunkConfig:
    """Построить ``ChunkConfig`` из runtime-конфига.

    Приоритет источников для ``max_chunk_chars``:

    1. Если передан ``context_window_tokens`` (например, из
       ``agents.defaults.contextWindowTokens`` в ``config.json``):
       ``max_chunk_chars = context_window_tokens * chunk_size_input_ratio
       * chars_per_token``.
       Это основной путь: размер chunk'а привязан к реальному
       контекстному окну модели.

    2. Если в ``llm.config.get_chunking_config()`` есть ключ
       ``context_window_tokens`` (legacy override для тестов):
       используем его по той же формуле.

    3. Fallback: ``chunk_size`` из llm config (default 100000).
       Сохранён для обратной совместимости, если context window
       недоступен.

    Дополнительные поля:
        target_chunk_chars (default 20000) — мягкий ориентир;
        preferred_min_before_strong_boundary (default 0.7) — порог для
        решения о закрытии chunk'а на strong boundary.

    Args:
        context_window_tokens: контекстное окно модели в токенах
            (типичный источник: ``config.json::agents.defaults.contextWindowTokens``).
            Если None — используется legacy fallback.

    Returns:
        ChunkConfig с вычисленным max_chunk_chars.
    """
    import llm.config as _llm_config_mod

    chunk_cfg = _llm_config_mod.get_chunking_config() or {}
    chars_per_token = float(chunk_cfg.get("chars_per_token", 3.5))
    ratio = chunk_cfg.get("chunk_size_input_ratio")

    cwt = context_window_tokens
    if cwt is None:
        cwt = chunk_cfg.get("context_window_tokens")

    if cwt and ratio:
        max_chunk_chars = int(cwt * float(ratio) * chars_per_token)
    else:
        max_chunk_chars = int(chunk_cfg.get("chunk_size", 100000))

    overlap = int(chunk_cfg.get("chunk_overlap", 0))
    table_threshold = int(chunk_cfg.get("table_chunk_threshold_chars", 6000))
    target_chunk_chars = int(chunk_cfg.get("target_chunk_chars", 20000))
    preferred_min = float(
        chunk_cfg.get("preferred_min_before_strong_boundary", 0.7),
    )

    return ChunkConfig(
        max_chunk_chars=max_chunk_chars,
        chunk_overlap_chars=overlap,
        chars_per_token=chars_per_token,
        table_chunk_threshold_chars=table_threshold,
        target_chunk_chars=target_chunk_chars,
        preferred_min_before_strong_boundary=preferred_min,
    )


def _make_chunk_id(idx: int) -> str:
    return f"{idx:03}"


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


def _ancestor_chain_titles(
    node_id: str,
    struct: DocumentStructure,
) -> list[str]:
    """Ancestor chain от root до node_id в формате ['Title1', 'Title2', ...].

    Каждый элемент — это title родительского section node'а (или
    DocumentStructure.title, если есть). Используется для построения
    контекстной преамбулы chunk'а, чтобы LLM понимал, к какому разделу
    документа относится chunk.

    Пример для chunk с primary_section='§ 1':
        ['Гражданский кодекс РФ (часть 1)', 'Раздел I. Общие положения',
         'Глава 1. Гражданское законодательство']
    """
    chain: list[str] = []
    if struct.title and struct.title.value:
        chain.append(struct.title.value)
    cur = struct.nodes.get(node_id)
    ancestors: list[str] = []
    while cur is not None and cur.node_id != struct.root_id:
        if cur.title and cur.parent_id != struct.root_id:
            ancestors.append(cur.title)
        if cur.parent_id is None:
            break
        cur = struct.nodes.get(cur.parent_id)
    chain.extend(reversed(ancestors))
    return chain


def _build_context_preamble(
    unit: "PackableUnit",
    struct: DocumentStructure,
    cache: dict[str, str],
) -> str:
    """Строит короткую преамбулу для chunk'а в формате:

        [Контекст: Doc Title > Parent1 > Parent2]

    Добавляется в начало text chunk'а. Не меняет block_indices
    (preamble — overlay, не реальный block).

    Кешируется для повторного использования.

    Возвращает пустую строку если:
    - primary_section == root (preamble нет);
    - ancestor chain содержит только primary (нет полезных ancestors);
    - у ancestors нет titles.
    """
    if unit.primary_section_id in (struct.root_id, ""):
        return ""
    key = unit.primary_section_id
    if key in cache:
        return cache[key]

    chain = _ancestor_chain_titles(key, struct)
    if len(chain) <= 1:
        cache[key] = ""
        return ""

    preamble = "[Контекст: " + " > ".join(chain) + "]\n\n"
    cache[key] = preamble
    return preamble


def _is_strong_boundary(
    prev_owner_id: str,
    nxt_owner_id: str,
    struct: DocumentStructure,
) -> bool:
    """DEPRECATED: оставлен для back-compat, используйте
    ``structural_packing._is_strong_boundary_units``."""
    if prev_owner_id == nxt_owner_id:
        return False
    if prev_owner_id == struct.root_id or nxt_owner_id == struct.root_id:
        return False
    prev = struct.nodes.get(prev_owner_id)
    nxt = struct.nodes.get(nxt_owner_id)
    if prev is None or nxt is None:
        return False
    prev_is_major = prev.semantic_type in _MAJOR_SEMANTIC_TYPES
    nxt_is_major = nxt.semantic_type in _MAJOR_SEMANTIC_TYPES
    return prev_is_major and nxt_is_major


def chunk_from_structure(
    doc: PhysicalDocument,
    struct: DocumentStructure,
    *,
    config: DocumentStructureChunkerConfig | None = None,
) -> list[Chunk]:
    """Создать ``Chunk``-и из ``PhysicalDocument`` + ``DocumentStructure``.

    Алгоритм (hierarchical structural packing):

    1. Tables → atomic chunks (каждый table block — свой chunk).
    2. Oversized blocks → split через ``_split_block_with_offsets``,
       каждый split-part — свой chunk.
    3. Structural units: recursive descent по DocumentStructure.
       Если subtree помещается в ``target_chunk_chars`` AND не содержит
       specials → один ``PackableUnit`` на всё subtree.
       Иначе → раскрываем на children + direct gaps родителя.
    4. Все unit'ы (structural + table + oversized) собираются в
       physical document order.
    5. Greedy packing: structural unit'ы могут объединяться, пока не
       встретится table/oversized_part или max overflow или strong boundary.
    6. Emit ``Chunk[]`` с правильными section_id, section_ids, section_path.

    Returns:
        ``list[Chunk]`` в physical document order. ``chunks[i].index``
        строго возрастает.
    """
    from chunking.structural_packing import (
        PackableUnit,
        build_packable_units,
        greedy_pack_units,
    )

    cfg = config or DocumentStructureChunkerConfig()
    chunk_cfg = cfg.chunk_config

    chars_per_token = chunk_cfg.chars_per_token
    max_chunk_chars = chunk_cfg.max_chunk_chars
    chunk_overlap = chunk_cfg.chunk_overlap_chars
    target_chunk_chars = chunk_cfg.target_chunk_chars
    preferred_min = chunk_cfg.preferred_min_before_strong_boundary

    if target_chunk_chars < max_chunk_chars * 0.3:
        target_chunk_chars = int(max_chunk_chars * 0.6)
    if target_chunk_chars > max_chunk_chars:
        target_chunk_chars = int(max_chunk_chars * 0.6)

    by_ord = doc.blocks_by_ord
    if not by_ord:
        return []

    ownership = build_block_ownership(struct)
    section_meta_cache: dict[str, tuple[str, str]] = {}
    preamble_cache: dict[str, str] = {}

    def _meta(section_id: str) -> tuple[str, str]:
        if section_id not in section_meta_cache:
            node = struct.nodes.get(section_id)
            heading = node.title if node is not None else ""
            section_meta_cache[section_id] = (
                _section_path_for(section_id, struct), heading,
            )
        return section_meta_cache[section_id]

    chunks: list[Chunk] = []
    chunk_index = 0
    document_table_counter = 0

    def _emit_chunk(
        *,
        text: str,
        block_indices: tuple[int, ...],
        block_types: tuple[str, ...],
        section_id: str,
        section_path: str,
        section_heading: str,
        section_ids: tuple[str, ...],
        page_start: int | None,
        page_end: int | None,
        table_id: str | None = None,
        source_char_start: int | None = None,
        source_char_end: int | None = None,
    ) -> Chunk:
        nonlocal chunk_index
        chunk_index += 1
        token_est = max(1, len(text) // max(1, int(chars_per_token)))

        chunk = Chunk(
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
            table_row_start=None,
            table_row_end=None,
            source_char_start=source_char_start,
            source_char_end=source_char_end,
            section_ids=section_ids,
        )
        chunks.append(chunk)
        return chunk

    def _emit_unit(unit: PackableUnit) -> None:
        nonlocal document_table_counter
        if not unit.block_indices:
            return

        blocks_in_unit = [by_ord[o] for o in unit.block_indices if o in by_ord]
        if not blocks_in_unit:
            return

        text_parts: list[str] = []
        page_start: int | None = None
        page_end: int | None = None
        block_types_list: list[str] = []
        for b in blocks_in_unit:
            text_parts.append(b.content)
            block_types_list.append(b.block_type)
            if b.page_index is not None:
                if page_start is None or b.page_index < page_start:
                    page_start = b.page_index
            if b.page_end is not None:
                if page_end is None or b.page_end > page_end:
                    page_end = b.page_end

        text = "\n\n".join(text_parts)
        preamble = _build_context_preamble(unit, struct, preamble_cache)
        if preamble:
            text = preamble + text
        section_path, section_heading = _meta(unit.primary_section_id)

        if unit.kind == "table":
            document_table_counter += 1
            table_id = unit.table_id or f"t_{document_table_counter:03d}"
            _emit_chunk(
                text=text,
                block_indices=unit.block_indices,
                block_types=tuple(block_types_list),
                section_id=unit.primary_section_id,
                section_path=section_path,
                section_heading=section_heading,
                section_ids=unit.section_ids,
                page_start=page_start,
                page_end=page_end,
                table_id=table_id,
            )
            return

        if unit.kind == "oversized_part":
            _emit_chunk(
                text=text,
                block_indices=unit.block_indices,
                block_types=tuple(block_types_list),
                section_id=unit.primary_section_id,
                section_path=section_path,
                section_heading=section_heading,
                section_ids=unit.section_ids,
                page_start=page_start,
                page_end=page_end,
                source_char_start=unit.source_char_start,
                source_char_end=unit.source_char_end,
            )
            return

        _emit_chunk(
            text=text,
            block_indices=unit.block_indices,
            block_types=tuple(block_types_list),
            section_id=unit.primary_section_id,
            section_path=section_path,
            section_heading=section_heading,
            section_ids=unit.section_ids,
            page_start=page_start,
            page_end=page_end,
        )

    def _unit_for_block(ord_i: int, owner: str) -> PackableUnit:
        """Создать unit для одного normal block."""
        block = by_ord[ord_i]
        section_ids = _collect_owner_section_ids(
            (ord_i,), ownership, struct.root_id,
        )
        return PackableUnit(
            kind="structural",
            block_indices=(ord_i,),
            section_ids=section_ids,
            primary_section_id=owner,
            char_count=block.char_count,
        )

    structural_units = build_packable_units(
        doc, struct, ownership,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )

    all_units: list[PackableUnit] = []
    structural_idx = 0
    doc_tables: dict[int, str] = {}
    doc_oversized_parts: dict[int, list[tuple[int, int]]] = {}

    for ord_i in sorted(by_ord.keys()):
        block = by_ord.get(ord_i)
        if block is None:
            continue
        if block.block_type == "table":
            document_table_counter += 1
            tid = f"t_{document_table_counter:03d}"
            doc_tables[ord_i] = tid
        elif block.char_count > max_chunk_chars:
            owner = ownership.get(ord_i, struct.root_id)
            parts = _split_block_with_offsets(
                block.content,
                chunk_size=max_chunk_chars,
                chunk_overlap=chunk_overlap,
            )
            doc_oversized_parts[ord_i] = []
            for part_text, cs, ce in parts:
                section_ids = _collect_owner_section_ids(
                    (ord_i,), ownership, struct.root_id,
                )
                all_units.append(
                    PackableUnit(
                        kind="oversized_part",
                        block_indices=(ord_i,),
                        section_ids=section_ids,
                        primary_section_id=owner,
                        char_count=len(part_text),
                        source_block=ord_i,
                        source_char_start=cs,
                        source_char_end=ce,
                    ),
                )

    structural_remaining = list(structural_units)

    for u in structural_remaining:
        all_units.append(u)

    all_units.sort(key=lambda u: (u.block_indices[0] if u.block_indices else 0))

    packed_units = greedy_pack_units(
        all_units,
        target_chunk_chars=target_chunk_chars,
        max_chunk_chars=max_chunk_chars,
        preferred_min_before_strong_boundary=preferred_min,
        struct=struct,
    )

    for unit in packed_units:
        _emit_unit(unit)

    return chunks


def _collect_owner_section_ids(
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


def chunk_from_structure_with_diagnostics(
    doc: PhysicalDocument,
    struct: DocumentStructure,
    *,
    config: DocumentStructureChunkerConfig | None = None,
) -> tuple[list[Chunk], "ChunkingDiagnostics"]:
    """Создать chunks + вернуть ``ChunkingDiagnostics``.

    Diagnostics собирается по результату chunking'а и не влияет на сам
    алгоритм. Полезно для smoke-тестов и CLI-отчётов.
    """
    from dataclasses import dataclass

    chunks = chunk_from_structure(doc, struct, config=config)

    char_counts = [c.char_count for c in chunks]
    sorted_counts = sorted(char_counts)

    n = len(sorted_counts)
    if n == 0:
        median = 0
    elif n % 2 == 1:
        median = sorted_counts[n // 2]
    else:
        median = (sorted_counts[n // 2 - 1] + sorted_counts[n // 2]) // 2

    small_threshold = 5000
    small_chunks = [c for c in chunks if c.char_count < small_threshold]

    physical_blocks = sum(len(c.block_indices) for c in chunks)
    table_chunks = sum(1 for c in chunks if c.table_id is not None)
    oversized_chunks = sum(
        1 for c in chunks
        if c.table_id is None and c.source_char_start is not None
    )
    multi_section = sum(1 for c in chunks if len(c.section_ids) > 1)

    diagnostics = ChunkingDiagnostics(
        physical_blocks=physical_blocks,
        sections=len([n for n in struct.nodes.values() if n.node_type == "section"]),
        chunks=len(chunks),
        special_blocks=sum(
            1 for b in doc.blocks
            if b.block_type == "table" or b.char_count > config.chunk_config.max_chunk_chars
        ) if config else 0,
        unassigned_blocks=0,
        structural_chunks=len(chunks) - table_chunks - oversized_chunks,
        avg_chunk_chars=sum(char_counts) / n if n > 0 else 0,
        median_chunk_chars=median,
        min_chunk_chars=min(char_counts) if char_counts else 0,
        max_chunk_chars=max(char_counts) if char_counts else 0,
        small_chunks_count=len(small_chunks),
        small_chunks_total_chars=sum(c.char_count for c in small_chunks),
        multi_section_chunks=multi_section,
        table_chunks=table_chunks,
        oversized_chunks=oversized_chunks,
    )
    return chunks, diagnostics


@dataclass(frozen=True)
class ChunkingDiagnostics:
    """Диагностика chunking'а (STRUCTURAL_PACKING_PLAN §4).

    Attributes:
        physical_blocks: суммарное число physical blocks в chunks.
        sections: число section nodes в DocumentStructure.
        chunks: всего chunks создано.
        special_blocks: tables + oversized blocks.
        unassigned_blocks: всегда 0 на валидной DocumentStructure.
        structural_chunks: chunks - tables - oversized_parts.
        avg_chunk_chars: средний размер chunk'а.
        median_chunk_chars: медиана.
        min_chunk_chars / max_chunk_chars.
        small_chunks_count: chunks < 5000 chars.
        small_chunks_total_chars: суммарный размер маленьких chunks.
        multi_section_chunks: chunks с len(section_ids) > 1.
        table_chunks / oversized_chunks.
    """

    physical_blocks: int
    sections: int
    chunks: int
    special_blocks: int
    unassigned_blocks: int
    structural_chunks: int

    avg_chunk_chars: float
    median_chunk_chars: float
    min_chunk_chars: int
    max_chunk_chars: int

    small_chunks_count: int
    small_chunks_total_chars: int
    multi_section_chunks: int
    table_chunks: int
    oversized_chunks: int


__all__ = [
    "DocumentStructureChunkerConfig",
    "ChunkPlanner",
    "ChunkingDiagnostics",
    "chunk_from_structure",
    "chunk_from_structure_with_diagnostics",
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
