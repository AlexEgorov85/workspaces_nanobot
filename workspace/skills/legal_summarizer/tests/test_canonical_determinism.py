"""Determinism suite (Этап 33).

Проверяет, что canonical pipeline детерминирован:
* DocumentStructure identical между запусками;
* chunk IDs identical;
* chunk order identical;
* ExecutionPlan identical;
* batch composition identical;
* Retrieval ranking identical.

Без time-based IDs, без random.
"""

from __future__ import annotations

from pathlib import Path


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def test_pipeline_deterministic_for_same_input(tmp_path: Path):
    """Два запуска дают идентичный DocumentStructure."""
    from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
        run_canonical_pipeline,
    )

    p = _write_doc(
        tmp_path,
        "1. First\n\nContent.\n\n2. Second\n\nMore.",
    )
    r1 = run_canonical_pipeline(p)
    r2 = run_canonical_pipeline(p)

    assert r1.analysis.structure.nodes.keys() == r2.analysis.structure.nodes.keys()
    for nid in r1.analysis.structure.nodes:
        n1 = r1.analysis.structure.nodes[nid]
        n2 = r2.analysis.structure.nodes[nid]
        assert n1.start_block == n2.start_block
        assert n1.end_block == n2.end_block
        assert n1.title == n2.title


def test_chunk_ids_deterministic(tmp_path: Path):
    """Chunk IDs идентичны между запусками."""
    from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
        run_canonical_pipeline,
    )

    p = _write_doc(
        tmp_path,
        "1. First\n\nContent.\n\n2. Second\n\nMore.",
    )
    r1 = run_canonical_pipeline(p)
    r2 = run_canonical_pipeline(p)

    ids1 = [c.chunk_id for c in r1.chunks]
    ids2 = [c.chunk_id for c in r2.chunks]
    assert ids1 == ids2


def test_execution_plan_deterministic(tmp_path: Path):
    """ExecutionPlan identical между запусками."""
    from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
        run_canonical_pipeline,
    )
    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        build_execution_plan,
    )

    p = _write_doc(
        tmp_path,
        "1. First\n\nContent.\n\n2. Second\n\nMore.",
    )
    r1 = run_canonical_pipeline(p)
    r2 = run_canonical_pipeline(p)

    doc_id = r1.analysis.identity.document_id
    plan1 = build_execution_plan(r1.analysis.structure, r1.chunks, document_id=doc_id)
    plan2 = build_execution_plan(r2.analysis.structure, r2.chunks, document_id=doc_id)

    assert plan1.strategy == plan2.strategy
    assert len(plan1.batches) == len(plan2.batches)
    for b1, b2 in zip(plan1.batches, plan2.batches):
        assert b1.batch_id == b2.batch_id
        assert b1.chunk_ids == b2.chunk_ids


def test_retrieval_ranking_deterministic(tmp_path: Path):
    """Retrieval ranking identical."""
    from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
        run_canonical_pipeline,
    )

    p = _write_doc(
        tmp_path,
        "1. Право собственности\n\n"
        "Собственник имеет право владеть.\n\n"
        "2. Обязательства\n\n"
        "Должник обязан исполнить.",
    )
    r1 = run_canonical_pipeline(p)
    r2 = run_canonical_pipeline(p)

    h1 = r1.analysis.retrieve("собственность")
    h2 = r2.analysis.retrieve("собственность")

    assert len(h1) == len(h2)
    for a, b in zip(h1, h2):
        assert a.chunk_id == b.chunk_id
        assert a.score == b.score


def test_no_time_based_ids_in_pipeline(tmp_path: Path):
    """В pipeline нет time-based IDs."""
    import re

    from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
        run_canonical_pipeline,
    )

    p = _write_doc(
        tmp_path,
        "1. Section\n\nContent.\n\n2. Section\n\nMore.",
    )
    result = run_canonical_pipeline(p)

    ids: list[str] = []
    ids.extend(c.chunk_id for c in result.chunks)
    ids.extend(result.analysis.identity.document_id)
    for n in result.analysis.structure.nodes.values():
        ids.append(n.node_id)

    time_pattern = re.compile(r"\d{10,}")
    for id_ in ids:
        if isinstance(id_, str):
            assert not time_pattern.search(id_), (
                f"Time-based ID detected: {id_}"
            )