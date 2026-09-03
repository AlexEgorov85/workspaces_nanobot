"""Provenance-aware reconstruction helpers.

Этот модуль соединяет :mod:`cached_retrieval` и
:func:`workspace.skills.legal_summarizer.scripts.structure.chunks.reconstruct_source_fragment`
— превращает ``CachedCandidate`` в **точный** исходный текст
через ``PhysicalDocument``. Гарантии:

* ``PhysicalDocument`` — единственный canonical source of truth.
* При stale cache → возвращаем ``None`` / ``is_stale=True``.
* При отсутствии cache / unknown provenance → ``None``.

API:
    * :func:`reconstruct_candidate_source` — один candidate → str (is_stale flag).
    * :func:`reconstruct_candidates_sources` — список → list[str / None].
"""

from __future__ import annotations

from typing import Any

from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
    CachedCandidate,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import (
    Chunk,
    reconstruct_source_fragment,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    PhysicalDocument,
)


def _candidate_to_chunk(candidate: CachedCandidate) -> Chunk:
    """Сконвертировать CachedCandidate в Chunk для реконструкции.

    Вспомогательная функция — объединяет block_indices / source_char_* /
    table_* / page_* в Chunk dataclass. Используется только внутри
    этого модуля.
    """
    return Chunk(
        chunk_id=candidate.chunk_id,
        index=-1,
        text=candidate.chunk_text_preview,
        char_count=len(candidate.chunk_text_preview),
        token_estimate=max(1, len(candidate.chunk_text_preview) // 4),
        page_start=candidate.page_start,
        page_end=candidate.page_end,
        section_id=candidate.section_id or "",
        section_path=candidate.section_path or "",
        section_heading="",
        block_indices=candidate.block_indices,
        block_types=candidate.block_types,
        table_id=candidate.table_id,
        table_row_start=candidate.table_row_start,
        table_row_end=candidate.table_row_end,
        source_char_start=candidate.source_char_start,
        source_char_end=candidate.source_char_end,
    )


def reconstruct_candidate_source(
    candidate: CachedCandidate,
    *,
    doc: PhysicalDocument,
    is_fresh: bool,
) -> tuple[str | None, bool]:
    """Восстановить точный исходный текст кандидата.

    Args:
        candidate: CachedCandidate из :mod:`cached_retrieval`.
        doc: PhysicalDocument (canonical source).
        is_fresh: результат :func:`workspace.skills.legal_summarizer.scripts.document_cache.cache_is_fresh`
            для текущего ``document_path``.

    Returns:
        ``(text, is_stale)``. ``text is None`` при stale или при невозможности
        восстановить. ``is_stale=True`` индикатор того, что cache freshness нарушена.

    Notes:
        При выбросе ValueError от :func:`reconstruct_source_fragment`
        возвращаем ``(None, False)`` — считаем реконструкцию неудачной,
        но НЕ stale.
    """
    if not is_fresh:
        return None, True

    chunk = _candidate_to_chunk(candidate)
    if not chunk.block_indices:
        return None, False
    try:
        return reconstruct_source_fragment(chunk, doc=doc), False
    except ValueError:
        return None, False


def reconstruct_candidates_sources(
    candidates: list[CachedCandidate],
    *,
    doc: PhysicalDocument,
    is_fresh: bool,
) -> list[dict[str, Any]]:
    """Восстановить текст для списка кандидатов.

    Returns:
        Список словарей ``{candidate, source, is_stale}``. ``source`` —
        точный исходный текст (или None при stale/failure).
    """
    return [
        {
            "candidate": c,
            "source": None if stale else (reconstruct_candidate_source(c, doc=doc, is_fresh=is_fresh)[0]),
            "is_stale": stale,
        }
        for c, stale in (
            (c, not is_fresh) for c in candidates
        )
    ]


__all__ = [
    "reconstruct_candidate_source",
    "reconstruct_candidates_sources",
]
