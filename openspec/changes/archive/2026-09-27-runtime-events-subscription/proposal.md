# Proposal: runtime-events-subscription

## Why

После upgrade на `nanobot-ai 0.3.5` метрика `metadata.context_window` в финальном
`OutboundMessage` собирается через fallback в `RuntimePatcher._attach_context_window`,
который читает `agent._last_usage` (накопление prompt_tokens по итерациям оборота).
Подход рабочий, но даёт два систематических дефекта:

1. **«used=0» на коротких оборотах.** Если ответ не вызывает ни одного tool'а
   (например, чистый small-talk или single-step генерация), `_last_usage`
   заполняется **после** модели; до того — пуст. Сейчас bridge собирается
   из fallback'а, и `metadata.context_window.used` приходит нулевой/устаревший
   до первого `after_iteration`-тика `DatabaseLoggingHook`. В Postgres-канале
   это видно в `_flush_live_context` (см.
   `lib/channels/postgres_channel.py::_flush_live_context`) как пустая
   строка занятости.
2. **`limit`/`model` известны только с задержкой.** Bridge засевает
   `seed_context_window(session_key, limit, model)` из `agent._state_build`,
   но этот метод удалён в 0.3.5 (`RuntimePatcher.patch_context_bridge_seed`
   стал no-op в `openspec/changes/nanobot-035-upgrade`, см. design.md §D7).

В nanobot 0.3.5 есть штатный runtime-event: `TurnRuntimeAdmitted(context, runtime)`
публикуется через `RuntimeEventPublisher.turn_runtime_admitted`
(`nanobot/bus/runtime_events.py:204`), который зовёт `self.bus.publish(...)`
(`nanobot/bus/queue.py:118` — локальный fan-out, доступный через
`bus.subscribe(handler, event_type)`). Поле `event.runtime.context_window_tokens`
и `event.runtime.model` — **публичные** и стабильные across upgrades.

Этот change делает подписку на это событие, чтобы seed bridge заполнялся
**точной** информацией сразу на старте каждого оборота.

## What Changes

### NEW

* Новый файл `lib/services/runtime_events_subscriber.py` — observer-сервис,
  инкапсулирующий `bus.subscribe(handler, TurnRuntimeAdmitted)` и
  `unsubscribe` в `stop()`. Handler пишет
  `(session_key, limit, model)` в `DatabaseLoggingHook._CONTEXT_BRIDGE`
  через `seed_context_window(...)`.

### MODIFIED

* `lib/core/application_context.py::_start_runtime_events_subscriber(...)` —
  публичный метод, регистрирующий подписку **ОДИН раз** на lifecycle
  между `AgentFactory.create` и стартом каналов. Возвращает объект
  observer'а с `stop()` для shutdown.
* `lib/lifecycle/gateway_runner.py` (и эквивалент в `cli_agent.py`) —
  вызов `_start_runtime_events_subscriber()` после инициализации
  сервисов и до старта каналов; вызов `stop()` в shutdown-последовательности
  **до** `MessageBus.drain()` (порядок важен: подписчик должен
  корректно дерегистрироваться до остановки шины).

### Сохраняется как есть

* `RuntimePatcher._attach_context_window` — fallback для тех случаев,
  когда observer ещё не успел засеять или был дерегистрирован. Метод
  остаётся в `RuntimePatcher` как гарантия корректного `metadata.context_window`
  в финале.
* `RuntimePatcher.patch_context_bridge_seed` — no-op с
  `(True, "context-bridge seed moved to bus.subscribe(TurnRuntimeAdmitted)")`.
  Ничего не меняется.

### Capabilities

#### New Capabilities

