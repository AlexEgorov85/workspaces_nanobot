"""AgentLoop: публичный и приватный API, используемый RuntimePatcher.

Приватные методы здесь — точки monkey-patch (docs/architecture/
runtime-patcher-inventory.md). Их переименование в новой версии nanobot
ломает адаптер; contract-тест должен это поймать до деплоя.
"""

from __future__ import annotations

import inspect

import pytest

from tests.contract.helpers import assert_params

pytestmark = pytest.mark.contract


def _loop_cls():
    from nanobot.agent.loop import AgentLoop

    return AgentLoop


def test_from_config_signature() -> None:
    sig = inspect.signature(_loop_cls().from_config)
    names = list(sig.parameters)
    assert names[0] == "config"
    assert sig.parameters["bus"].default is None


def test_public_entrypoints() -> None:
    loop = _loop_cls()
    assert inspect.iscoroutinefunction(loop.run)
    assert inspect.iscoroutinefunction(loop.process_direct)
    assert_params(
        loop.process_direct,
        ["content", "session_key", "channel", "chat_id", "sender_id", "media"],
    )


def test_patched_private_methods_exist() -> None:
    loop = _loop_cls()
    for name in (
        "_assemble_outbound",
        "_state_build",
        "_state_compact",
        "_state_restore",
        "_state_save",
        "_state_respond",
        "_save_turn",
        "invalidate_runtime_config",
    ):
        assert hasattr(loop, name), f"AgentLoop.{name} missing"


def test_assemble_outbound_signature() -> None:
    assert_params(
        _loop_cls()._assemble_outbound,
        ["msg", "final_content", "all_msgs", "stop_reason", "had_injections", "streamed_content"],
        kwonly=["turn_latency_ms"],
    )


def test_save_turn_signature() -> None:
    assert_params(
        _loop_cls()._save_turn,
        ["session", "messages", "skip"],
        kwonly=["turn_latency_ms"],
    )
