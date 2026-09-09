"""BriefContextBuilder: один структурный Chunk для всего документа.

BRIEF CONTRACT: один документ → ровно один Chunk.

Brief не является выборкой canonical chunks. Brief является компактным
структурным представлением всего документа, собранным из
``DocumentStructure`` и ``PhysicalDocument`` напрямую (а не из
``analysis.chunks``).

Архитектурные инварианты (см. план brief-refactor §0-§30):

* ровно один ``Chunk`` (тип возврата — ``Chunk``, не ``list[Chunk]``);
* никакого повторного parsing PDF/DOCX/TXT;
* никакого повторного построения ``DocumentStructure``;
* никакого LLM для определения структуры;
* никакой привязки к словам «Раздел»/«Глава»/«Статья»;
* таблицы передаются атомарно (целиком в рамках секции);
* при нехватке budget текст секций сокращается, но секции
  целиком НЕ удаляются (п.13).

Расчёт ``max_chars`` (п.18) — динамический от контекстного окна модели::

    max_chars = agents.defaults.contextWindowTokens
              * chunking.brief_input_ratio
              * chars_per_token

Источник ``contextWindowTokens`` — ``config.json`` (поле
``agents.defaults.contextWindowTokens``); резолвится тем же путём,
что и ``ChunkPlanner.build_chunk_config_from_runtime``. Если
контекстное окно недоступно — fallback на ``BriefContextConfig.max_chars_fallback``.

Структура итогового chunk.text::

    DOCUMENT STRUCTURE

    <title>
    ├── <node>
    │   ├── <child>
    │   └── <child>
    ├── <node>
    └── <node>

    DOCUMENT CONTENT

    [Preamble]

    <text>

    [Section 1]

    <text>

    [Section 2]

    <text>
"""

from __future__ import annotations

from dataclasses import dataclass

from chunking.chunks import Chunk
from chunking.chunker import _make_chunk_id as _canonical_chunk_id
from document.analysis import DocumentAnalysis
from document.physical import DocumentBlock
from document.structure import (
    DocumentStructure,
    StructureNode,
)

from application.brief_compression import (
    BriefSection,
    render_sections,
)


_MEANINGFUL_NODE_TYPES = frozenset({
    "section",
    "body",
    "table",
    "list",
    "list_item",
    "caption",
    "title",
    "preamble",
})


@dataclass(frozen=True)
class BriefContextConfig:
    """Параметры BriefContextBuilder.

    Attributes:
        max_chars_fallback: жёсткий потолок ``chunk.text``, используемый
            только если контекстное окно модели недоступно
            (см. ``resolve_max_chars``).
        input_ratio: доля контекстного окна под итоговый brief-chunk
            (0 < r < 1). Если ``None`` — использован fallback.
        chars_per_token: оценка токенов для русского текста
            (используется в ``Chunk.token_estimate`` и для перевода
            ``max_chars`` в токены).
        structure_max_chars: предел на блок DOCUMENT STRUCTURE.
            Используется только если структура документа превышает
            этот порог (чтобы оставить budget для контента).
    """

    max_chars_fallback: int = 30000
    input_ratio: float | None = 0.13
    chars_per_token: float = 3.5
    structure_max_chars: int = 12000


def _resolve_context_window_tokens() -> int | None:
    """Прочитать ``contextWindowTokens`` из SETTINGS.

    Источник: ``config.json::agents.defaults.contextWindowTokens`` (или
    override в ``project.json`` через ``agents.defaults.contextWindowTokens``).
    Тот же путь, что и в ``application.pipeline_structure._read_context_window_tokens``.
    """
    try:
        from config import SETTINGS
    except Exception:
        return None
    try:
        raw = (
            SETTINGS.get("agents", {})
            .get("defaults", {})
            .get("contextWindowTokens")
        )
    except Exception:
        return None
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def resolve_max_chars(config: BriefContextConfig) -> int:
    """Динамически рассчитать ``max_chars`` (п.18).

    Формула:
        ``max_chars = contextWindowTokens * input_ratio * chars_per_token``

    Fallback на ``config.max_chars_fallback``, если:
    * ``contextWindowTokens`` не задан в конфиге;
    * ``input_ratio`` не задан (``None``);
    * результат получается неположительным.
    """
    cwt = _resolve_context_window_tokens()
    if cwt is None or config.input_ratio is None:
        return max(1, config.max_chars_fallback)
    if config.input_ratio <= 0:
        return max(1, config.max_chars_fallback)
    chars = int(cwt * float(config.input_ratio) * config.chars_per_token)
    return chars if chars > 0 else max(1, config.max_chars_fallback)


def _node_label(node: StructureNode) -> str:
    """Человекочитаемый label структурного узла для outline."""
    if node.title:
        return f"[{node.title}]"
    return f"[<{node.node_type}>]"


