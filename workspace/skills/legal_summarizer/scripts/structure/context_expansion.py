"""Semantic context expansion (PLAN §10).

Для выбранного chunk'а вернуть расширенный контекст через поиск
**neighbours по target index** (PLAN §10):

    target
    ↓
    immediate previous chunk
    immediate next chunk
    ↓
    same subsection restriction
    ↓
    parent heading
    ↓
    token budget

**Не** выбирать «все chunks той же section → первые N».
Семантика — **target_idx ± k** (adjacent neighbours), а не
section-prefix.

Token accounting:

* ``used_tokens`` обновляется **после каждого** добавленного neighbour;
* ``total_tokens == tokens(target) + sum(tokens(neighbours))``.

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
    """Параметры context expansion.

    Attributes:
        max_neighbour_blocks: максимум neighbours (default 2 — один
            слева, один справа).
        max_context_tokens: token budget для neighbours (не считая
            target).
        include_same_subsection: если ``True``, neighbours только из
            того же section_id, что и target (для skip через sections).
        include_parent_heading: если ``True``, добавить parent heading
            в ``parent_heading`` поля ``ExpandedContext``.
    """

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


def _section_for_chunk(chunk: Chunk) -> str:
    return chunk.section_id or ""


def _can_use_neighbour(
    candidate: Chunk,
    target: Chunk,
    cfg: ContextExpansionConfig,
) -> bool:
    """Можно ли использовать ``candidate`` как neighbour для ``target``.

    Если ``include_same_subsection`` — только из той же секции.
    """
    if not cfg.include_same_subsection:
        return True
    return _section_for_chunk(candidate) == _section_for_chunk(target)


def expand_context(
    target: Chunk,
    chunks: tuple[Chunk, ...],
    struct: DocumentStructure,
    *,
    config: ContextExpansionConfig | None = None,
    estimator: TokenEstimator | None = None,
) -> ExpandedContext:
    """Расширить контекст для ``target``.

    Алгоритм (PLAN §10):

    1. ``target_idx = index of target in sorted(chunks)``;
    2. Поочерёдно проверяем ``target_idx - 1``, ``target_idx + 1``,
       ``target_idx - 2``, ``target_idx + 2``, ... ;
    3. Если neighbour проходит subsection check и budget check —
       добавляем; иначе skip и пробуем следующий;
    4. После каждого добавления пересчитываем ``used_tokens``;
    5. Останавливаемся при ``max_neighbour_blocks`` или
       ``max_context_tokens``.

    Returns:
        ``ExpandedContext`` с ``total_tokens ==
        tokens(target) + sum(tokens(neighbours))``.
    """
    cfg = config or ContextExpansionConfig()
    est = estimator or TokenEstimator(TokenEstimatorConfig())

    section_id = _section_for_chunk(target)
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
            parent_heading=parent_heading if cfg.include_parent_heading else "",
            section_title=section_title,
            total_tokens=est.estimate(target.text),
        )

    chosen_neighbours: list[Chunk] = []
    used_tokens = 0
    truncated = False

    target_tokens = est.estimate(target.text)

    left = target_idx - 1
    right = target_idx + 1
    take_left = True
    while (
        len(chosen_neighbours) < cfg.max_neighbour_blocks
        and (left >= 0 or right < len(sorted_chunks))
    ):
        candidate: Chunk | None = None
        if take_left and left >= 0:
            candidate = sorted_chunks[left]
            left -= 1
        elif not take_left and right < len(sorted_chunks):
            candidate = sorted_chunks[right]
            right += 1
        elif left >= 0:
            candidate = sorted_chunks[left]
            left -= 1
        elif right < len(sorted_chunks):
            candidate = sorted_chunks[right]
            right += 1
        else:
            break
        take_left = not take_left

        if _can_use_neighbour(candidate, target, cfg):
            cand_tokens = est.estimate(candidate.text)
            if used_tokens + cand_tokens <= cfg.max_context_tokens:
                chosen_neighbours.append(candidate)
                used_tokens += cand_tokens
            else:
                truncated = True
                break

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