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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StructureNode":
        """Обратная сериализация для ``to_dict``.

        Используется при восстановлении ``DocumentStructure`` из
        document-level cache. Допускает отсутствие опциональных полей
        (``evidence``, ``source_refs``) — для них берутся dataclass defaults.
        """
        number_raw = data.get("number")
        number: NumberingInfo | None = None
        if number_raw is not None:
            components = tuple(number_raw.get("components", ()))
            number = NumberingInfo(
                raw=str(number_raw.get("raw", "")),
                scheme=str(number_raw.get("scheme", "")),
                components=components,
                level=int(number_raw.get("level", 0)),
                ordinal=(
                    int(number_raw["ordinal"])
                    if number_raw.get("ordinal") is not None
                    else None
                ),
            )

        evidence_raw = data.get("evidence") or []
        evidence = tuple(
            StructureEvidence(
                source=str(e.get("source", "")),
                weight=float(e.get("weight", 0.0)),
                detail=str(e.get("detail", "")),
            )
            for e in evidence_raw
        )

        return cls(
            node_id=str(data["node_id"]),
            node_type=str(data["node_type"]),
            semantic_type=(
                str(data["semantic_type"])
                if data.get("semantic_type") is not None
                else None
            ),
            level=int(data["level"]),
            title=str(data.get("title", "")),
            number=number,
            parent_id=(
                str(data["parent_id"])
                if data.get("parent_id") is not None
                else None
            ),
            children=tuple(str(c) for c in data.get("children", ())),
            start_block=int(data["start_block"]),
            end_block=int(data["end_block"]),
            confidence=float(data.get("confidence", 1.0)),
            evidence=evidence,
            source_refs=tuple(str(s) for s in data.get("source_refs", ())),
        )


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
        """Mapping ``ordinal DocumentBlock`` → ``node_id``.

        Этот метод — единая точка для consumers, которым нужно
        ``block → node_id``. Реализация делегирует ``owner_for_block``
        и заполняет весь диапазон ``[0, total_blocks)`` (с root fallback).

        Возвращает словарь для **всех** блоков ``[0, total_blocks)``,
        где непокрытые блоки (например, root preamble) → ``root_id``.
        """
        ownership = build_block_ownership(self)
        return {
            b: ownership.get(b, self.root_id)
            for b in range(self.total_blocks)
        }

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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DocumentStructure":
        """Обратная сериализация для ``to_dict``.

        Используется при восстановлении ``DocumentAnalysis`` из
        document-level cache. Сохраняет равенство
        ``nodes.keys() == data["nodes"].keys()`` и ``root_id`` /
        ``preamble_node_id`` валидность.
        """
        title_raw = data.get("title")
        title_obj: DocumentTitle | None = None
        if title_raw is not None:
            title_obj = DocumentTitle(
                value=str(title_raw.get("value", "")),
                source=str(title_raw.get("source", "")),
                confidence=float(title_raw.get("confidence", 0.0)),
                block_ordinal=(
                    int(title_raw["block_ordinal"])
                    if title_raw.get("block_ordinal") is not None
                    else None
                ),
            )

        nodes_raw = data.get("nodes") or {}
        nodes = {nid: StructureNode.from_dict(n) for nid, n in nodes_raw.items()}

        numbering_raw = data.get("numbering") or []
        numbering = tuple(
            NumberingInfo(
                raw=str(ni.get("raw", "")),
                scheme=str(ni.get("scheme", "")),
                components=tuple(ni.get("components", ())),
                level=int(ni.get("level", 0)),
                ordinal=(
                    int(ni["ordinal"])
                    if ni.get("ordinal") is not None
                    else None
                ),
            )
            for ni in numbering_raw
        )

        return cls(
            document_id=str(data["document_id"]),
            title=title_obj,
            nodes=nodes,
            root_id=str(data["root_id"]),
            preamble_node_id=str(data["preamble_node_id"]),
            numbering=numbering,
            total_blocks=int(data["total_blocks"]),
            coverage_ratio=float(data.get("coverage_ratio", 0.0)),
        )


