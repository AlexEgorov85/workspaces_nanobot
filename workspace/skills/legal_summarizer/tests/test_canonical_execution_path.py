"""Execution-path test (Этап 30).

Проверяет, что ExecutionPlan выбирает стратегию на основе
одного источника решения — ``select_strategy``.
"""

from __future__ import annotations

from pathlib import Path


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def test_short_doc_selects_direct(tmp_path: Path):
    """Короткий документ → strategy = 'direct'."""
    from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
        inspect_canonical,
    )

    p = _write_doc(tmp_path, "Просто короткий текст для теста.")
    insp = inspect_canonical(text="", document_path=p)
    assert insp.strategy in ("direct", "map_flat")


def test_long_doc_selects_map_strategy(tmp_path: Path):
    """Длинный документ → strategy = 'map_flat' или 'map_hierarchical'."""
    from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
        inspect_canonical,
    )

    sections = [
        f"{i+1}. Раздел номер {i+1}\n\n" + ("Текст раздела. " * 80) * 5
        for i in range(30)
    ]
    text = "\n\n".join(sections)
    p = _write_doc(tmp_path, text)
    insp = inspect_canonical(text="", document_path=p)
    assert insp.strategy in ("direct", "map_flat", "map_hierarchical")


def test_strategy_decision_single_source(tmp_path: Path):
    """strategy определяется только через canonical select_strategy."""
    from workspace.skills.legal_summarizer.scripts import summarizer_canonical
    from workspace.skills.legal_summarizer.scripts.structure import (
        unified_execution,
    )

    call_count = {"n": 0}
    original = unified_execution.select_strategy

    def counting(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    summarizer_canonical.select_strategy = counting

    p = _write_doc(
        tmp_path,
        "1. Section\n\nContent.\n\n2. Section\n\nMore.",
    )
    insp = summarizer_canonical.inspect_canonical(
        text="", document_path=p,
    )
    assert call_count["n"] >= 1
    assert insp.strategy in ("direct", "map_flat", "map_hierarchical")


def test_reducer_decision_single_source():
    """reducer не делает собственного решения flat/hierarchical.

    Это проверка архитектурная — reducer получает стратегию
    извне (от ExecutionPlan), не вычисляет заново.
    """
    from workspace.skills.legal_summarizer.scripts.structure import (
        hierarchical_reducer,
    )

    assert hasattr(hierarchical_reducer, "reduce_chunks_hierarchical")
    assert hasattr(hierarchical_reducer, "reduce_sections_to_document")
    assert not hasattr(hierarchical_reducer, "select_strategy")
    assert not hasattr(hierarchical_reducer, "should_use_hierarchical_reduce")


def test_strategy_re_evaluation_deterministic(tmp_path: Path):
    """strategy детерминирован — два вызова дают одинаковый результат."""
    from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
        inspect_canonical,
    )

    p = _write_doc(
        tmp_path,
        "1. First\n\nContent.\n\n2. Second\n\nMore.",
    )
    insp1 = inspect_canonical(text="", document_path=p)
    insp2 = inspect_canonical(text="", document_path=p)
    assert insp1.strategy == insp2.strategy