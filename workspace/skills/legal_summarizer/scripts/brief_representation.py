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
"""

from __future__ import annotations

from dataclasses import replace

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


def apply_brief_text_budget(
    chunks: list[Chunk],
    *,
    truncate_chars: int | None,
) -> list[Chunk]:
    """Вернуть список chunks с обрезанным ``text`` для LLM-input в brief.

    Args:
        chunks: выбранные chunks (после ``select_brief_chunks_structured``).
        truncate_chars: макс. символов на block. ``None`` или ``<= 0``
            → legacy-поведение (вернуть chunks как есть, без копий).

    Returns:
        Список новых Chunk'ов с обрезанным ``text``. Если chunk покрывает
        несколько ``block_indices`` — обрезается конкатенация
        (``\\n\\n`` уже есть в ``text``). Сейчас: обрезаем по chars на
        уровне ``text``.

    Notes:
        * ``source_char_start`` / ``source_char_end`` остаются от оригинала
          (provenance). Обрезанный ``text`` — это лишь представление
          для LLM; реконструкция через ``reconstruct_source_fragment``
          работает по полным offsets.
        * Если ``len(text) <= truncate_chars`` → chunk возвращается
          без изменений (immutability сохраняется — dataclasses.replace
          создаёт новый объект только если нужно).
    """
    if truncate_chars is None or truncate_chars <= 0:
        return list(chunks)
    if not chunks:
        return []
    out: list[Chunk] = []
    for c in chunks:
        # Tables не обрезаем (invariant §6: tables атомарны).
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


__all__ = ["apply_brief_text_budget"]
