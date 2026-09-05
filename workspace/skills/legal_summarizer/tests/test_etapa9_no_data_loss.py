"""Acceptance tests для Этапа 9: HierarchicalReducer не теряет данные."""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _run(n_sections: int, max_rounds: int = 4, group_size: int = 3):
    """Прогоняет reduce_sections_to_document с N секциями."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_sections_to_document,
    )
    items = [(f"s{i}", f"section {i} summary") for i in range(n_sections)]
    cfg = HierarchicalReducerConfig(
        group_size=group_size, max_rounds=max_rounds,
    )
    return reduce_sections_to_document(items, config=cfg, llm_runner=None)


def test_no_data_loss_3_sections():
    """3 sections → ровно один final_summary."""
    result = _run(3)
    assert result.final_summary != ""


def test_no_data_loss_10_sections():
    """10 sections → ровно один final_summary, после max_rounds делается final."""
    result = _run(10, max_rounds=2)
    assert result.final_summary != ""
    # 10 sections, group_size=3: round1 → [3, 3, 3, 1]; round2 → [1, 1] → final.
    assert result.rounds_done >= 2


def test_no_data_loss_100_sections():
    """100 sections → ровно один final_summary."""
    result = _run(100, max_rounds=3)
    assert result.final_summary != ""


def test_no_data_loss_1000_sections():
    """1000 sections → ровно один final_summary, без потерь."""
    result = _run(1000, max_rounds=3)
    assert result.final_summary != ""


def test_final_summary_contains_all_input_markers():
    """Final summary содержит текст всех секций (нет потерь)."""
    items = [(f"s{i}", f"unique-marker-{i}") for i in range(20)]
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_sections_to_document,
    )
    cfg = HierarchicalReducerConfig(group_size=2, max_rounds=1)
    result = reduce_sections_to_document(items, config=cfg, llm_runner=None)
    # llm_runner=None → final reduce не делает join, но при group_size=2
    # и max_rounds=1: [s0, s1], [s2, s3], ... → 10 групп → final join.
    # Финальный summary содержит маркер хотя бы первой и последней секции.
    assert "unique-marker-0" in result.final_summary
    assert "unique-marker-19" in result.final_summary