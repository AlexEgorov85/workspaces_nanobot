"""Подписчик на runtime-события nanobot 0.3.5.

Использует публичный pub-sub API:

* `nanobot.bus.queue.MessageBus.subscribe(handler, EventType)`
  (`nanobot/bus/queue.py:92`) — локальный fan-out, доступен
  **только** при публикации через `bus.publish(...)`
  (`nanobot/bus/queue.py:118`), **не** через `bus.publish_event(...)`
  (это outbound-queue для каналов).

Подписчик инкапсулирует три независимых подписки:

* `TurnRuntimeAdmitted` — seed лимита окна/модели в
  `DatabaseLoggingHook._CONTEXT_BRIDGE` (см. `design.md` в
  `openspec/changes/runtime-events-subscription`).
* `TurnCompleted` — запись нового event_type `turn_completed` в
  `agent_gateway_logs` через `DbLoggingService.log_event(...)`. Метрики
  оборота: `latency_ms`, `outcome`, `failure_*`, `usage`.
* `SubagentTurnCompleted` — кастомный event нашего namespace, см.
  `openspec/changes/post-0.3.5-patches-cleanup/design.md D1`.
  Публикуется из `_SubagentLoggingHook.after_run`. Подписчик пишет
  `subagent_run_finished` в `agent_gateway_logs`.

Lifecycle:

* `start()` регистрирует подписки, собирая `unsubscribe` в
  `self._unsubscribers`.
* `stop()` вызывает их в LIFO-порядке.
* Повторный `start()` без `stop()` логирует warning и no-op (см.
  спеку `runtime-events-subscription`).

Использование:

```python
subscriber = RuntimeEventsSubscriber(bus, db_logging_service=svc)
subscriber.start()
# ... runtime ...
subscriber.stop()  # вызвать ДО MessageBus.drain()
```

См. `openspec/changes/runtime-events-subscription/specs/runtime/
runtime-events-observability/spec.md` для нормативного контракта.
"""

from __future__ import annotations

from typing import Any, Callable

from loguru import logger

from nanobot.bus.runtime_events import TurnCompleted, TurnRuntimeAdmitted

from lib.events.subagent import SubagentTurnCompleted
from lib.hooks.database_logging_hook import seed_context_window
from lib.services.db_logging_service import LogEvent


def _set_subagent_default_bus(bus: Any) -> None:
    """Установить bus для автопривязки к новым инстансам
    ``_SubagentLoggingHook``.

    Использует monkey-patch set_default_bus класса (если патч уже
    применён через ``RuntimePatcher.patch_subagent_logging``); no-op
    если класс ещё не подменён.

    См. openspec/changes/post-0.3.5-patches-cleanup/design.md D3
    (Subagent публикует SubagentTurnCompleted через bus.publish).
    """
    try:
        from nanobot.agent.subagent import _SubagentHook

        set_bus = getattr(_SubagentHook, "set_default_bus", None)
        if callable(set_bus):
            set_bus(bus)
    except Exception:
        pass


def _set_subagent_subscriber_registered(registered: bool) -> None:
    """Отметить, что ``SubagentLoggingSubscriber`` активен.

    Это сигнал ``_SubagentLoggingHook._finalize`` пропустить прямую
    запись ``subagent_run_finished`` в БД (подписчик уже записал).
    No-op если класс ещё не подменён monkey-patch'ем.

    См. openspec/changes/post-0.3.5-patches-cleanup/design.md D4.
    """
    try:
        from nanobot.agent.subagent import _SubagentHook

        set_flag = getattr(_SubagentHook, "set_subscriber_registered", None)
        if callable(set_flag):
            set_flag(registered)
    except Exception:
        pass


