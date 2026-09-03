"""Structure-Aware Chunker для legal_summarizer.

Преобразует ``DocumentBlock[]`` + ``SectionTree`` в ``Chunk[]`` с сохранением:

* ``section_id`` / ``section_path`` / ``section_heading``
* ``page_start`` / ``page_end``
* ``block_indices`` (отсортированы, монотонны — invariant #3)
* ``table_id`` / ``table_row_start`` / ``table_row_end`` для таблиц

Ключевое правило: **tables атомарны**. Chunk не может содержать часть таблицы.
Если таблица > max_chunk_chars → split by rows, каждый row-chunk несёт
``table_id`` + ``row_start``/``row_end``.

Body (paragraphs) chunk'ятся per-section через ``lib.services.text_splitter``
с overlap'ом.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    ROOT_SECTION_ID,
    DocumentSection,
    SectionTree,
)


_SPLIT_SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ", "")


def _split_block_with_offsets(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[tuple[str, int, int]]:
    """Разбить текст block'а на подчасти и сохранить абсолютные offsets.

    Возвращает список ``(part_text, char_start, char_end)``, где
    ``text[char_start:char_end] == part_text``. Offsets отсчитываются от
    начала ``text`` (block.content).

    Используется как fallback, когда block больше ``max_chunk_chars``.
    Реализует split детерминированно по separators и сохраняет offsets.

    Гарантии:
        * ``chunk_overlap`` для skill'а = 0 (см. invariant #21 +
          ``test_chunk_overlap_project_json_default_is_zero``).
        * ``text[char_start:char_end] == part_text`` **посимвольно** —
          invariant #7 (exact reconstruction).
    """
    if not text:
        return [("", 0, 0)]
    if len(text) <= chunk_size:
        return [(text, 0, len(text))]

    parts: list[tuple[str, int, int]] = []
    cursor = 0
    n = len(text)
    while cursor < n:
        end = min(cursor + chunk_size, n)
        if end >= n:
            parts.append((text[cursor:end], cursor, end))
            break
        window_start = cursor + (chunk_size // 2)
        cut = -1
        for sep in _SPLIT_SEPARATORS:
            if not sep:
                cut = end
                break
            search_in = text[window_start:end]
            idx_in_window = search_in.rfind(sep)
            if idx_in_window >= 0:
                cut = window_start + idx_in_window + len(sep)
                break
        if cut <= cursor:
            cut = end
        parts.append((text[cursor:cut], cursor, cut))
        if chunk_overlap <= 0:
            cursor = cut
        else:
            cursor = max(cut - chunk_overlap, cursor + 1)
    if not parts:
        return [(text, 0, len(text))]
    return parts


@dataclass(frozen=True)
class Chunk:
    """Результат structure-aware chunker'а.

    Attributes:
        chunk_id: ``"001"``, ``"002"``, ... (zero-padded width=3).
        index: 0..N-1, document order.
        text: содержимое.
        char_count: ``len(text)``.
        token_estimate: ``ceil(char_count / chars_per_token)``.
        page_start / page_end: 1-based (или None для DOCX без page info).
        section_id: ``"s_0001"`` или ``"s_root"``.
        section_path: ``"1"`` / ``"1 > 1.2"`` / ``""``.
        section_heading: текст heading'а (``""`` для root).
        block_indices: tuple[int, ...] — ordinal'ы DocumentBlock'ов.
        block_types: tuple[str, ...] — параллельный массив.
        table_id: ``"t_001"`` если chunk — это таблица.
        table_row_start / table_row_end: для split-table (1-based rows).
        tokens_per_chunk_estimate: token_estimate (alias для удобства).
    """

    chunk_id: str
    index: int
    text: str
    char_count: int
    token_estimate: int
    page_start: int | None
    page_end: int | None
    section_id: str
    section_path: str
    section_heading: str
    block_indices: tuple[int, ...]
    block_types: tuple[str, ...]
    table_id: str | None = None
    table_row_start: int | None = None
    table_row_end: int | None = None
    source_char_start: int | None = None
    source_char_end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "index": self.index,
            "text": self.text,
            "char_count": self.char_count,
            "token_estimate": self.token_estimate,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section_id": self.section_id,
            "section_path": self.section_path,
            "section_heading": self.section_heading,
            "block_indices": list(self.block_indices),
            "block_types": list(self.block_types),
            "table_id": self.table_id,
            "table_row_start": self.table_row_start,
            "table_row_end": self.table_row_end,
            "source_char_start": self.source_char_start,
            "source_char_end": self.source_char_end,
        }


@dataclass(frozen=True)
class ChunkConfig:
    """Параметры chunker'а."""

    max_chunk_chars: int
    chunk_overlap_chars: int
    chars_per_token: float = 3.5
    table_chunk_threshold_chars: int = 6000
    min_chunk_chars: int = 200
    min_section_chars: int = 200


