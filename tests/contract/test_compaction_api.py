"""Compaction API: AutoCompact + Consolidator + ключи конфига."""

from __future__ import annotations

import pytest

from tests.contract.helpers import assert_params

pytestmark = pytest.mark.contract


def test_autocompact_init_signature() -> None:
    from nanobot.agent.autocompact import AutoCompact

    assert_params(AutoCompact.__init__, ["sessions", "consolidator", "session_ttl_minutes"])


def test_autocompact_archive_kwonly_runtime() -> None:
    from nanobot.agent.autocompact import AutoCompact

    assert_params(AutoCompact._archive, ["key"], kwonly=["runtime"])
    import inspect

    assert inspect.iscoroutinefunction(AutoCompact._archive)


def test_consolidator_init_signature() -> None:
    from nanobot.agent.memory import Consolidator

    assert_params(
        Consolidator.__init__,
        [
            "store",
            "sessions",
            "build_messages",
            "get_tool_definitions",
            "consolidation_ratio",
            "unified_session",
        ],
    )


def test_consolidator_methods_present() -> None:
    from nanobot.agent.memory import Consolidator

    for name in (
        "maybe_consolidate_by_tokens",
        "estimate_session_prompt_tokens",
        "compact_idle_session",
        "archive",
        "pick_consolidation_boundary",
        "get_lock",
    ):
        assert callable(getattr(Consolidator, name, None)), f"Consolidator.{name} missing"


def test_config_consolidation_keys() -> None:
    from nanobot.config.schema import AgentDefaults

    fields = AgentDefaults.model_fields
    assert "session_ttl_minutes" in fields, (
        "AgentDefaults.session_ttl_minutes missing (camelCase alias: idleCompactAfterMinutes)"
    )
    assert "consolidation_ratio" in fields, (
        "AgentDefaults.consolidation_ratio missing (camelCase alias: consolidationRatio)"
    )
    ttl_alias = str(fields["session_ttl_minutes"].validation_alias)
    ratio_alias = str(fields["consolidation_ratio"].validation_alias)
    assert "idleCompactAfterMinutes" in ttl_alias
    assert "consolidationRatio" in ratio_alias
