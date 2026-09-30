"""Project-local event types extending nanobot.events.AgentEvent.

Импортируются через ``bus.subscribe(handler, EventType)`` —
``MessageBus.publish`` (см. ``nanobot/bus/queue.py:118``) принимает
любой dataclass, наследующий ``nanobot.events.AgentEvent``.
"""

from __future__ import annotations

from lib.events.subagent import SubagentTurnCompleted

__all__ = ["SubagentTurnCompleted"]
