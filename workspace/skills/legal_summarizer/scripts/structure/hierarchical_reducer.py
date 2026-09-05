"""Единый HierarchicalReducer (PLAN §24, Этапы 9, 19).

Единственная реализация hierarchical reduce для canonical pipeline.

Два режима:

* ``reduce_chunks_hierarchical`` — section-level + document-level.
* ``reduce_sections_to_document`` — rounds of groups + финальный reduce
  (Этап 9: данные не теряются).

LLM-trim (PLAN §26) убран как нормальный этап pipeline. Остаётся
**deterministic truncation** как emergency fallback (PLAN §27).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class HierarchicalReducerConfig:
    """Параметры HierarchicalReducer'а."""

    group_size: int = 3
    max_rounds: int = 4
    input_budget_chars: int = 60_000
    section_summary_max_chars: int = 4_000


def deterministic_truncate(text: str, max_chars: int) -> str:
    """PLAN §27: deterministic head + tail truncate с omission marker.

    Используется как **emergency fallback** — нормальный путь — это
    hierarchical reduce с правильным budget.
    """
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head - 200
    if tail < 0:
        tail = 0
    skipped = len(text) - head - tail
    if tail:
        return (
            text[:head]
            + f"\n\n[...пропущено {skipped} символов...]\n\n"
            + text[-tail:]
        )
    return text[:head] + f"\n\n[...пропущено {skipped} символов...]"


def _fit_input(text: str, budget: int) -> str:
    """Обёртка над deterministic_truncate."""
    return deterministic_truncate(text, budget)


@dataclass(frozen=True)
class HierarchicalReducerResult:
    """Результат HierarchicalReducer.reduce."""

    final_summary: str
    section_summaries: dict[str, str] = field(default_factory=dict)
    rounds_done: int = 0
    truncated: bool = False


LLMRunner = Callable[..., str]


def reduce_sections_to_document(
    section_summaries: list[tuple[str, str]],
    *,
    config: HierarchicalReducerConfig | None = None,
    llm_runner: LLMRunner | None = None,
    length: str = "detailed",
    focus: str | None = None,
    structure: dict | None = None,
    question: str | None = None,
) -> HierarchicalReducerResult:
    """Hierarchical reduce: section-level → rounds → final.

    Используется, когда section_summaries уже есть (Этап 40 follow-up
    или pre-computed section summaries).

    Этап 9 invariant: данные не теряются. Если после ``max_rounds``
    остаётся более одной группы, делается **финальный reduce** (один
    дополнительный round, объединяющий всё, что осталось). Если
    ``llm_runner is None`` — финальный reduce пропускается и берётся
    детерминированный join.
    """
    cfg = config or HierarchicalReducerConfig()
    rounds = 0
    current = list(section_summaries)
    truncated = False

    while len(current) > 1 and rounds < cfg.max_rounds:
        rounds += 1
        next_level: list[tuple[str, str]] = []
        for i in range(0, len(current), cfg.group_size):
            group = current[i:i + cfg.group_size]
            joined = "\n\n".join(f"[{sid}]\n{summary}" for sid, summary in group)
            if len(joined) > cfg.input_budget_chars:
                joined = _fit_input(joined, cfg.input_budget_chars)
                truncated = True
            if llm_runner is None:
                next_level.append((f"r{rounds}_g{i // cfg.group_size}", joined))
            else:
                text = llm_runner(
                    joined,
                    length=length,
                    focus=focus,
                    structure=structure,
                    question=question,
                )
                next_level.append((f"r{rounds}_g{i // cfg.group_size}", text))
        current = next_level

    # Финальный reduce (Этап 9): если после max_rounds осталось >1
    # группы — делаем один дополнительный round, чтобы не потерять
    # данные.
    if len(current) > 1:
        rounds += 1
        joined = "\n\n".join(f"[{sid}]\n{summary}" for sid, summary in current)
        if len(joined) > cfg.input_budget_chars:
            joined = _fit_input(joined, cfg.input_budget_chars)
            truncated = True
        if llm_runner is None:
            final = joined
        else:
            final = llm_runner(
                joined,
                length=length,
                focus=focus,
                structure=structure,
                question=question,
            )
        current = [("final", final)]

    final_summary = current[0][1] if current else ""
    return HierarchicalReducerResult(
        final_summary=final_summary,
        section_summaries={},
        rounds_done=rounds,
        truncated=truncated,
    )


def reduce_chunks_hierarchical(
    chunks: list,
    chunk_summaries: dict[str, str],
    *,
    section_ids: list[str],
    section_headings: dict[str, str] | None = None,
    section_paths: dict[str, str] | None = None,
    config: HierarchicalReducerConfig | None = None,
    llm_runner: LLMRunner | None = None,
    length: str = "detailed",
    focus: str | None = None,
) -> HierarchicalReducerResult:
    """Hierarchical reduce: section-level + document-level.

    Используется когда chunks ещё не просуммированы.
    """
    cfg = config or HierarchicalReducerConfig()
    section_summaries: dict[str, str] = {}

    for sid in section_ids:
        chunk_ids = [
            c.chunk_id for c in chunks
            if c.section_id == sid and c.chunk_id in chunk_summaries
        ]
        if not chunk_ids:
            continue
        items = [(cid, chunk_summaries[cid]) for cid in chunk_ids]
        joined = "\n\n".join(f"[Chunk {cid}]\n{s}" for cid, s in items)
        if len(joined) > cfg.input_budget_chars:
            joined = _fit_input(joined, cfg.input_budget_chars)
        heading = (section_headings or {}).get(sid, "")
        if llm_runner is None:
            summary = joined
        else:
            summary = llm_runner(
                joined,
                length=length,
                focus=None,
                section_path=(section_paths or {}).get(sid, ""),
                section_heading=heading,
            )
        if len(summary) > cfg.section_summary_max_chars:
            summary = _fit_input(summary, cfg.section_summary_max_chars)
        section_summaries[sid] = summary

    section_items = [
        (sid, section_summaries[sid])
        for sid in section_ids if sid in section_summaries
    ]
    if len(section_items) > 1:
        final_result = reduce_sections_to_document(
            section_items,
            config=config,
            llm_runner=llm_runner,
            length=length,
            focus=focus,
        )
        return HierarchicalReducerResult(
            final_summary=final_result.final_summary,
            section_summaries=section_summaries,
            rounds_done=final_result.rounds_done,
            truncated=final_result.truncated,
        )

    final = section_items[0][1] if section_items else ""
    return HierarchicalReducerResult(
        final_summary=final,
        section_summaries=section_summaries,
        rounds_done=0,
    )


__all__ = [
    "HierarchicalReducerConfig",
    "HierarchicalReducerResult",
    "LLMRunner",
    "deterministic_truncate",
    "reduce_chunks_hierarchical",
    "reduce_sections_to_document",
]