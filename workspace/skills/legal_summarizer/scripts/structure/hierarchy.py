"""StructureTreeBuilder.

Строит ``DocumentStructure`` из:

* ``HeadingCandidate`` (из heading.py);
* ``NumberingInfo`` (из numbering.py);
* ``StructureEvidence``;
* ``HeadingEvidence`` (через heuristics).

Иерархия строится из совокупности evidence. Приоритеты:

1. explicit legal numbering (``Статья``, ``Глава``, ``Раздел`` и т.д.).
2. validated PDF outline / document style hierarchy (``docx_style`` +
   ``pdf_outline``).
3. explicit style level (``docx_style``).
4. numbering-derived level (``decimal``, ``cyrillic_alpha`` и т.д.).
5. visual heuristics (short text, typography, body after).

Nested parents: каждая секция получает ``parent_id`` — предыдущую
секцию меньшего или равного ``effective_level``, чей диапазон
перекрывает текущую. Это даёт настоящее дерево:

    root
    ├── Глава 1
    │   ├── Статья 1
    │   └── Статья 2
    └── Глава 2
        └── Статья 3

Всегда предпочитаем более глубокий явный родитель (explicit legal
numbering → outline → style level → numbering depth → visual).

Back-compat: builder производит **только** ``DocumentStructure``.

Детерминированный: LLM не используется.
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


def _scheme_priority(scheme: str | None) -> int:
    """Приоритет схемы для выбора parent.

    Меньше = глубже в иерархии (выше шанс стать дочерним).
    """
    if scheme in ("legal_chapter", "legal_section_roman"):
        return 1
    if scheme in ("legal_article",):
        return 2
    if scheme in ("legal_clause", "paragraph_mark"):
        return 3
    if scheme in ("appendix",):
        return 4
    return 99


def _level_from_numbering(ni: NumberingInfo | None) -> int:
    """Вывести level из numbering.

    Для decimal — это ``len(components)`` (1 → 1.0 → 2, etc.).
    Для остальных — ``ni.level`` (1-based).
    """
    if ni is None:
        return 99
    if ni.scheme == "decimal":
        return len(ni.components) if ni.components else 1
    return max(1, ni.level)


def _effective_level(c: HeadingCandidate, ni: NumberingInfo | None) -> int:
    """Вычислить ``effective level`` кандидата.

    Приоритет:

    1. ``docx_style`` / ``pdf_outline`` — собственный ``c.level``.
    2. legal / appendix — ``_scheme_priority`` (1..4).
    3. decimal — ``len(components)`` (по numbering).
    4. visual — ``c.level`` как fallback.
    """
    if c.source in ("docx_style", "pdf_outline"):
        return max(1, c.level)
    if ni is not None:
        if ni.scheme in (
            "legal_chapter",
            "legal_section_roman",
            "appendix",
            "legal_article",
            "legal_clause",
            "paragraph_mark",
        ):
            return _scheme_priority(ni.scheme)
        if ni.scheme == "decimal":
            return _level_from_numbering(ni)
    return max(1, c.level)


@dataclass(frozen=True)
class StructureTreeBuilderConfig:
    """Параметры builder'а (минимальный набор)."""

    default_section_level: int = 1
    document_id: str = "doc"
    include_body_nodes: bool = True


def _evidence_from_candidate(c: HeadingCandidate) -> tuple[StructureEvidence, ...]:
    """Преобразовать ``HeadingCandidate`` в tuple ``StructureEvidence``.

    Weight выбирается по ``source``:

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
    """Вычислить level кандидата с учётом source-приоритета."""
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

    Args:
        candidates: ``HeadingCandidate`` (например, из
            ``detect_heading_candidates``).
        total_blocks: ``len(PhysicalDocument.blocks)`` — для
            ``coverage_ratio`` и root диапазона.
        config: параметры builder'а.
        title: ``DocumentTitle`` или ``None``.

    Returns:
        ``DocumentStructure`` с одним ``root`` (``n_0000``) и одним
        ``section`` node'ом на каждого кандидата, плюс ``preamble``
        node для непокрытых blocks в начале.

    Nested parents: для каждого кандидата parent — это последний
    предыдущий кандидат с ``effective_level < current.effective_level``.
    Если таких нет, parent = root. Это даёт реальное nested дерево,
    а не плоский список.
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
    numbering_by_section: dict[str, NumberingInfo | None] = {}
    eff_level_by_section: dict[str, int] = {}
    start_block_by_section: dict[str, int] = {}

    for i, c in enumerate(accepted, start=1):
        nid = _make_node_id(i)
        ni = parse_numbering(c.text)
        numbering_list.append(ni)
        eff_level = _effective_level(c, ni)
        semantic = _resolve_semantic_type(c)
        start = c.block_index
        end = (
            accepted[i].block_index - 1
            if i < len(accepted)
            else max(0, total_blocks - 1)
        )

        parent_id = root.node_id
        for cand_id in reversed(section_ids):
            cand_eff = eff_level_by_section[cand_id]
            if cand_eff < eff_level:
                parent_id = cand_id
                break
            if (
                cand_eff == eff_level
                and numbering_by_section[cand_id] is not None
                and ni is not None
                and numbering_by_section[cand_id].scheme == ni.scheme
                and numbering_by_section[cand_id].components[:-1]
                == ni.components[:-1]
                and len(ni.components) > 1
            ):
                parent_id = cand_id
                break

        node = StructureNode(
            node_id=nid,
            node_type="section",
            semantic_type=semantic,
            level=eff_level,
            title=c.text,
            number=ni,
            parent_id=parent_id,
            children=(),
            start_block=start,
            end_block=end,
            confidence=c.score,
            evidence=_evidence_from_candidate(c),
            source_refs=(c.source,),
        )
        nodes[nid] = node
        section_ids.append(nid)
        numbering_by_section[nid] = ni
        eff_level_by_section[nid] = eff_level
        start_block_by_section[nid] = start

    siblings_children: dict[str, list[str]] = {sid: [] for sid in section_ids}
    siblings_children[root.node_id] = []
    for sid in section_ids:
        parent = nodes[sid].parent_id
        if parent is not None and parent in siblings_children:
            siblings_children[parent].append(sid)

    for sid in section_ids:
        node = nodes[sid]
        nodes[sid] = StructureNode(
            node_id=node.node_id,
            node_type=node.node_type,
            semantic_type=node.semantic_type,
            level=node.level,
            title=node.title,
            number=node.number,
            parent_id=node.parent_id,
            children=tuple(siblings_children[sid]),
            start_block=node.start_block,
            end_block=node.end_block,
            confidence=node.confidence,
            evidence=node.evidence,
            source_refs=node.source_refs,
        )

    nodes[root.node_id] = StructureNode(
        node_id=root.node_id,
        node_type=root.node_type,
        semantic_type=root.semantic_type,
        level=root.level,
        title=root.title,
        number=root.number,
        parent_id=None,
        children=tuple(siblings_children[root.node_id]),
        start_block=root.start_block,
        end_block=root.end_block,
        confidence=root.confidence,
        evidence=root.evidence,
        source_refs=root.source_refs,
    )

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