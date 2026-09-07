"""Этап 35: Reducer regression — детерминированность + без потери данных."""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from execution.config import (
    HierarchicalReducerConfig,
)
from execution.hierarchical import (
    reduce_sections_to_document,
)


def test_reducer_deterministic():
    """Одинаковый ввод → одинаковый вывод и одинаковые rounds."""
    items = [(f"s{i}", f"МАРКЕР_{i:03d} summary " + "x" * 50) for i in range(1, 7)]
    calls_log = []

    def _runner(text, *, length="detailed", focus=None, structure=None, question=None):
        calls_log.append(text)
        return "REDUCED [" + ",".join(
            s for s in ("МАРКЕР_001", "МАРКЕР_002", "МАРКЕР_003", "МАРКЕР_004",
                        "МАРКЕР_005", "МАРКЕР_006") if s in text
        ) + "]"

    r1 = reduce_sections_to_document(
        items, config=HierarchicalReducerConfig(group_size=3, max_rounds=2),
        llm_runner=_runner,
    )
    calls_log.clear()
    r2 = reduce_sections_to_document(
        items, config=HierarchicalReducerConfig(group_size=3, max_rounds=2),
        llm_runner=_runner,
    )
    assert r1.final_summary == r2.final_summary, "reducer not deterministic"
    assert r1.rounds_done == r2.rounds_done


def test_reducer_no_data_loss():
    """Все входные маркеры присутствуют после каждого round'а."""
    cfg = HierarchicalReducerConfig(group_size=3, max_rounds=2)
    items = [(f"s{i}", f"МАРКЕР_{i:03d}") for i in range(1, 7)]

    # llm_runner=None → детерминированный join: текст сохраняется как есть,
    # поэтому можно проверить сохранение всех маркеров напрямую.
    r = reduce_sections_to_document(
        items, config=cfg, llm_runner=None,
    )
    for i in range(1, 7):
        marker = f"МАРКЕР_{i:03d}"
        assert marker in r.final_summary, f"marker {marker} lost"


def test_reducer_rounds_bounded():
    """rounds_done <= max_rounds + 1 (финальный reduce)."""
    cfg = HierarchicalReducerConfig(group_size=2, max_rounds=3)
    items = [(f"s{i}", "text") for i in range(1, 13)]
    r = reduce_sections_to_document(items, config=cfg, llm_runner=None)
    assert r.rounds_done <= cfg.max_rounds + 1, (
        f"rounds_done={r.rounds_done} exceeds bound {cfg.max_rounds + 1}"
    )
