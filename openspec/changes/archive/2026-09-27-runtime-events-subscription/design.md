## Context

В `nanobot-ai 0.3.5` runtime-метаданные (model, `context_window_tokens`) доступны
через event-stream: `RuntimeEventPublisher` публикует типизированные
`AgentEvent` в `bus.publish` (локальный fan-out). Подписчики регистрируются
через `bus.subscribe(handler, event_type)` (`nanobot/bus/queue.py:92`) и
получают события ровно тех типов, которые они запросили (см.
`bus/queue.py:104-107`).

До 0.3.5 наш проект получал ту же информацию через wrapping
`agent._state_build` (private method) — патч `patch_context_bridge_seed`
в `RuntimePatcher` вызывал `seed_context_window(session_key, limit, model)`
перед первой итерацией. Этот патч стал no-op после upgrade (см.
`openspec/changes/nanobot-035-upgrade` §D7). Для замены нужен observer
на `TurnRuntimeAdmitted(context, runtime)`.

`TurnRuntimeAdmitted` — это **публичный** dataclass в
`nanobot.events` с полями `context: RuntimeEventContext` и
`runtime: LLMRuntime`. Публикация идёт через
`RuntimeEventPublisher.turn_runtime_admitted` после выбора runtime'а
для оборота (`AgentLoop._dispatch` / `_turn_outbound` → bus-side
publish). Подписка `bus.subscribe(handler, TurnRuntimeAdmitted)` обходит
все приватные API.

Дополнительный аргумент в пользу подписки: `DatabaseLoggingHook`
уже сейчас пишет **по-итерационный** usage в `_CONTEXT_BRIDGE[session_key]`
через `_store_iteration_usage` в `after_iteration`. Подписка на
`TurnRuntimeAdmitted` закроет обратную сторону — стартовый seed лимита
и модели. В мосте получается согласованная пара значений: limit
(из подписки) + used (из `after_iteration`). UI получает точную метрику
**уже на первом обороте**, без ожидания первого tool-вызова.

## Goals / Non-Goals

### Goals

1. Подписаться на `TurnRuntimeAdmitted` через `bus.subscribe(handler, event_type)`
   в рамках lifecycle `ApplicationContext.start()`.
2. Засеять `DatabaseLoggingHook._CONTEXT_BRIDGE[session_key]` точным
   `(limit, model)` сразу на старте каждого оборота — до первого
   `_assemble_outbound` (то есть до момента формирования
   `metadata.context_window`).
3. Корректно дерегистрироваться на `ApplicationContext.stop()` /
   `gateway_runner.stop()` / `cli_agent.shutdown` — **до** `MessageBus.drain()`.
4. Изолированное тестирование: unit-тест с fake-bus и fake-bridge,
   контрактный тест на сигнатуру `TurnRuntimeAdmitted` (freeze API).
5. Сохранить `_attach_context_window`-fallback как обороноспособную защиту:
   если подписка не сработала или была дерегистрирована — `metadata.context_window`
   всё равно соберётся из `agent._last_usage` (см. §D7 `nanobot-035-upgrade`).

### Non-Goals

* Подписка на другие runtime-события.
* Изменение `_attach_context_window` / `_store_iteration_usage` /
  другого кода `DatabaseLoggingHook`.
* Покрытие Redis/Streamlit compaction-events (см.
  `openspec/changes/nanobot-035-upgrade` §R8).
* Изменение шины, схем, миграций БД.

## Решения

### D1: Расположение observer'а — `lib/services/runtime_events_subscriber.py`

Класс `RuntimeEventsSubscriber` инкапсулирует всё, что относится к подписке.
Тот же паттерн, что `CompactionEventSubscriber`
(`lib/services/compaction_event_subscriber.py`) и `DbLoggingService`
(`lib/services/db_logging_service.py`) — отдельный файл, отдельная
ответственность, явный lifecycle (`start()`/`stop()`). DI принимается
через конструктор: `bus: MessageBus`, `context_bridge: ContextBridge`.

Публичный API:

```python
class RuntimeEventsSubscriber:
    def __init__(
        self,
        *,
        bus: MessageBus,
        context_bridge: ContextBridge,  # helper-обёртка над seed_context_window
    ) -> None: ...

    async def start(self) -> None:
        """Регистрирует подписки на интересующие runtime-события."""
        ...

    async def stop(self) -> None:
        """Дерегистрирует все подписки, сделанные через start()."""
        ...
```

`start()` сохраняет возвращаемые `bus.subscribe(..., event_type)` объекты
(unsubscribe-closures) в `_unsubscribes: list[Callable[[], None]]`. `stop()`
вызывает их в LIFO-порядке — это требование `bus.subscribe` (handler'ы
вызываются в порядке регистрации, поэтому дерегистрация в обратном
порядке безопаснее для in-flight событий).

**Альтернативы.**

* (а) Разместить observer в `lib/hooks/`. Отклонено: `lib/hooks/` —
  каркас для `AgentHook` (per-turn хуки через `hook_factories`),
  а `RuntimeEventsSubscriber` — это lifecycle-подписчик на шину,
  разный уровень абстракции. `lib/services/` — это правильный
  слой (см. `CompactionEventSubscriber`).
