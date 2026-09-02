"""Packing implementation — выделено из ``packing.py`` в этапе 22.

Содержит:

* ``_BATCH_OVERHEAD_TOKENS`` — внутренняя константа.
* ``_build_batches_strict`` — greedy strict section-locality packing.
* ``pack_chunks`` — public entry point.

Модели — в ``packing_models.py``.
"""
from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.packing_models import (
    ContextBatch,
    PackingConfig,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.token_budget import TokenBudget


_BATCH_OVERHEAD_TOKENS = 80


def _build_batches_strict(
    chunks: list[Chunk],
    available: int,
) -> list[ContextBatch]:
    """Greedy strict section-locality packing (legacy поведение)."""
    batches: list[ContextBatch] = []
    cur_chunks: list[Chunk] = []
    cur_tokens = _BATCH_OVERHEAD_TOKENS
    cur_section_id: str | None = None
    cur_section_path: str | None = None
    cur_pages: list[int | None] = []

    def _flush() -> None:
        nonlocal cur_chunks, cur_tokens, cur_section_id, cur_section_path, cur_pages
        if not cur_chunks:
            return
        page_start = next((p for p in cur_pages if p is not None), None)
        page_end = next((p for p in reversed(cur_pages) if p is not None), None)
        batch_id = f"cb_{len(batches):03d}"
        content_tokens = cur_tokens - _BATCH_OVERHEAD_TOKENS
        batches.append(
            ContextBatch(
                batch_id=batch_id,
                chunks=tuple(cur_chunks),
                total_tokens_estimate=cur_tokens,
                content_tokens_estimate=max(content_tokens, 0),
                available_chunk_tokens=available,
                section_paths=tuple({c.section_path for c in cur_chunks}),
                page_range=(page_start, page_end),
            )
        )
        cur_chunks = []
        cur_tokens = _BATCH_OVERHEAD_TOKENS
        cur_section_id = None
        cur_section_path = None
        cur_pages = []

    for chunk in chunks:
        same_section = (
            cur_section_id is None
            or chunk.section_id == cur_section_id
        )
        fits = cur_tokens + chunk.token_estimate <= available

        if cur_chunks and (not same_section or not fits):
            _flush()

        cur_chunks.append(chunk)
        cur_tokens += chunk.token_estimate
        cur_section_id = chunk.section_id
        cur_section_path = chunk.section_path
        cur_pages.append(chunk.page_start)
        cur_pages.append(chunk.page_end)

    _flush()

    return batches


def pack_chunks(
    chunks: list[Chunk],
    budget: TokenBudget,
    config: PackingConfig | None = None,
) -> list[ContextBatch]:
    """Greedy section-locality packing (optional adjacent mixing).

    Args:
        chunks: список Chunk в document order (после structure-aware chunker'а).
        budget: TokenBudget.
        config: ``PackingConfig``. Если None — default (strict section-locality).

    Returns:
        list[ContextBatch]. Может быть пустой, если chunks пустой.

    Raises:
        ValueError: если chunk больше budget.available_chunk_tokens.
    """
    if not chunks:
        return []

    available = budget.available_chunk_tokens

    for c in chunks:
        if c.token_estimate > available:
            raise ValueError(
                f"Chunk {c.chunk_id} (tokens≈{c.token_estimate}) больше budget "
                f"available_chunk_tokens={available}. Re-chunk required."
            )

    cfg = config or PackingConfig()

    if not cfg.allow_adjacent_sections:
        # Default: strict section-locality.
        return _build_batches_strict(chunks, available)

    # Locality-aware pass.
    # Шаг 1: greedy strict pass → baseline batches.
    strict_batches = _build_batches_strict(chunks, available)

    if len(strict_batches) < 2:
        return strict_batches

    # Шаг 2: для каждого batch пытаемся «заимствовать» chunks из **следующего**
    # batch'а (adjacent section), если есть бюджет.
    # Если в next_b остались незаимствованные chunks — они идут в result
    # как самостоятельный batch (не теряются).
    result: list[ContextBatch] = []
    skip_next = False
    for i, b in enumerate(strict_batches):
        if skip_next:
            skip_next = False
            continue

        current_chunks = list(b.chunks)
        current_tokens = b.total_tokens_estimate
        current_pages = [p for c in current_chunks for p in (c.page_start, c.page_end)]

        next_idx = i + 1
        borrowed_count = 0
        if next_idx < len(strict_batches):
            next_b = strict_batches[next_idx]
            next_section_path = next_b.section_paths[0] if next_b.section_paths else None
            current_section_path = b.section_paths[0] if b.section_paths else None

            is_adjacent = (
                current_section_path is not None
                and next_section_path is not None
                and current_section_path != next_section_path
            )

            if is_adjacent:
                remaining = available - current_tokens
                min_remaining = int(available * cfg.min_remaining_for_mix)
                for nc in next_b.chunks:
                    if nc.token_estimate <= remaining and remaining >= min_remaining:
                        current_chunks.append(nc)
                        current_tokens += nc.token_estimate
                        current_pages.append(nc.page_start)
                        current_pages.append(nc.page_end)
                        remaining -= nc.token_estimate
                        borrowed_count += 1
                    else:
                        break

        page_start = next((p for p in current_pages if p is not None), None)
        page_end = next((p for p in reversed(current_pages) if p is not None), None)
        result.append(
            ContextBatch(
                batch_id=b.batch_id,
                chunks=tuple(current_chunks),
                total_tokens_estimate=current_tokens,
                content_tokens_estimate=current_tokens - _BATCH_OVERHEAD_TOKENS,
                available_chunk_tokens=available,
                section_paths=tuple({c.section_path for c in current_chunks}),
                page_range=(page_start, page_end),
            )
        )

        # Если заимствовали что-то из next_b — следующий batch в strict_batches
        # уже не нужен (мы его обработали). Если остались leftover chunks
        # (borrowed < len), они попадают в отдельный batch.
        if borrowed_count > 0:
            if borrowed_count < len(strict_batches[next_idx].chunks):
                leftover_chunks = strict_batches[next_idx].chunks[borrowed_count:]
                leftover_tokens = (
                    _BATCH_OVERHEAD_TOKENS
                    + sum(c.token_estimate for c in leftover_chunks)
                )
                leftover_pages = [p for c in leftover_chunks for p in (c.page_start, c.page_end)]
                page_start = next((p for p in leftover_pages if p is not None), None)
                page_end = next((p for p in reversed(leftover_pages) if p is not None), None)
                result.append(
                    ContextBatch(
                        batch_id=strict_batches[next_idx].batch_id,
                        chunks=tuple(leftover_chunks),
                        total_tokens_estimate=leftover_tokens,
                        content_tokens_estimate=leftover_tokens - _BATCH_OVERHEAD_TOKENS,
                        available_chunk_tokens=available,
                        section_paths=tuple({c.section_path for c in leftover_chunks}),
                        page_range=(page_start, page_end),
                    )
                )
            # В обоих случаях (полное или частичное заимствование) —
            # следующий strict_batch уже обработан.
            skip_next = True

    return result


__all__ = ["pack_chunks"]