def _collect_subtree_ordinals(
    node: StructureNode,
    struct: DocumentStructure,
) -> list[int]:
    """Собрать physical ordinals всех блоков в subtree ``node``.

    Обход в pre-order по ``children``. Для каждого узла добавляются
    блоки его собственного диапазона ``[start_block, end_block]``,
    не пересекающиеся с уже собранными (исключает дубликаты при
    overlapping ranges, п.29).
    """
    used: set[int] = set()
    result: list[int] = []

    def visit(n: StructureNode) -> None:
        for b in range(n.start_block, n.end_block + 1):
            if b in used:
                continue
            used.add(b)
            result.append(b)
        for cid in n.children:
            child = struct.nodes.get(cid)
            if child is None:
                continue
            visit(child)

    visit(node)
    return result


def _render_outline(
    struct: DocumentStructure,
    *,
    max_chars: int,
) -> str:
    """DOCUMENT STRUCTURE: рекурсивный outline всех значимых узлов.

    Не ограничивает глубину — структура должна быть максимально
    информативной для LLM, и имеет собственный hard budget.
    """
    lines: list[str] = []

    title = struct.title.value if struct.title is not None else None
    if title:
        lines.append(title)

    root = struct.nodes.get(struct.root_id)
    if root is None:
        return "\n".join(lines[:max_chars])

    def render_node(node: StructureNode, *, prefix: str, is_last: bool) -> None:
        label = _node_label(node)
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{label}")
        new_prefix = prefix + ("    " if is_last else "│   ")
        children_ids = [
            cid for cid in node.children
            if struct.nodes.get(cid) is not None
            and struct.nodes[cid].node_type in _MEANINGFUL_NODE_TYPES
        ]
        for i, cid in enumerate(children_ids):
            child = struct.nodes[cid]
            if child is None:
                continue
            render_node(
                child, prefix=new_prefix, is_last=(i == len(children_ids) - 1),
            )

    children_ids = [
        cid for cid in root.children
        if struct.nodes.get(cid) is not None
        and struct.nodes[cid].node_type in _MEANINGFUL_NODE_TYPES
    ]
    if not children_ids:
        return _trim_structure("\n".join(lines), max_chars=max_chars)

    for i, cid in enumerate(children_ids):
        child = struct.nodes[cid]
        if child is None:
            continue
        render_node(
            child, prefix="", is_last=(i == len(children_ids) - 1),
        )

    return _trim_structure("\n".join(lines), max_chars=max_chars)


