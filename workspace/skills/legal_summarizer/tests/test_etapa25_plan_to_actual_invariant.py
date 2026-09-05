"""Этап 25: strict invariant plan → actual batches.

Проверяем три жёстких требования к ``_map_plan_to_chunk_batches``:

8.1 Unknown chunk → RuntimeError (no silent ignore)
8.2 Duplicate chunk across batches → RuntimeError
8.3 Exact order: ``actual_batches`` имеет ту же форму (list-of-lists),
    не просто одинаковое множество.

Поскольку ``ExecutionPlan`` — frozen dataclass, тесты строят план
через ``build_execution_plan`` и затем заменяют ``batches`` через
``dataclasses.replace`` на патологические варианты.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

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


def _make_plan_with_batches(insp, chunks, batch_chunk_ids_list):
    """Строит ExecutionPlan с заданными batch chunk_ids."""
    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        build_execution_plan,
        select_strategy,
    )

    struct = insp.structure
    strategy = select_strategy(struct, list(chunks))
    if strategy == "direct":
        pytest.skip("test requires map strategy")
    document_id = insp.analysis.identity.document_id if insp.analysis else "d"
    return build_execution_plan(
        struct, tuple(chunks), document_id=document_id,
    ), strategy


def test_unknown_chunk_raises(tmp_path):
    """8.1: unknown chunk_id → RuntimeError."""
    import summarizer

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:3])
    plan, _ = _make_plan_with_batches(insp, selected, [])

    # Строим «отравленный» план: в первом батче — левый chunk_id.
    from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
        PlannedBatch,
    )
    poisoned_batches = (
        PlannedBatch(
            batch_id="cb_000",
            chunk_ids=(selected[0].chunk_id, "c_NOT_EXIST"),
            token_estimate=0,
        ),
    )
    poisoned_plan = replace(plan, batches=poisoned_batches)

    with pytest.raises(RuntimeError, match="unknown chunk_id"):
        summarizer._map_plan_to_chunk_batches(poisoned_plan, selected)


def test_duplicate_chunk_raises(tmp_path):
    """8.2: один chunk_id в двух батчах → RuntimeError."""
    import summarizer

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:3])
    plan, _ = _make_plan_with_batches(insp, selected, [])

    from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
        PlannedBatch,
    )
    # B1 = c0,c1; B2 = c1,c2 → c1 дублируется.
    poisoned_batches = (
        PlannedBatch(
            batch_id="cb_000",
            chunk_ids=(selected[0].chunk_id, selected[1].chunk_id),
            token_estimate=0,
        ),
        PlannedBatch(
            batch_id="cb_001",
            chunk_ids=(selected[1].chunk_id, selected[2].chunk_id),
            token_estimate=0,
        ),
    )
    poisoned_plan = replace(plan, batches=poisoned_batches)

    with pytest.raises(RuntimeError, match="duplicate"):
        summarizer._map_plan_to_chunk_batches(poisoned_plan, selected)


def test_exact_batch_order_preserved(tmp_path):
    """8.3: actual_batches имеет точно ту же форму, что и plan.batches."""
    import summarizer

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:5])
    plan, _ = _make_plan_with_batches(insp, selected, [])

    from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
        PlannedBatch,
    )
    ids = tuple(c.chunk_id for c in selected)
    # B1 = c0,c1; B2 = c2; B3 = c3,c4.
    new_batches = (
        PlannedBatch(batch_id="cb_000", chunk_ids=(ids[0], ids[1]), token_estimate=0),
        PlannedBatch(batch_id="cb_001", chunk_ids=(ids[2],), token_estimate=0),
        PlannedBatch(batch_id="cb_002", chunk_ids=(ids[3], ids[4]), token_estimate=0),
    )
    new_plan = replace(plan, batches=new_batches)

    actual = summarizer._map_plan_to_chunk_batches(new_plan, selected)
    actual_shape = [[c.chunk_id for c in batch] for batch in actual]
    expected_shape = [
        [ids[0], ids[1]],
        [ids[2]],
        [ids[3], ids[4]],
    ]
    assert actual_shape == expected_shape, (
        f"shape mismatch:\n  actual={actual_shape}\n  expected={expected_shape}"
    )


def test_missing_chunks_raises_in_run_map_reduce(tmp_path, monkeypatch):
    """Реальный путь: если plan покрывает не все chunks → RuntimeError.

    ``_map_plan_to_chunk_batches`` сам по себе не проверяет missing —
    это invariant уровня ``_run_map_reduce``. Проверяем через прямой вызов
    ``_run_map_reduce`` с отравленным планом.
    """
    import summarizer
    from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
        PlannedBatch,
    )

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    selected = list(insp.chunks[:4])
    plan, _ = _make_plan_with_batches(insp, selected, [])

    # План покрывает только первые 3 chunk_id — c3 потерян.
    ids = tuple(c.chunk_id for c in selected)
    bad_batches = (
        PlannedBatch(batch_id="cb_000", chunk_ids=(ids[0], ids[1]), token_estimate=0),
        PlannedBatch(batch_id="cb_001", chunk_ids=(ids[2],), token_estimate=0),
    )
    bad_plan = replace(plan, batches=bad_batches)

    insp2 = replace(
        insp, execution_plan=bad_plan,
    )

    with pytest.raises(RuntimeError, match="missing"):
        summarizer._run_map_reduce(
            selected,
            plan=bad_plan,
            strategy=plan.strategy,
            length="detailed",
            focus=None,
            question=None,
            structure=None,
            analysis=insp.analysis,
            operation_id="op",
            document_path=str(p),
            workspace_root=tmp_path,
            chars_in=insp.chars_in,
            estimated_llm_calls=1,
            article_count=0,
            existing_manifest=None,
        )
