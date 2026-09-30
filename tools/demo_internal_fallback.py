"""Демонстрация fallback-ответа при internal-ошибке AgentLoop.

Запускать::

    PYTHONIOENCODING=utf-8 python tools/demo_internal_fallback.py

Что показывает:
  1) Реальный OutboundMessage, который получит пользователь (текст, channel, metadata).
  2) Что upstream-литерал "Sorry, I encountered an error." НЕ публикуется.
  3) Что ``turn_completed`` runtime-event всё равно вызывается.
  4) Что запись попадает в ``agent_gateway_logs`` (если есть ``DbLoggingService``).

Скрипт использует stub-``TurnDelivery`` с настоящим Bus и asyncio-циклом,
имитируя except-блок ``AgentLoop._process_message``.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanobot.bus.events import InboundMessage, OutboundMessage


class _StubBus:
    def __init__(self) -> None:
        self.published: list[OutboundMessage] = []

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        self.published.append(msg)


class _RuntimeEventPublisher:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def turn_completed(self, **kw) -> None:
        self.events.append(kw)


class _StubTurnDelivery:
    """Поведение upstream ``nanobot/agent/turn_delivery.py:TurnDelivery.fail``."""

    def __init__(self) -> None:
        self.bus: _StubBus | None = None
        self.session_key: str | None = None
        self.lifecycle_message: InboundMessage | None = None
        self._failure_error_kind: str | None = None
        self.runtime_event_publisher: _RuntimeEventPublisher | None = None

    async def fail(self, *, publish_completion: bool) -> None:
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=self.lifecycle_message.channel,
                chat_id=self.lifecycle_message.chat_id,
                content="Sorry, I encountered an error.",
                metadata=dict(self.lifecycle_message.metadata or {}),
            )
        )
        if publish_completion:
            await self.runtime_event_publisher.turn_completed(
                channel=self.lifecycle_message.channel,
                chat_id=self.lifecycle_message.chat_id,
                session_key=self.session_key,
                metadata=self.lifecycle_message.metadata,
                outcome="failed",
                failure_kind="internal",
            )


def _install_stub_module() -> None:
    mod = types.ModuleType("nanobot.agent.turn_delivery")
    mod.TurnDelivery = _StubTurnDelivery
    sys.modules["nanobot.agent.turn_delivery"] = mod


async def main() -> None:
    from lib.services.runtime_patcher import (
        RuntimePatcher,
        _DEFAULT_INTERNAL_ERROR_TEXT,
    )

    _install_stub_module()

    patcher = RuntimePatcher()
    ok, msg = patcher.patch_turn_delivery_fail(
        settings=None,
        agent_id="demo_agent",
    )
    if not ok:
        print(f"patch failed: {msg}")
        return

    bus = _StubBus()
    pub = _RuntimeEventPublisher()

    inst = _StubTurnDelivery.__new__(_StubTurnDelivery)
    inst.__dict__.update(
        bus=bus,
        session_key="demo-session",
        lifecycle_message=InboundMessage(
            channel="telegram",
            sender_id="user-42",
            chat_id="chat-1",
            content="hi",
            metadata={"trace": "abc"},
        ),
        _failure_error_kind="RuntimeError",
        runtime_event_publisher=pub,
    )

    try:
        raise RuntimeError("explosion in tool")
    except Exception:
        await inst.fail(publish_completion=True)

    print("=" * 70)
    print("ЧТО УВИДИТ ПОЛЬЗОВАТЕЛЬ:")
    print("=" * 70)
    if not bus.published:
        print("  (ничего)")
        return
    for i, o in enumerate(bus.published, 1):
        print(f"  [{i}] channel={o.channel!r} chat_id={o.chat_id!r}")
        print(f"      content={o.content!r}")
        print(f"      _error_kind={o.metadata.get('_error_kind')!r}  _final_turn={o.metadata.get('_final_turn')!r}")
    print("=" * 70)
    print(f"ВСЕГО OUTBOUND'ов: {len(bus.published)} (должен быть ровно 1)")
    print("=" * 70)
    print("RUNTIME-EVENT turn_completed:")
    for e in pub.events:
        print(f"  outcome={e['outcome']!r} failure_kind={e['failure_kind']!r} session_key={e.get('session_key')!r}")
    print("=" * 70)
    print(f"DEFAULT fallback text: {_DEFAULT_INTERNAL_ERROR_TEXT!r}")


if __name__ == "__main__":
    asyncio.run(main())