def _make_table_chunk_text(rows: list[str], row_start: int, row_end: int) -> str:
    header = f"[TABLE rows {row_start}–{row_end}]\n"
    return header + "\n".join(rows[row_start - 1:row_end])


def _split_table_into_chunks(
    table_text: str,
    rows_total: int,
    threshold_chars: int,
) -> list[tuple[str, int, int]]:
    """Split a table by rows into multiple chunks, each ≤ threshold.

    Returns: list of (text, row_start, row_end). All rows preserved.
    """
    lines = table_text.split("\n")
    chunks: list[tuple[str, int, int]] = []
    cur_lines: list[str] = []
    cur_start = 1
    cur_len = 0
    for i, line in enumerate(lines, start=1):
        added = len(line) + (1 if cur_lines else 0)
        if cur_lines and cur_len + added > threshold_chars:
            chunks.append(("\n".join(cur_lines), cur_start, i - 1))
            cur_lines = [line]
            cur_start = i
            cur_len = len(line)
        else:
            cur_lines.append(line)
            cur_len += added
    if cur_lines:
        chunks.append(("\n".join(cur_lines), cur_start, cur_start + len(cur_lines) - 1))
    return chunks


class StructureAwareChunker:
    """Преобразует PhysicalDocument + SectionTree в list[Chunk]."""

    def chunk(
        self,
        doc: PhysicalDocument,
        tree: SectionTree,
        config: ChunkConfig,
    ) -> list[Chunk]:
        blocks_by_ord: dict = {b.ordinal: b for b in doc.blocks}

        chunks: list[Chunk] = []
        counter = 0

        for sid, section in tree.sections.items():
            if sid == ROOT_SECTION_ID:
                continue
            section_chunks = self._chunk_section(
                section=section,
                blocks_by_ord=blocks_by_ord,
                config=config,
                start_counter=counter,
            )
            chunks.extend(section_chunks)
            counter += len(section_chunks)

        if tree.sections[ROOT_SECTION_ID].block_indices:
            root_chunks = self._chunk_section(
                section=tree.sections[ROOT_SECTION_ID],
                blocks_by_ord=blocks_by_ord,
                config=config,
                start_counter=counter,
            )
            chunks.extend(root_chunks)
            counter += len(root_chunks)

        return self._finalize(chunks, config)

    def _chunk_section(
        self,
        section: DocumentSection,
        blocks_by_ord: dict[int, DocumentBlock],
        config: ChunkConfig,
        start_counter: int,
    ) -> list[Chunk]:
        """Block-aware chunking.

        Алгоритм:
            1. Разделить блоки секции на body и tables (как раньше).
            2. Для body: greedy накопление целых блоков в chunk пока
               ``current_chars + block_chars <= max_chunk_chars``.
               Если следующий блок не влезает → flush current chunk, начать новый.
               Если ОДИН блок больше ``max_chunk_chars`` → fallback на
               ``split_text`` для этого блока (как было).
            3. Tables обрабатываются отдельно и атомарны (как было).

        Преимущества над старым «join → split_text»:
            - chunk boundaries = block boundaries (где возможно);
            - меньше overlap-артефактов;
            - ``block_indices`` точно отражают содержимое каждого chunk.
        """
        body_blocks: list[DocumentBlock] = []
        table_blocks: list[DocumentBlock] = []
        for idx in section.block_indices:
            b = blocks_by_ord.get(idx)
            if b is None:
                continue
            if b.block_type == "table":
                table_blocks.append(b)
            else:
                body_blocks.append(b)

        out: list[Chunk] = []
        counter = start_counter

        if body_blocks:
            chunks_data: list[
                tuple[str, tuple[int, ...], tuple[str, ...], int | None, int | None]
            ] = []
            current_texts: list[str] = []
            current_indices: list[int] = []
            current_types: list[str] = []
            current_chars = 0

            def _flush_body() -> None:
                nonlocal current_texts, current_indices, current_types, current_chars
                if current_texts:
                    chunks_data.append((
                        "\n\n".join(current_texts),
                        tuple(current_indices),
                        tuple(current_types),
                        None,
                        None,
                    ))
                    current_texts = []
                    current_indices = []
                    current_types = []
                    current_chars = 0

            for b in body_blocks:
                b_chars = len(b.content)
                # Если блок сам по себе больше budget — fallback на split.
                if b_chars > config.max_chunk_chars:
                    _flush_body()
                    parts_with_offsets = _split_block_with_offsets(
                        b.content,
                        chunk_size=config.max_chunk_chars,
                        chunk_overlap=config.chunk_overlap_chars,
                    )
                    for part, char_start, char_end in parts_with_offsets:
                        chunks_data.append(
                            (part, (b.ordinal,), (b.block_type,), char_start, char_end)
                        )
                    continue

                # Пытаемся добавить целый блок.
                if current_chars + b_chars > config.max_chunk_chars and current_texts:
                    _flush_body()
                current_texts.append(b.content)
                current_indices.append(b.ordinal)
                current_types.append(b.block_type)
                current_chars += b_chars

            _flush_body()

            for part, block_indices, block_types, char_start, char_end in chunks_data:
                if block_indices:
                    page_starts = [
                        blocks_by_ord[o].page_index
                        for o in block_indices
                        if blocks_by_ord[o].page_index is not None
                    ]
                    page_start = min(page_starts) if page_starts else None
                    page_end = max(page_starts) if page_starts else None
                else:
                    page_start = None
                    page_end = None

                chunk_id = f"{counter:03d}"
                out.append(
                    Chunk(
                        chunk_id=chunk_id,
                        index=counter,
                        text=part,
                        char_count=len(part),
                        token_estimate=max(1, int(len(part) / config.chars_per_token + 0.999)),
                        page_start=page_start,
                        page_end=page_end,
                        section_id=section.section_id,
                        section_path=section.section_path,
                        section_heading=section.heading,
                        block_indices=block_indices,
                        block_types=block_types,
                        source_char_start=char_start,
                        source_char_end=char_end,
                    )
                )
                counter += 1

        for t_idx, table_block in enumerate(table_blocks):
            table_id = f"t_{table_block.ordinal:04d}"
            rows_total = table_block.block_metadata.get("row_count", 1)
            if table_block.char_count <= config.table_chunk_threshold_chars:
                chunk_id = f"{counter:03d}"
                out.append(
                    Chunk(
                        chunk_id=chunk_id,
                        index=counter,
                        text=table_block.content,
                        char_count=table_block.char_count,
                        token_estimate=max(1, int(table_block.char_count / config.chars_per_token + 0.999)),
                        page_start=table_block.page_index,
                        page_end=table_block.page_end,
                        section_id=section.section_id,
                        section_path=section.section_path,
                        section_heading=section.heading,
                        block_indices=(table_block.ordinal,),
                        block_types=("table",),
                        table_id=table_id,
                        table_row_start=1,
                        table_row_end=rows_total,
                    )
                )
                counter += 1
            else:
                row_chunks = _split_table_into_chunks(
                    table_block.content,
                    rows_total=rows_total,
                    threshold_chars=config.table_chunk_threshold_chars,
                )
                for txt, row_start, row_end in row_chunks:
                    chunk_id = f"{counter:03d}"
                    out.append(
                        Chunk(
                            chunk_id=chunk_id,
                            index=counter,
                            text=txt,
                            char_count=len(txt),
                            token_estimate=max(1, int(len(txt) / config.chars_per_token + 0.999)),
                            page_start=table_block.page_index,
                            page_end=table_block.page_end,
                            section_id=section.section_id,
                            section_path=section.section_path,
                            section_heading=section.heading,
                            block_indices=(table_block.ordinal,),
                            block_types=("table",),
                            table_id=table_id,
                            table_row_start=row_start,
                            table_row_end=row_end,
                        )
                    )
                    counter += 1

        return out

    def _finalize(self, chunks: list[Chunk], config: ChunkConfig) -> list[Chunk]:
        normalized: list[Chunk] = []
        for i, c in enumerate(chunks):
            if c.block_types and c.block_types[-1] == "table":
                pass
            normalized.append(
                Chunk(
                    chunk_id=f"{i:03d}",
                    index=i,
                    text=c.text,
                    char_count=c.char_count,
                    token_estimate=c.token_estimate,
                    page_start=c.page_start,
                    page_end=c.page_end,
                    section_id=c.section_id,
                    section_path=c.section_path,
                    section_heading=c.section_heading,
                    block_indices=c.block_indices,
                    block_types=c.block_types,
                    table_id=c.table_id,
                    table_row_start=c.table_row_start,
                    table_row_end=c.table_row_end,
                    source_char_start=c.source_char_start,
                    source_char_end=c.source_char_end,
                )
            )
        return normalized


