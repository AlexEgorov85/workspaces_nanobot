"""ExecutionPlan (PLAN §21, Этап 21).

Immutable план выполнения, который объединяет результаты chunks,
batches и token estimation. Используется всеми downstream'ами:

* **inspect** — оценить работу (без LLM).
* **run** — выполнить все батчи.
* **manifest** — сохранить план на диск (для resume).
* **recovery** — восстановить из manifest без повторного packing.

Back-compat: legacy ``pack_chunks`` продолжает работать (используется
тестами). ``ExecutionPlan`` — новый канонический API, который
планируется использовать в Этапе 45 (DocumentStructure как SoT для всех).

Детерминированный (PLAN §61, §75): один документ + одна стратегия →
один план (включаяя chunk IDs и batch composition).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


@dataclass(frozen=True)
class PlannedBatch:
    """Один batch в ``ExecutionPlan``.

    Attributes:
        batch_id: стабильный идентификатор (``"cb_000"``).
        chunk_ids: tuple of ``Chunk.chunk_id`` (порядок — document order).
        token_estimate: оценка токенов через ``TokenEstimator``.
        section_ids: tuple of section_id (parent chunks).
        is_question_batch: ``True`` если это batch для question mode
            (может быть partial по retrieval).
    """

    batch_id: str
    chunk_ids: tuple[str, ...]
    token_estimate: int
    section_ids: tuple[str, ...] = ()
    is_question_batch: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "chunk_ids": list(self.chunk_ids),
            "token_estimate": self.token_estimate,
            "section_ids": list(self.section_ids),
            "is_question_batch": self.is_question_batch,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    """Immutable план выполнения документа.

    Attributes:
        document_id: ``DocumentIdentity.document_id``.
        strategy: ``"direct"`` | ``"map_flat"`` | ``"map_hierarchical"``.
        chunks: tuple of ``Chunk`` (в document order).
        batches: tuple of ``PlannedBatch``.
        total_chunks: ``len(chunks)``.
        total_batches: ``len(batches)``.
        total_input_tokens: суммарная оценка токенов.
        estimated_llm_calls: сколько LLM-вызовов потребуется.
        estimated_total_sec: оценка времени (через heuristic, не runtime).
        metadata: произвольная мета (например, ``"question"`` mode).
    """

    document_id: str
    strategy: str
    chunks: tuple[Chunk, ...]
    batches: tuple[PlannedBatch, ...]
    total_chunks: int
    total_batches: int
    total_input_tokens: int
    estimated_llm_calls: int
    estimated_total_sec: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_batch(self, batch_id: str) -> PlannedBatch | None:
        for b in self.batches:
            if b.batch_id == batch_id:
                return b
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "strategy": self.strategy,
            "chunks": [c.to_dict() for c in self.chunks],
            "batches": [b.to_dict() for b in self.batches],
            "total_chunks": self.total_chunks,
            "total_batches": self.total_batches,
            "total_input_tokens": self.total_input_tokens,
            "estimated_llm_calls": self.estimated_llm_calls,
            "estimated_total_sec": self.estimated_total_sec,
            "metadata": dict(self.metadata),
        }


def _make_batch_id(idx: int) -> str:
    return f"cb_{idx:03d}"


def build_direct_plan(
    chunks: tuple[Chunk, ...],
    *,
    document_id: str,
    token_estimator,
    estimated_llm_calls: int = 1,
    estimated_total_sec: float = 60.0,
    metadata: dict[str, Any] | None = None,
) -> ExecutionPlan:
    """Построить ``ExecutionPlan`` для DIRECT стратегии (single call)."""
    total_tokens = token_estimator.estimate_many([c.text for c in chunks])
    batch = PlannedBatch(
        batch_id=_make_batch_id(0),
        chunk_ids=tuple(c.chunk_id for c in chunks),
        token_estimate=total_tokens,
        section_ids=tuple({c.section_id for c in chunks}),
    )
    return ExecutionPlan(
        document_id=document_id,
        strategy="direct",
        chunks=chunks,
        batches=(batch,),
        total_chunks=len(chunks),
        total_batches=1,
        total_input_tokens=total_tokens,
        estimated_llm_calls=estimated_llm_calls,
        estimated_total_sec=estimated_total_sec,
        metadata=metadata or {},
    )


def build_map_plan(
    chunks: tuple[Chunk, ...],
    *,
    document_id: str,
    strategy: str,
    batches_input: list[tuple[str, ...]],
    token_estimator,
    estimated_llm_calls: int | None = None,
    estimated_total_sec: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> ExecutionPlan:
    """Построить ``ExecutionPlan`` для MAP_FLAT / MAP_HIERARCHICAL.

    Args:
        batches_input: список tuple chunk_ids (порядок — execution order).
        strategy: ``"map_flat"`` или ``"map_hierarchical"``.
        estimated_llm_calls: если None, считается как ``len(batches)``.
    """
    chunk_by_id = {c.chunk_id: c for c in chunks}
    planned: list[PlannedBatch] = []
    for i, batch_chunk_ids in enumerate(batches_input):
        batch_chunks = [chunk_by_id[cid] for cid in batch_chunk_ids if cid in chunk_by_id]
        total_tokens = token_estimator.estimate_many([c.text for c in batch_chunks])
        planned.append(
            PlannedBatch(
                batch_id=_make_batch_id(i),
                chunk_ids=tuple(batch_chunk_ids),
                token_estimate=total_tokens,
                section_ids=tuple({c.section_id for c in batch_chunks}),
            )
        )

    total_tokens = token_estimator.estimate_many([c.text for c in chunks])
    if estimated_llm_calls is None:
        estimated_llm_calls = len(planned)

    return ExecutionPlan(
        document_id=document_id,
        strategy=strategy,
        chunks=chunks,
        batches=tuple(planned),
        total_chunks=len(chunks),
        total_batches=len(planned),
        total_input_tokens=total_tokens,
        estimated_llm_calls=estimated_llm_calls,
        estimated_total_sec=estimated_total_sec,
        metadata=metadata or {},
    )


__all__ = [
    "PlannedBatch",
    "ExecutionPlan",
    "build_direct_plan",
    "build_map_plan",
]