class RuntimeEventsSubscriber:
    """Observer-сервис для runtime-событий nanobot 0.3.5.

    Сигнатура полностью описана в `openspec/changes/runtime-events-
    subscription/specs/runtime/runtime-events-observability/spec.md`
    и расширена в `openspec/changes/post-0.3.5-patches-cleanup/specs/
    runtime/runtime-events-observability/spec.md`.
    """

    def __init__(
        self,
        bus: Any,
        db_logging_service: Any | None = None,
    ) -> None:
        self._bus = bus
        self._db_logging_service = db_logging_service
        self._unsubscribers: list[Callable[[], None]] = []
        self._started = False

    def start(self) -> None:
        """Зарегистрировать подписки на TurnRuntimeAdmitted через
        ``bus.subscribe(...)``.

        KOMMIT-1 (runtime-events-subscription): подписка только на
        ``TurnRuntimeAdmitted`` — seed лимита окна/модели в мост.
        Расширение на ``TurnCompleted`` + ``SubagentTurnCompleted``
        добавляется в KOMMIT-2 (post-0.3.5-patches-cleanup).

        Повторный вызов без ``stop()`` — no-op с warning (см. спеку).
        """
        if self._started:
            logger.warning(
                "RuntimeEventsSubscriber.start() called twice without stop(); "
                "skipping duplicate subscribe"
            )
            return
        self._unsubscribers.append(
            self._bus.subscribe(self._handle_turn_runtime_admitted, TurnRuntimeAdmitted)
        )
        self._unsubscribers.append(
            self._bus.subscribe(self._handle_turn_completed, TurnCompleted)
        )
        self._unsubscribers.append(
            self._bus.subscribe(self._handle_subagent_turn_completed, SubagentTurnCompleted)
        )
        # Wire subagent publishing: новые _SubagentLoggingHook инстансы
        # (создаются через patched _SubagentHook в runtime_patcher.py)
        # автоматически получают self._bus для публикации
        # SubagentTurnCompleted. См. design.md D3.
        _set_subagent_default_bus(self._bus)
        # Также сигнализируем _finalize о том, что подписчик активен.
        # Без этого флага будут дубли: и handler пишет через pub-sub,
        # и _finalize пишет напрямую. См. design.md D4.
        _set_subagent_subscriber_registered(True)
        self._started = True
        logger.debug(
            "RuntimeEventsSubscriber: зарегистрированы подписки на "
            "TurnRuntimeAdmitted, TurnCompleted, SubagentTurnCompleted"
        )

    def stop(self) -> None:
        """Дерегистрировать подписки в LIFO-порядке.

        Вызывать ДО ``MessageBus.drain()`` (порядок важен: in-flight
        handler'ы должны корректно дерегистрироваться до остановки шины).
        """
        while self._unsubscribers:
            unsubscribe = self._unsubscribers.pop()
            try:
                unsubscribe()
            except Exception as exc:
                logger.opt(exception=True).warning(
                    "RuntimeEventsSubscriber: unsubscribe failed: {}", exc
                )
        # Откатить флаг subagent-subscriber — после stop() _finalize
        # снова пишет subagent_run_finished напрямую (на случай
        # повторного старта). См. design.md D4.
        _set_subagent_subscriber_registered(False)
        self._started = False

    async def _handle_turn_runtime_admitted(self, event: TurnRuntimeAdmitted) -> None:
        """Seed лимита окна/модели в мост ``_CONTEXT_BRIDGE``.

        Вызывается из upstream `RuntimeEventPublisher.turn_runtime_admitted`
        перед первой LLM-итерацией оборота (для всех не-system каналов,
        см. `nanobot/agent/turn_delivery.py:_default_route`).
        """
        try:
            session_key = (event.context.session_key or "").strip()
            if not session_key:
                return
            runtime = event.runtime
            limit = getattr(runtime, "context_window_tokens", 0) or 0
            model = getattr(runtime, "model", "") or ""
            seed_context_window(session_key, limit=int(limit), model=str(model))
        except Exception as exc:
            logger.opt(exception=True).warning(
                "seed_context_window failed for {}: {}",
                getattr(event.context, "session_key", "?"),
                exc,
            )

    async def _handle_turn_completed(self, event: TurnCompleted) -> None:
        """Записать ``LogEvent(event_type="turn_completed")`` в
        ``agent_gateway_logs`` через ``DbLoggingService``.

        Пишет метрики оборота: latency, outcome, failure_kind,
        usage_tokens, runtime_model. Не содержит ``final_content`` —
        для пользовательского контента остаётся ``run_finished`` в
        ``DatabaseLoggingHook.after_run``.
        """
        if self._db_logging_service is None:
            return
        try:
            context = event.context
            runtime = event.runtime
            usage = getattr(event, "usage", None)
            round_usages = list(getattr(event, "round_usages", ()) or ())

            def _usage_to_dict(u: Any) -> dict | None:
                if u is None:
                    return None
                if isinstance(u, dict):
                    return dict(u)
                # 0.3.5: LLMUsage — frozen dataclass c to_turn_dict()/to_dict().
                if hasattr(u, "to_dict"):
                    try:
                        payload = u.to_dict()
                        return dict(payload) if isinstance(payload, dict) else None
                    except Exception:
                        pass
                if hasattr(u, "to_turn_dict"):
                    try:
                        payload = u.to_turn_dict()
                        return dict(payload) if isinstance(payload, dict) else None
                    except Exception:
                        pass
                # Fallback: duck-typing через атрибуты total_tokens/prompt_tokens.
                total = getattr(u, "total_tokens", None)
                if isinstance(total, int):
                    return {"total_tokens": total}
                return None

            usage_dict = _usage_to_dict(usage)
            usage_tokens = (
                usage_dict.get("total_tokens") if isinstance(usage_dict, dict) else None
            )
            if not isinstance(usage_tokens, int) or usage_tokens <= 0:
                round_total = 0
                for ru in round_usages:
                    d = _usage_to_dict(ru)
                    if isinstance(d, dict):
                        t = d.get("total_tokens")
                        if isinstance(t, int):
                            round_total += t
                usage_tokens = round_total or None

            payload: dict[str, Any] = {
                "latency_ms": int(event.latency_ms)
                if isinstance(event.latency_ms, int)
                else None,
                "outcome": str(getattr(event, "outcome", "completed")),
                "failure_kind": getattr(event, "failure_kind", None),
                "failure_error_kind": getattr(event, "failure_error_kind", None),
                "failure_attempts": getattr(event, "failure_attempts", None),
                "usage_tokens": usage_tokens,
                "runtime_model": getattr(runtime, "model", None)
                if runtime is not None
                else None,
            }
            self._db_logging_service.log_event(LogEvent(
                event_type="turn_completed",
                actor="agent",
                name="turn",
                session_id=context.session_key,
                channel=context.channel,
                summary=f"turn {payload['outcome']} "
                f"(latency={payload['latency_ms']}ms)",
                payload=payload,
            ))
        except Exception as exc:
            logger.opt(exception=True).warning(
                "_handle_turn_completed failed: {}", exc
            )

    async def _handle_subagent_turn_completed(
        self, event: SubagentTurnCompleted
    ) -> None:
        """Записать ``LogEvent(event_type="subagent_run_finished")``.

        Контракт payload ИДЕНТИЧЕН ``_SubagentLoggingHook._finalize``
        (`lib/services/runtime_patcher.py:1712-1737`): ``task_id``,
        ``task``, ``final_content``, ``tools_used``, ``stop_reason``,
        ``request_id``, ``parent_request_id``, ``parent_user_id``,
        ``usage_tokens``, ``had_error``, ``error``.
        """
        if self._db_logging_service is None:
            return
        try:
            payload = {
                "task_id": event.task_id,
                "task": event.task,
                "final_content": event.final_content or "",
                "tools_used": list(event.tools_used or []),
                "stop_reason": event.stop_reason,
                "request_id": event.request_id
                or f"subagent:{event.task_id}",
                "parent_request_id": event.parent_request_id,
                "parent_user_id": event.parent_user_id,
                "usage_tokens": getattr(
                    getattr(event, "usage", None), "total_tokens", None
                )
                if event.usage is not None
                else None,
                "had_error": bool(event.had_error),
            }
            if event.error:
                payload["error"] = event.error
            self._db_logging_service.log_event(LogEvent(
                event_type="subagent_run_finished",
                level="ERROR" if event.had_error else "INFO",
                actor="agent",
                name=event.task_id,
                session_id=f"subagent:{event.task_id}",
                channel="subagent",
                user_id=event.parent_user_id,
                request_id=payload["request_id"],
                summary=(payload["task"] or payload["final_content"])[:200],
                payload=payload,
            ))
        except Exception as exc:
            logger.opt(exception=True).warning(
                "_handle_subagent_turn_completed failed: {}", exc
            )


__all__ = ["RuntimeEventsSubscriber"]
