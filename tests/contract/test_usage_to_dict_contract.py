"""Type contract для ``_usage_to_dict`` из database_logging_hook.

Регрессия для nanobot 0.3.5: ``AgentHookContext.usage`` стал
``LLMUsage``-dataclass'ом вместо dict. Адаптер нормализует оба
типа в dict (``to_turn_dict()`` для ``LLMUsage`` с маппингом
``input_tokens`` → ``prompt_tokens``).

Тесты НЕ мокают тип — берут настоящий ``LLMUsage`` из nanobot 0.3.5.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract


@pytest.fixture
def real_usage():
    """Реальный ``LLMUsage`` из nanobot — frozen dataclass с кешем."""
    from nanobot.providers.base import LLMUsage

    return LLMUsage.reported(
        input_tokens=22933,
        output_tokens=45,
        total_tokens=22978,
        cache_read_tokens=22784,
    )


def test_usage_to_dict_none_returns_none():
    from lib.hooks.database_logging_hook import _usage_to_dict

    assert _usage_to_dict(None) is None


def test_usage_to_dict_empty_dict_returns_none():
    """Пустой dict → None (нет смысла сохранять)."""
    from lib.hooks.database_logging_hook import _usage_to_dict

    assert _usage_to_dict({}) is None


def test_usage_to_dict_dict_returns_copy(real_usage):
    """Legacy dict-форма — копия (не deep copy через to_turn_dict)."""
    from lib.hooks.database_logging_hook import _usage_to_dict

    d = _usage_to_dict(real_usage.to_dict())
    assert d == real_usage.to_dict()
    d["prompt_tokens"] = 999
    # Изменение копии не должно затронуть оригинал.
    assert real_usage.input_tokens == 22933


def test_usage_to_dict_llm_usage_returns_turn_shape(real_usage):
    """LLMUsage → to_turn_dict с prompt_tokens/completion_tokens."""
    from lib.hooks.database_logging_hook import _usage_to_dict

    d = _usage_to_dict(real_usage)
    assert isinstance(d, dict)
    assert d["prompt_tokens"] == 22933
    assert d["completion_tokens"] == 45
    assert d["total_tokens"] == 22978
    assert d["cached_tokens"] == 22784


def test_usage_to_dict_uncached_llm_usage():
    """Без cache_read — cached_tokens должен отсутствовать."""
    from nanobot.providers.base import LLMUsage

    from lib.hooks.database_logging_hook import _usage_to_dict

    usage = LLMUsage.reported(
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
    )
    d = _usage_to_dict(usage)
    assert d["prompt_tokens"] == 100
    assert d["completion_tokens"] == 10
    assert "cached_tokens" not in d


def test_usage_to_dict_handles_dataclass_without_methods():
    """Объект с .to_turn_dict(), но не LLMUsage — также нормализуется."""

    class _MiniUsage:
        def to_turn_dict(self):
            return {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}

    from lib.hooks.database_logging_hook import _usage_to_dict

    d = _usage_to_dict(_MiniUsage())
    assert d == {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}


def test_store_iteration_usage_with_llm_usage_writes_only_dict():
    """Регрессия: после записи ``LLMUsage`` в мосте ДОЛЖЕН быть dict,
    иначе ``get_context_window`` упадёт на ``.get('prompt_tokens')``."""
    from lib.hooks.database_logging_hook import (
        _CONTEXT_BRIDGE,
        _store_iteration_usage,
        get_context_window,
        pop_context_bridge,
        seed_context_window,
    )

    pop_context_bridge("sX")
    from nanobot.providers.base import LLMUsage

    seed_context_window("sX", limit=40000, model="m")
    _store_iteration_usage(
        "sX",
        LLMUsage.reported(input_tokens=200, output_tokens=10, total_tokens=210),
    )

    stored = _CONTEXT_BRIDGE["sX"]["usage"]
    assert isinstance(stored, dict), (
        "В мосте лежит не dict — get_context_window/get_iteration_usage "
        "не смогут использовать .get(...)"
    )
    block = get_context_window("sX")
    assert block == {
        "used": 200,
        "limit": 40000,
        "pct": round(200 / 40000, 4),
        "model": "m",
    }
    pop_context_bridge("sX")
