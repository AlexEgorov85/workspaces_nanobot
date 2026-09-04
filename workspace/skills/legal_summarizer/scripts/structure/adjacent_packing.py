"""Controlled adjacent-section packing (PLAN §22, Этап 22).

Сейчас ``packing_impl.pack_chunks`` строго section-locality greedy —
что безопасно, но для 600-страничного документа даёт
``map_calls == chunks_total`` (см. baseline F3).

Целевая политика (PLAN §22):

1. Same section — preferred (как раньше).
2. Adjacent sections — allowed (до 2 секций на batch).
3. max 2 semantic sections per batch.
4. Сохранять order внутри batch.
5. Не смешивать unrelated distant sections.
6. Никогда не уничтожать section provenance (каждый chunk всё ещё
   несёт свой ``section_id``).

Это **не** меняет `` Chunk``, только решает, какие chunk_ids идут
в один batch при ``build_map_plan``.
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator, TokenEstimatorConfig,
)


@dataclass(frozen=True)
class AdjacentPackingConfig:
    """Параметры adjacent-section packing."""

    max_sections_per_batch: int = 2
    per_batch_token_budget: int = 6000
    chars_per_token: float = 3.5


def _section_id_for_chunk(chunk: Chunk) -> str:
    return chunk.section_id or ""


def pack_chunks_with_adjacent(
    chunks: tuple[Chunk, ...],
    *,
    config: AdjacentPackingConfig | None = None,
) -> list[tuple[str, ...]]:
    """Сгруппировать ``Chunk`` в batches с controlled adjacent sections.

    Возвращает список tuple chunk_ids — порядок execution.

    Алгоритм:

    * Идём по chunks в document order.
    * Начинаем новый batch, если:
            - max_sections_per_batch превышен;
            - budget превышен;
            - встретился ``root`` (``section_id="s_root"``);
            - встретился ``table`` chunk (не смешивается с non-table).
    * Adjacent sections разрешены (section_id может меняться).
    """
    cfg = config or AdjacentPackingConfig()
    estimator = TokenEstimator(TokenEstimatorConfig(chars_per_token=cfg.chars_per_token))

    batches: list[tuple[str, ...]] = []
    current_chunk_ids: list[str] = []
    current_section_ids: set[str] = set()
    current_tokens = 0

    for chunk in chunks:
        sec_id = _section_id_for_chunk(chunk)

        if sec_id == "s_root":
            if current_chunk_ids:
                batches.append(tuple(current_chunk_ids))
                current_chunk_ids = []
                current_section_ids = set()
                current_tokens = 0
            current_chunk_ids.append(chunk.chunk_id)
            batches.append(tuple(current_chunk_ids))
            current_chunk_ids = []
            current_section_ids = set()
            current_tokens = 0
            continue

        if chunk.table_id is not None and current_chunk_ids:
            if any(not c.table_id for c in [chunk]):
                batches.append(tuple(current_chunk_ids))
                current_chunk_ids = []
                current_section_ids = set()
                current_tokens = 0

        is_new_section = sec_id not in current_section_ids
        would_exceed_sections = (
            is_new_section and len(current_section_ids) >= cfg.max_sections_per_batch
        )
        chunk_tokens = estimator.estimate(chunk.text)
        would_exceed_budget = (
            current_tokens + chunk_tokens > cfg.per_batch_token_budget
        )

        if (would_exceed_sections or would_exceed_budget) and current_chunk_ids:
            batches.append(tuple(current_chunk_ids))
            current_chunk_ids = []
            current_section_ids = set()
            current_tokens = 0

        current_chunk_ids.append(chunk.chunk_id)
        if sec_id:
            current_section_ids.add(sec_id)
        current_tokens += chunk_tokens

    if current_chunk_ids:
        batches.append(tuple(current_chunk_ids))

    return batches


__all__ = [
    "AdjacentPackingConfig",
    "pack_chunks_with_adjacent",
]