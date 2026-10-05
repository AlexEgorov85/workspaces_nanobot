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
* `TurnCompleted` — запись event_type `agent.completed` (метрики оборота) в
  `agent_gateway_logs` через `DbLoggingService.log_event(...)`. Метрики
  оборота: `latency_ms`, `outcome`, `failure_*`, `usage`.
* `SubagentTurnCompleted` — кастомный event нашего namespace, см.
  `openspec/changes/post-0.3.5-patches-cleanup/design.md D1`.
  Публикуется из `_SubagentLoggingHook.after_run`. Подписчик пишет
  `agent.completed` (итог подагента) в `agent_gateway_logs` — то же
  каноническое имя, различие видно по `payload`/`name`.

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

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger
from nanobot.bus.runtime_events import TurnCompleted, TurnRuntimeAdmitted

from lib.events.subagent import SubagentTurnCompleted
from lib.hooks.database_logging_hook import seed_context_window
from lib.services.db_logging_service import LogEvent
from lib.services.turn_identity import (
    TurnIdentityStore,
    read_turn_context,
    request_id_from,
)


def _current_sender_id() -> str | None:
    """``sender_id`` текущего оборота из контекста фреймворка.

    Тот же самый ``InboundMessage.sender_id``, который ``make_inbound_logger``
    отдаёт в ``register_request(user_id=...)`` (``nanobot/agent/loop.py:747``
    берёт его из ``ctx.msg.sender_id``). Значение не выдумывается и не
    подставляется: его нет — значит личности нет.

    Нужен именно во время оборота: ``bind_request_context`` в
    ``nanobot/agent/loop.py`` снимается до ``delivery.complete()``, поэтому
    после финала оборота контекста уже не существует.
    """
    # Чтение ``RequestContext`` живёт в ``lib/services/turn_identity.py``:
    # тот же contextvar читались хук подписи вызовов и клиент платформы, и три
    # копии правила разъезжались бы молча — подпись в журнале и файл сессии
    # описали бы разные вызовы.
    turn = read_turn_context()
    return turn.user_id if turn is not None else None


