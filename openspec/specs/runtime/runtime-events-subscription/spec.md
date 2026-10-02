# runtime/runtime-events-subscription Specification

## Purpose
Defines the observer-pattern contract for nanobot-ai runtime events
(`TurnRuntimeAdmitted`) in our project: a single integration point for
subscribing to `MessageBus.publish`-based event streams, with explicit
lifecycle (`start`/`stop`) and idempotent context-bridge seeding.

## Scope

`agent` — подписка на runtime-события — агентская
Реализация: `lib/services/runtime_events_subscriber.py`

## Requirements

### Requirement: Подписка на TurnRuntimeAdmitted для seed context bridge

`RuntimeEventsSubscriber.start()` MUST ДОЛЖЕН зарегистрировать через
`bus.subscribe(handler, TurnRuntimeAdmitted)` ровно ОДИН async-handler,
который:

* читает `event.context.session_key` (string) и `event.runtime.context_window_tokens`
  (int) и `event.runtime.model` (string);
* вызывает `context_bridge.seed_context_window(session_key, limit=..., model="...")`
  **до** любых других side-effects;
* если `session_key` пуст — выходит без записи и без side-effects;
* исключения от `seed_context_window` MUST ДОЛЖНЫ быть поглощены с
  warning (handler MUST NOT НЕ ДОЛЖЕН бросать дальше).

#### Scenario: Стандартный оборот агента с известным runtime

- **WHEN** `RuntimeEventPublisher.turn_runtime_admitted(msg, session_key, runtime)`
  зовётся upstream'ом перед первой итерацией оборота
- **AND** `event.runtime.context_window_tokens = 40000` и `event.runtime.model = "MiniMax-M3"`
- **THEN** `DatabaseLoggingHook._CONTEXT_BRIDGE[session_key]` MUST ДОЛЖЕН
  после события содержать `{"limit": 40000, "model": "MiniMax-M3"}`
- **AND** `metadata.context_window` в `_assemble_outbound` финального
  outbound'а MUST ДОЛЖЕН быть непуст даже при пустом `_last_usage`.

#### Scenario: Гонка подписки с in-flight publish

- **WHEN** `RuntimeEventsSubscriber.start()` запущен после `apply_all`
  и до старта каналов
- **AND** `MessageBus.publish(TurnRuntimeAdmitted(...))` зовётся сразу
  после `start()` (рассогласование на 1 мс)
- **THEN** handler MUST ДОЛЖЕН получить событие через локальный
  `_handlers` fan-out (`nanobot.bus.queue.MessageBus.publish`).

#### Scenario: Дерегистрация в stop

- **WHEN** `RuntimeEventsSubscriber.stop()` зовётся после `start()`
- **THEN** все subscribe-closures MUST ДОЛЖНЫ быть вызваны в LIFO-порядке.
- **AND** in-flight handler предыдущего `TurnRuntimeAdmitted`
  MUST ДОЛЖЕН завершиться до того, как `MessageBus` будет остановлен.

### Requirement: Контракт handler'а

Handler MUST ДОЛЖЕН быть `async def handler(event)` или sync-callable.
Handler MUST NOT НЕ ДОЛЖЕН делать ничего, кроме (а) обновления моста
через `context_bridge.seed_context_window(...)` и (б) опционального
логирования через `loguru.logger`. Никаких HTTP/RPC/DB-вызовов.

#### Scenario: Исключение в handler'е

- **WHEN** `context_bridge.seed_context_window` бросает
  `RuntimeError("bridge down")`
- **THEN** handler MUST ДОЛЖЕН залогировать warning через
  `logger.opt(exception=True).warning(...)` и MUST NOT НЕ ДОЛЖЕН
  пробрасывать исключение.
- **AND** другие runtime-handler'ы MUST ДОЛЖНЫ получить свои события
  без задержек (исключение НЕ прерывает `bus.publish`).

#### Scenario: Пустой session_key — no-op

- **WHEN** событие `TurnRuntimeAdmitted` приходит с `context.session_key`
  пустой строкой или `None`
- **THEN** handler MUST ДОЛЖЕН выйти без вызова `seed_context_window`
  и без записи в лог.

### Requirement: Идемпотентность seed

Двойной вызов `seed_context_window` для одной и той же `session_key`
MUST ДОЛЖЕН быть идемпотентным. Это требование к `ContextBridge`-реализации
(НЕ к handler'у): первая запись выигрывает, последующие MUST NOT
НЕ ДОЛЖНЫ затирать более новый `usage` от `_store_iteration_usage`.

#### Scenario: Повторный seed не затирает usage

- **WHEN** `bus.publish(TurnRuntimeAdmitted(limit=40000))` отрабатывает,
  потом `seed_context_window` пишет `usage` через `_store_iteration_usage`
- **AND** в течение оборота приходит `TurnRuntimeAdmitted(limit=80000)`
- **THEN** bridge MUST ДОЛЖЕН хранить либо `limit=40000`, либо `limit=80000`
  (в зависимости от того, что новее в рамках bridge-семантики),
  но MUST NOT НЕ ДОЛЖЕН затирать валидный `usage`.

### Requirement: Защита от двойного start

`RuntimeEventsSubscriber.start()` MUST NOT НЕ ДОЛЖЕН регистрировать
подписки повторно, если `start()` уже был вызван и `stop()` не был.

#### Scenario: Повторный start

- **WHEN** `start()` уже вызван, `stop()` не вызывался
- **AND** `start()` вызывается снова
- **THEN** MUST ДОЛЖЕН быть залогирован warning через `logger.warning(...)`
- **AND** MUST NOT НЕ ДОЛЖЕН быть вызван `bus.subscribe(...)` повторно.

### Requirement: Контекстный мост как абстракция

`ContextBridge` MUST ДОЛЖЕН быть протоколом с единственным методом
`seed_context_window(session_key: str, *, limit: int, model: str = "") -> None`.
Реализация по умолчанию `DatabaseLoggingContextBridge` MUST ДОЛЖЕН
делегировать в `lib.hooks.database_logging_hook.seed_context_window`
(ничего больше).

#### Scenario: Default-имплементация — мост в DatabaseLoggingHook

- **WHEN** `DatabaseLoggingContextBridge.seed_context_window("postgres:1", limit=40000, model="MiniMax-M3")` зовётся
- **THEN** `lib.hooks.database_logging_hook.seed_context_window` MUST ДОЛЖЕН быть вызван с теми же аргументами.
- **AND** больше никаких side-effects (никаких DB-вызовов, никаких HTTP).

### Requirement: Контракт Subscribe между observer'ом и MessageBus

Подписка MUST ДОЛЖЕН использовать только публичный API:
`MessageBus.subscribe(handler, event_type)` (см.
`nanobot/bus/queue.py:92`). Observer MUST NOT НЕ ДОЛЖЕН вызывать
`publish_event` или модифицировать `bus.outbound`/`OutboundMessage.event`
(это разные очереди — для compaction-events другой consumer).

#### Scenario: Observer не лезет в compaction-канал

- **WHEN** `RuntimeEventsSubscriber.start()` запущен
- **AND** upstream публикует `ContextCompactionEvent` через
  `EventSink.emit` → `bus.publish_event` → `OutboundMessage.event`
- **THEN** observer MUST NOT НЕ ДОЛЖЕН получать это событие
  (он подписан на `TurnRuntimeAdmitted`, не на `ContextCompactionEvent`).
- **AND** `postgres_channel.send` по-прежнему зовёт
  `compaction_event_subscriber.feed(msg)` для compaction-событий.