def reconstruct_source_fragment(
    chunk: Chunk,
    *,
    doc: PhysicalDocument | None = None,
    blocks: tuple[DocumentBlock, ...] | None = None,
) -> str:
    """Восстановить точный исходный текст чанка из PhysicalDocument.

    Контракт:
        * ``block_indices`` — отсортированные ordinal'ы ``DocumentBlock``;
          для обычного chunk (целый block или greedy-соединение 2+ блоков
          одной секции) — этого достаточно.
        * ``source_char_start`` / ``source_char_end`` — обязательны для
          split chunks. ``None`` означает «целый block» (back-compat).

    Поддерживаемые случаи:
        1. Целый block (``source_char_start is None``) → ``block.content``.
        2. Split block → ``block.content[source_char_start:source_char_end]``.
        3. Multi-block chunk → конкатенация ``block.content`` через ``"\\n\\n"``.
        4. Table chunk → ``block.content`` (таблица атомарна).
    """
    blocks_by_ord: dict[int, DocumentBlock] = {}
    if doc is not None:
        blocks_by_ord = {b.ordinal: b for b in doc.blocks}
    elif blocks is not None:
        blocks_by_ord = {b.ordinal: b for b in blocks}
    else:
        raise ValueError("reconstruct_source_fragment: нужен doc или blocks")

    if not chunk.block_indices:
        raise ValueError(
            f"chunk {chunk.chunk_id}: пустые block_indices — нельзя реконструировать"
        )

    if len(chunk.block_indices) == 1:
        ordinal = chunk.block_indices[0]
        block = blocks_by_ord.get(ordinal)
        if block is None:
            raise ValueError(
                f"chunk {chunk.chunk_id}: block ordinal={ordinal} не найден"
            )
        if chunk.source_char_start is None and chunk.source_char_end is None:
            return block.content
        start = chunk.source_char_start
        end = chunk.source_char_end
        if start is None or end is None:
            raise ValueError(
                f"chunk {chunk.chunk_id}: split chunk требует оба offsets "
                f"(start={start}, end={end})"
            )
        if start < 0 or end > len(block.content) or start > end:
            raise ValueError(
                f"chunk {chunk.chunk_id}: offsets вне диапазона "
                f"[0, {len(block.content)}] (start={start}, end={end})"
            )
        return block.content[start:end]

    parts: list[str] = []
    for ordinal in chunk.block_indices:
        block = blocks_by_ord.get(ordinal)
        if block is None:
            raise ValueError(
                f"chunk {chunk.chunk_id}: block ordinal={ordinal} не найден"
            )
        parts.append(block.content)
    return "\n\n".join(parts)


__all__ = [
    "Chunk",
    "ChunkConfig",
    "StructureAwareChunker",
    "reconstruct_source_fragment",
]