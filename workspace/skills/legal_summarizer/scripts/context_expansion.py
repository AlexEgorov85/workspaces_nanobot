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
    * :func:`expand_followup_context` — ``(target_text, doc, target_block_idx)``
      → ``{blocks: list[str], bounded: bool}``.
"""

from __future__ import annotations

from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)


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


def expand_followup_context(
    *,
    target_block_index: int,
    doc: PhysicalDocument,
    target_source_text: str | None = None,
    neighbor_count: int = 1,
    max_total_chars: int = 8000,
) -> dict[str, Any]:
    """Расширить контекст вокруг целевого block'а в документе.

    Args:
        target_block_index: ordinal целевого ``DocumentBlock`` в ``doc.blocks``.
        doc: ``PhysicalDocument``.
        target_source_text: уже восстановленный точный текст целевого chunk'а
            (если None — используется полный ``doc.blocks[target].content``).
        neighbor_count: число соседей с каждой стороны (default 1).
        max_total_chars: верхняя граница суммарного контекста.

    Returns:
        ``dict``:
            * ``blocks``: list of ``(block_index, block_text)`` в document order.
            * ``bounded``: bool — был ли расширен до лимита.
            * ``total_chars``: фактический размер.
    """
    blocks = doc.blocks
    if target_block_index < 0 or target_block_index >= len(blocks):
        raise ValueError(
            f"target_block_index={target_block_index} вне диапазона [0, {len(blocks)})"
        )

    target = blocks[target_block_index]
    is_table = _is_table(target)
    is_heading = _is_heading(target)

    selected: list[tuple[int, str]] = []
    bounded = False

    if is_heading:
        # Heading: target + следующие paragraphs (heading сам обозначает
        # начало секции).
        selected.append((target.ordinal, target.content if target_source_text is None else target_source_text))
        for j in range(target_block_index + 1, min(target_block_index + 1 + neighbor_count * 2, len(blocks))):
            nxt = blocks[j]
            if _is_table(nxt):
                continue
            selected.append((nxt.ordinal, nxt.content))
    elif is_table:
        if target_block_index > 0:
            prev = blocks[target_block_index - 1]
            selected.append((prev.ordinal, prev.content))
        selected.append((target.ordinal, target.content))
        if target_block_index + 1 < len(blocks):
            nxt = blocks[target_block_index + 1]
            if not _is_table(nxt):
                selected.append((nxt.ordinal, nxt.content))
    else:
        if target_block_index > 0:
            prev = blocks[target_block_index - 1]
            if not _is_table(prev):
                selected.append((prev.ordinal, prev.content))
        selected.append(
            (target.ordinal, target.content if target_source_text is None else target_source_text)
        )
        for j in range(
            target_block_index + 1,
            min(target_block_index + 1 + neighbor_count, len(blocks)),
        ):
            nxt = blocks[j]
            if _is_table(nxt):
                continue
            selected.append((nxt.ordinal, nxt.content))
            break

    total = sum(len(t) for _, t in selected)
    if total > max_total_chars:
        bounded = True
        target_idx_in_list = 1 if len(selected) > 1 and selected[0][0] < selected[1][0] else 0
        target_block = selected[target_idx_in_list]
        new_selected = [target_block]
        running = len(target_block[1])
        for i in range(target_idx_in_list + 1, len(selected)):
            nxt = selected[i]
            if running + len(nxt[1]) > max_total_chars:
                break
            new_selected.append(nxt)
            running += len(nxt[1])
        prev_items: list[tuple[int, str]] = []
        for i in range(target_idx_in_list - 1, -1, -1):
            p = selected[i]
            if running + len(p[1]) > max_total_chars:
                break
            prev_items.append(p)
            running += len(p[1])
        new_selected = list(reversed(prev_items)) + new_selected
        selected = new_selected

    return {
        "blocks": selected,
        "bounded": bounded,
        "total_chars": sum(len(t) for _, t in selected),
    }


__all__ = ["expand_followup_context"]
