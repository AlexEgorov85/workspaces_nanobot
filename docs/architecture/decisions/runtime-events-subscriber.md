# ADR: RuntimeEventsSubscriber на TurnRuntimeAdmitted

- **Status:** Accepted (2026-09-26)
- **Date:** 2026-09-26
- **Deciders:** opencode (по решению автора change `post-0-3-5-patches-cleanup`)

## Context

После апгрейда upstream `nanobot-ai 0.3.5` runtime-patcher проекта должен
seedить лимит контекстного окна (`context_window_tokens`) и `model` в
`DatabaseLoggingHook._CONTEXT_BRIDGE` **до** первой LLM-итерации каждого
оборота. Это нужно для того, чтобы `metadata.context_window` в финальном
`OutboundMessage` показывал реальные значения (а не дефолтные или
`ContextWindowNotSeededError`).

Upstream предоставляет два кандидата для интеграции:

1. **Обёртка `_state_build`** (внутренний метод `AgentLoop._state_build`,
   вызываемый перед `_run_iteration`). Минусы: приватный API (может
   исчезнуть в следующих версиях); требует monkey-patch на upstream.
2. **Wrapper в `Console`** (внутренний метод `AgentLoop.console`,
   используется для отправки стрима в CLI). Минусы: не публичный hook;
   `console` вызывается только при CLI-стриме, не для всех каналов
   (postgres, streamlit, websocket).
3. **Патч на `RuntimeEventPublisher`** (subclass override). Минусы:
   ломает локальный fan-out для других потребителей (если upstream когда-то
   подключит других подписчиков); выходит за рамки нашего scope.
4. **Observer через `bus.subscribe(TurnRuntimeAdmitted)`** — публичный
   pub-sub API upstream, доступный через `MessageBus`. Подписчик получает
   события для **всех** каналов. Реализация — отдельный сервис
   `RuntimeEventsSubscriber` со start/stop lifecycle.

## Decision

Принят **вариант 4**: observer-паттерн на `bus.subscribe(TurnRuntimeAdmitted)`.

Реализация:
- `lib/services/runtime_events_subscriber.py` — класс
  `RuntimeEventsSubscriber(bus, db_logging_service=None)`.
  - `start()` оборачивает handler `_handle_turn_runtime_admitted`
    через `bus.subscribe(handler, TurnRuntimeAdmitted)` и сохраняет
    `unsubscribe` в `self._unsubscribers`. Повторный `start()` без
    `stop()` — no-op с warning (защита от двойного start).
  - `stop()` вызывает unsub'ы в LIFO-порядке (через `self._unsubscribers.pop()`).
  - handler читает `event.context.session_key` (проверка `isinstance(str)`);
    при пустом `session_key` — early return. Затем вызывает
    `seed_context_window(session_key, limit=int(limit), model=str(model))`
    в try/except — исключения глотаются с warning (runtime-path не должен
    падать из-за observability).
- `lib/core/application_context.py` — `ApplicationContext.start()`
  создаёт `RuntimeEventsSubscriber` после `runtime_patcher.apply_all()`
  и **до** старта каналов. `ApplicationContext.stop()` вызывает
  `subscriber.stop()` **до** `MessageBus.drain()` и до channel stop
  (in-flight handler'ы успевают завершиться).

## Alternatives considered

| Альтернатива | Почему отклонено |
|---|---|
| Обёртка `_state_build` | Приватный API upstream (может исчезнуть); monkey-patch на приватный метод создаёт скрытую зависимость |
| Wrapper в `Console` | `console` не публичный hook; вызывается только для CLI-стрима, не для postgres/streamlit/websocket |
| Патч на `RuntimeEventPublisher` | Subclass override ломает локальный fan-out для других потребителей; выходит за scope |
| **Observer через `bus.subscribe(TurnRuntimeAdmitted)`** ✓ | Публичный pub-sub API, fan-out для всех каналов, явный lifecycle, расширяемо |

## Consequences

**Positive:**
- ~80 строк кода, ~100 строк тестов (10 тестов в
  `tests/test_runtime_events_subscriber.py`).
- Расширяемость: новые runtime-event-подписки идут тем же observer'ом
  (`bus.subscribe(handler, EventType)`) — расширение через тот же
  `_unsubscribers` список.
- Явный lifecycle (start/stop) — симметрично `CompactionEventSubscriber`
  (см. `nanobot-035-upgrade` change, задача 3.1).
- DI через конструктор (`bus`, `db_logging_service=None`) — без скрытых
  каналов на `agent`.

**Negative / Trade-offs:**
- `bus.publish(...)` (sync) ≠ `bus.publish_event(...)` (async, для каналов).
  Подписки через `subscribe` ловят **только** `publish`-вызовы. Это
  корректное поведение: `publish` идёт через локальный fan-out, не
  через outbound-queue каналов. Upstream явно разделяет эти два пути.
- Защита от двойного `start` через флаг `_started` (no-op + warning) —
  простой, но работает корректно для нашего lifecycle (start → stop → start).

## References

- `lib/services/runtime_events_subscriber.py` — реализация
- `lib/core/application_context.py:399-499` — DI + lifecycle
- `tests/test_runtime_events_subscriber.py` — 10 unit-тестов
- `tests/contract/test_runtime_events_api.py` — 8 contract-тестов на upstream API
- `openspec/changes/nanobot-035-upgrade/design.md` §D7/R7 — исходное обоснование
- `openspec/changes/runtime-events-subscription/` — формальная спецификация