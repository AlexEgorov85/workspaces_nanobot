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

from lib.services.text_splitter import split_text
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    ROOT_SECTION_ID,
    DocumentSection,
    SectionTree,
)


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
            body_texts = [b.content for b in body_blocks]
            joined = "\n\n".join(body_texts)
            if len(joined) <= config.max_chunk_chars:
                parts = [joined]
            else:
                parts = split_text(
                    joined,
                    chunk_size=config.max_chunk_chars,
                    chunk_overlap=config.chunk_overlap_chars,
                )
                if not parts:
                    parts = [joined]

            page_start = next(
                (b.page_index for b in body_blocks if b.page_index is not None), None
            )
            page_end = next(
                (b.page_index for b in reversed(body_blocks) if b.page_index is not None),
                None,
            )

            body_indices = tuple(b.ordinal for b in body_blocks)
            body_types = tuple(b.block_type for b in body_blocks)

            for i, part in enumerate(parts):
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
                        block_indices=body_indices if i == 0 else (),
                        block_types=body_types if i == 0 else (),
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
                )
            )
        return normalized


__all__ = [
    "Chunk",
    "ChunkConfig",
    "StructureAwareChunker",
]