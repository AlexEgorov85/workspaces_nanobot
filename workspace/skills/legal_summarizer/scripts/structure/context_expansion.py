"""Semantic context expansion (PLAN §37, Этап 37).

Для выбранного chunk'а вернуть расширенный контекст:

* target chunk;
* same subsection (если есть);
* parent heading;
* neighbour blocks (не более ``max_neighbour_blocks``);
* same section metadata.

Не расширять безлимитно. Ввести ``max_context_tokens`` через
единый ``TokenEstimator``.

Использует ``DocumentStructure`` (не ``SectionTree``) — это новый
canonical путь (PLAN §18, §45).
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator, TokenEstimatorConfig,
)


@dataclass(frozen=True)
class ContextExpansionConfig:
    """Параметры context expansion."""

    max_neighbour_blocks: int = 2
    max_context_tokens: int = 4000
    include_same_subsection: bool = True
    include_parent_heading: bool = True


@dataclass(frozen=True)
class ExpandedContext:
    """Результат context expansion."""

    target_chunk: Chunk
    neighbour_chunks: tuple[Chunk, ...]
    parent_heading: str
    section_title: str
    total_tokens: int
    truncated: bool = False


def _section_for_chunk(chunk: Chunk, struct: DocumentStructure) -> str:
    return chunk.section_id or ""


def _is_within_subsection(
    chunk_a: Chunk,
    chunk_b: Chunk,
    struct: DocumentStructure,
) -> bool:
    return chunk_a.section_id == chunk_b.section_id and bool(chunk_a.section_id)


def expand_context(
    target: Chunk,
    chunks: tuple[Chunk, ...],
    struct: DocumentStructure,
    *,
    config: ContextExpansionConfig | None = None,
    estimator: TokenEstimator | None = None,
) -> ExpandedContext:
    """Расширить контекст для ``target``.

    Использует ``DocumentStructure`` как SoT (PLAN §45) — не делает
    linear lookup через blocks.
    """
    cfg = config or ContextExpansionConfig()
    est = estimator or TokenEstimator(TokenEstimatorConfig())

    section_id = _section_for_chunk(target, struct)
    section = struct.nodes.get(section_id) if section_id else None
    section_title = section.title if section else ""

    parent_id = section.parent_id if section else None
    parent = struct.nodes.get(parent_id) if parent_id else None
    parent_heading = parent.title if parent else ""

    sorted_chunks = sorted(chunks, key=lambda c: c.index)
    target_idx = next(
        (i for i, c in enumerate(sorted_chunks) if c.chunk_id == target.chunk_id),
        -1,
    )
    if target_idx < 0:
        return ExpandedContext(
            target_chunk=target, neighbour_chunks=(),
            parent_heading=parent_heading, section_title=section_title,
            total_tokens=est.estimate(target.text),
        )

    neighbours: list[Chunk] = []
    if cfg.include_same_subsection and section_id:
        for c in sorted_chunks:
            if c.chunk_id == target.chunk_id:
                continue
            if _is_within_subsection(target, c, struct):
                neighbours.append(c)

    target_tokens = est.estimate(target.text)
    remaining = max(0, cfg.max_context_tokens - target_tokens)

    chosen_neighbours: list[Chunk] = []
    used_tokens = 0
    truncated = False
    for n in neighbours:
        n_tokens = est.estimate(n.text)
        if used_tokens + n_tokens > remaining:
            truncated = True
            break
        chosen_neighbours.append(n)
        used_tokens += n_tokens

    if len(chosen_neighbours) > cfg.max_neighbour_blocks:
        chosen_neighbours = chosen_neighbours[: cfg.max_neighbour_blocks]

    return ExpandedContext(
        target_chunk=target,
        neighbour_chunks=tuple(chosen_neighbours),
        parent_heading=parent_heading if cfg.include_parent_heading else "",
        section_title=section_title,
        total_tokens=target_tokens + used_tokens,
        truncated=truncated,
    )


__all__ = [
    "ContextExpansionConfig",
    "ExpandedContext",
    "expand_context",
]