* `runtime/runtime-events-subscription` — нормативный контракт на
  observer-паттерн для nanobot runtime-событий в нашем проекте: какие
  события подписываются, в каком порядке, как unsubscribe, как изолировано
  тестирование. Описывает структуру `RuntimeEventsSubscriber`, его
  lifecycle, контракт handler'а, требования к контрактным тестам.
  Документирует, что `TurnRuntimeAdmitted` подписка — это первый
  потребитель; будущие runtime-события (`UserInputAccepted`,
  `SessionTurnStarted`, `RunStatusChanged`) подключаются тем же механизмом.

#### Modified Capabilities

* `runtime/context` — добавлено требование: `ApplicationContext.start()`
  MUST ДОЛЖЕН запустить `RuntimeEventsSubscriber` после
  `RuntimePatcher.apply_all` и до старта каналов; `bridge.seed_context_window`
  MUST ДОЛЖЕН быть вызван к моменту первого `OutboundMessage`, идущего
  в канал. Уточняет, что `metadata.context_window` MUST ДОЛЖЕН быть
  непустым при `_final_turn=True` (включая обороты без tool-вызовов).

### Impact

* `lib/services/runtime_events_subscriber.py` (new): ~80 строк +
  unit-тест `tests/test_runtime_events_subscriber.py` (~50 строк).
* `lib/core/application_context.py`: +1 метод (~25 строк),
  +1 вызов в `start()`, +1 вызов в `stop()`. Lifecycle не меняется.
* `lib/lifecycle/gateway_runner.py` + `cli_agent.py`: +2 вызова
  (start/stop) каждый — тривиальная подвязка. Альтернатива — централизовать
  в `ApplicationContext`, чтобы caller не дублировал; решение в design §D2.
* `tests/contract/test_runtime_events_api.py` (new): фиксирует, что
  `TurnRuntimeAdmitted` имеет публичные поля `context`/`runtime`, и
  что `bus.subscribe(handler, TurnRuntimeAdmitted)` принимает
  callable вида `async def handler(event)`.
* `tests/test_runtime_events_subscriber.py` (new): unit-тест с
  fake-bus и fake-bridge.
* Документация: `docs/architecture/runtime-patcher-inventory.md` —
  статус `context_bridge_seed` обновляется с `DEPRECATED` на
  `MOVED` (со ссылкой на `RuntimeEventsSubscriber`). Новый ADR
  `docs/architecture/decisions/runtime-events-subscriber.md` с
  архитектурным обоснованием observer-паттерна.

### Вне scope

* Подписка на другие runtime-события (`UserInputAccepted`,
  `SessionTurnStarted`, `RunStatusChanged`, `RecoveryStateEvent`,
  `RetryWaitEvent`, `RetryStatusEvent`). Они идут через тот же
  `bus.subscribe(event_type=...)` API, но конкретные потребители
  проектируются отдельно (при необходимости).
* Изменение `_attach_context_window` (fallback остаётся как
  обороноспособная защита, см. R7 в дизайне `nanobot-035-upgrade`).
* Покрытие каналов compaction-events для Redis/Streamlit
  (`openspec/changes/nanobot-035-upgrade` §R8) — отдельный change.
* Изменение `MessageBus` или `bus.subscribe` API (используется
  публичный интерфейс `nanobot/bus/queue.py:92`).
* Изменение `DatabaseLoggingHook._CONTEXT_BRIDGE` (используется
  публичный API `seed_context_window`).

### Зависимости

* `nanobot-ai>=0.3.5` (уже зафиксировано в `requirements.txt`).
* `lib/services/database_logging_hook.py` — публичный helper
  `seed_context_window` уже есть.
* `lib/services/runtime_patcher.py` — `apply_all` уже отрабатывает
  до этого change.

### Связанные спеки

* `openspec/changes/nanobot-035-upgrade` (применена в 0.3.5) —
  §R7 явно отсрочила эту работу в `ISSUE-NB035-5`. Этот change
  закрывает тот ISSUE.
* `openspec/changes/nanobot-035-upgrade` §R8 — покрытие
  `compaction_events_subscriber` для Redis/Streamlit — отдельный
  change (тоже не блокирует merge этого).
