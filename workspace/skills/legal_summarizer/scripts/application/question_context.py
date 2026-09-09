"""Question synthesis context builder (#7).

Собирает LLM-вход для ``llm_document_reduce(question=...)`` из
document-level cache (commit #6) без повторного map-вызова LLM.

Три уровня (от cheap к deep):
  1. **section_summary** — per-section LLM summary из
     ``documents/<doc_id>/sections/<sid>.json`` (#0c).
     Если секция, к которой относится chunk, уже просуммирована — это
     самое компактное представление, и для большинства юридических
     вопросов достаточно (раздел «Обязанности сторон» целиком).
  2. **chunk_summary** — per-chunk LLM summary из
     ``documents/<doc_id>/chunks/<cid>.json`` (#4).
     Если нет section summary, но есть chunk summary — это 2-4 предложения
     на chunk.
  3. **chunk.text** — оригинальный текст chunk'а (из
     ``DocumentAnalysis.chunks``, in-memory; НЕ из PhysicalDocument —
     текст уже есть после cache hit).

Правило: для каждого выбранного chunk'а передаём **всегда** section heading
(бесплатно), и **все три уровня**, если они доступны. Source text
никогда не опускаем — это lossless fallback, если summaries неполные.
"""

from __future__ import annotations

from typing import Iterable

from cache.manifest import (
    load_document_chunk_summaries,
    load_document_section_summaries,
)
from chunking.chunks import Chunk


def _format_section_summary_block(
    *,
    chunk_id: str,
    section_id: str,
    section_path: str,
    section_heading: str,
    page_start: int | None,
    page_end: int | None,
    section_summary: str | None,
    chunk_summary: str | None,
    source_text: str,
    include_section_summary: bool,
    include_chunk_summary: bool,
    include_source_text: bool,
) -> str:
    """Один chunk-блок для LLM-входа.

    Блок **всегда** содержит метаданные (section heading, / page range).
    Содержимое зависит от того, что доступно в document cache.
    """
    label = section_heading or section_path or "(root)"
    pages_str = ""
    if page_start is not None and page_end is not None:
        if page_start == page_end:
            pages_str = f", page {page_start}"
        else:
            pages_str = f", pages {page_start}-{page_end}"
    elif page_start is not None:
        pages_str = f", page {page_start}"

    head = f"[CHUNK {chunk_id} | Section: {label}{pages_str}]"
    body = _summaries_block_text(
        _FakeChunk(chunk_id=chunk_id, section_heading=label),
        section_summary=section_summary,
        chunk_summary=chunk_summary,
    )
    if include_source_text and source_text:
        body += "\nSOURCE TEXT:\n" + source_text
    return head + body


def _summaries_block_text(
    chunk: "Chunk",
    *,
    section_summary: str | None,
    chunk_summary: str | None,
) -> str:
    """Возвращает текст summaries блока (без metadata header и source text)."""
    parts: list[str] = []
    if section_summary:
        parts.append("\nCACHED SECTION SUMMARY:")
        parts.append(section_summary.strip())
    if chunk_summary:
        parts.append("\nCACHED CHUNK SUMMARY:")
        parts.append(chunk_summary.strip())
    return "\n".join(parts)


class _FakeChunk:
    __slots__ = ("chunk_id", "section_heading")

    def __init__(self, *, chunk_id: str, section_heading: str) -> None:
        self.chunk_id = chunk_id
        self.section_heading = section_heading