* (б) Observer прямо в `ApplicationContext`. Отклонено: смешивает
  application-lifecycle с business-logic, тестируемость падает
  (нужно поднимать контекст для unit-теста). Отдельный файл —
  чистый unit-тест с fake-зависимостями.

**Обоснование.** Зеркалит структуру `CompactionEventSubscriber`
(`lib/services/compaction_event_subscriber.py`): отдельный файл,
DI-injection, явные `start()`/`stop()`. Тест пишется с маленькими
mock'ами `MessageBus` и `ContextBridge` без поднятия всего
`ApplicationContext`.

### D2: Контракт handler'а

Один handler на `TurnRuntimeAdmitted`:

```python
async def _seed_context_bridge(event: TurnRuntimeAdmitted) -> None:
    session_key = event.context.session_key
    if not session_key:
        return
    limit = event.runtime.context_window_tokens or 0
    model = event.runtime.model or ""
    context_bridge.seed_context_window(
        session_key,
        limit=limit,
        model=model,
    )
```

**Обработка ошибок.** Исключения внутри handler'а НЕ должны ломать
никакой event-flow — handler вызывается через `bus.publish`, который
оборачивает вызов в `try/except` (`bus/queue.py:118-126`,
см. метод `publish`). Тем не менее оборачиваем вызов
`seed_context_window` в локальный `try/except` с warning — это
ускоряет диагностику, если seed начнёт падать.

**Контракт:** `handler(event)` — корутина или sync-callable. Получает
только те поля, которые есть в публичной сигнатуре dataclass. Никаких
side-effects кроме обновления моста. Handler MUST NOT НЕ ДОЛЖЕН
бросать необработанные исключения.

### D3: Контекстный мост как `ContextBridge`-протокол

`DatabaseLoggingHook.seed_context_window` уже существует, но это
module-level функция, а не метод observer'а. Чтобы уважать SRP
(handler ничего не знает про `DatabaseLoggingHook`), вводим тонкую
обёртку:

```python
class ContextBridge:
    """Абстракция моста над ``DatabaseLoggingHook._CONTEXT_BRIDGE``.

    Метод ``seed_context_window`` тонко-обёрнут над
    ``lib.hooks.database_logging_hook.seed_context_window`` —
    модуль-уровневой функцией. Это позволяет handler'у быть
    framework-agnostic (нет прямой зависимости от DatabaseLoggingHook).
    """
    def seed_context_window(
        self,
        session_key: str,
        *,
        limit: int,
        model: str = "",
    ) -> None: ...
```

Конкретная реализация — `DatabaseLoggingContextBridge` —
тонкая обёртка:

```python
class DatabaseLoggingContextBridge:
    def seed_context_window(self, session_key, *, limit, model=""):
        from lib.hooks.database_logging_hook import seed_context_window
        seed_context_window(session_key, limit=limit, model=model)
```

Возможные будущие альтернативы (Redis-кэш, Prometheus-exposition,
trace-recorder) реализуются как параллельные классы без правки handler'а.

**Альтернативы.**

* (а) Передавать `DatabaseLoggingHook`-инстанс напрямую в observer.
  Отклонено: handler знает, что мост — это `DatabaseLoggingHook`,
  теряется возможность подменить реализацию (например, для теста).
* (б) Использовать module-level `seed_context_window` без обёртки.
  Отклонено: импорт цикл `lib/services` → `lib/hooks` уже бы не
  возник, но `bus.publish` в async-контексте и так требует явного
  контракта; обёртка дисциплинирует teardown.

### D4: Точка подключения в lifecycle

`ApplicationContext.start()` добавляет **ОДИН** новый шаг после
`RuntimePatcher.apply_all` и **ДО** старта каналов:

```python
async def start(self) -> None:
    # ... существующая инициализация ...

    self.runtime_patcher = RuntimePatcher()
    self.runtime_patcher.apply_all(...)

    # NEW: подписка на runtime-события
    self.runtime_events_subscriber = RuntimeEventsSubscriber(
        bus=self.bus,
        context_bridge=DatabaseLoggingContextBridge(),
    )
    await self.runtime_events_subscriber.start()

    # ... старт каналов ...
```

Симметричный `await self.runtime_events_subscriber.stop()` в
`ApplicationContext.stop()` **перед** `MessageBus.drain()`. Этот
порядок критичен: `bus.drain()` ждёт завершения in-flight handler'ов
(`bus/queue.py:145-147`), и если unsub перед этим — мы теряем
хвост событий. Корректный порядок: unsub → drain → channel stop.

**Альтернативы.**

* (а) Вызвать subscribe напрямую в `gateway_runner.py` /
  `cli_agent.py`. Отклонено: дублирование вызовов в двух entrypoint'ах;
  риск рассинхрона (забыли в одном — канал работает без bridge).
* (б) Поместить в `BusFactory` (`lib/core/bus_factory.py`).
  Отклонено: `BusFactory` конструирует MessageBus; observer —
  отдельная concern, lifecycle-привязан.

