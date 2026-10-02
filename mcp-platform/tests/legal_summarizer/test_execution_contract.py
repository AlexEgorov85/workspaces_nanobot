"""главный execution invariant.

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

from pathlib import Path


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
    import libs.legal_summarizer.application.service as summarizer
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = tuple(insp.chunks[:4])
    selected_ids = tuple(c.chunk_id for c in selected)

    ctx = summarizer.build_execution_context(
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

    actual = summarizer.map_plan_to_chunk_batches(ctx.plan, list(selected))
    actual_ids = tuple(c.chunk_id for batch in actual for c in batch)
    assert tuple(sorted(actual_ids)) == tuple(sorted(selected_ids))

def test_question_invariant_selected_planned_processed(tmp_path):
    """Question case: selected_ids == planned_ids == processed_ids.

    Выборку строит САМА question-ветка ``build_execution_context``
    (``select_chunks_for_mode(question=...)``), а не внешний
    ``selected_chunks``.

    Раньше тест передавал ``selected_chunks=`` и аргумент ``question=``
    не передавался вовсе. ``build_execution_context`` при заданном
    ``selected_chunks`` вопрос не учитывает вообще
    (``context_builder.py``: ветка ``if selected_chunks is not None``
    идёт до ``select_chunks_for_mode``), поэтому имя и docstring
    обещали question-кейс, а проверяли произвольную подвыборку
    ``chunks[1:4]``. Теперь ветка реально исполняется — вместе с
    retrieval / lexical fallback внутри ``select_chunks_for_mode``.
    """
    import libs.legal_summarizer.application.service as summarizer
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    # Вопрос идёт в selection, selection — единственный источник ctx.chunks.
    ctx = summarizer.build_execution_context(
        insp, question="Текст",
    )

    # Предусловие проверяется явно: при одном chunk'е стратегия
    # вырождается в direct и план не строится — проверять нечего,
    # а тест обязан упасть, а не пройти молча.
    assert len(ctx.chunks) > 1, (
        "question-ветка обязана выбрать >1 chunk, иначе стратегия "
        f"выродится в direct без плана: выбрано {len(ctx.chunks)}"
    )
    assert ctx.strategy in ("map_flat", "map_hierarchical")
    assert ctx.plan is not None

    selected_ids = tuple(c.chunk_id for c in ctx.chunks)

    planned_ids = tuple(
        cid for batch in ctx.plan.batches for cid in batch.chunk_ids
    )
    assert tuple(sorted(planned_ids)) == tuple(sorted(selected_ids)), (
        f"planned set != question-selected set: "
        f"planned={planned_ids}, selected={selected_ids}"
    )

    actual = summarizer.map_plan_to_chunk_batches(ctx.plan, list(ctx.chunks))
    actual_ids = tuple(c.chunk_id for batch in actual for c in batch)
    assert tuple(sorted(actual_ids)) == tuple(sorted(selected_ids))

def test_ordered_batches_match(tmp_path):
    """planned_batches == actual_batches (exact list-of-lists)."""
    import libs.legal_summarizer.application.service as summarizer
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = list(insp.chunks[:5])
    ctx = summarizer.build_execution_context(
        insp, selected_chunks=selected,
    )
    assert ctx.plan is not None

    planned_shape = [list(batch.chunk_ids) for batch in ctx.plan.batches]
    actual = summarizer.map_plan_to_chunk_batches(ctx.plan, selected)
    actual_shape_str = [[c.chunk_id for c in batch] for batch in actual]

    assert planned_shape == actual_shape_str, (
        f"shape mismatch:\n  planned={planned_shape}\n  actual={actual_shape_str}"
    )

def test_each_chunk_appears_exactly_once(tmp_path):
    """Каждый chunk из selected появляется в plan ровно один раз."""
    import libs.legal_summarizer.application.service as summarizer
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))

    selected = list(insp.chunks[:5])
    ctx = summarizer.build_execution_context(
        insp, selected_chunks=selected,
    )
    assert ctx.plan is not None

    actual = summarizer.map_plan_to_chunk_batches(ctx.plan, selected)
    all_ids = [c.chunk_id for batch in actual for c in batch]
    assert len(all_ids) == len(set(all_ids)), (
        f"duplicate chunk in actual batches: {all_ids}"
    )
    assert sorted(all_ids) == sorted(c.chunk_id for c in selected)
