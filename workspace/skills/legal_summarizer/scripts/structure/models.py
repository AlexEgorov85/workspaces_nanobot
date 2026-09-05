"""DocumentStructure — единый контракт семантической структуры документа.

Это **canonical model** для семантической структуры, отдельная от
``PhysicalDocument`` (см. PLAN §3, §10).

Архитектурные инварианты:

* ``StructureNode`` ссылается на ``DocumentBlock`` через ``start_block``
  / ``end_block`` (ordinals), **не копирует текст**.
* ``StructureNode.semantic_type`` отделён от ``node_type`` (например,
  один и тот же ``block_type="paragraph"`` может иметь разный
  ``semantic_type`` — ``heading`` / ``body`` / ``list_item``).
* ``DocumentStructure`` — единый source of truth для
  ChunkPlanner / packer / retrieval / brief / reducer (PLAN §45).
* Структура **детерминированная** (PLAN §61): LLM не участвует
  в её построении.

``DocumentStructure`` — единственный production тип структуры.
Legacy ``SectionTree`` / ``DocumentSection`` / ``HeadingCandidate``
удалены в предыдущих рефакторингах.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StructureEvidence:
    """Один evidence для решения о structural node.

    Используется в ``StructureNode.evidence`` (PLAN §8 — пять уровней
    уверенности: very-high / high / medium / low).

    Attributes:
        source: имя источника (например, ``"docx_style"``,
            ``"legal_numbering"``, ``"pdf_outline"``, ``"typography"``,
            ``"neighbor_consistency"``).
        weight: относительный вес (0..1). Дефолты — в ``HeadingEvidence``
            (см. ``heading.py``).
        detail: текстовое описание того, что именно было обнаружено
            (для diagnostics / audit).
    """

    source: str
    weight: float
    detail: str = ""


@dataclass(frozen=True)
class NumberingInfo:
    """Парсер numbering для heading/list caption.

    Поддерживает минимум (PLAN §6):

    * ``1.``, ``1.1``, ``1.1.1`` — decimal scheme.
    * ``Статья 12``, ``Статья 12.1`` — legal_article scheme.
    * ``Глава 3`` — legal_chapter scheme.
    * ``Раздел I`` — legal_section_roman scheme.
    * ``§ 5`` — paragraph_mark scheme.
    * ``Пункт 1`` — legal_clause scheme.
    * ``а)``, ``б)`` — cyrillic_alpha scheme.
    * ``Приложение 1``, ``Приложение А`` — appendix scheme.

    Attributes:
        raw: исходная строка (например, ``"12.1"``).
        scheme: одно из имён схем выше (``"decimal"`` /
            ``"legal_article"`` и т.д.).
        components: числовые/строковые компоненты (``(12, 1)`` для
            ``"12.1"``; для ``"I"`` — ``"I"``).
        level: nesting depth (1-based, ``"12.1"`` → ``level=2``).
        ordinal: ordinal среди siblings **одного уровня и схемы**
            (например, для ``"1.1"`` ordinal=1; ``"1.2"`` → ordinal=2).
            ``None`` если вычислить нельзя без siblings.
    """

    raw: str
    scheme: str
    components: tuple[Any, ...]
    level: int
    ordinal: int | None = None


@dataclass(frozen=True)
class DocumentTitle:
    """Title документа (PLAN §14).

    Различает три источника:

    * ``source="metadata"`` — из DOCX/PDF/PPTX metadata.
    * ``source="visual"`` — первая визуально выделенная строка
      (typography heuristics).
    * ``source="inferred"`` — heading candidate с минимальным level
      и высокой confidence.

    Attributes:
        value: строка title.
        source: один из ``"metadata"``/``"visual"``/``"inferred"``.
        confidence: 0..1.
        block_ordinal: ordinal DocumentBlock, откуда взят title
            (``None`` для ``source="metadata"``).
    """

    value: str
    source: str
    confidence: float
    block_ordinal: int | None = None


@dataclass(frozen=True)
class StructureNode:
    """Узел семантической структуры документа (PLAN §3.2, Этап 4).

    Семантика полей:

    * ``node_id``: стабильный идентификатор вида ``"n_0001"``.
    * ``node_type``: ``"section"`` | ``"body"`` | ``"table"`` | ``"list"`` |
        ``"list_item"`` | ``"caption"`` | ``"title"`` | ``"preamble"`` |
        ``"metadata"``.
    * ``semantic_type``: например ``"article"``, ``"clause"``,
        ``"subsection"``, ``"chapter"`` — уточняет ``node_type``.
        ``None`` для non-section типов.
    * ``level``: nesting level (0 для root).
    * ``title``: heading / caption text. ``""`` для root и body.
    * ``number``: ``NumberingInfo`` или ``None``.
    * ``parent_id``: node_id родителя или ``None``.
      **Semantic hierarchy** — указывает на parent node в логическом
      дереве (статья → глава → раздел → root). Не зависит от range.
    * ``children``: tuple of child node_id.
    * ``start_block`` / ``end_block``: ordinals ``DocumentBlock`` в
      canonical document order. **Direct physical block ownership** —
      диапазон блоков, непосредственно принадлежащих узлу
      (``start_block == c.block_index``, ``end_block == next-1`` или
      ``total_blocks - 1``). Не вычисляется как subtree range.
    * ``confidence``: 0..1.
    * ``evidence``: tuple of ``StructureEvidence``.
    * ``source_refs``: tuple of provenance markers (например,
        ``("pdf_outline",)`` для outline-derived nodes).

    Все поля frozen: ``StructureNode`` — immutable.

    Invariant (Этап 4): ``start_block == end_block`` допустим — это
    валидная одно-блочная секция. ``parent_id`` НЕ обязан покрывать
    range ребёнка (subtree ≠ parent range).

    """

    node_id: str
    node_type: str
    semantic_type: str | None
    level: int
    title: str
    number: NumberingInfo | None
    parent_id: str | None
    children: tuple[str, ...]
    start_block: int
    end_block: int
    confidence: float
    evidence: tuple[StructureEvidence, ...] = ()
    source_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "semantic_type": self.semantic_type,
            "level": self.level,
            "title": self.title,
            "number": (
                {
                    "raw": self.number.raw,
                    "scheme": self.number.scheme,
                    "components": list(self.number.components),
                    "level": self.number.level,
                    "ordinal": self.number.ordinal,
                }
                if self.number is not None
                else None
            ),
            "parent_id": self.parent_id,
            "children": list(self.children),
            "start_block": self.start_block,
            "end_block": self.end_block,
            "confidence": self.confidence,
            "evidence": [
                {"source": e.source, "weight": e.weight, "detail": e.detail}
                for e in self.evidence
            ],
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class DocumentStructure:
    """Canonical DocumentStructure (PLAN §3.2, §8).

    Attributes:
        document_id: идентификатор документа (``DocumentIdentity.document_id``).
        title: ``DocumentTitle`` или ``None``.
        nodes: dict ``node_id → StructureNode``.
        root_id: node_id корневого узла.
        preamble_node_id: node_id блока preamble (или ``root_id``).
        numbering: tuple всех ``NumberingInfo`` (для diagnostics).
        total_blocks: ``len(PhysicalDocument.blocks)``.
        coverage_ratio: доля significant blocks, покрытых nodes
            (для ``StructureValidator``).
    """

    document_id: str
    title: DocumentTitle | None
    nodes: dict[str, StructureNode]
    root_id: str
    preamble_node_id: str
    numbering: tuple[NumberingInfo, ...]
    total_blocks: int
    coverage_ratio: float = 0.0

    def get_node(self, node_id: str) -> StructureNode | None:
        return self.nodes.get(node_id)

    def iter_nodes(self) -> list[StructureNode]:
        """Все ноды в document order (по ``start_block``)."""
        return sorted(self.nodes.values(), key=lambda n: (n.start_block, n.level))

    def iter_sections(self) -> list[StructureNode]:
        """Только section-узлы (исключая body / table / list_item)."""
        return [
            n for n in self.iter_nodes()
            if n.node_type == "section"
        ]

    def iter_children(self, parent_id: str) -> list[StructureNode]:
        parent = self.nodes.get(parent_id)
        if parent is None:
            return []
        return [self.nodes[cid] for cid in parent.children if cid in self.nodes]

    def block_to_node(self) -> dict[int, str]:
        """Mapping ``ordinal DocumentBlock`` → ``node_id`` (PLAN §3, §45, Этап 5).

        Делегирует ``block_ownership.block_to_node`` — единственному
        каноническому механизму определения ownership. Возвращает
        словарь для **всех** блоков ``[0, total_blocks)``, где
        непокрытые блоки (например, root preamble) → ``root_id``.
        """
        from workspace.skills.legal_summarizer.scripts.structure.block_ownership import (
            block_to_node as _canonical_block_to_node,
        )
        return _canonical_block_to_node(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": (
                {
                    "value": self.title.value,
                    "source": self.title.source,
                    "confidence": self.title.confidence,
                    "block_ordinal": self.title.block_ordinal,
                }
                if self.title is not None
                else None
            ),
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "root_id": self.root_id,
            "preamble_node_id": self.preamble_node_id,
            "numbering": [
                {
                    "raw": ni.raw,
                    "scheme": ni.scheme,
                    "components": list(ni.components),
                    "level": ni.level,
                    "ordinal": ni.ordinal,
                }
                for ni in self.numbering
            ],
            "total_blocks": self.total_blocks,
            "coverage_ratio": self.coverage_ratio,
        }


__all__ = [
    "DocumentStructure",
    "StructureNode",
    "StructureEvidence",
    "NumberingInfo",
    "DocumentTitle",
]


# Конвенция node_id (для generator ниже).
def _make_node_id(counter: int) -> str:
    return f"n_{counter:04d}"