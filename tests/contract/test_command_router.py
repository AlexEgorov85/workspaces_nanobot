"""CommandRouter: СЂРµРіРёСЃС‚СЂР°С†РёСЏ Рё dispatch slash-РєРѕРјР°РЅРґ."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.contract


def test_exact_dispatch() -> None:
    from nanobot.command.router import CommandContext, CommandRouter

    router = CommandRouter()
    seen: list[str] = []

    async def handler(ctx):
        seen.append(ctx.key)
        return "pong"

    router.exact("/ping", handler)

    ctx = CommandContext(
        msg=None, session=None, key="cli:direct", raw="/ping", loop=router,
    )
    result = asyncio.run(router.dispatch(ctx))
    assert result == "pong"
    assert seen == ["cli:direct"]


def test_prefix_dispatch_extracts_args() -> None:
    from nanobot.command.router import CommandContext, CommandRouter

    router = CommandRouter()
    args_holder: list[str] = []

    async def handler(ctx):
        args_holder.append(ctx.args)
        return None

    router.prefix("/compact", handler)

    ctx = CommandContext(
        msg=None, session=None, key="k", raw="/compact now please", loop=router,
    )
    assert asyncio.run(router.dispatch(ctx)) is None
    assert args_holder == [" now please"]


def test_unhandled_dispatch_returns_outbound() -> None:
    """В nanobot 0.3.5 ``CommandRouter.dispatch`` возвращает
    ``OutboundMessage`` даже для неизвестных команд (сообщение
    ``Unknown command ... Did you mean ...?``)."""
    from nanobot.bus.events import OutboundMessage
    from nanobot.command.router import CommandContext, CommandRouter

    router = CommandRouter()

    class _Msg:
        channel = "cli"
        chat_id = "direct"
        metadata = {}

    ctx = CommandContext(msg=_Msg(), session=None, key="k", raw="/unknown", loop=router)
    result = asyncio.run(router.dispatch(ctx))
    assert isinstance(result, OutboundMessage)
    assert "Unknown command" in result.content


def test_is_dispatchable_command() -> None:
    from nanobot.command.router import CommandRouter

    router = CommandRouter()
    assert isinstance(router.is_dispatchable_command("/ping"), bool)
