"""Brief budget allocation (PLAN §16).

Распределение общего LLM-text budget между выбранными chunks
для brief-режима. Это canonical replacement для legacy
``brief_representation.allocate_brief_budget``.

Инварианты:

* ``PhysicalDocument`` НЕ мутируется. Все chunks — новые экземпляры
  (``dataclasses.replace``).
* ``source_char_start`` / ``source_char_end`` остаются от оригинала
  (provenance сохраняется).
* Tables пропускаются целиком (atomicity invariant §6).
* Если ``total_budget_chars`` is None или <= 0 → no-op.
* Суммарный объём текстовых chunks после обрезки ≤ ``total_budget_chars``.
"""

from __future__ import annotations

from dataclasses import replace

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


def _is_table_chunk(c: Chunk) -> bool:
    return bool(c.block_types) and c.block_types == ("table",)


def allocate_brief_budget(
    chunks: list[Chunk],
    *,
    total_budget_chars: int | None,
) -> list[Chunk]:
    """Распределить общий LLM-text budget между chunks.

    Принимает уже выбранные chunks (после coverage-фазы) и обрезает их
    ``text`` так, чтобы **суммарный объём** для LLM не превышал
    ``total_budget_chars``.

    Алгоритм:

    1. Разделяем chunks на текстовые и табличные.
    2. Если суммарный объём текстовых chunks ≤ budget → no-op.
    3. Иначе — пропорционально распределяем budget по текущей длине
       каждого текстового chunk'а. Резервируем ``suffix_len`` под " …"
       суффикс. ``min_per_chunk`` — нижняя граница (200 chars).
    """
    if total_budget_chars is None or total_budget_chars <= 0:
        return list(chunks)
    if not chunks:
        return []

    table_chunks = [c for c in chunks if _is_table_chunk(c)]
    text_chunks = [c for c in chunks if not _is_table_chunk(c)]

    if not text_chunks:
        return list(chunks)

    total_text_chars = sum(len(c.text) for c in text_chunks)
    if total_text_chars <= total_budget_chars:
        return list(chunks)

    suffix_len = 2
    n = len(text_chunks)
    available = max(0, total_budget_chars - suffix_len * n)

    min_per_chunk = 200
    if available < min_per_chunk * n:
        min_per_chunk = max(50, available // max(1, n))

    out: list[Chunk] = list(table_chunks)
    for c in text_chunks:
        share = max(
            min_per_chunk,
            int(round(len(c.text) * available / max(1, total_text_chars))),
        )
        share = min(share, total_budget_chars)
        if len(c.text) <= share:
            out.append(c)
            continue
        truncated = c.text[:share].rstrip()
        if len(truncated) < len(c.text):
            truncated = truncated + " …"
        out.append(
            replace(c, text=truncated, char_count=len(truncated))
        )
    return out


def total_input_chars(chunks: list[Chunk]) -> int:
    """Суммарный объём ``text`` в chunks."""
    return sum(len(c.text) for c in chunks)


__all__ = [
    "allocate_brief_budget",
    "total_input_chars",
]