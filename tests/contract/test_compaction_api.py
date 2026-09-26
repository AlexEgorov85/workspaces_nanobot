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
    """В nanobot 0.3.5 сигнатура ``Consolidator.__init__``:
    ``(store, sessions, build_messages, get_tool_definitions, resolve_prompt_context=None)``.
    Параметры ``consolidation_ratio``/``unified_session`` удалены (последние
    живут как настройки consolidation'а на стороне AgentDefaults)."""
    from nanobot.agent.memory import Consolidator

    assert_params(
        Consolidator.__init__,
        [
            "store",
            "sessions",
            "build_messages",
            "get_tool_definitions",
        ],
    )


def test_consolidator_methods_present() -> None:
    """В nanobot 0.3.5 ``Consolidator`` использует ``summarize_provider_compaction``
    вместо ``maybe_consolidate_by_tokens`` (помечен DEPRECATED). Имена
    методов изменились — ``archive_session``/``pick_consolidation_boundary``
    удалены. Оставлены публичные методы, на которые опирается
    ``ContextCompactionService.compact``.
    """
    from nanobot.agent.memory import Consolidator

    for name in (
        "compact_idle_session",
        "estimate_session_prompt_tokens",
        "summarize_provider_compaction",
        "summarize_transcript",
        "archive_session",
        "get_lock",
    ):
        assert callable(getattr(Consolidator, name, None)), f"Consolidator.{name} missing"


def test_config_consolidation_keys() -> None:
    """В nanobot 0.3.5 ``AgentDefaults.consolidation_ratio``/``consolidationRatio``
    удалён (порог токен-консолидации зашит внутри Consolidator); осталось
    только ``session_ttl_minutes`` (alias ``idleCompactAfterMinutes``)."""
    from nanobot.config.schema import AgentDefaults

    fields = AgentDefaults.model_fields
    assert "session_ttl_minutes" in fields, (
        "AgentDefaults.session_ttl_minutes missing (camelCase alias: idleCompactAfterMinutes)"
    )
    ttl_alias = str(fields["session_ttl_minutes"].validation_alias)
    assert "idleCompactAfterMinutes" in ttl_alias
    assert "consolidation_ratio" not in fields
