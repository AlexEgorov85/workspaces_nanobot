"""Context expansion для follow-up retrieval.

Этот модуль — детерминированный bounded window вокруг найденного
target fragment в ``PhysicalDocument``. Без embeddings, без graph
retrieval.

Стратегия:

* Обычный block: target + 1 предыдущий + 1 следующий.
* Heading block: только целевой + следующие paragraphs (heading не нуждается
  в контексте «назад»).
* Table block: целевая таблица + heading/paragraph before + paragraph after.

Размер ограничен ``max_total_chars`` (default 8000).

API:
    * :func:`expand_followup_context` — ``(target_ordinal, doc, target_source_text)``
      → ``ExpandedContext`` с blocks, bounded-флагом, total_chars, **target provenance**.

Invariants:
    * ``target_ordinal`` — это ``DocumentBlock.ordinal`` (identity), **не**
      индекс массива ``doc.blocks``. Находим target через ``blocks_by_ord``.
      Это позволяет корректно работать даже если в ``doc.blocks`` ordinal'ы
      не идут подряд (фильтрация/удаление блоков upstream'ом).
    * Возвращаемый :class:`ExpandedContext` сохраняет ``target_ordinal``,
      ``target_source_text``, ``target_source_char_start/end``, чтобы
      caller мог восстановить **точный primary target** даже после
      expansion (для claim-level citations).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)


@dataclass(frozen=True)
class ExpandedContext:
    """Результат :func:`expand_followup_context`.

    Attributes:
        blocks: list of ``(block_ordinal, block_text)`` в document order.
        bounded: bool — был ли расширен до лимита.
        total_chars: фактический размер.
        target_ordinal: ordinal primary target block'а.
        target_source_text: точный исходный текст target (split-aware).
        target_source_char_start / target_source_char_end: offsets target'а
            **внутри** ``doc.blocks_by_ord[target_ordinal].content`` для
            split-chunk case. ``None / None`` для whole-block target.
        target_block_type: ``"paragraph"`` / ``"table"`` / etc.
        source_spans: tuple ``(block_ordinal, char_start, char_end,
            is_target_marker)`` для каждого block'а в ``blocks``. Primary
            target помечен ``is_target_marker=1``, соседи — ``0``.
    """

    blocks: tuple[tuple[int, str], ...]
    bounded: bool
    total_chars: int
    target_ordinal: int
    target_source_text: str
    target_source_char_start: int | None
    target_source_char_end: int | None
    target_block_type: str
    source_spans: tuple[tuple[int, int, int | None, int | None, int], ...]


def _is_heading(b: DocumentBlock) -> bool:
    """Признак heading-блока (по текущей схеме)."""
    if b.block_type != "paragraph":
        return False
    style = (b.block_metadata or {}).get("style", "") or ""
    if "heading" in style.lower() or "Heading" in style:
        return True
    text = (b.content or "").strip()
    if not text:
        return False
    return len(text) <= 80 and (
        text[:1].isdigit() or text.lower().startswith(("статья", "раздел", "глава"))
    )


def _is_table(b: DocumentBlock) -> bool:
    return b.block_type == "table"


def _normalize_blocks(
    raw_blocks: list[tuple[DocumentBlock, str | None]],
) -> list[tuple[int, str, DocumentBlock]]:
    """Превратить (block, target_source_text_or_None) в list[(ordinal, text, block)].

    ``target_source_text_or_None`` — override для target block'а
    (split-aware exact text). Если None — используется ``block.content``.
    """
    out: list[tuple[int, str, DocumentBlock]] = []
    for b, override in raw_blocks:
        text = override if override is not None else b.content
        out.append((b.ordinal, text, b))
    return out


def _enforce_budget(
    selected: list[tuple[int, str, DocumentBlock]],
    target_ordinal: int,
    max_total_chars: int,
) -> tuple[list[tuple[int, str, DocumentBlock]], bool]:
    """Ограничить суммарный размер; target обязателен."""
    if not selected:
        return [], False
    bounded = False
    total = sum(len(t) for _, t, _ in selected)
    if total <= max_total_chars:
        return selected, bounded

    bounded = True
    target_entry = next(
        (e for e in selected if e[0] == target_ordinal),
        selected[0],
    )
    new_selected = [target_entry]
    running = len(target_entry[1])
    target_pos = next(
        (i for i, e in enumerate(selected) if e[0] == target_ordinal),
        0,
    )
    for i in range(target_pos + 1, len(selected)):
        entry = selected[i]
        if running + len(entry[1]) > max_total_chars:
            break
        new_selected.append(entry)
        running += len(entry[1])
    prev_items: list[tuple[int, str, DocumentBlock]] = []
    for i in range(target_pos - 1, -1, -1):
        entry = selected[i]
        if running + len(entry[1]) > max_total_chars:
            break
        prev_items.append(entry)
        running += len(entry[1])
    new_selected = list(reversed(prev_items)) + new_selected
    return new_selected, bounded


def expand_followup_context(
    *,
    target_ordinal: int,
    doc: PhysicalDocument,
    target_source_text: str | None = None,
    target_source_char_start: int | None = None,
    target_source_char_end: int | None = None,
    neighbor_count: int = 1,
    max_total_chars: int = 8000,
) -> ExpandedContext:
    """Расширить контекст вокруг целевого block'а в документе.

    Args:
        target_ordinal: ordinal целевого ``DocumentBlock``. **Не** индекс в
            массиве ``doc.blocks`` — это identity, ищущий через
            ``{b.ordinal: b for b in doc.blocks}``. Любая фильтрация/
            пропуск блоков в upstream не сломает navigation.
        doc: ``PhysicalDocument``.
        target_source_text: уже восстановленный точный текст целевого
            chunk'а (если split chunk — ``block.content[start:end]``).
            Если ``None`` — используется ``block.content`` полностью.
        target_source_char_start / target_source_char_end: offsets
            target_source_text внутри ``doc.blocks_by_ord[target_ordinal].content``
            (для split chunk case). ``None / None`` — для whole-block
            target.
        neighbor_count: число соседей с каждой стороны (default 1).
        max_total_chars: верхняя граница суммарного контекста.

    Returns:
        :class:`ExpandedContext` (см. dataclass).
    """
    blocks_by_ord: dict[int, DocumentBlock] = {b.ordinal: b for b in doc.blocks}
    if target_ordinal not in blocks_by_ord:
        raise ValueError(
            f"target_ordinal={target_ordinal} вне диапазона известных ordinals "
            f"({sorted(blocks_by_ord)})"
        )

    target = blocks_by_ord[target_ordinal]
    is_table = _is_table(target)
    is_heading = _is_heading(target)

    pos_target = doc.blocks.index(target)
    selected: list[tuple[DocumentBlock, str | None]] = []

    if is_heading:
        selected.append((target, target_source_text))
        for j in range(pos_target + 1, min(pos_target + 1 + neighbor_count * 2, len(doc.blocks))):
            nxt = doc.blocks[j]
            if _is_table(nxt):
                continue
            selected.append((nxt, None))
    elif is_table:
        if pos_target > 0:
            prev = doc.blocks[pos_target - 1]
            selected.append((prev, None))
        selected.append((target, target_source_text))
        if pos_target + 1 < len(doc.blocks):
            nxt = doc.blocks[pos_target + 1]
            if not _is_table(nxt):
                selected.append((nxt, None))
    else:
        if pos_target > 0:
            prev = doc.blocks[pos_target - 1]
            if not _is_table(prev):
                selected.append((prev, None))
        selected.append((target, target_source_text))
        for j in range(
            pos_target + 1,
            min(pos_target + 1 + neighbor_count, len(doc.blocks)),
        ):
            nxt = doc.blocks[j]
            if _is_table(nxt):
                continue
            selected.append((nxt, None))
            break

    normalized = _normalize_blocks(selected)
    bounded_normalized, bounded = _enforce_budget(normalized, target_ordinal, max_total_chars)

    source_spans: list[tuple[int, int, int | None, int | None, int]] = []
    for ordinal, _text, block in bounded_normalized:
        if ordinal == target_ordinal:
            cs = target_source_char_start if target_source_char_start is not None else None
            ce = target_source_char_end if target_source_char_end is not None else None
            source_spans.append((ordinal, 0 if cs is None else cs, len(block.content) if ce is None else ce, cs, ce, 1))
        else:
            source_spans.append((ordinal, 0, len(block.content), 0, len(block.content), 0))

    final_target_text = target_source_text if target_source_text is not None else target.content
    return ExpandedContext(
        blocks=tuple((o, t) for o, t, _b in bounded_normalized),
        bounded=bounded,
        total_chars=sum(len(t) for _, t, _b in bounded_normalized),
        target_ordinal=target_ordinal,
        target_source_text=final_target_text,
        target_source_char_start=target_source_char_start,
        target_source_char_end=target_source_char_end,
        target_block_type=target.block_type,
        source_spans=tuple(source_spans),
    )


def expanded_context_to_dict(ctx: ExpandedContext) -> dict[str, Any]:
    """Сериализация в dict для backward-compat с прежним API consumers."""
    return {
        "blocks": list(ctx.blocks),
        "bounded": ctx.bounded,
        "total_chars": ctx.total_chars,
        "target_ordinal": ctx.target_ordinal,
        "target_source_text": ctx.target_source_text,
        "target_source_char_start": ctx.target_source_char_start,
        "target_source_char_end": ctx.target_source_char_end,
        "target_block_type": ctx.target_block_type,
        "source_spans": [
            {
                "block_ordinal": o,
                "char_start": cs,
                "char_end": ce,
                "source_char_start": scs,
                "source_char_end": sce,
                "is_target": bool(marker),
            }
            for (o, cs, ce, scs, sce, marker) in ctx.source_spans
        ],
    }


__all__ = ["expand_followup_context", "ExpandedContext", "expanded_context_to_dict"]
