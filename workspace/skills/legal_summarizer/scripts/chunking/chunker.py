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
from chunking.block_ownership import (
    build_block_ownership,
    owner_for_block,
)
from document.structure import (
    DocumentStructure,
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


def build_chunk_config_from_runtime() -> ChunkConfig:
    """Построить ``ChunkConfig`` из runtime-конфига.

    Источник истины — ``context_window_tokens`` (берётся из
    ``skills.legal_summarizer.chunking`` или LLM config). Размер чанка
    вычисляется как:

        max_chunk_chars = context_window_tokens * chunk_size_input_ratio
                         / chars_per_token

    Fallback: если ``context_window_tokens`` не задан — используем
    ``chunk_size`` (literal fallback). НЕ использовать
    захардкоженное 100000.

    Дополнительные поля:
        target_chunk_chars (default 20000) — мягкий ориентир;
        preferred_min_before_strong_boundary (default 0.7) — порог для
        решения о закрытии chunk'а на strong boundary.

    Args:
        chunk_size_config_override: для тестов
            (см. ``tests/architecture/test_layer_boundaries.py``).
    """
    import llm.config as _llm_config_mod

    chunk_cfg = _llm_config_mod.get_chunking_config() or {}
    chars_per_token = float(chunk_cfg.get("chars_per_token", 3.5))

    ctx_tokens = chunk_cfg.get("context_window_tokens")
    ratio = chunk_cfg.get("chunk_size_input_ratio")
    if ctx_tokens and ratio:
        max_chunk_chars = int(ctx_tokens * float(ratio) * chars_per_token)
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


def _is_strong_boundary(
    prev_owner_id: str,
    nxt_owner_id: str,
    struct: DocumentStructure,
) -> bool:
    """True, если prev→next — major→major переход между разными узлами.

    Strong boundary = два соседних structural unit'а с major semantic_type
    (chapter / section / appendix / razdel), при условии что prev_owner !=
    nxt_owner (т.е. это разные structural узлы).

    Returns False если:
    - prev_owner == nxt_owner;
    - один из owners = root_id (root — generic, не имеет strong boundary);
    - один из owners = None или отсутствует в struct.nodes;
    - оба не являются major.

    Caller проверяет условие "current >= preferred_min * target" отдельно.
    """
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
    if not (prev_is_major and nxt_is_major):
        return False

    return True


def chunk_from_structure(
    doc: PhysicalDocument,
    struct: DocumentStructure,
    *,
    config: DocumentStructureChunkerConfig | None = None,
) -> list[Chunk]:
    """Создать ``Chunk``-и из ``PhysicalDocument`` + ``DocumentStructure``.

    Алгоритм (STRUCTURAL_PACKING_PLAN v3 §2):

    1. Строим ``block_ownership`` (deepest owner для каждого block).
    2. Итерируем **physical blocks** в ordinal order (НЕ sections).
    3. Tables — atomic chunks.
    4. Oversized blocks — split через ``_split_block_with_offsets``,
       каждый split-part — свой chunk.
    5. Normal blocks — greedy packing:
       - same owner и помещается в max → merge;
       - max overflow → close current, start new;
       - strong boundary + current ≥ preferred_min*target → close;
       - иначе (слабая граница) → merge.
    6. chunk_id = zero-padded index (1-based).

    Returns:
        ``list[Chunk]`` в physical document order. ``chunks[i].index``
        строго возрастает.
    """
    cfg = config or DocumentStructureChunkerConfig()
    chunk_cfg = cfg.chunk_config

    chars_per_token = chunk_cfg.chars_per_token
    max_chunk_chars = chunk_cfg.max_chunk_chars
    chunk_overlap = chunk_cfg.chunk_overlap_chars
    target_chunk_chars = chunk_cfg.target_chunk_chars
    preferred_min = chunk_cfg.preferred_min_before_strong_boundary

    by_ord = doc.blocks_by_ord
    if not by_ord:
        return []

    ownership = build_block_ownership(struct)
    section_meta_cache: dict[str, tuple[str, str]] = {}

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
        owners: tuple[str, ...],
        page_start: int | None,
        page_end: int | None,
        table_id: str | None = None,
        table_row_start: int | None = None,
        table_row_end: int | None = None,
        source_char_start: int | None = None,
        source_char_end: int | None = None,
    ) -> Chunk:
        nonlocal chunk_index
        chunk_index += 1
        token_est = max(1, len(text) // max(1, int(chars_per_token)))

        seen: list[str] = []
        for o in owners:
            if o == struct.root_id:
                continue
            if o not in seen:
                seen.append(o)
        section_ids = tuple(seen)

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
            table_row_start=table_row_start,
            table_row_end=table_row_end,
            source_char_start=source_char_start,
            source_char_end=source_char_end,
            section_ids=section_ids,
        )
        chunks.append(chunk)
        return chunk

    def _flush_current(
        current_blocks: list[int],
        current_owners: list[str],
    ) -> None:
        if not current_blocks:
            return
        section_id = current_owners[0]
        section_path, section_heading = _meta(section_id)

        text_parts: list[str] = []
        page_start: int | None = None
        page_end: int | None = None
        block_types_list: list[str] = []
        for ord_idx, owner in zip(current_blocks, current_owners):
            block = by_ord.get(ord_idx)
            if block is None:
                continue
            text_parts.append(block.content)
            block_types_list.append(block.block_type)
            if block.page_index is not None:
                if page_start is None or block.page_index < page_start:
                    page_start = block.page_index
            if block.page_end is not None:
                if page_end is None or block.page_end > page_end:
                    page_end = block.page_end

        _emit_chunk(
            text="\n\n".join(text_parts),
            block_indices=tuple(current_blocks),
            block_types=tuple(block_types_list),
            section_id=section_id,
            section_path=section_path,
            section_heading=section_heading,
            owners=tuple(current_owners),
            page_start=page_start,
            page_end=page_end,
        )

    current_blocks: list[int] = []
    current_owners: list[str] = []
    current_chars = 0
    current_anchor_owner: str | None = None

    ordered_ords = sorted(by_ord.keys())

    def _start_new_at(ord_i: int, owner: str, block: DocumentBlock) -> None:
        nonlocal current_blocks, current_owners, current_chars, current_anchor_owner
        current_blocks = [ord_i]
        current_owners = [owner]
        current_chars = block.char_count
        current_anchor_owner = owner

    for ord_i in ordered_ords:
        block = by_ord.get(ord_i)
        if block is None:
            continue
        owner = ownership.get(ord_i, struct.root_id)

        if block.block_type == "table":
            _flush_current(current_blocks, current_owners)
            current_blocks = []
            current_owners = []
            current_chars = 0
            current_anchor_owner = None

            document_table_counter += 1
            section_path, section_heading = _meta(owner)
            _emit_chunk(
                text=block.content,
                block_indices=(ord_i,),
                block_types=(block.block_type,),
                section_id=owner,
                section_path=section_path,
                section_heading=section_heading,
                owners=(owner,),
                page_start=block.page_index,
                page_end=block.page_end,
                table_id=f"t_{document_table_counter:03d}",
            )
            continue

        if block.char_count > max_chunk_chars:
            _flush_current(current_blocks, current_owners)
            current_blocks = []
            current_owners = []
            current_chars = 0
            current_anchor_owner = None

            section_path, section_heading = _meta(owner)
            parts = _split_block_with_offsets(
                block.content,
                chunk_size=max_chunk_chars,
                chunk_overlap=chunk_overlap,
            )
            for part_text, cs, ce in parts:
                _emit_chunk(
                    text=part_text,
                    block_indices=(ord_i,),
                    block_types=(block.block_type,),
                    section_id=owner,
                    section_path=section_path,
                    section_heading=section_heading,
                    owners=(owner,),
                    page_start=block.page_index,
                    page_end=block.page_end,
                    source_char_start=cs,
                    source_char_end=ce,
                )
            continue

        if not current_blocks:
            _start_new_at(ord_i, owner, block)
            continue

        assert current_anchor_owner is not None

        if owner == current_anchor_owner and current_chars + block.char_count <= max_chunk_chars:
            current_blocks.append(ord_i)
            current_owners.append(owner)
            current_chars += block.char_count
            continue

        if current_chars + block.char_count > max_chunk_chars:
            _flush_current(current_blocks, current_owners)
            current_blocks = []
            current_owners = []
            current_chars = 0
            current_anchor_owner = None
            _start_new_at(ord_i, owner, block)
            continue

        if _is_strong_boundary(current_anchor_owner, owner, struct) and (
            current_chars >= target_chunk_chars * preferred_min
        ):
            _flush_current(current_blocks, current_owners)
            current_blocks = []
            current_owners = []
            current_chars = 0
            current_anchor_owner = None
            _start_new_at(ord_i, owner, block)
            continue

        current_blocks.append(ord_i)
        current_owners.append(owner)
        current_chars += block.char_count

    _flush_current(current_blocks, current_owners)

    return chunks


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
