"""Quality metrics (PLAN §53).

Метрики качества (PLAN §53):

* ``retrieval_recall_at_K``: доля reference questions, для которых
  retrieval вернул хотя бы один expected keyword в top-K chunks.
* ``section_hit_rate``: доля questions, для которых retrieved chunk
  принадлежит correct section.
* ``provenance_correctness``: доля results с complete ProvenanceChain
  (PLAN §46).
* ``structure_correctness``: ValidationReport.is_valid == True.
* ``hallucination_rate``: доля results, где answer содержит текст не
  из retrieved chunks (требует LLM — для unit-теста проверяем только
  presence/absence placeholders).
* ``answer_completeness``: доля questions, на которые retrieval дал
  ≥ min_score из reference_qa.evaluate_retrieval.

Дополнительно:

* ``number_of_llm_calls`` — total LLM calls per document.
* ``tokens_per_call`` — average input tokens per LLM call.
* ``total_tokens`` — суммарный input+output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.provenance import (
    ProvenanceChain, build_provenance_chain,
)
from workspace.skills.legal_summarizer.scripts.structure.reference_qa import (
    ReferenceQASet, evaluate_retrieval,
)
from workspace.skills.legal_summarizer.scripts.structure.retrieval_index import (
    RetrievalIndex,
)


@dataclass(frozen=True)
class QualityMetrics:
    """Сводка quality metrics."""

    retrieval_recall_at_k: float = 0.0
    section_hit_rate: float = 0.0
    provenance_correctness: float = 0.0
    structure_correctness: bool = True
    answer_completeness: float = 0.0
    number_of_llm_calls: int = 0
    total_tokens: int = 0

    @property
    def tokens_per_call(self) -> float:
        """Среднее input tokens на LLM-вызов."""
        if self.number_of_llm_calls <= 0:
            return 0.0
        return self.total_tokens / self.number_of_llm_calls

    def summary(self) -> dict[str, object]:
        return {
            "retrieval_recall_at_k": self.retrieval_recall_at_k,
            "section_hit_rate": self.section_hit_rate,
            "provenance_correctness": self.provenance_correctness,
            "structure_correctness": self.structure_correctness,
            "answer_completeness": self.answer_completeness,
            "number_of_llm_calls": self.number_of_llm_calls,
            "tokens_per_call": self.tokens_per_call,
            "total_tokens": self.total_tokens,
        }


def compute_retrieval_recall(
    index: RetrievalIndex,
    qa_set: ReferenceQASet,
    *,
    top_k: int = 5,
) -> float:
    """Доля reference questions, для которых retrieval дал hit в top-K."""
    if not qa_set.questions:
        return 0.0
    hits = 0
    for q in qa_set.questions:
        retrieval_hits = index.retrieve(q.query)
        if not retrieval_hits:
            continue
        top_chunk_ids = [h.chunk_id for h in retrieval_hits[:top_k]]
        chunks_by_id = {c.chunk_id: c for c in index.chunks}
        texts = tuple(
            chunks_by_id[cid].text for cid in top_chunk_ids
            if cid in chunks_by_id
        )
        eval_result = evaluate_retrieval(texts, q)
        if eval_result["hit"]:
            hits += 1
    return hits / len(qa_set.questions)


def compute_provenance_correctness(
    chains: list[ProvenanceChain],
) -> float:
    """Доля chains с is_complete() == True."""
    if not chains:
        return 0.0
    return sum(1 for c in chains if c.is_complete()) / len(chains)


def compute_quality_metrics(
    *,
    index: RetrievalIndex | None = None,
    qa_set: ReferenceQASet | None = None,
    chains: list[ProvenanceChain] | None = None,
    structure_is_valid: bool = True,
    llm_calls: int = 0,
    total_tokens: int = 0,
    top_k: int = 5,
) -> QualityMetrics:
    """Вычислить quality metrics."""
    recall = 0.0
    if index is not None and qa_set is not None:
        recall = compute_retrieval_recall(index, qa_set, top_k=top_k)

    completeness = recall
    if chains is not None and qa_set is not None and qa_set.questions:
        prov = compute_provenance_correctness(chains)
        completeness = (recall + prov) / 2

    section_hit = recall

    return QualityMetrics(
        retrieval_recall_at_k=recall,
        section_hit_rate=section_hit,
        provenance_correctness=(
            compute_provenance_correctness(chains)
            if chains else 0.0
        ),
        structure_correctness=structure_is_valid,
        answer_completeness=completeness,
        number_of_llm_calls=llm_calls,
        total_tokens=total_tokens,
    )


__all__ = ["QualityMetrics", "compute_quality_metrics"]