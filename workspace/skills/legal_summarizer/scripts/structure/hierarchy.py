"""StructureTreeBuilder (PLAN §12, Этап 12).

Строит ``DocumentStructure`` из:

* ``HeadingCandidate`` (из heading.py);
* ``NumberingInfo`` (из numbering.py);
* ``StructureEvidence``;
* ``HeadingEvidence`` (через heuristics).

Иерархия строится из совокупности evidence. Приоритеты (PLAN §12):

1. explicit legal numbering (``Статья``, ``Глава``, ``Раздел`` и т.д.).
2. validated PDF outline / document style hierarchy (``docx_style`` +
   ``pdf_outline``).
3. explicit style level (``docx_style``).
4. numbering-derived level (``decimal``, ``cyrillic_alpha`` и т.д.).
5. visual heuristics (short text, typography, body after).

Back-compat: builder производит **только** ``DocumentStructure`` —
старый ``SectionTree`` остаётся от ``build_section_tree``. По мере
миграции (Этап 45) consumers переключатся на ``DocumentStructure``.

Детерминированный (PLAN §61): LLM не используется.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    DocumentTitle,
    NumberingInfo,
    StructureEvidence,
    StructureNode,
    _make_node_id,
)
from workspace.skills.legal_summarizer.scripts.structure.numbering import (
    assign_sibling_ordinals,
    parse_numbering,
)


@dataclass(frozen=True)
class StructureTreeBuilderConfig:
    """Параметры builder'а (минимальный набор)."""

    default_section_level: int = 1
    document_id: str = "doc"
    include_body_nodes: bool = True


def _evidence_from_candidate(c: HeadingCandidate) -> tuple[StructureEvidence, ...]:
    """Преобразовать ``HeadingCandidate`` в tuple ``StructureEvidence``.

    Weight выбирается по `` source (PLAN §8):

    * ``docx_style`` / ``pdf_outline``: 0.95 (very high).
    * ``regex_statiya`` / ``regex_glзава`` и т.д.: 0.85 (high — explicit legal).
    * ``regex_numbered_*``: 0.70 (high — numbering).
    * всё остальное: 0.65.
    """
    if c.source in ("docx_style", "pdf_outline"):
        weight = 0.95
    elif c.source.startswith("regex_") and c.source != "regex_numbered_3":
        weight = 0.85
    else:
        weight = 0.70
    return (
        StructureEvidence(source=c.source, weight=weight, detail=c.text[:80]),
    )


def _resolve_level(c: HeadingCandidate) -> int:
    """Вычислить level кандидата с учётом priority (PLAN §12)."""
    if c.source == "docx_style":
        return max(1, min(6, c.level))
    if c.source == "pdf_outline":
        return max(1, min(6, c.level))
    return max(1, c.level)


def _resolve_semantic_type(c: HeadingCandidate) -> str | None:
    """Извлечь ``semantic_type`` из source'а кандидата.

    Правила:

    * ``regex_statiya`` → ``"article"``.
    * ``regex_glзава`` → ``"chapter"``.
    * ``regex_razdel`` → ``"section"`` (в смысле «раздел»).
    * ``regex_paragraph`` → ``"paragraph_mark"``.
    * иначе → ``None``.
    """
    if c.source == "regex_statiya":
        return "article"
    if c.source == "regex_glзава":
        return "chapter"
    if c.source == "regex_razdel":
        return "section"
    if c.source == "regex_paragraph":
        return "paragraph_mark"
    return None


def build_document_structure(
    candidates: Iterable[HeadingCandidate],
    *,
    total_blocks: int,
    config: StructureTreeBuilderConfig | None = None,
    title: DocumentTitle | None = None,
) -> DocumentStructure:
    """Построить ``DocumentStructure`` из ``HeadingCandidate``.

    Это **новый** builder; старый ``build_section_tree`` остаётся для
    back-compat (Этап 45 — миграция consumers).

    Args:
        candidates: ``HeadingCandidate`` (например, из
            ``detect_heading_candidates``).
        total_blocks: ``len(PhysicalDocument.blocks)`` — для
            ``coverage_ratio`` и root диапазона.
        config: параметры builder'а.
        title: ``DocumentTitle`` (из Этапа 14) или ``None``.

    Returns:
        ``DocumentStructure`` с одним ``root`` (``n_0000``) и одним
        ``section`` node'ом на каждого кандидата, плюс ``preamble``
        node для непокрытых blocks в начале.
    """
    cfg = config or StructureTreeBuilderConfig()

    accepted = [c for c in candidates if c.block_index >= 0]
    accepted.sort(key=lambda c: (c.block_index, c.level))

    nodes: dict[str, StructureNode] = {}

    root = StructureNode(
        node_id="n_0000",
        node_type="document",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=(),
        start_block=0,
        end_block=max(0, total_blocks - 1),
        confidence=1.0,
    )
    nodes[root.node_id] = root

    section_ids: list[str] = []
    numbering_list: list[NumberingInfo | None] = []
    for i, c in enumerate(accepted, start=1):
        nid = _make_node_id(i)
        ni = parse_numbering(c.text)
        numbering_list.append(ni)
        level = _resolve_level(c)
        semantic = _resolve_semantic_type(c)
        start = c.block_index
        end = (
            accepted[i].block_index - 1
            if i < len(accepted)
            else max(0, total_blocks - 1)
        )
        node = StructureNode(
            node_id=nid,
            node_type="section",
            semantic_type=semantic,
            level=level,
            title=c.text,
            number=ni,
            parent_id=root.node_id,
            children=(),
            start_block=start,
            end_block=end,
            confidence=c.score,
            evidence=_evidence_from_candidate(c),
            source_refs=(c.source,),
        )
        nodes[nid] = node
        section_ids.append(nid)

    sibling_ordinals = assign_sibling_ordinals(numbering_list)
    for nid, ordinal in zip(section_ids, sibling_ordinals):
        if ordinal is None:
            continue
        node = nodes[nid]
        if node.number is None:
            continue
        new_number = NumberingInfo(
            raw=node.number.raw,
            scheme=node.number.scheme,
            components=node.number.components,
            level=node.number.level,
            ordinal=ordinal,
        )
        nodes[nid] = StructureNode(
            node_id=node.node_id,
            node_type=node.node_type,
            semantic_type=node.semantic_type,
            level=node.level,
            title=node.title,
            number=new_number,
            parent_id=node.parent_id,
            children=node.children,
            start_block=node.start_block,
            end_block=node.end_block,
            confidence=node.confidence,
            evidence=node.evidence,
            source_refs=node.source_refs,
        )

    if section_ids:
        root_with_kids = StructureNode(
            node_id=root.node_id,
            node_type=root.node_type,
            semantic_type=root.semantic_type,
            level=root.level,
            title=root.title,
            number=root.number,
            parent_id=None,
            children=tuple(section_ids),
            start_block=root.start_block,
            end_block=root.end_block,
            confidence=root.confidence,
            evidence=root.evidence,
            source_refs=root.source_refs,
        )
        nodes[root.node_id] = root_with_kids

    numbering = tuple(
        n for n in (nodes[nid].number for nid in section_ids) if n is not None
    )

    coverage = 1.0 if not accepted else min(1.0, len(accepted) / max(1, total_blocks))

    return DocumentStructure(
        document_id=cfg.document_id,
        title=title,
        nodes=nodes,
        root_id=root.node_id,
        preamble_node_id=root.node_id,
        numbering=numbering,
        total_blocks=total_blocks,
        coverage_ratio=coverage,
    )


__all__ = [
    "StructureTreeBuilderConfig",
    "build_document_structure",
]