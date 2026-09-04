"""Reference QA для benchmark'ов (PLAN §52).

Для каждого benchmark-сценария набор reference questions с expected
answer и expected_section_id. Используется в Этапе 53 для метрик
retrieval recall@K, section hit rate, provenance correctness.

Reference questions (PLAN §52):

* Какова цена?
* Каковы сроки?
* Кто несёт ответственность?
* Какие основания расторжения?
* Какие штрафы?
* Какие обязательства сторон?

Для каждого вопроса хранится:

* ``query``: текст вопроса.
* ``expected_section_keywords``: ключевые слова, которые должны
  встретиться в ответе.
* ``min_score``: минимальный retrieval score для passing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReferenceQuestion:
    """Один reference вопрос."""

    query: str
    expected_section_keywords: tuple[str, ...]
    min_score: float = 0.5
    description: str = ""


@dataclass(frozen=True)
class ReferenceQASet:
    """Набор reference questions для одного benchmark-документа."""

    document_name: str
    questions: tuple[ReferenceQuestion, ...]


def standard_qa_set(document_name: str = "legal-doc") -> ReferenceQASet:
    """Стандартный набор reference questions (PLAN §52)."""
    return ReferenceQASet(
        document_name=document_name,
        questions=(
            ReferenceQuestion(
                query="Какова цена договора?",
                expected_section_keywords=("цена", "стоимость", "сумма"),
                description="цена / стоимость",
            ),
            ReferenceQuestion(
                query="Каковы сроки оплаты?",
                expected_section_keywords=("срок", "оплат"),
                description="срок",
            ),
            ReferenceQuestion(
                query="Кто несёт ответственность?",
                expected_section_keywords=("ответственность", "штраф"),
                description="ответственность",
            ),
            ReferenceQuestion(
                query="Какие основания расторжения?",
                expected_section_keywords=("расторжение", "прекращение"),
                description="расторжение",
            ),
            ReferenceQuestion(
                query="Какие штрафы за просрочку?",
                expected_section_keywords=("штраф", "неустойка", "пеня"),
                description="штраф",
            ),
            ReferenceQuestion(
                query="Какие обязательства сторон?",
                expected_section_keywords=("обязан", "обязательств"),
                description="обязательства",
            ),
        ),
    )


def evaluate_retrieval(
    retrieved_chunk_texts: tuple[str, ...],
    question: ReferenceQuestion,
) -> dict[str, Any]:
    """Оценить retrieved chunks против reference question.

    Returns:
        dict с полями:
        * ``hit`` — найдены ли expected keywords в retrieved chunks.
        * ``hits_count`` — сколько keywords найдено.
        * ``score`` — нормализованный score (0..1).
    """
    if not retrieved_chunk_texts:
        return {"hit": False, "hits_count": 0, "score": 0.0}

    combined = " ".join(retrieved_chunk_texts).lower()
    hits_count = sum(
        1 for kw in question.expected_section_keywords
        if kw.lower() in combined
    )
    total = len(question.expected_section_keywords)
    score = hits_count / total if total else 0.0
    return {
        "hit": hits_count > 0,
        "hits_count": hits_count,
        "score": score,
    }


__all__ = [
    "ReferenceQuestion",
    "ReferenceQASet",
    "standard_qa_set",
    "evaluate_retrieval",
]