### D5: Контрактные тесты на `TurnRuntimeAdmitted` API

Создаём `tests/contract/test_runtime_events_api.py`. Фиксирует:

* `TurnRuntimeAdmitted` имеет публичные поля `context: RuntimeEventContext`
  и `runtime: LLMRuntime` (а не `_context` или `_runtime`).
* `RuntimeEventContext` имеет поля `channel`, `chat_id`, `session_key`,
  `metadata`, `attributes`.
* `bus.subscribe(handler, TurnRuntimeAdmitted)` принимает
  callable (sync или async), возвращает unsubscribe-closure.
* `bus.publish(event)` вызывает handler с event'ом,
  awaited'ит если handler — корутина.

Это «freeze API» для будущих upgrade'ов: при апгрейде nanobot контракт
ловит любые rename поля/класса и сразу падает.

## Risks / Trade-offs

* **R1: Входящие event'ы могут быть in-flight на момент `stop()`**.
  `bus.subscribe` возвращает unsubscribe-closure; `RuntimeEventsSubscriber.stop()`
  вызывает её синхронно. Гарантия `bus.publish` — обработка текущих
  in-flight handler'ов через `bus.drain()`. Stop-порядок в design §D4
  (unsub → drain) — единственный безопасный.

* **R2: При активном `_attach_context_window`-fallback подписка станет
  избыточной записью.** Безобидно: `seed_context_window` дедует через
  `setdefault`-семантику в `_CONTEXT_BRIDGE` (см.
  `lib/hooks/database_logging_hook.py::seed_context_window`). Первая
  запись выигрывает. Тест `tests/contract/test_runtime_events_api.py`
  проверяет, что монотонное значение `(limit, model)` остаётся
  стабильным между двумя записями.

* **R3: Двойная подписка при перезапуске gateway.** Если observer
  не отписан и перезапущен — handler получит события дважды.
  Защита: `start()` проверяет, что `_unsubscribes` пуст; если нет —
  логирует warning и отказывается.

* **R4: Подписка в CLI-agent не даст эффекта, если `MessageBus`
  не инициализирован.** Текущий `cli_agent.py::AgentFactory.create`
  создаёт `bus=MessageBus()` локально (без upstream-singleton). Observer
  подключается на тот же инстанс — события работают. Если будущий
  CLI использует ту же шину — попадёт и туда (плюс).

## Migration Plan

1. **Контрактные тесты.** `tests/contract/test_runtime_events_api.py` —
   freeze API `TurnRuntimeAdmitted`/`bus.subscribe`. Запустить против
   установленного `nanobot-ai==0.3.5`.
2. **ContextBridge.** `lib/services/context_bridge.py` (если такого
   модуля нет) с `DatabaseLoggingContextBridge` — тонкая обёртка
   над `seed_context_window`.
3. **Observer.** `lib/services/runtime_events_subscriber.py`
   (см. §D1).
4. **ApplicationContext.** Добавить `_start_runtime_events_subscriber`
   (вызывается в `start()` после `apply_all`, до каналов) и
   `_stop_runtime_events_subscriber` (вызывается в `stop()` до
   `MessageBus.drain()`).
5. **Тесты observer'а.** `tests/test_runtime_events_subscriber.py`
   с fake-bus (queue-like) и fake-bridge (capture list).
6. **Inventory.**
   `docs/architecture/runtime-patcher-inventory.md` — обновить
   статус `context_bridge_seed` (DEPRECATED → MOVED) со ссылкой
   на `RuntimeEventsSubscriber`.
7. **ADR.** `docs/architecture/decisions/runtime-events-subscriber.md`
   — обоснование observer-паттерна, включая fail-fast по отсутствию
   `RuntimeEventPublisher` в upstream.
8. **Валидация.** `openspec validate runtime-events-subscription
   --strict` → 0 ошибок. `pytest tests/contract/test_runtime_events_api.py
   tests/test_runtime_events_subscriber.py -q` → 0 failed.

## Rollback

Шаг `ApplicationContext.start()` можно откатить однострочным удалением
вызова `_start_runtime_events_subscriber()`. Bridge ОСТАЁТСЯ
заполненным через `_attach_context_window`-fallback (без изменений
в точности). Никаких миграций БД не требуется.

## Open Questions

* **Q1: Подписываться ли на `UserInputAccepted` сразу?** Нет — это
  другой ответственный consumer (subagent-сегрегация user_id),
  которого сейчас нет. Это будущий ADR.
* **Q2: Нужен ли периодический refresh моста?** Нет — лимит окна
  может меняться только через `config.json::provider.model` или
  `default.contextWindowTokens`, что требует перезапуска gateway.
  Подписка ловит каждый оборот — refresh не нужен.
* **Q3: Что делать, если `RuntimeEventPublisher` rename'нут в 0.4.0?**
  Контрактный тест `test_runtime_events_api.py` ловит это сразу;
  следующий upgrade делает `patch_context_bridge_seed` снова
  активным fallback'ом (но на новом API). Это видно через
  `pytest tests/contract -q` — этот change процедурно подключён
  к upgrade-compatibility workflow.
