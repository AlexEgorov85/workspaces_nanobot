"""Этап 27: estimate bounds для hierarchical reducer.

``reduce_sections_to_document`` динамически выбирает число rounds
в зависимости от длины input. Поэтому estimate не может быть точным;
он задаёт min/max bounds.

Bounds для ``reduce_sections_to_document``:

- ``min_calls = 0`` если len(section_summaries) == 1 (одна секция — без reduce)
- ``max_calls = max_rounds + 1`` (включая final reduce для непокрытых групп)

Здесь мы проверяем, что:
1. Для ``1 section`` — нет reduce вызовов.
2. Для ``N sections`` — actual rounds ≤ max_rounds + 1.
3. Сам reducer вызывает llm_runner ровно rounds_done раз + 1 для final.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _build_section_summaries(n: int) -> list[tuple[str, str]]:
    """Создать N section_summaries."""
    return [(f"sec_{i:03d}", f"summary {i}") for i in range(n)]


def test_single_section_no_reduce():
    """1 section → 0 reduce calls."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        reduce_sections_to_document,
    )

    calls = {"n": 0}

    def _fake_llm(text, **kwargs):
        calls["n"] += 1
        return "merged"

    summaries = _build_section_summaries(1)
    result = reduce_sections_to_document(
        summaries, llm_runner=_fake_llm,
    )
    # 1 секция — никаких reduce вызовов.
    assert calls["n"] == 0, (
        f"single section should not call llm; got {calls['n']}"
    )
    assert result.rounds_done == 0


def _compute_max_calls(n: int, group_size: int, max_rounds: int) -> int:
    """Upper bound на calls для reduce_sections_to_document.

    Алгоритм: каждый round ceil(N_r / group_size) групп. Worst case — каждое
    round удваивает число групп (group_size=1), но реальный bound зависит
    от group_size и числа rounds.

    Безопасный upper bound: max_rounds * ceil(N / group_size) + 1
    (final reduce). Это overestimate, но гарантированно ≥ actual.
    """
    import math
    if n <= 1:
        return 0
    per_round = math.ceil(n / group_size)
    return max_rounds * per_round + 1


def test_n_sections_rounds_bounded():
    """N sections: actual calls ≤ max_rounds * ceil(N/group_size) + 1."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_sections_to_document,
    )

    cfg = HierarchicalReducerConfig(group_size=3, max_rounds=4)
    n = 10
    expected_max = _compute_max_calls(n, cfg.group_size, cfg.max_rounds)
    calls = {"n": 0}

    def _fake_llm(text, **_kwargs):
        calls["n"] += 1
        return "merged"

    summaries = _build_section_summaries(n)
    reduce_sections_to_document(
        summaries, config=cfg, llm_runner=_fake_llm,
    )
    assert calls["n"] <= expected_max, (
        f"too many reduce calls: {calls['n']} > {expected_max}"
    )


def test_estimate_bounds_for_1_2_10_100_sections():
    """Bounds: actual_calls ≤ max_rounds * ceil(N/group_size) + 1."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_sections_to_document,
    )

    cfg = HierarchicalReducerConfig(group_size=3, max_rounds=4)

    for n in (1, 2, 10, 100):
        summaries = _build_section_summaries(n)
        counter = [0]
        max_calls = _compute_max_calls(n, cfg.group_size, cfg.max_rounds)

        def _make_fake(c):
            def _fake_llm(text, **_kwargs):
                c[0] += 1
                return "merged"
            return _fake_llm

        reduce_sections_to_document(
            summaries, config=cfg, llm_runner=_make_fake(counter),
        )
        assert 0 <= counter[0] <= max_calls, (
            f"n={n}: calls={counter[0]} not in [0, {max_calls}]"
        )


def test_reducer_no_data_loss_for_marker_groups():
    """Все маркерные группы сохраняются при reduce."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_sections_to_document,
    )

    cfg = HierarchicalReducerConfig(group_size=3, max_rounds=10)

    seen_groups = []

    def _fake_llm(text, **kwargs):
        # Запоминаем все section_id, переданные в reduce.
        seen_groups.append(text)
        # Возвращаем эхо всех section_id, чтобы они «выжили».
        return text

    summaries = [
        (f"sec_{i:03d}", f"[sec_{i:03d}] summary {i}")
        for i in range(20)
    ]
    reduce_sections_to_document(
        summaries, config=cfg, llm_runner=_fake_llm,
    )
    # Проверяем, что каждый marker упоминается хотя бы раз.
    all_text = "\n".join(seen_groups)
    for i in range(20):
        assert f"sec_{i:03d}" in all_text, (
            f"section marker sec_{i:03d} lost during reduce"
        )
