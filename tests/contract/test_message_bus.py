"""MessageBus: очередь inbound/outbound."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.contract


def test_inbound_outbound_roundtrip() -> None:
    from nanobot.bus.events import InboundMessage, OutboundMessage
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    assert bus.inbound_size == 0
    assert bus.outbound_size == 0

    inbound = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="direct",
        content="hello",
    )
    asyncio.run(bus.publish_inbound(inbound))
    assert bus.inbound_size == 1
    got = asyncio.run(bus.consume_inbound())
    assert got.content == "hello"

    outbound = OutboundMessage(
        channel="cli",
        chat_id="direct",
        content="world",
    )
    asyncio.run(bus.publish_outbound(outbound))
    got_out = asyncio.run(bus.consume_outbound())
    assert got_out.content == "world"
