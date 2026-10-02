"""Кастомный runtime-event для subagent-оборотов.

Определяется в нашем namespace (см.
``openspec/changes/post-0.3.5-patches-cleanup/design.md D1``), потому
что ``nanobot.events.SubagentTurnCompleted`` не существует в 0.3.5.
Subagent исполняется ``SubagentManager._run_subagent`` /
``_run_admitted_subagent`` (``nanobot/agent/subagent.py``) — мимо
``TurnDelivery`` основного цикла, поэтому штатный ``TurnCompleted`` там не
публикуется.

Публикуется через ``bus.publish(event)`` из
``_SubagentLoggingHook._publish_subagent_turn_completed``
(``lib/services/runtime_patcher.py``; зовётся из ``after_run`` и
``on_error`` того же хука), подписка на ``SubagentTurnCompleted``
регистрируется в ``RuntimeEventsSubscriber._handle_subagent_turn_completed``
— контракт payload ``subagent_run_finished`` остаётся идентичен
``_SubagentLoggingHook._finalize`` (там же).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nanobot.events import AgentEvent


@dataclass(frozen=True)
class SubagentTurnCompleted(AgentEvent):
    """Финальное завершение subagent-оборота.

    Поля соответствуют payload ``subagent_run_finished`` в
    ``agent_gateway_logs``: ``task_id``, ``task``, ``final_content``,
    ``tools_used``, ``stop_reason``, ``request_id``,
    ``parent_request_id``, ``parent_user_id``, ``usage``, ``had_error``,
    ``error``.
    """

    task_id: str
    parent_request_id: str | None
    parent_user_id: str | None
    final_content: str
    tools_used: list[str]
    stop_reason: str | None
    request_id: str | None
    task: str | None = None
    usage: Any | None = None
    had_error: bool = False
    error: str | None = None


__all__ = ["SubagentTurnCompleted"]
