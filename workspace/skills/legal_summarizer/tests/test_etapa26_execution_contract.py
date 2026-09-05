"""Этап 26: главный execution invariant.

Для любого ctx должны выполняться:

  selected_ids == planned_ids == processed_ids
  planned_batches == actual_batches (exact shape)

Тестируем в двух вариантах:

1. Flat (selected = первые 4 chunks → map_flat)
2. Question (selected = релевантные вопросу chunks)

Используем canonical path:
  _build_execution_context
    → ctx.plan (planned batches)
  _map_plan_to_chunk_batches
    → actual batches (== planned по построению)

Эти функции — единственный источник истины для плана и actual batches.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _build_doc(sections: int = 6) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)


def test_flat_invariant_selected_planned_processed(tmp_path):
    """Flat case: selected_ids == planned_ids == (actual batches union)."""
    import summarizer

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = tuple(insp.chunks[:4])
    selected_ids = tuple(c.chunk_id for c in selected)

    ctx = summarizer._build_execution_context(
        insp, selected_chunks=list(selected),
    )
    assert ctx.strategy in ("map_flat", "map_hierarchical")
    assert ctx.plan is not None

    planned_ids = tuple(
        cid for batch in ctx.plan.batches for cid in batch.chunk_ids
    )
    assert tuple(sorted(planned_ids)) == tuple(sorted(selected_ids)), (
        f"planned set != selected set: "
        f"planned={planned_ids}, selected={selected_ids}"
    )

    actual = summarizer._map_plan_to_chunk_batches(ctx.plan, list(selected))
    actual_ids = tuple(c.chunk_id for batch in actual for c in batch)
    assert tuple(sorted(actual_ids)) == tuple(sorted(selected_ids))


def test_question_invariant_selected_planned_processed(tmp_path):
    """Question case: selected_ids == planned_ids == processed_ids."""
    import summarizer

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = tuple(insp.chunks[i] for i in (1, 3))
    selected_ids = tuple(c.chunk_id for c in selected)

    ctx = summarizer._build_execution_context(
        insp, selected_chunks=list(selected),
    )
    assert ctx.strategy in ("map_flat", "map_hierarchical")
    assert ctx.plan is not None

    planned_ids = tuple(
        cid for batch in ctx.plan.batches for cid in batch.chunk_ids
    )
    assert tuple(sorted(planned_ids)) == tuple(sorted(selected_ids))

    actual = summarizer._map_plan_to_chunk_batches(ctx.plan, list(selected))
    actual_ids = tuple(c.chunk_id for batch in actual for c in batch)
    assert tuple(sorted(actual_ids)) == tuple(sorted(selected_ids))


def test_ordered_batches_match(tmp_path):
    """planned_batches == actual_batches (exact list-of-lists)."""
    import summarizer

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = list(insp.chunks[:5])
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=selected,
    )
    assert ctx.plan is not None

    planned_shape = [list(batch.chunk_ids) for batch in ctx.plan.batches]
    actual = summarizer._map_plan_to_chunk_batches(ctx.plan, selected)
    actual_shape_str = [[c.chunk_id for c in batch] for batch in actual]

    assert planned_shape == actual_shape_str, (
        f"shape mismatch:\n  planned={planned_shape}\n  actual={actual_shape_str}"
    )


def test_each_chunk_appears_exactly_once(tmp_path):
    """Каждый chunk из selected появляется в plan ровно один раз."""
    import summarizer

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = list(insp.chunks[:5])
    ctx = summarizer._build_execution_context(
        insp, selected_chunks=selected,
    )
    assert ctx.plan is not None

    actual = summarizer._map_plan_to_chunk_batches(ctx.plan, selected)
    all_ids = [c.chunk_id for batch in actual for c in batch]
    assert len(all_ids) == len(set(all_ids)), (
        f"duplicate chunk in actual batches: {all_ids}"
    )
    assert sorted(all_ids) == sorted(c.chunk_id for c in selected)
