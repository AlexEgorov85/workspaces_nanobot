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
       │   expand_followup_context
       │       │
       │       ▼
       │   list[Chunk] с точным source text
       │
       └── нет / слабо / stale
               │
               ▼
           None → caller existing fallback

Важно: не создаём новый LLM/reducer/execution pipeline.
Возвращаемые ``Chunk``-и подставляются в существующий pipeline.
"""

from __future__ import annotations

from typing import Any

from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
    is_confident as _is_confident,
    select_cached_candidates,
)
from workspace.skills.legal_summarizer.scripts.context_expansion import (
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
    candidate_source_text: str,
    candidate_block_index: int,
    doc: PhysicalDocument,
    expanded_blocks: list[tuple[int, str]],
) -> Chunk | None:
    """Собрать Chunk для downstream pipeline (с provenance + expanded context)."""
    if not expanded_blocks:
        return None
    text_parts: list[str] = []
    block_indices: list[int] = []
    block_types: list[str] = []
    page_indices: list[int] = []
    for idx, txt in expanded_blocks:
        text_parts.append(txt)
        block_indices.append(idx)
        block_types.append("paragraph")
        b = doc.blocks[idx] if 0 <= idx < len(doc.blocks) else None
        if b is not None and b.page_index is not None:
            page_indices.append(b.page_index)

    joined = "\n\n".join(text_parts)
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
        block_types=tuple(block_types),
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

    Args:
        question: пользовательский follow-up вопрос.
        document_id, session_key, document_path, workspace_root: для
            :func:`load_doc_cache` и :func:`cache_is_fresh`.
        doc: ``PhysicalDocument`` (canonical source).
        max_candidates: сколько cache-записей обрабатывать.
        min_top_score: min_top_score для ``select_cached_candidates``.
        neighbor_count: для ``expand_followup_context``.
        max_total_chars: для ``expand_followup_context``.

    Returns:
        ``list[Chunk]`` (готовых для LLM) или ``None``.
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
    for cand in candidates:
        if not cand.block_indices:
            continue
        target_block_idx = cand.block_indices[0]
        source_text, is_stale = reconstruct_candidate_source(
            cand, doc=doc, is_fresh=True,
        )
        if source_text is None or is_stale:
            continue

        expanded = expand_followup_context(
            target_block_index=target_block_idx,
            doc=doc,
            target_source_text=source_text,
            neighbor_count=neighbor_count,
            max_total_chars=max_total_chars,
        )
        chunk = _build_chunk_from_reconstructed(
            chunk_id=cand.chunk_id,
            source_text=source_text,
            candidate_source_text=source_text,
            candidate_block_index=target_block_idx,
            doc=doc,
            expanded_blocks=expanded["blocks"],
        )
        if chunk is not None:
            chunks.append(chunk)
    return chunks if chunks else None


__all__ = ["retrieve_followup_context_via_cache"]