# ---------------------------------------------------------------------------
# Block ownership (formerly ``domain/structure.py``)
# ---------------------------------------------------------------------------
#
# Чистые функции на ``DocumentStructure``, которые определяют
# «какой section node владеет данным ``DocumentBlock``». Это **document
# API**: использует только поля ``DocumentStructure`` (nodes, root_id,
# total_blocks) и не зависит ни от каких downstream subsystems.
#
# Семантика:
#
# * каждый block принадлежит **ровно одному** section node — самому
#   глубокому (deepest), чей диапазон ``[start_block, end_block]``
#   содержит block. Это решает проблему двойного ownership между
#   parent и child nodes.
# * blocks вне section ranges принадлежат root preamble
#   (``struct.root_id``).
# * blocks с ordinal вне ``[0, total_blocks)`` — невалидны, возвращают
#   ``None``.


def _depth_of(node_id: str, struct: DocumentStructure) -> int:
    """Глубина узла в дереве (root = 0)."""
    depth = 0
    cur = struct.nodes.get(node_id)
    while cur is not None and cur.parent_id is not None:
        depth += 1
        cur = struct.nodes.get(cur.parent_id)
    return depth


def build_block_ownership(
    struct: DocumentStructure,
) -> dict[int, str]:
    """Построить ``block_ordinal → owning_section_node_id``.

    Каждый block, попадающий в диапазон какого-либо section, принадлежит
    **самому глубокому** section, чей диапазон его содержит. Это даёт
    ровно одного owner на block.

    Returns:
        ``dict[block_ordinal, owning_section_node_id]``. Blocks вне
        section ranges отсутствуют в dict (см. ``owner_for_block`` для
        fallback на ``struct.root_id``).
    """
    candidates = [n for n in struct.nodes.values() if n.node_type == "section"]
    candidates.sort(key=lambda n: _depth_of(n.node_id, struct), reverse=True)

    owner: dict[int, str] = {}
    for node in candidates:
        for b in range(node.start_block, node.end_block + 1):
            owner.setdefault(b, node.node_id)
    return owner


def owner_for_block(
    struct: DocumentStructure,
    ordinal: int,
    ownership: dict[int, str] | None = None,
) -> str | None:
    """Получить owner для block ``ordinal``.

    Returns:
        - ``owning_section_node_id`` если block принадлежит section;
        - ``struct.root_id`` если block не принадлежит ни одной section
          (preamble / orphan block);
        - ``None`` если ``ordinal`` вне диапазона документа.

    Args:
        struct: ``DocumentStructure``.
        ordinal: ``DocumentBlock.ordinal``.
        ownership: предвычисленная ``build_block_ownership(struct)``.
            Если ``None`` — будет построена на лету.
    """
    if ordinal < 0 or ordinal >= struct.total_blocks:
        return None
    if ownership is None:
        ownership = build_block_ownership(struct)
    return ownership.get(ordinal, struct.root_id)


def block_to_node(struct: DocumentStructure) -> dict[int, str]:
    """Mapping ``block_ordinal → node_id``.

    Этот метод — единая точка для consumers, которым нужно
    ``block → node_id``. Реализация делегирует ``owner_for_block``
    и заполняет весь диапазон ``[0, total_blocks)`` (с root fallback).
    """
    ownership = build_block_ownership(struct)
    return {
        b: ownership.get(b, struct.root_id)
        for b in range(struct.total_blocks)
    }


__all__ = [
    "DocumentStructure",
    "StructureNode",
    "StructureEvidence",
    "NumberingInfo",
    "DocumentTitle",
    "build_block_ownership",
    "owner_for_block",
    "block_to_node",
]


# Конвенция node_id (для generator ниже).
def _make_node_id(counter: int) -> str:
    return f"n_{counter:04d}"
