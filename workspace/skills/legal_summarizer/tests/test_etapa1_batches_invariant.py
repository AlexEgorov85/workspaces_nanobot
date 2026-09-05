"""Acceptance tests для Этапа 1: ExecutionPlan == actual batches.

Regression: каждый chunk должен попасть в map-phase ровно один раз,
а реальные batches должны быть один-к-одному с plan.batches.
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


def _install_llm_mocks(monkeypatch):
    """Подменяем llm_batch / llm_section_reduce / llm_document_reduce.

    Мокируем в **всех** местах, где они импортируются:

    * ``llm_calls`` (оригинальный module);
    * ``summarizer`` namespace (``_llm_*`` для direct path);
    * ``pipeline`` namespace (``_llm_batch`` для map path — импортируется
      через ``from llm_calls import llm_batch as _llm_batch``).
    """
    batches: list[tuple[str, ...]] = []
    section_reduces = {"n": 0}
    document_reduces = {"n": 0}

    def _patched_llm_batch(chunks, *, chunks_total, structure, length, question=None):
        batches.append(tuple(c.chunk_id for c in chunks))
        return {c.chunk_id: f"summary for {c.chunk_id}" for c in chunks}

    def _patched_llm_document_reduce(text, *, length, focus, structure, question=None):
        document_reduces["n"] += 1
        return "Итоговое саммари документа."

    def _patched_llm_section_reduce(
        section_path, section_heading, joined_text, *, length, question=None,
    ):
        section_reduces["n"] += 1
        return "Итоговое описание раздела."

    from workspace.skills.legal_summarizer.scripts import llm_calls
    monkeypatch.setattr(llm_calls, "llm_batch", _patched_llm_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _patched_llm_section_reduce)
    monkeypatch.setattr(
        llm_calls, "llm_document_reduce", _patched_llm_document_reduce,
    )

    import summarizer as _summarizer
    monkeypatch.setattr(_summarizer, "_llm_batch", _patched_llm_batch)
    monkeypatch.setattr(_summarizer, "_llm_section_reduce", _patched_llm_section_reduce)
    monkeypatch.setattr(
        _summarizer, "_llm_document_reduce", _patched_llm_document_reduce,
    )

    # Этап 1 acceptance: ``summarizer._run_one_batch_async`` →
    # ``pipeline.process_context_batch`` → ``pipeline._llm_batch``.
    # Подменяем также в ``pipeline`` namespace, чтобы перехватить map path.
    from workspace.skills.legal_summarizer.scripts import pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _patched_llm_batch)

    return batches, section_reduces, document_reduces


def _build_long_text() -> str:
    return (
        "1. Общие положения\n\n"
        + ("Это текст документа. " * 60) * 120
        + "\n\n2. Предмет договора\n\n"
        + ("Текст предмета. " * 60) * 120
        + "\n\n3. Срок действия\n\n"
        + ("Срок действия текст. " * 60) * 120
        + "\n\n4. Ответственность сторон\n\n"
        + ("Ответственность текст. " * 60) * 120
    )


def test_actual_batches_match_planned(tmp_path: Path, monkeypatch):
    """Test A+B+C: plan.batches[i].chunk_ids == actual batches, no duplication."""
    batches, _, _ = _install_llm_mocks(monkeypatch)

    import summarizer

    text = _build_long_text()
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    assert insp.strategy != "direct", (
        "Test expects map path; adjust doc size if it now goes direct"
    )
    assert len(insp.context_batches) >= 2, (
        "Test expects ≥2 planned batches; document must exceed one batch"
    )

    planned_chunk_ids: list[tuple[str, ...]] = list(insp.context_batches)

    result = summarizer.run(
        text,
        length="detailed",
        document_path=str(p),
        workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] in ("completed", "partial"), result

    actual = list(batches)

    expected_processed: set[str] = set()
    for batch in planned_chunk_ids:
        expected_processed.update(batch)
    actual_processed: set[str] = set()
    for batch in actual:
        actual_processed.update(batch)

    assert actual_processed == expected_processed, (
        f"actual={sorted(actual_processed)}, expected={sorted(expected_processed)}"
    )

    seen: list[str] = []
    for batch in actual:
        seen.extend(batch)
    assert len(seen) == len(set(seen)), (
        f"duplicate chunks in map batches: "
        f"{ {c: seen.count(c) for c in set(seen) if seen.count(c) > 1} }"
    )


def test_no_chunk_processed_more_than_once(tmp_path: Path, monkeypatch):
    """Test C: каждый chunk_id попадает ровно один раз."""
    batches, _, _ = _install_llm_mocks(monkeypatch)

    import summarizer

    text = _build_long_text()
    p = _write_doc(tmp_path, text)

    summarizer.run(
        text,
        length="detailed",
        document_path=str(p),
        workspace_root=tmp_path,
        confirmed=True,
    )

    seen: list[str] = []
    for batch in batches:
        seen.extend(batch)
    assert len(seen) == len(set(seen)), (
        f"duplicate chunks: "
        f"{ {c: seen.count(c) for c in set(seen) if seen.count(c) > 1} }"
    )


def test_each_chunk_processed_at_least_once(tmp_path: Path, monkeypatch):
    """Test B: каждый chunk_id попадает хотя бы один раз."""
    batches, _, _ = _install_llm_mocks(monkeypatch)

    import summarizer

    text = _build_long_text()
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    planned = [set(batch) for batch in insp.context_batches]
    expected = set()
    for s in planned:
        expected.update(s)

    summarizer.run(
        text,
        length="detailed",
        document_path=str(p),
        workspace_root=tmp_path,
        confirmed=True,
    )

    actual: set[str] = set()
    for batch in batches:
        actual.update(batch)

    assert actual == expected, (
        f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
    )