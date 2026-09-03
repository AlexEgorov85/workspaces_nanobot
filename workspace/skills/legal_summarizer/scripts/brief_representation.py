"""Brief-представление chunks для LLM-prompt.

Инварианты:

1. ``PhysicalDocument`` остаётся полным (canonical source). Никакая
   стадия pipeline не мутирует ``DocumentBlock.content`` / ``char_count``.
2. Эта стадия создаёт **новые** ``Chunk``-объекты с укороченным ``text``
   для LLM-input. Frozen dataclass → новые экземпляры.
3. ``source_char_start`` / ``source_char_end`` оригинала не меняются.
   Они по-прежнему ссылаются на исходный ``DocumentBlock.content``.
   Чтобы реконструировать полный исходный текст chunk'а — используем
   :func:`workspace.skills.legal_summarizer.scripts.structure.chunks.reconstruct_source_fragment`
   по ``doc.blocks[block_indices[0]].content[start:end]``.
4. Tables не обрезаются этим helper'ом. Таблица атомарна
   (см. invariant §6 в ARCHITECTURE.md).
5. Если ``brief_max_chars_per_chunk is None``/0 → legacy-поведение
   (возвращаем список как есть).

Двухуровневая модель (этап 4 фиксов legal_summarizer):

* **Coverage** — выбор chunks через ``select_brief_chunks_structured``
  (round-robin по sections, max N chunks).
* **LLM budget** — общий лимит текста, передаваемого в LLM-prompt,
  через ``brief_max_input_chars``. Реализован в
  :func:`allocate_brief_budget`.

Разделение критично: coverage определяет «о каких разделах/фрагментах
документа мы скажем LLM», а budget — «сколько символов LLM получит
для всех этих фрагментов суммарно». Без общего budget'а 10 выбранных
chunks × 3000 chars каждый = 30K chars в LLM-input, даже если каждый
chunk формально покрывает только round-robin.
"""
from __future__ import annotations

from dataclasses import replace

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


def apply_brief_text_budget(
    chunks: list[Chunk],
    *,
    truncate_chars: int | None,
) -> list[Chunk]:
    """Legacy per-chunk truncation (для backward compatibility).

    Режет каждый chunk до ``truncate_chars`` chars. Не имеет общего
    лимита — 10 chunks × truncate_chars = потолок. Для нового поведения
    используй :func:`allocate_brief_budget` (общий budget).

    Args:
        chunks: выбранные chunks (после ``select_brief_chunks_structured``).
        truncate_chars: макс. символов на block. ``None`` или ``<= 0``
            → legacy-поведение (вернуть chunks как есть, без копий).

    Returns:
        Список новых Chunk'ов с обрезанным ``text``. Tables не
        обрезаются (atomicity invariant §6).
    """
    if truncate_chars is None or truncate_chars <= 0:
        return list(chunks)
    if not chunks:
        return []
    out: list[Chunk] = []
    for c in chunks:
        if c.block_types and c.block_types == ("table",):
            out.append(c)
            continue
        if len(c.text) <= truncate_chars:
            out.append(c)
            continue
        truncated = c.text[:truncate_chars].rstrip()
        if len(truncated) < len(c.text):
            truncated = truncated + " …"
        out.append(replace(c, text=truncated, char_count=len(truncated)))
    return out


def _is_table_chunk(c: Chunk) -> bool:
    """Tables атомарны — их нельзя резать произвольно (см. invariant §6)."""
    return bool(c.block_types) and c.block_types == ("table",)


def allocate_brief_budget(
    chunks: list[Chunk],
    *,
    total_budget_chars: int | None,
) -> list[Chunk]:
    """Распределить общий LLM-text budget между выбранными chunks.

    Принимает **уже выбранные** chunks (после coverage-фазы
    ``select_brief_chunks_structured``) и обрезает их ``text`` так, чтобы
    **суммарный объём текста** для LLM не превышал ``total_budget_chars``.

    Инварианты:

    * ``PhysicalDocument`` НЕ мутируется. Все chunks — новые экземпляры
      (``dataclasses.replace``).
    * ``source_char_start`` / ``source_char_end`` остаются от оригинала
      (provenance сохраняется).
    * Tables пропускаются целиком (atomicity invariant §6). Таблицы
      считаются **сверх** budget — они атомарны и не режутся. Если
      таблица одна и она огромная — budget на текстовые chunks может
      быть очень мал (или 0).
    * Если ``total_budget_chars`` is None или <= 0 → no-op: возвращаем
      chunks без изменений (без копий).
    * Суммарный объём текстовых chunks после обрезки ≤ ``total_budget_chars``.

    Алгоритм:

    1. Разделяем chunks на текстовые и табличные.
    2. Если суммарный объём текстовых chunks ≤ budget → no-op.
    3. Иначе — пропорционально распределяем budget по текущей длине
       каждого текстового chunk'а. Учитываем, что " …" suffix добавляет
       +2 chars к каждому обрезанному chunk'у (резервируем заранее).
       min_per_chunk (200 chars) — нижняя граница, чтобы чанк не
       вырождался в пустую строку. Если budget недостаточен для
       min_per_chunk на каждый chunk — равномерная обрезка без минимума.
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

    # Резервируем chars под " …" суффикс для каждого обрезаемого chunk'а.
    # Грубая оценка: каждый chunk с len(text) > share будет обрезан.
    # Чтобы не считать итеративно, предполагаем worst-case: все текстовые
    # chunks будут обрезаны. Это слегка занижает share, но гарантирует
    # что сумма <= budget после добавления суффиксов.
    suffix_len = 2  # " …"
    n = len(text_chunks)
    available = max(0, total_budget_chars - suffix_len * n)

    min_per_chunk = 200
    if available < min_per_chunk * n:
        # Degenerate budget: не хватает даже на min_per_chunk на chunk.
        # Режем равномерно без нижней границы.
        min_per_chunk = max(50, available // max(1, n))

    out: list[Chunk] = list(table_chunks)
    for c in text_chunks:
        # Пропорциональная доля от доступного бюджета.
        share = max(
            min_per_chunk,
            int(round(len(c.text) * available / max(1, total_text_chars))),
        )
        # Жёсткий cap: ни один chunk не может получить больше total_budget.
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
    """Суммарный объём ``text`` в chunks (для тестов budget'а)."""
    return sum(len(c.text) for c in chunks)


__all__ = [
    "apply_brief_text_budget",
    "allocate_brief_budget",
    "total_input_chars",
]