def build_question_context(
    selected_chunks: list[Chunk],
    *,
    document_id: str,
    workspace_root: str | None,
    budget_chars: int,
) -> str:
    """Собрать LLM-вход для question synthesis.

    Загружает section summaries (level 1) и chunk summaries (level 2)
    из document-level cache. Source text (level 3) берётся из
    ``Chunk.text`` (in-memory из DocumentAnalysis.chunks после cache hit).

    Применяет ``fit_input``-подобный budget, начиная с самого длинного
    chunk source text (детерминированно: сначала укорачиваем самые
    длинные блоки).

    Args:
        selected_chunks: chunks, выбранные lexical retrieval'ом
            (``DocumentAnalysis.retrieve`` или ``relaxed_lexical_fallback``).
        document_id: ``DocumentIdentity.document_id``.
        workspace_root: корень workspace.
        budget_chars: максимальный размер выхода (chars). Должен быть
            согласован с ``DOCUMENT_REDUCE_INPUT_BUDGET_CHARS`` из
            ``execution.map_reduce``.

    Returns:
        Готовая строка для передачи в ``llm_document_reduce``.
    """
    if not selected_chunks:
        return ""

    # 1. Загрузить chunk summaries.
    chunk_summaries = load_document_chunk_summaries(
        document_id,
        [c.chunk_id for c in selected_chunks],
        workspace_root,
    )

    # 2. Собрать уникальные section_id'ы и загрузить section summaries.
    section_ids = sorted({c.section_id for c in selected_chunks if c.section_id})
    section_summaries = load_document_section_summaries(
        document_id, section_ids, workspace_root,
    )

    # 3. Собрать блоки (порядок = порядок selected_chunks).
    blocks: list[str] = []
    for chunk in selected_chunks:
        block = _format_section_summary_block(
            chunk_id=chunk.chunk_id,
            section_id=chunk.section_id or "",
            section_path=chunk.section_path or "",
            section_heading=chunk.section_heading or "",
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section_summary=(
                section_summaries.get(chunk.section_id)
                if chunk.section_id else None
            ),
            chunk_summary=chunk_summaries.get(chunk.chunk_id),
            source_text=chunk.text,
            include_section_summary=True,
            include_chunk_summary=True,
            include_source_text=True,
        )
        blocks.append(block)

    joined = "".join(blocks)

    # 4. Budget truncate. Если общий размер > budget_chars — обрезаем
    # source text каждого chunk'а до равной доли, сохраняя summaries.
    # Доля вычисляется так, чтобы (metadata + summaries) каждого блока +
    # truncated source text <= budget_chars.
    if len(joined) <= budget_chars:
        return joined

    # Вычислить «несущую» часть каждого блока (metadata + summaries).
    overhead_per_chunk: list[int] = []
    for chunk, block in zip(selected_chunks, blocks):
        chunk_summary = chunk_summaries.get(chunk.chunk_id)
        section_summary = (
            section_summaries.get(chunk.section_id)
            if chunk.section_id else None
        )
        overhead = len(
            _format_section_summary_block(
                chunk_id=chunk.chunk_id,
                section_id=chunk.section_id or "",
                section_path=chunk.section_path or "",
                section_heading=chunk.section_heading or "",
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section_summary=section_summary,
                chunk_summary=chunk_summary,
                source_text="",
                include_section_summary=True,
                include_chunk_summary=True,
                include_source_text=False,
            )
        )
        overhead_per_chunk.append(overhead)

    total_overhead = sum(overhead_per_chunk)
    # Учесть завершающие переводы строк между блоками.
    # Каждый блок = overhead (без source text) + "\nSOURCE TEXT:\n"
    # + truncated_source. Уменьшаем budget на сумму overhead с запасом
    # на межблочные переводы (\n\n × (N-1)) и на сам "\nSOURCE TEXT:\n"
    # каждого блока.
    inter_block = 2 * max(0, len(selected_chunks) - 1)
    source_template_total = 14 * len(selected_chunks)  # "\nSOURCE TEXT:\n"
    available_for_sources = max(
        0,
        budget_chars
        - total_overhead
        - inter_block
        - source_template_total,
    )
    per_chunk_source_budget = max(0, available_for_sources // max(1, len(selected_chunks)))

    truncated_blocks: list[str] = []
    for chunk, overhead in zip(selected_chunks, overhead_per_chunk):
        chunk_summary = chunk_summaries.get(chunk.chunk_id)
        section_summary = (
            section_summaries.get(chunk.section_id)
            if chunk.section_id else None
        )
        # NB: overhead включает "\nSOURCE TEXT:\n" (15 chars), потому что
        # при вычислении был передан include_source_text=False.
        # Здесь добавляем source_text[:per_chunk_source_budget] —
        # итоговый блок = overhead_with_source_template + source_text.
        truncated_source_part = chunk.text[:per_chunk_source_budget]
        if truncated_source_part:
            truncated_blocks.append(
                f"[CHUNK {chunk.chunk_id} | Section: "
                f"{chunk.section_heading or chunk.section_path or '(root)'}]"
                + _summaries_block_text(
                    chunk,
                    section_summary=section_summary,
                    chunk_summary=chunk_summary,
                )
                + "\nSOURCE TEXT:\n"
                + truncated_source_part
            )
        else:
            truncated_blocks.append(
                f"[CHUNK {chunk.chunk_id} | Section: "
                f"{chunk.section_heading or chunk.section_path or '(root)'}]"
                + _summaries_block_text(
                    chunk,
                    section_summary=section_summary,
                    chunk_summary=chunk_summary,
                )
            )

    return "".join(truncated_blocks)


__all__ = ["build_question_context"]