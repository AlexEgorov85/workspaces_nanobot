"""Packing models — выделено из ``packing.py`` в этапе 22.

Только dataclasses. Никакой логики.

Используется через:
* ``packing.py`` (facade с re-exports)
* ``packing_impl.py`` (packer)
* ``summarizer.py``, ``pipeline.py``, ``llm_calls.py``, ``prompts.py``
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


@dataclass(frozen=True)
class ContextBatch:
    """Несколько chunks в одном LLM call.

    Attributes:
        batch_id: стабильный идентификатор (``"cb_000"``).
        chunks: tuple Chunk в document order.
        total_tokens_estimate: оценка общего числа токенов в batch
            (включая ``_BATCH_OVERHEAD_TOKENS`` overhead).
        content_tokens_estimate: оценка **только контента** (без overhead).
        available_chunk_tokens: доступный бюджет для одного chunk
            (``TokenBudget.available_chunk_tokens``). Используется для
            корректного расчёта ``utilization``.
        section_paths: tuple уникальных section_path в batch.
        page_range: (min_page, max_page) или (None, None) если нет page info.
    """

    batch_id: str
    chunks: tuple[Chunk, ...]
    total_tokens_estimate: int
    section_paths: tuple[str, ...]
    page_range: tuple[int | None, int | None]
    content_tokens_estimate: int = 0
    available_chunk_tokens: int = 0

    @property
    def utilization(self) -> float:
        """Реальная utilization = ``content / available``.

        Формула:
            - Если ``available_chunk_tokens`` задан → ``content / available``,
              clamped к ``[0.0, 1.0]``.
            - Иначе → 0.0 (нет данных для расчёта).
        """
        if self.available_chunk_tokens <= 0:
            return 0.0
        return min(1.0, self.content_tokens_estimate / self.available_chunk_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "chunk_ids": [c.chunk_id for c in self.chunks],
            "total_tokens_estimate": self.total_tokens_estimate,
            "content_tokens_estimate": self.content_tokens_estimate,
            "available_chunk_tokens": self.available_chunk_tokens,
            "utilization": round(self.utilization, 4),
            "section_paths": list(self.section_paths),
            "page_range": list(self.page_range),
        }


@dataclass(frozen=True)
class PackingConfig:
    """Параметры packing'а."""

    allow_adjacent_sections: bool = False
    """Если True — разрешаем mixing chunks из adjacent sections в одном batch
    (когда есть бюджет). Если False — strict section-locality (default,
    для обратной совместимости с тестами)."""

    min_remaining_for_mix: float = 0.5
    """Минимальная доля свободного budget (от available), при которой
    разрешено «заимствовать» chunks из соседней секции. Используется
    только при ``allow_adjacent_sections=True``."""


__all__ = ["ContextBatch", "PackingConfig"]