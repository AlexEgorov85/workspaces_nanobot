from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def fake_bus_module():
    with patch.dict("sys.modules"):
        nano = types.ModuleType("nanobot")
        bus = types.ModuleType("nanobot.bus")
        queue = types.ModuleType("nanobot.bus.queue")

        class _MessageBus:
            def __init__(self):
                self.published = []

            async def publish_inbound(self, msg):
                self.published.append(("in", msg))

            async def publish_outbound(self, msg):
                self.published.append(("out", msg))

        queue.MessageBus = _MessageBus
        sys.modules["nanobot"] = nano
        sys.modules["nanobot.bus"] = bus
        sys.modules["nanobot.bus.queue"] = queue
        yield _MessageBus


class TestBusFactory:
    def test_plain_message_bus(self, fake_bus_module):
        from lib.core.bus_factory import BusFactory

        bus = BusFactory().create()
        assert isinstance(bus, fake_bus_module)

    def test_inbound_logger_invoked(self, fake_bus_module):
        from lib.core.bus_factory import BusFactory

        seen = []
        async def _log(msg):
            seen.append(msg)

        bus = BusFactory(inbound_logger=_log).create()
        asyncio.run(bus.publish_inbound("hello"))
        assert seen == ["hello"]

    def test_outbound_logger_invoked(self, fake_bus_module):
        from lib.core.bus_factory import BusFactory

        seen = []
        async def _log(msg):
            seen.append(msg)

        bus = BusFactory(outbound_logger=_log).create()
        asyncio.run(bus.publish_outbound("world"))
        assert seen == ["world"]

    def test_logger_error_swallows(self, fake_bus_module):
        from lib.core.bus_factory import BusFactory

        async def _bad(msg):
            raise RuntimeError("oops")

        bus = BusFactory(inbound_logger=_bad).create()
        asyncio.run(bus.publish_inbound("x"))  # не должно упасть
        assert bus.published == [("in", "x")]

    def test_no_logger_keeps_original_method(self, fake_bus_module):
        from lib.core.bus_factory import BusFactory

        bus = BusFactory().create()
        asyncio.run(bus.publish_inbound("a"))
        assert ("in", "a") in bus.published