@dataclass(frozen=True)
class _TurnIdentity:
    """Личность оборота, снятая в тот момент, когда она ещё существует.

    События конца оборота (``agent.completed``) публикуются уже ПОСЛЕ
    ``DatabaseLoggingHook.after_run``, который дергает ``clear_request`` и
    опустошает индекс вопросов сервиса, и ПОСЛЕ снятия ``RequestContext``.
    К этому моменту подписать событие нечем, поэтому личность снимается на
    ``TurnRuntimeAdmitted`` — в середине оборота, где она ещё есть.
    """

    user_id: str
    request_id: str | None = None


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
    запись ``agent.completed`` в БД (подписчик уже записал).
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
        turn_identities: TurnIdentityStore | None = None,
    ) -> None:
        self._bus = bus
        self._db_logging_service = db_logging_service
        # Личность оборота берётся из хранилища рантайма, а не из журнала:
        # журнал не ею владеет, и ходить за чужой личностью в его словарь
        # было ровно тем, из-за чего подпись терялась.
        self._turn_identities = turn_identities
        self._unsubscribers: list[Callable[[], None]] = []
        self._started = False
        # Снятая в середине оборота личность, перенесённая на его событие.
        # Ключ — ``session_key``; сессии обрабатываются конкурентно, поэтому
        # доступ под замком. Это копия, а не сам снимок из хранилища: снимок
        # остаётся финальной доставке ответа.
        self._turn_identities_seen: dict[str, _TurnIdentity] = {}
        self._identity_lock = threading.Lock()
        # События ``agent.completed``, у которых личность снять не удалось.
        # Считаются здесь, а не молча теряются в транспорте.
        self._unidentified_turn_events = 0

    def unidentified_turn_events(self) -> int:
        """Сколько ``agent.completed`` ушло в журнал без полной личности.

        Ненулевое значение — прямое указание, что обороты теряются:
        транспорт не отправит событие без ``user_id``, а оператор об этом
        раньше не узнавал.
        """
        return self._unidentified_turn_events

    def _capture_identity(self, session_key: str) -> None:
        """Перенести личность оборота на его событие.

        Источник — ``TurnIdentityStore`` рантайма: там она лежит с момента
        входа оборота. Событие приходит после конца оборота, когда снимок
        уже не виден ни в индексе вопросов, ни в contextvar задачи, поэтому
        подписать его может только тот, кто заранее сохранил личность, —
        подписчик.

        Переносится только непустой ``sender_id``: подставленное значение
        (``"unknown"`` и подобное) записало бы событие в чужую личность —
        ровно то, от чего журнал намеренно отказывается. Если личности нет,
        копия не создаётся вовсе, и ``agent.completed`` уйдёт без неё
        (с WARNING и счётчиком), а не с выдуманной.
        """
        service = self._db_logging_service
        if service is None:
            # Раньше возврат был молчаливым, и этот случай был неотличим от
            # «личность не нашлась»: молчащий ранний выход стоит ПЕРЕД всеми
            # остальными проверками и глушит их объяснения. Теперь он говорит
            # о себе — иначе промах этого вида ищут месяцами.
            logger.warning(
                "захват личности оборота невозможен для сессии {}: журнал не "
                "передан подписчику, спрашивать request_id негде",
                session_key,
            )
            return

        # Откуда берётся личность. Хранилище принадлежит рантайму и кладёт её
        # на входе оборота, где известны и отправитель, и вопрос.
        #
        # Журнал её не выведет сам: для события без вопроса он берёт снимок
        # входа только если в снимке самом вопроса нет, иначе событие утечёт в
        # чужой scope. Значит событие оборота подписывает тот, кто знает, что
        # это за оборот, — подписчик, — и личность берёт у её владельца.
        #
        # Прежним источником был contextvar, и это было гарантированно слепо:
        # контекст привязан к задаче оборота, а подписчик живёт в своей. На
        # практике agent.completed терял подпись в каждом обороте, тогда как
        # следом идущий agent.delivered её получал.
        identity = None
        store = self._turn_identities
        if store is not None:
            try:
                identity = store.current(session_key)
            except Exception:
                identity = None

        user_id = identity.user_id if identity is not None else None
        if not isinstance(user_id, str) or not user_id:
            # Журнал личности не знает — остаётся контекст оборота. Выдумывать
            # значение нельзя: подставленный отправитель записал бы событие в
            # чужую личность.
            user_id = _current_sender_id()
        if not user_id:
            # Промах без объяснения неразбираем: молчаливый возврат выглядит
            # как «личности нет», а означает одно из трёх — стор не передан,
            # запись по сессии не найдена или отправитель в контексте пуст. На
            # живом прогоне их надо различать: без этого следующий промах снова
            # останется загадкой, и выискивать придётся снова.
            logger.warning(
                "захват личности оборота не удался для сессии {}: стор {}, "
                "записей в нём {}, запись по сессии {}, отправитель в контексте {}",
                session_key,
                "передан" if store is not None else "НЕ ПЕРЕДАН",
                len(store) if store is not None else 0,
                "есть" if identity is not None else "НЕ НАЙДЕНА",
                _current_sender_id() or "ПУСТ",
            )
            return

        request_id: str | None = (
            identity.request_id if identity is not None else None
        )
        if not request_id:
            # Журнал здесь — фасад над тем же хранилищем, а не второй
            # источник: ответ у них один. Отказ хранилища не повод терять
            # user_id, он уже получен и его достаточно для подписи вызова.
            request_id = request_id_from(service, session_key)
        with self._identity_lock:
            self._turn_identities_seen[session_key] = _TurnIdentity(
                user_id=user_id, request_id=request_id
            )

    def _take_identity(self, session_key: str) -> _TurnIdentity | None:
        """Забрать снимок личности оборота (одноразово)."""
        if not session_key:
            return None
        with self._identity_lock:
            return self._turn_identities_seen.pop(session_key, None)

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
        # Снимки identity переживать остановку не должны: следующий start()
        # обслуживает уже другие обороты.
        with self._identity_lock:
            self._turn_identities_seen.clear()

    async def _handle_turn_runtime_admitted(self, event: TurnRuntimeAdmitted) -> None:
        """Seed лимита окна/модели в мост ``_CONTEXT_BRIDGE``.

        Вызывается из upstream `RuntimeEventPublisher.turn_runtime_admitted`
        перед первой LLM-итерацией оборота (для всех не-system каналов,
        см. `nanobot/agent/turn_delivery.py:_default_route`).

        Здесь же снимается личность оборота: событие приходит в середине
        оборота, когда ``RequestContext`` привязан, а индекс вопросов
        сервиса ещё не очищен. На ``TurnCompleted`` обоих уже не будет.
        """
        try:
            session_key = (event.context.session_key or "").strip()
            if not session_key:
                return
            runtime = event.runtime
            limit = getattr(runtime, "context_window_tokens", 0) or 0
            model = getattr(runtime, "model", "") or ""
            seed_context_window(session_key, limit=int(limit), model=str(model))
            self._capture_identity(session_key)
        except Exception as exc:
            logger.opt(exception=True).warning(
                "seed_context_window failed for {}: {}",
                getattr(event.context, "session_key", "?"),
                exc,
            )

    async def _handle_turn_completed(self, event: TurnCompleted) -> None:
        """Записать ``LogEvent(event_type="agent.completed")`` в
        ``agent_gateway_logs`` через ``DbLoggingService``.

        Пишет метрики оборота: latency, outcome, failure_kind,
        usage_tokens, runtime_model. Не содержит ``final_content`` —
        для пользовательского контента остаётся ``agent.responded`` в
        ``DatabaseLoggingHook.after_run``.

        Личность (``user_id``/``request_id``) берётся из снимка, снятого
        на ``TurnRuntimeAdmitted``: к моменту публикации этого события
        индекс вопросов сервиса уже очищен ``clear_request``, а
        ``RequestContext`` снят, поэтому подписать вызов иначе нечем.

        **Документированный выбор при отсутствии снимка:** событие всё
        равно уходит в очередь (так его поведение не меняется для вызовов
        без ``sender_id``), но потеря становится видимой на месте —
        WARNING и рост :meth:`unidentified_turn_events`. Выдуманный
        ``user_id`` не подставляется никогда: транспорт всё равно
        отбросил бы группу, но записал бы её под чужой личностью.
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
            identity = self._take_identity((context.session_key or "").strip())
            if identity is None:
                # Не выдумываем «unknown»: событие уходит без подписи.
                #
                # Раньше здесь стояло «событие не будет записано», и это было
                # неверно: журнал умеет достроить подпись по привязке живого
                # вопроса. На боевых данных именно так и вышло — промахнувшийся
                # подписчик давал неподписанное событие, а строка доезжала в
                # базу подписанной. Правдивый текст: подписи от подписчика не
                # будет, и достроит ли журнал — зависит от того, жива ли ещё
                # привязка вопроса.
                self._unidentified_turn_events += 1
                logger.warning(
                    "turn_completed без личности у подписчика для сессии {}: "
                    "подпись достроит журнал, только если привязка живого "
                    "вопроса ещё стоит; сам захват промахнулся (всего таких: {})",
                    context.session_key,
                    self._unidentified_turn_events,
                )
            self._db_logging_service.log_event(LogEvent(
                event_type="agent.completed",
                actor="agent",
                name="turn",
                session_id=context.session_key,
                channel=context.channel,
                user_id=identity.user_id if identity is not None else None,
                request_id=identity.request_id if identity is not None else None,
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
        """Записать ``LogEvent(event_type="agent.completed")``.

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
                event_type="agent.completed",
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
