"""Tests for the per-iteration context-window bridge in database_logging_hook."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_bridge():
    """Очищаем мост до/после каждого теста, чтобы ключи не утекали."""
    from lib.hooks.database_logging_hook import pop_context_bridge

    pop_context_bridge("session:A")
    pop_context_bridge("session:B")
    yield
    pop_context_bridge("session:A")
    pop_context_bridge("session:B")


class TestSeedContextWindow:
    def test_empty_session_key_noop(self):
        from lib.hooks.database_logging_hook import seed_context_window

        seed_context_window(None, limit=10, model="x")
        seed_context_window("", limit=10, model="x")

    def test_seeds_limit_and_model(self):
        from lib.hooks.database_logging_hook import _CONTEXT_BRIDGE, seed_context_window

        seed_context_window("session:A", limit=65536, model="MiniMax-M3")
        assert _CONTEXT_BRIDGE["session:A"]["limit"] == 65536
        assert _CONTEXT_BRIDGE["session:A"]["model"] == "MiniMax-M3"


class TestStoreIterationUsage:
    def test_empty_session_key_noop(self):
        from lib.hooks.database_logging_hook import _store_iteration_usage

        _store_iteration_usage(None, {"prompt_tokens": 1})
        _store_iteration_usage("", {"prompt_tokens": 1})

    def test_stores_usage(self):
        from lib.hooks.database_logging_hook import (
            _CONTEXT_BRIDGE,
            _store_iteration_usage,
        )

        _store_iteration_usage("session:A", {"prompt_tokens": 12345})
        assert _CONTEXT_BRIDGE["session:A"]["usage"] == {"prompt_tokens": 12345}


class TestGetContextWindow:
    def test_no_block_no_usage_no_limit(self):
        from lib.hooks.database_logging_hook import get_context_window

        assert get_context_window("session:A") is None

    def test_prebuild_block_wins(self):
        from lib.hooks.database_logging_hook import (
            _store_context_window,
            _store_iteration_usage,
            get_context_window,
            seed_context_window,
        )

        seed_context_window("session:A", limit=10, model="x")
        _store_iteration_usage("session:A", {"prompt_tokens": 999})
        _store_context_window(
            "session:A", {"used": 1, "limit": 10, "pct": 0.1, "model": "prebuilt"},
        )
        assert get_context_window("session:A") == {
            "used": 1, "limit": 10, "pct": 0.1, "model": "prebuilt",
        }

    def test_compose_on_the_fly(self):
        from lib.hooks.database_logging_hook import (
            _store_iteration_usage,
            get_context_window,
            seed_context_window,
        )

        seed_context_window("session:A", limit=65536, model="MiniMax-M3")
        _store_iteration_usage("session:A", {"prompt_tokens": 32768})
        assert get_context_window("session:A") == {
            "used": 32768, "limit": 65536, "pct": 0.5, "model": "MiniMax-M3",
        }

    def test_compose_skips_when_limit_zero(self):
        from lib.hooks.database_logging_hook import (
            _store_iteration_usage,
            get_context_window,
            seed_context_window,
        )

        seed_context_window("session:A", limit=0, model="x")
        _store_iteration_usage("session:A", {"prompt_tokens": 100})
        assert get_context_window("session:A") is None

    def test_compose_skips_when_used_zero(self):
        from lib.hooks.database_logging_hook import (
            _store_iteration_usage,
            get_context_window,
            seed_context_window,
        )

        seed_context_window("session:A", limit=10, model="x")
        _store_iteration_usage("session:A", {"prompt_tokens": 0})
        assert get_context_window("session:A") is None

    def test_compose_pct_clamped(self):
        from lib.hooks.database_logging_hook import (
            _store_iteration_usage,
            get_context_window,
            seed_context_window,
        )

        seed_context_window("session:A", limit=10, model="x")
        _store_iteration_usage("session:A", {"prompt_tokens": 99999})
        assert get_context_window("session:A")["pct"] == 1.0


class TestIterationUsage:
    def test_no_entry_returns_none(self):
        from lib.hooks.database_logging_hook import get_iteration_usage

        assert get_iteration_usage("session:A") is None

    def test_returns_copy(self):
        from lib.hooks.database_logging_hook import (
            _store_iteration_usage,
            get_iteration_usage,
        )

        _store_iteration_usage("session:A", {"prompt_tokens": 1})
        snap = get_iteration_usage("session:A")
        assert snap == {"prompt_tokens": 1}
        snap["prompt_tokens"] = 999
        # Изменение копии не должно затронуть мост.
        assert _store_iteration_usage  # noop import alias
        from lib.hooks.database_logging_hook import _CONTEXT_BRIDGE
        assert _CONTEXT_BRIDGE["session:A"]["usage"]["prompt_tokens"] == 1


class TestPopContextBridge:
    def test_pop_removes_session(self):
        from lib.hooks.database_logging_hook import (
            _store_context_window,
            _store_iteration_usage,
            get_context_window,
            pop_context_bridge,
            seed_context_window,
        )

        seed_context_window("session:A", limit=10, model="x")
        _store_iteration_usage("session:A", {"prompt_tokens": 1})
        _store_context_window("session:A", {"used": 1, "limit": 10, "pct": 0.1, "model": "x"})
        assert get_context_window("session:A") is not None

        pop_context_bridge("session:A")
        assert get_context_window("session:A") is None

    def test_pop_empty_session_key_noop(self):
        from lib.hooks.database_logging_hook import pop_context_bridge

        pop_context_bridge(None)
        pop_context_bridge("")
        pop_context_bridge("nonexistent")
