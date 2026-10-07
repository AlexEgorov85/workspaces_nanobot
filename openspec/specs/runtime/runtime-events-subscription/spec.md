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
* вызывает `seed_context_window(session_key, limit=..., model="...")`
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
через `seed_context_window(...)` и (б) опционального
логирования через `loguru.logger`. Никаких HTTP/RPC/DB-вызовов.

#### Scenario: Исключение в handler'е

- **WHEN** `seed_context_window` бросает
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
MUST ДОЛЖЕН быть идемпотентным. Это требование к `seed_context_window`
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

### Requirement: Контекстное окно сеется прямой функцией

`RuntimeEventsSubscriber` MUST ДОЛЖЕН сеять контекстное окно прямым
вызовом `lib.hooks.database_logging_hook.seed_context_window`
(`lib/services/runtime_events_subscriber.py:57`). Отдельного протокола
`ContextBridge` и модуля-обёртки `lib/services/context_bridge.py` MUST NOT
быть: producer у этого окна один, и посредник между двумя функциями ничего
не добавлял, кроме лишнего слоя.

Ответственность при этом не сливается: subscriber отвечает только за
подписку, а `seed_context_window` — только за запись bridge-state в
`DatabaseLoggingHook`.

Раньше здесь требовался протокол `ContextBridge` с реализацией
`DatabaseLoggingContextBridge`. Модуль-обёртка не был создан — это записано
как DEVIATION в `openspec/changes/archive/2026-09-27-runtime-events-subscription/tasks.md:5-12`
(«абстракция избыточна для единственного producer'а»), и canon не был приведён
к принятому решению.

#### Scenario: Сев контекстного окна

- **WHEN** подписчик получает событие, требующее записи контекстного окна
- **THEN** `lib.hooks.database_logging_hook.seed_context_window` MUST ДОЛЖЕН
  быть вызван напрямую с теми же аргументами
- **AND** больше никаких side-effects (никаких DB-вызовов, никаких HTTP)
- **AND** имя `ContextBridge` MUST NOT появляться в коде проекта

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

## Responsibility

Единая точка подписки на runtime-события шины: регистрация обработчиков,
перенос личности оборота на его событие и запись `agent.completed` в журнал.
Владелец — `lib/services/runtime_events_subscriber.py`; подписка начинается
в `ApplicationContext.start` и снимается в остановке
(`lib/core/application_context.py:774`, `:779`, `:901`).

## Boundary

- **Внутри:** подписка/отписка, захват личности оборота, запись
  `agent.completed`, seed лимита окна в мост.
- **Снаружи:** публикация событий — `RuntimeEventPublisher`; мост окна и его
  словарь — `lib/hooks/database_logging_hook.py`; запись в БД —
  `DbLoggingService`; владение личностью оборота — `TurnIdentityStore`
  рантайма.

## Public Contract

- `RuntimeEventsSubscriber(bus, db_logging_service=None, turn_identities=None)`
  (`lib/services/runtime_events_subscriber.py:150-155`).
- `start()` (`:274`) — регистрирует подписки и включает публикацию subagent;
  `stop()` (`:315`) — дерегистрирует в LIFO и чистит снимки личности.
- Обработчики: `_handle_turn_runtime_admitted` (`:339`),
  `_handle_turn_completed` (`:366`), `_handle_subagent_turn_completed`.
- `unidentified_turn_events() -> int` (`:174-181`) — счётчик событий, ушедших
  без полной личности.
- Модулевые переключатели subagent-публикации:
  `_set_subagent_default_bus` (`:101`), `_set_subagent_subscriber_registered`
  (`:122`).
- `__all__ = ["RuntimeEventsSubscriber"]` (`:533`).

## Inputs

- `bus` — `MessageBus`; подписка идёт через `bus.subscribe(handler, EventType)`
  (`:291-299`).
- Типы событий: `TurnRuntimeAdmitted`, `TurnCompleted`,
  `SubagentTurnCompleted`.
- `event.context.session_key` (str), `event.runtime.context_window_tokens` (int),
  `event.runtime.model` (str) (`:351-357`).
- `db_logging_service` — `DbLoggingService` или `None`;
  `turn_identities` — `TurnIdentityStore` или `None`.

## Outputs

- Seed окна в мост: `seed_context_window(session_key, limit=int(limit), model=str(model))`
  (`:357`).
- `LogEvent(event_type="agent.completed")` в `agent_gateway_logs` с метриками
  оборота: latency, outcome, failure_kind, usage_tokens, runtime_model
  (`:366-373`).
- Событие завершения subagent-оборота.
- WARNING при неудачном seed (`:359-364`), при отсутствии журнала
  (`:204-208`), при неудачной отписке (`:325-328`).

## State

`_turn_identities_seen: dict[session_key, _TurnIdentity]` под
`threading.Lock` (`lib/services/runtime_events_subscriber.py:168-169`) —
копия снимка личности, а не сам снимок: снимок остаётся финальной доставке
ответа. Плюс счётчик `_unidentified_turn_events` (`:172`) и список
`_unsubscribers` (`:162`). Снимки не переживают остановку — `stop()`
очищает словарь (`:336-337`).

## Dependencies

- `nanobot.bus.queue.MessageBus.subscribe` / `unsubscribe` (`:291-324`);
- типы событий рантайма nanobot;
- `lib.hooks.database_logging_hook.seed_context_window`
  (`lib/hooks/database_logging_hook.py:82`) — мост окна, общий словарь
  `_CONTEXT_BRIDGE` под локом (`:78-79`);
- `lib.events.subagent` — событие и публикация subagent-оборотов;
- `lib.services.db_logging_service` — журнал;
- `lib/core/application_context.py:771-787` — сборка и старт.

## Configuration

Отдельной секции настроек нет. Настройками здесь служат состав
экземпляра (`db_logging_service`, `turn_identities`) и то, какие события
объявлены в `_unsubscribers` при `start()`. Уровни журнала и глубина вывода
приходят из своих подсистем, а не отсюда.

## Lifecycle

1. `ApplicationContext` создаёт подписчик и зовёт `start()`
   (`lib/core/application_context.py:774`, `:779`); неудача старта
   логируется, а не роняет контекст (`:782-787`).
2. `start()` подписывает три обработчика (`:291-299`), передаёт шину
   subagent-публикации (`:304`) и выставляет флаг «подписчик активен»
   (`:308`).
3. Повторный `start()` без `stop()` — no-op с WARNING, дублирующая подписка
   не создаётся (`:285-290`).
4. `stop()` дерегистрирует в LIFO (`:321-328`), снимает флаг subagent
   (`:332`) и чистит снимки личности (`:336-337`).
5. Порядок остановки: `stop()` подписчика — **после** `drain()` шины, чтобы
   in-flight обработчики корректно дерегистрировались
   (`lib/services/runtime_events_subscriber.py:317-319`,
   `lib/core/application_context.py:897-901`).

## Data Ownership

Мост окна принадлежит хуку журнала: `_CONTEXT_BRIDGE` и его функции
(`lib/hooks/database_logging_hook.py:78-176`). Подписчик его только наполняет
и кладёт личность на событие — он не владеет ни словарём окна, ни снимком
личности (`lib/services/runtime_events_subscriber.py:166-168`). Запись
принадлежит журналу; уникальные поля события (`user_id`, `request_id`)
переносятся, а не выдумываются.

## Error Behavior

- Исключение от `seed_context_window` поглощается с WARNING и traceback в
  loguru; наружу не пробрасывается
  (`lib/services/runtime_events_subscriber.py:359-364`).
- Пустой `session_key` — выход без записи и без побочных эффектов (`:352-353`).
- `db_logging_service is None` на `TurnCompleted` — тихий выход (`:387-388`);
  на захвате личности вместо этого WARNING с объяснением: молчащий ранний
  выход глушил бы объяснения остальных проверок (`:199-209`).
- Неудачная отписка не прерывает остановку: WARNING и продолжение цикла
  (`:325-328`).
- Отсутствие личности не выдумывается: событие всё равно уходит в очередь,
  потеря становится видимой через WARNING и рост
  `unidentified_turn_events` (`:380-385`).

## Invariants

- `seed_context_window` вызывается **до** любых других побочных эффектов
  (`:357-358`).
- Подписка ровно одна на каждое событие: повторный `start()` без `stop()`
  ничего не добавляет (`:285-290`).
- Флаг «подписчик активен» поднимается вместе с подписками и снимается в
  `stop()`, иначе были бы дубли: и handler пишет через pub-sub, и `_finalize`
  пишет напрямую (`:305-308`, `:329-332`).
- Переносится только непустой `sender_id`: подставленное значение записало бы
  событие в чужую личность (`:192-196`).
- `session_key` нормализуется обрезкой пробелов (`:351`).
- Снимки личности не переживают `stop()` — следующий `start()` обслуживает
  уже другие обороты (`:334-337`).

## Forbidden Behavior

- Пробрасывать исключение из обработчика события наружу.
- Выдумывать `user_id`/`request_id`, когда снимка нет: транспорт всё равно
  отбросил бы группу, но записал бы её под чужой личностью (`:382-385`).
- Брать личность из contextvar задачи оборота: подписчик живёт в своей
  задаче, контекст привязан к задаче оборота, поэтому такой источник
  гарантированно слеп (`:219-220`).
- Ходить за личностью в словарь журнала — журнал ею не владеет
  (`:158-160`).
- Подписываться дважды без `stop()`.
- Останавливать подписчик **до** `MessageBus.drain()` (`:317-319`).
- Молча выходить при `db_logging_service is None` в захвате личности: этот
  случай неотличим от «личность не нашлась» (`:200-208`).

## Consumers

- `lib/core/application_context.py:771-787` — сборка и старт; `:897-901` —
  остановка.
- `lib/hooks/database_logging_hook.py:82` — приём seed'а окна.
- `lib/events/subagent.py` — событие `SubagentTurnCompleted` и его публикация.
- `nanobot.bus.queue.MessageBus` — источник событий и место отписки.
- Оператор — по `agent.completed` в `agent_gateway_logs` и по ненулевому
  `unidentified_turn_events`.

## Implementation

Существующие на диске пути:

- `lib/services/runtime_events_subscriber.py` — подписка, личность оборота,
  запись `agent.completed`;
- `lib/hooks/database_logging_hook.py` — мост `_CONTEXT_BRIDGE`,
  `seed_context_window` (`:82`), чтение и снятие записи;
- `lib/events/subagent.py` — событие и публикация subagent-оборотов;
- `lib/services/runtime_patcher.py` — патч `_SubagentHook`, чьи инстансы
  автоматически получают шину;
- `lib/core/application_context.py` — старт и остановка подписчика;
- `lib/services/db_logging_service.py` — запись в журнал.

## Verification

- `tests/test_runtime_events_subscriber.py` — подписка, отписка, seed, личность
  оборота, счётчик неопознанных событий;
- `tests/test_turn_identity.py` — владение и перенос личности оборота.