def _trim_structure(text: str, *, max_chars: int) -> str:
    """Обрезать outline до ``max_chars`` по newline-boundary."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    return text[:cut]


def _select_meaningful_level(
    struct: DocumentStructure,
) -> list[StructureNode]:
    """Определить верхний содержательный уровень для brief.

    Алгоритм (п.6):

    1. Непосредственные дети root, имеющие ``node_type`` ∈ MEANINGFUL.
    2. Если root содержит только служебные/неинформативные узлы —
       идём глубже через первый meaningful child.

    Не делаем специальной обработки «Раздел»/«Глава» — алгоритм
    работает с общей структурой и опирается на ``node_type``.
    """
    root = struct.nodes.get(struct.root_id)
    if root is None:
        return []

    def _children_of(node: StructureNode) -> list[StructureNode]:
        out: list[StructureNode] = []
        for cid in node.children:
            child = struct.nodes.get(cid)
            if child is None:
                continue
            if child.node_type in _MEANINGFUL_NODE_TYPES:
                out.append(child)
        return out

    current = root
    while True:
        kids = _children_of(current)
        if kids:
            return sorted(kids, key=lambda n: (n.start_block, n.level))
        if current.children:
            nxt = struct.nodes.get(current.children[0])
            if nxt is None or nxt is current:
                return []
            current = nxt
            continue
        return []


def _preamble_ordinals(
    struct: DocumentStructure,
    top_level_nodes: list[StructureNode],
) -> list[int]:
    """Physical ordinals, попадающие в preamble (до первого top-level узла).

    Preamble = всё, что между ``0`` и ``min(start_block по top-level)``.
    Если top-level нет — пустой список.
    """
    if not top_level_nodes:
        return []
    first_start = min(n.start_block for n in top_level_nodes)
    if first_start <= 0:
        return []
    return list(range(0, first_start))


def _render_section_content(
    node: StructureNode,
    struct: DocumentStructure,
    blocks_by_ord: dict[int, DocumentBlock],
) -> str:
    """Собрать весь текст subtree ``node`` в document order.

    Таблицы (block_type == 'table') включаются атомарно (п.10): одна
    строка ``DocumentBlock.content`` попадает в результат целиком или
    не попадает вообще (только если блок пустой — пропускается).
    """
    ordinals = _collect_subtree_ordinals(node, struct)
    parts: list[str] = []
    for ordinal in ordinals:
        block = blocks_by_ord.get(ordinal)
        if block is None:
            continue
        if not block.content.strip():
            continue
        parts.append(block.content)
    return "\n\n".join(parts)


def build_brief_chunk(
    analysis: DocumentAnalysis,
    *,
    config: BriefContextConfig | None = None,
) -> Chunk:
    """Построить ровно один ``Chunk`` — компактное структурное
    представление всего документа.

    Args:
        analysis: ``DocumentAnalysis`` (cache of ``PhysicalDocument`` +
            ``DocumentStructure`` + canonical chunks — здесь
            ``chunks`` **не** используется).
        config: ``BriefContextConfig``. Если ``None`` — дефолтный
            (``max_chars_fallback=30000``, ``input_ratio=0.13``,
            ``chars_per_token=3.5``, ``structure_max_chars=12000``).

    Returns:
        ``Chunk`` с ``chunk_id="001"``, ``index=0``,
        ``char_count <= resolve_max_chars(config)``.

    Raises:
        ValueError: если в PhysicalDocument найдены повторяющиеся или
            вне-диапазонные ordinals (п.29).
    """
    cfg = config or BriefContextConfig()
    max_chars = resolve_max_chars(cfg)

    physical = analysis.physical
    struct = analysis.structure
    blocks_by_ord = physical.blocks_by_ord

    _validate_blocks(blocks_by_ord, expected_total=struct.total_blocks)

    section_outline = _render_outline(
        struct, max_chars=min(cfg.structure_max_chars, max_chars),
    )

    top_level = _select_meaningful_level(struct)

    section_blocks: list[BriefSection] = []
    used_ordinals: list[int] = []

    preamble_ordinals = _preamble_ordinals(struct, top_level)
    if preamble_ordinals:
        preamble_parts: list[str] = []
        for ordinal in preamble_ordinals:
            block = blocks_by_ord.get(ordinal)
            if block is None or not block.content.strip():
                continue
            preamble_parts.append(block.content)
            used_ordinals.append(ordinal)
        if preamble_parts:
            section_blocks.append(BriefSection(
                heading="[Preamble]",
                text="\n\n".join(preamble_parts),
            ))

    for node in top_level:
        node_text = _render_section_content(node, struct, blocks_by_ord)
        heading = node.title or f"[{node.node_type}]"
        section_blocks.append(BriefSection(
            heading=f"[{heading}]",
            text=node_text,
        ))
        for ordinal in _collect_subtree_ordinals(node, struct):
            if ordinal in used_ordinals:
                continue
            used_ordinals.append(ordinal)

    content_budget = max(
        0, max_chars - len(section_outline) - len("\n\nDOCUMENT CONTENT\n\n"),
    )
    rendered_content = render_sections(
        section_blocks,
        available_chars=content_budget,
    )

    parts: list[str] = []
    if section_outline:
        parts.append("DOCUMENT STRUCTURE\n\n" + section_outline)
    if rendered_content:
        parts.append("DOCUMENT CONTENT\n\n" + rendered_content)

    text = "\n\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()

    chunk_id = _canonical_chunk_id(1)
    token_est = max(1, len(text) // max(1, int(cfg.chars_per_token)))

    used_ordinals_unique = tuple(
        sorted({o for o in used_ordinals if blocks_by_ord.get(o) is not None}),
    )
    block_types = tuple(
        blocks_by_ord[o].block_type for o in used_ordinals_unique
    )

    section_ids = tuple(
        n.node_id for n in top_level if n.node_type == "section"
    )

    primary_section_id = top_level[0].node_id if top_level else struct.root_id
    primary_heading = top_level[0].title if top_level else ""

    return Chunk(
        chunk_id=chunk_id,
        index=0,
        text=text,
        char_count=len(text),
        token_estimate=token_est,
        page_start=None,
        page_end=None,
        section_id=primary_section_id,
        section_path="",
        section_heading=primary_heading,
        block_indices=used_ordinals_unique,
        block_types=block_types,
        section_ids=section_ids,
    )


def _validate_blocks(
    blocks_by_ord: dict[int, DocumentBlock],
    *,
    expected_total: int,
) -> None:
    """П.29: ordinal существует, не повторяется, идёт в document order.

    Здесь проверяем: каждый ordinal в ``[0, expected_total)`` встречается
    не более одного раза и не выходит за границы документа.
    ``DocumentBlock.ordinal`` в текущей реализации invariant
    ``blocks[i].ordinal == i`` уже гарантирован upstream
    (``PhysicalDocument.from_path`` + ``DocumentLoader``); мы делаем
    только защитную проверку на отсутствие дыр.
    """
    seen: set[int] = set()
    for ordinal in blocks_by_ord.keys():
        if ordinal < 0 or ordinal >= expected_total:
            raise ValueError(
                f"BriefContextBuilder: ordinal={ordinal} вне диапазона "
                f"[0, {expected_total})",
            )
        if ordinal in seen:
            raise ValueError(
                f"BriefContextBuilder: повторяющийся ordinal={ordinal}",
            )
        seen.add(ordinal)
    expected = set(range(expected_total))
    missing = expected - seen
    if missing:
        raise ValueError(
            "BriefContextBuilder: отсутствуют ordinals "
            f"{sorted(missing)[:5]}{'...' if len(missing) > 5 else ''}",
        )


__all__ = [
    "BriefContextConfig",
    "build_brief_chunk",
    "resolve_max_chars",
]
