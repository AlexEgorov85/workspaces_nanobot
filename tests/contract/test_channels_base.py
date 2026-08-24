"""BaseChannel: ABC-контракт канала (PostgresChannel/RedisChannel)."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.contract


def test_abstract_surface() -> None:
    from nanobot.channels.base import BaseChannel

    abstract = set(BaseChannel.__abstractmethods__)
    for name in ("start", "stop", "send"):
        assert name in abstract, f"BaseChannel.{name} must stay abstract"


def test_channel_helpers_present() -> None:
    from nanobot.channels.base import BaseChannel

    for name in (
        "login",
        "send_delta",
        "send_reasoning",
        "supports_streaming",
        "is_allowed",
        "_handle_message",
        "default_config",
        "refresh_feature_metadata",
    ):
        assert hasattr(BaseChannel, name), f"BaseChannel.{name} missing"


def test_constructor_takes_config_and_bus() -> None:
    from nanobot.channels.base import BaseChannel

    sig = inspect.signature(BaseChannel.__init__)
    names = list(sig.parameters)
    assert names[:3] == ["self", "config", "bus"]


def test_minimal_subclass_instantiable() -> None:
    from nanobot.channels.base import BaseChannel

    class MinimalChannel(BaseChannel):
        @property
        def supports_streaming(self) -> bool:
            return False

        async def start(self) -> None: ...
        async def stop(self) -> None: ...
        async def send(self, msg): ...

    channel = MinimalChannel(config=None, bus=None)
    assert channel.supports_streaming is False
    assert isinstance(channel.is_allowed("anyone"), bool)
