"""Объединение heading-кандидатов (PLAN §9, Этап 9).

Если несколько источников (DOCX style + numbering + regex + PDF outline)
говорят об одном и том же ``DocumentBlock``, нельзя создавать
несколько heading-кандидатов — это раздувает структуру.

Вместо этого — **один структурный кандидат** с комбинированным
``source_refs`` и ``score`` (max из всех).

Сейчас в ``heading.py`` есть ``detect_heading_candidates``, который
для каждого block уже возвращает максимум один кандидат (т.к. после
``docx_style`` идёт ``continue``, а regex и outline проверяются
последовательно). То есть де-факто агрегация уже есть — но она
размазана и плохо отделима от тестов.

Этот модуль — **явный** aggregator, который можно вызвать из будущих
pipelines (Этап 12 — StructureTreeBuilder). Сейчас он
**декларативный** — собирает кандидатов в ``AggregatedCandidate``
с комбинированными evidence. Заменяет дубликаты в ``HeadingCandidate``
(например, PDF outline + DOCX style указывают на одно и то же место).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate,
)


@dataclass(frozen=True)
class AggregatedCandidate:
    """Один объединённый heading-кандидат (PLAN §9).

    Attributes:
        block_index: ordinal ``DocumentBlock`` (для outline
            кандидатов ``block_index`` остаётся ``-1``, и
            downstream mapping обязан решить эту ситуацию — Этап 11).
        text: текст кандидата (один из исходных текстов; для outline
            кандидатов — outline title, для остальных — block content).
        level: максимальный level среди объединённых источников.
        confidence: максимальный score среди источников.
        sources: tuple имён источников (``"docx_style"``,
            ``"legal_numbering"``, ``"pdf_outline"``, ``"regex_*"`` и т.д.).
        raw_numbers: tuple raw_number'ов из всех источников.
    """

    block_index: int
    text: str
    level: int
    confidence: float
    sources: tuple[str, ...]
    raw_numbers: tuple[str, ...]


def aggregate_by_block(
    candidates: Iterable[HeadingCandidate],
) -> list[AggregatedCandidate]:
    """Объединить ``HeadingCandidate`` по ``block_index``.

    Outline-кандидаты (с ``block_index = -1``) не агрегируются с
    обычными — они остаются отдельно (для mapping в Этапе 11).
    """
    by_block: dict[int, list[HeadingCandidate]] = {}
    outline_only: list[HeadingCandidate] = []

    for c in candidates:
        if c.block_index < 0:
            outline_only.append(c)
            continue
        by_block.setdefault(c.block_index, []).append(c)

    out: list[AggregatedCandidate] = []

    for idx in sorted(by_block):
        cs = by_block[idx]
        out.append(
            AggregatedCandidate(
                block_index=idx,
                text=cs[0].text,
                level=max(c.level for c in cs),
                confidence=max(c.score for c in cs),
                sources=tuple(c.source for c in cs),
                raw_numbers=tuple(c.raw_number for c in cs if c.raw_number),
            )
        )

    for c in outline_only:
        out.append(
            AggregatedCandidate(
                block_index=-1,
                text=c.text,
                level=c.level,
                confidence=c.score,
                sources=(c.source,),
                raw_numbers=(),
            )
        )

    return out


__all__ = ["AggregatedCandidate", "aggregate_by_block"]