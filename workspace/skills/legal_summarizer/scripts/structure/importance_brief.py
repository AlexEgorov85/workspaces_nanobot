"""Importance-aware brief selection (PLAN §31, Этап 31).

Текущий ``brief_strategy.py`` использует round-robin coverage
(``brief_coverage_ratio=0.2``). Это семантически слабо.

Importance-aware sampling (PLAN §31):

* **Priority 1**: title / preamble / TOC / outline.
* **Priority 2**: первый meaningful chunk каждой top-level section.
* **Priority 3**: lexical legal-important sections.
* **Priority 4**: заключительные положения.
* **Priority 5**: остальные через coverage/round-robin.

Это **детерминированный** algorithm (PLAN §61).
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)


_LEGAL_IMPORTANT_KEYWORDS = (
    "цена", "стоимость", "оплата", "срок", "ответственность",
    "штраф", "неустойка", "расторжение", "прекращение",
    "права", "обязанности", "конфиденциальность",
    "гарантии", "риски", "ответственность сторон",
)


@dataclass(frozen=True)
class BriefSelectionConfig:
    """Параметры importance-aware brief selection."""

    target_chunk_count: int = 8
    coverage_ratio: float = 0.5
    min_section_chunks: int = 1
    legal_keywords: tuple[str, ...] = _LEGAL_IMPORTANT_KEYWORDS


def _is_legal_important(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def _section_for_chunk(chunk: Chunk, struct: DocumentStructure) -> str:
    return chunk.section_id or ""


def _is_top_level_section(section_id: str, struct: DocumentStructure) -> bool:
    if section_id == struct.root_id:
        return False
    node = struct.nodes.get(section_id)
    if node is None:
        return False
    return node.level <= 2


def _is_conclusion_section(section_id: str, struct: DocumentStructure) -> bool:
    """Заключительные положения: section_path > N-1 или последний по order."""
    node = struct.nodes.get(section_id)
    if node is None:
        return False
    sections_in_order = [
        n for n in struct.nodes.values()
        if n.node_type == "section" and n.parent_id == struct.root_id
    ]
    if not sections_in_order:
        return False
    last = max(sections_in_order, key=lambda n: n.start_block)
    return node.node_id == last.node_id


def select_brief_chunks(
    chunks: tuple[Chunk, ...],
    struct: DocumentStructure,
    *,
    config: BriefSelectionConfig | None = None,
) -> list[Chunk]:
    """Importance-aware brief selection.

    Returns:
        Список ``Chunk`` в document order (не importance order).
        Длина — до ``target_chunk_count``.
    """
    cfg = config or BriefSelectionConfig()
    target = max(1, cfg.target_chunk_count)
    if not chunks:
        return []

    selected: list[Chunk] = []
    seen_ids: set[str] = set()

    def _add(chunk: Chunk) -> None:
        if chunk.chunk_id in seen_ids:
            return
        selected.append(chunk)
        seen_ids.add(chunk.chunk_id)

    section_to_chunks: dict[str, list[Chunk]] = {}
    for c in chunks:
        sid = _section_for_chunk(c, struct)
        section_to_chunks.setdefault(sid, []).append(c)

    section_ids_in_order = [
        n.node_id for n in struct.nodes.values()
        if n.node_type == "section" and n.parent_id == struct.root_id
    ]

    for sid in section_ids_in_order:
        sec_chunks = section_to_chunks.get(sid, [])
        if not sec_chunks:
            continue
        if _is_top_level_section(sid, struct):
            if sec_chunks:
                _add(sec_chunks[0])
                if len(selected) >= target:
                    break

    for sid in section_ids_in_order:
        sec_chunks = section_to_chunks.get(sid, [])
        if any(_is_legal_important(c.text, cfg.legal_keywords) for c in sec_chunks):
            for c in sec_chunks[: cfg.min_section_chunks]:
                _add(c)
                if len(selected) >= target:
                    break
        if len(selected) >= target:
            break

    for sid in reversed(section_ids_in_order):
        if _is_conclusion_section(sid, struct):
            for c in section_to_chunks.get(sid, [])[:1]:
                _add(c)
            break

    step = max(1, int(round(1.0 / max(0.01, cfg.coverage_ratio))))
    for i, c in enumerate(chunks):
        if i % step == 0:
            _add(c)
        if len(selected) >= target:
            break

    final = [c for c in chunks if c.chunk_id in seen_ids]
    return final[:target]


__all__ = ["BriefSelectionConfig", "select_brief_chunks"]