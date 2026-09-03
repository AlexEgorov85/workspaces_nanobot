"""Cache-assisted follow-up question retrieval.

Главная точка интеграции с :func:`workspace.skills.legal_summarizer.scripts.summarizer.run`
для follow-up вопросов. Возвращает готовые ``Chunk``-ы с
**точным исходным текстом из ``PhysicalDocument``** (через
``reconstruct_source_fragment`` + ``expand_followup_context``), либо
``None`` — caller использует existing lexical/relaxed fallback.

Стратегия:

    question
       │
       ▼
    select_cached_candidates
       │
       ├── confident + fresh
       │       │
       │       ▼
       │   PhysicalDocument (canonical source)
       │       │
       │       ▼
       │   reconstruct_candidate_source
       │       │
       │       ▼
       │   expand_followup_context  (target provenance сохраняется!)
       │       │
       │       ▼
       │   list[Chunk] с точным source text + target_* поля
       │
       └── нет / слабо / stale
               │
               ▼
           None → caller existing fallback

Invariants:
    * Возвращаемые ``Chunk``-и **сохраняют provenance target** в полях
      ``target_block_indices``, ``target_source_char_start/end``,
      ``source_spans``. Это критично для claim-level citations — без
      этого нельзя отличить exact target от соседей, добавленных для
      LLM-контекста.
    * Не создаём новый LLM/reducer/execution pipeline. Возвращаемые
      ``Chunk``-и подставляются в существующий pipeline.

P0 corrections:
    * ``target_block_index`` → ``target_ordinal`` (ordinal = identity,
      индекс массива = position; их не смешиваем).
    * Target provenance сохраняется при expansion.
"""

from __future__ import annotations

from typing import Any

from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
    is_confident as _is_confident,
    select_cached_candidates,
)
from workspace.skills.legal_summarizer.scripts.context_expansion import (
    ExpandedContext,
    expand_followup_context,
)
from workspace.skills.legal_summarizer.scripts.document_cache import (
    cache_is_fresh,
    load_doc_cache,
)
from workspace.skills.legal_summarizer.scripts.provenance_reconstruction import (
    reconstruct_candidate_source,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    PhysicalDocument,
)


def _build_chunk_from_reconstructed(
    *,
    chunk_id: str,
    source_text: str,
    target_block_ordinal: int,
    target_source_char_start: int | None,
    target_source_char_end: int | None,
    target_block_type: str,
    doc: PhysicalDocument,
    expanded: ExpandedContext,
) -> Chunk | None:
    """Собрать Chunk для downstream pipeline.

    Сохраняет:
        * expanded ``text`` (target + соседи) для LLM-input;
        * ``block_indices`` / ``source_char_*`` для expanded text
          (как и раньше, для back-compat с downstream);
        * ``target_block_indices`` / ``target_source_char_start/end`` —
          provenance **primary target**;
        * ``source_spans`` — список spans с ``is_target`` маркером.
    """
    if not expanded.blocks:
        return None

    text_parts: list[str] = []
    block_indices: list[int] = []
    page_indices: list[int] = []
    for idx, txt in expanded.blocks:
        text_parts.append(txt)
        block_indices.append(idx)
        b = doc.blocks_by_ord.get(idx)
        if b is not None and b.page_index is not None:
            page_indices.append(b.page_index)

    joined = "\n\n".join(text_parts)

    source_spans: list[tuple[int, int, int | None, int | None]] = [
        (ord_, cs, ce, marker)
        for (ord_, cs, ce, _scs, _sce, marker) in expanded.source_spans
    ]

    return Chunk(
        chunk_id=chunk_id,
        index=-1,
        text=joined,
        char_count=len(joined),
        token_estimate=max(1, len(joined) // 4),
        page_start=min(page_indices) if page_indices else None,
        page_end=max(page_indices) if page_indices else None,
        section_id="",
        section_path="",
        section_heading="",
        block_indices=tuple(block_indices),
        block_types=tuple(["paragraph"] * len(block_indices)) if target_block_type != "table" else tuple(["table"] * len(block_indices)),
        source_char_start=target_source_char_start,
        source_char_end=target_source_char_end,
        target_block_indices=(target_block_ordinal,),
        target_source_char_start=target_source_char_start,
        target_source_char_end=target_source_char_end,
        source_spans=tuple(source_spans),
    )


def retrieve_followup_context_via_cache(
    *,
    question: str,
    document_id: str,
    session_key: str | None,
    document_path: str | Any | None,
    workspace_root: Any,
    doc: PhysicalDocument,
    max_candidates: int = 3,
    min_top_score: int = 3,
    neighbor_count: int = 1,
    max_total_chars: int = 8000,
) -> list[Chunk] | None:
    """Полная цепочка cache-assisted follow-up retrieval.

    Возвращает ``list[Chunk]`` для подстановки в существующий question-path
    pipeline или ``None`` при weak/no match / stale / ошибке.

    Returns:
        ``list[Chunk]`` (готовых для LLM, **с сохранённой target provenance**)
        или ``None``.
    """
    if not session_key:
        return None
    cache = load_doc_cache(document_id, session_key, workspace_root)
    if not cache:
        return None
    if not cache_is_fresh(document_id, session_key, workspace_root, document_path):
        return None

    candidates = select_cached_candidates(
        question, cache,
        max_candidates=max_candidates,
        min_score=2, min_top_score=min_top_score,
    )
    if not candidates:
        return None
    if not _is_confident(candidates, min_score=min_top_score):
        return None

    chunks: list[Chunk] = []
    blocks_by_ord = {b.ordinal: b for b in doc.blocks}
    for cand in candidates:
        if not cand.block_indices:
            continue
        target_ordinal = cand.block_indices[0]
        if target_ordinal not in blocks_by_ord:
            continue

        source_text, is_stale = reconstruct_candidate_source(
            cand, doc=doc, is_fresh=True,
        )
        if source_text is None or is_stale:
            continue

        expanded = expand_followup_context(
            target_ordinal=target_ordinal,
            doc=doc,
            target_source_text=source_text,
            target_source_char_start=cand.source_char_start,
            target_source_char_end=cand.source_char_end,
            neighbor_count=neighbor_count,
            max_total_chars=max_total_chars,
        )
        chunk = _build_chunk_from_reconstructed(
            chunk_id=cand.chunk_id,
            source_text=source_text,
            target_block_ordinal=target_ordinal,
            target_source_char_start=cand.source_char_start,
            target_source_char_end=cand.source_char_end,
            target_block_type=blocks_by_ord[target_ordinal].block_type,
            doc=doc,
            expanded=expanded,
        )
        if chunk is not None:
            chunks.append(chunk)
    return chunks if chunks else None


__all__ = ["retrieve_followup_context_via_cache"]
