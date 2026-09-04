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
        block_indices: tuple[int, ...] — ordinal'ы DocumentBlock'ов
            (для ``text``, не для provenance target).
        block_types: tuple[str, ...] — параллельный массив.
        table_id: ``"t_001"`` если chunk — это таблица.
        table_row_start / table_row_end: для split-table (1-based rows).
        source_char_start / source_char_end: offsets для split block'а
            **внутри ``block_indices[0]``**.
        target_block_indices: tuple[int, ...] — ordinal'ы DocumentBlock'ов
            **для original provenance target** (только для follow-up
            reconstructed chunks). ``None`` → не установлено (legacy /
            обычные map-reduce chunks).
        target_source_char_start / target_source_char_end: offsets target'а
            внутри ``target_block_indices[0]`` (split chunk case). ``None``
            для whole-block target.
        source_spans: tuple[tuple[int, int, int | None, int | None], ...] —
            список ``(block_ordinal, block_char_start, block_char_end,
            target_marker)`` где ``target_marker=1`` помечает span, который
            был primary target при cache-assisted retrieval.
            ``()`` для обычных chunks.
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
    target_block_indices: tuple[int, ...] | None = None
    target_source_char_start: int | None = None
    target_source_char_end: int | None = None
    source_spans: tuple[tuple[int, int, int | None, int | None], ...] = ()

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
            "target_block_indices": (
                list(self.target_block_indices)
                if self.target_block_indices is not None else None
            ),
            "target_source_char_start": self.target_source_char_start,
            "target_source_char_end": self.target_source_char_end,
            "source_spans": [
                {
                    "block_ordinal": b,
                    "char_start": cs,
                    "char_end": ce,
                    "is_target": bool(marker),
                }
                for (b, cs, ce, marker) in self.source_spans
            ],
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
    "reconstruct_source_fragment",
]