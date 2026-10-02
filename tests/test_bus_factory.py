"""Шина сообщений: сборка и логирующие обёртки.

Раньше это был отдельный модуль ``lib.core.bus_factory`` с классом-обёрткой.
Сборка ужата до двух функций в composition root
(``lib.core.application_context._create_bus`` / ``_wrap_bus_publish``):
``MessageBus`` — класс библиотеки, а всё наше — это создать шину и подменить
два метода публикации. Контракт (логгер зовётся ДО оригинала, ошибка логгера
не роняет публикацию) проверен здесь без привязки к отдельному файлу.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import patch

import pytest


@pytest.fixture
def fake_bus_module():
    """Подменяем ``nanobot.bus.queue`` на минимальную шину с теми же методами.

    ``_create_bus`` импортирует ``MessageBus`` лениво (внутри функции),
    поэтому подмена ``sys.modules`` действует ровно на момент создания.
    """

    class _MessageBus:
        def __init__(self):
            self.published = []

        async def publish_inbound(self, msg):
            self.published.append(("in", msg))

        async def publish_outbound(self, msg):
            self.published.append(("out", msg))

    with patch.dict(
        "sys.modules",
        {
            "nanobot": types.ModuleType("nanobot"),
            "nanobot.bus": types.ModuleType("nanobot.bus"),
            "nanobot.bus.queue": types.ModuleType("nanobot.bus.queue"),
        },
    ):
        import nanobot.bus.queue as queue

        queue.MessageBus = _MessageBus
        yield _MessageBus


class TestCreateBus:
    def test_plain_message_bus(self, fake_bus_module):
        from lib.core.application_context import _create_bus

        bus = _create_bus(None, None)
        assert isinstance(bus, fake_bus_module)

    def test_inbound_logger_invoked(self, fake_bus_module):
        from lib.core.application_context import _create_bus

        seen = []

        async def _log(msg):
            seen.append(msg)

        bus = _create_bus(_log, None)
        asyncio.run(bus.publish_inbound("hello"))
        assert seen == ["hello"]

    def test_outbound_logger_invoked(self, fake_bus_module):
        from lib.core.application_context import _create_bus

        seen = []

        async def _log(msg):
            seen.append(msg)

        bus = _create_bus(None, _log)
        asyncio.run(bus.publish_outbound("world"))
        assert seen == ["world"]

    def test_logger_error_swallows(self, fake_bus_module):
        from lib.core.application_context import _create_bus

        async def _bad(msg):
            raise RuntimeError("oops")

        bus = _create_bus(_bad, None)
        asyncio.run(bus.publish_inbound("x"))  # не должно упасть
        assert bus.published == [("in", "x")]

    def test_no_logger_keeps_original_method(self, fake_bus_module):
        from lib.core.application_context import _create_bus

        bus = _create_bus(None, None)
        asyncio.run(bus.publish_inbound("a"))
        assert ("in", "a") in bus.published
