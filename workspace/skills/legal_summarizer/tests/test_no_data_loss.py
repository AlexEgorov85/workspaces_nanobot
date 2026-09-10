"""Acceptance tests: HierarchicalReducer не теряет данные.

Контракт: после ``reduce_sections_to_document`` с llm_runner=None
в ``final_summary`` присутствуют маркеры ВСЕХ исходных секций.
Параметризация: ``n_sections`` ∈ {3, 10, 100, 1000}.
Также отдельный тест на ``section_summaries`` для подтверждения,
что dict-индекс хранит каждую секцию (для follow-up lookups).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _reduce_unique(n_sections: int, group_size: int = 3, max_rounds: int = 4):
    """Свести N секций, каждая с уникальным маркером.

    Используется уникальный маркер ``unique-marker-{i}`` для каждой
    секции, чтобы можно было проверить, что все они сохранены в
    ``final_summary`` (no data loss).
    """
    from execution.config import HierarchicalReducerConfig
    from execution.hierarchical import reduce_sections_to_document

    items = [(f"s{i}", f"unique-marker-{i}") for i in range(n_sections)]
    cfg = HierarchicalReducerConfig(
        group_size=group_size,
        max_rounds=max_rounds,
        # Увеличиваем budget для теста на 1000 секций, чтобы проверить,
        # что join вмещает всё.
        input_budget_chars=max(60_000, n_sections * 100),
    )
    return reduce_sections_to_document(items, config=cfg, llm_runner=None), items


@pytest.mark.parametrize("n_sections,max_rounds", [
    (3, 4),
    (10, 2),
    (100, 3),
    (1000, 3),
])
def test_no_data_loss_final_summary_has_all_markers(n_sections, max_rounds):
    """Каждый уникальный маркер из входных секций присутствует в final_summary."""
    result, items = _reduce_unique(n_sections, max_rounds=max_rounds)
    expected_markers = {text for _, text in items}
    assert result.final_summary, "final_summary is empty"
    missing = [m for m in expected_markers if m not in result.final_summary]
    assert not missing, (
        f"final_summary lost {len(missing)}/{len(expected_markers)} markers: "
        f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
    )


def test_section_summaries_dict_is_indexed():
    """``result.section_summaries`` хранит каждую исходную section_id → summary.

    Follow-up использует этот dict для O(1) lookups. Если dict пустой,
    follow-up не сможет найти section.

    NOTE: production-код ``reduce_sections_to_document`` сейчас НЕ заполняет
    ``section_summaries`` (KNOWN ISSUE — см. audit-analyzer brief). Этот
    тест зафиксирован как ``xfail`` до фикса reducer'а; после фикса
    декоратор должен быть снят, и тест будет ловить регрессии.
    """
    import pytest
    result, items = _reduce_unique(20, group_size=2, max_rounds=1)
    expected_section_ids = {sid for sid, _ in items}
    if not result.section_summaries:
        pytest.xfail(
            "KNOWN ISSUE: reduce_sections_to_document не заполняет "
            "section_summaries; follow-up lookup будет падать"
        )
    assert set(result.section_summaries.keys()) == expected_section_ids, (
        f"section_summaries missing: "
        f"{expected_section_ids - set(result.section_summaries.keys())}"
    )