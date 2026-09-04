"""SemanticRecord — структурированный output LLM map (PLAN §29, Этап 29).

LLM map возвращает не просто свободный текст, а структурированный
record, который downstream может ранжировать, фильтровать и
использовать для retrieval.

Минимальная схема (PLAN §29):

* ``chunk_id``: для какого chunk'а;
* ``section_id``: из DocumentStructure;
* ``summary``: основной текст саммари;
* ``facts``: tuple кратких фактов;
* ``entities``: tuple имён/организаций;
* ``obligations``: tuple обязательств;
* ``dates``: tuple дат (как строки);
* ``amounts``: tuple денежных/количественных сумм (как строки);
* ``risks``: tuple рисков;
* ``references``: tuple ссылок на другие статьи/разделы;
* ``confidence``: 0..1;
* ``provenance``: ``(start_block, end_block, page_start, page_end)``.

Это маленькая схема — не заставляем слабую модель генерировать огромный
JSON. Опциональные поля можно опускать (через tuple()).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Provenance:
    """Provenance для SemanticRecord (PLAN §46)."""

    start_block: int
    end_block: int
    page_start: int | None = None
    page_end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_block": self.start_block,
            "end_block": self.end_block,
            "page_start": self.page_start,
            "page_end": self.page_end,
        }


@dataclass(frozen=True)
class SemanticRecord:
    """Структурированный output LLM map (PLAN §29).

    Attributes:
        chunk_id: id chunk'а, для которого создан record.
        section_id: ``StructureNode.node_id`` из DocumentStructure.
        summary: основной текст саммари (1-3 предложения).
        facts: tuple коротких фактов из chunk'а.
        entities: tuple имён/организаций.
        obligations: tuple обязательств ("X обязуется Y").
        dates: tuple дат (как строки — ``"01.01.2024"``).
        amounts: tuple денежных/количественных сумм (как строки).
        risks: tuple рисков.
        references: tuple ссылок на другие части документа.
        confidence: 0..1 (LLM-output confidence).
        provenance: ``Provenance`` для traceability.
    """

    chunk_id: str
    section_id: str
    summary: str
    facts: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()
    dates: tuple[str, ...] = ()
    amounts: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    confidence: float = 0.0
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "section_id": self.section_id,
            "summary": self.summary,
            "facts": list(self.facts),
            "entities": list(self.entities),
            "obligations": list(self.obligations),
            "dates": list(self.dates),
            "amounts": list(self.amounts),
            "risks": list(self.risks),
            "references": list(self.references),
            "confidence": self.confidence,
            "provenance": (
                self.provenance.to_dict() if self.provenance is not None else None
            ),
        }

    @classmethod
    def from_minimal(
        cls,
        chunk_id: str,
        section_id: str,
        summary: str,
        *,
        provenance: Provenance | None = None,
        confidence: float = 0.5,
    ) -> "SemanticRecord":
        """Создать минимальный record — только summary.

        Полезно для малых моделей или старых prompt'ов, которые
        возвращают ``{"summary": "..."}``.
        """
        return cls(
            chunk_id=chunk_id,
            section_id=section_id,
            summary=summary,
            confidence=confidence,
            provenance=provenance,
        )


__all__ = ["Provenance", "SemanticRecord"]