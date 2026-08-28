"""Context Packing для legal_summarizer (Phase 2B).

Section-locality greedy first-fit: последовательно добавляем chunk в
текущий ContextBatch, **никогда** не перепрыгивая через section boundary.

Алгоритм:
  1. Идём по chunks в document order.
  2. Открываем новый batch.
  3. Пытаемся добавить следующий chunk, если:
     а) его section_path совпадает с первым chunk'ом текущего batch;
     б) он влезает в available_chunk_tokens budget.
  4. Если оба условия — добавляем.
  5. Если не влезает — закрываем batch, открываем новый.
  6. Если section_path отличается — закрываем batch, открываем новый.

Семантическая локальность > utilization (invariant #8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


@dataclass(frozen=True)
class TokenBudget:
    """Расчёт available budget для chunk contents."""

    context_window_tokens: int
    system_prompt_tokens: int
    instruction_tokens: int
    output_reserve_tokens: int
    safety_margin: float
    chars_per_token: float

    @property
    def available_chunk_tokens(self) -> int:
        used = (
            self.system_prompt_tokens
            + self.instruction_tokens
            + self.output_reserve_tokens
        )
        raw = (self.context_window_tokens - used) * self.safety_margin
        return max(int(raw), 1000)


@dataclass(frozen=True)
class ContextBatch:
    """Несколько chunks в одном LLM call."""

    batch_id: str
    chunks: tuple[Chunk, ...]
    total_tokens_estimate: int
    section_paths: tuple[str, ...]
    page_range: tuple[int | None, int | None]

    @property
    def utilization(self) -> float:
        budget = self.total_tokens_estimate if self.total_tokens_estimate else 1
        return min(1.0, self.total_tokens_estimate / max(budget, 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "chunk_ids": [c.chunk_id for c in self.chunks],
            "total_tokens_estimate": self.total_tokens_estimate,
            "section_paths": list(self.section_paths),
            "page_range": list(self.page_range),
        }


_BATCH_OVERHEAD_TOKENS = 80


def pack_chunks(
    chunks: list[Chunk],
    budget: TokenBudget,
) -> list[ContextBatch]:
    """Greedy section-locality packing.

    Args:
        chunks: список Chunk в document order (после structure-aware chunker'а).
        budget: TokenBudget.

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
        batches.append(
            ContextBatch(
                batch_id=batch_id,
                chunks=tuple(cur_chunks),
                total_tokens_estimate=cur_tokens,
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


__all__ = [
    "TokenBudget",
    "ContextBatch",
    "pack_chunks",
]