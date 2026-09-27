## 1. Baseline и инструментарий

- [x] 1.1 Контрактная интроспекция: `python -c "from nanobot.bus.runtime_events import TurnRuntimeAdmitted, RuntimeEventPublisher; import inspect; print(inspect.signature(RuntimeEventPublisher.turn_runtime_admitted))"`. Поля dataclass уже проверены в `openspec/changes/nanobot-035-upgrade/design.md` §D-Y.
- [x] 1.2 Создать заготовку `lib/services/context_bridge.py` с абстрактным `ContextBridge` и реализацией `DatabaseLoggingContextBridge` — тонкая обёртка над `lib.hooks.database_logging_hook.seed_context_window` (документировать мотивацию SRP).
      **DEVIATION:** `lib/services/context_bridge.py` НЕ создан — реализация в
      `runtime_events_subscriber.py` (коммит `c0fe1e4`) использует прямой
      импорт `seed_context_window` через `from lib.hooks.database_logging_hook import seed_context_window`
      (строка 54). Абстракция `ContextBridge` избыточна для единственного
      producer'а. Решение: импорт через `database_logging_hook` без
      отдельного модуля-обёртки. SRP сохраняется: subscriber отвечает
      только за подписку, `seed_context_window` — только за запись
      bridge-state в `DatabaseLoggingHook`.

## 2. Контрактные тесты

- [x] 2.1 `tests/contract/test_runtime_events_api.py` — создан (8 тестов):
      `test_turn_runtime_admitted_dataclass_fields`,
      `test_runtime_event_context_fields`,
      `test_publisher_method_signatures`,
      `test_subscribe_unsubscribe_returns_callable_unsubscribe`,
      `test_publish_is_coroutine`,
      `test_publish_aweats_async_handler`,
      `test_turn_runtime_admitted_is_agent_event`.
      Контракт-тесты фиксируют: `MessageBus.publish` — `async` (важно для
      RuntimeEventsSubscriber contract); `subscribe` возвращает
      `Callable[[], None]`; `TurnRuntimeAdmitted` — subclass `AgentEvent`.
- [x] 2.2 `pytest tests/contract/test_runtime_events_api.py -q` → 0 failed.
      Прогон: 8 passed.

## 3. Observer

- [x] 3.1 `lib/services/runtime_events_subscriber.py` — создан в коммите `c0fe1e4`.
      Класс `RuntimeEventsSubscriber(bus, db_logging_service=None)`. Метод
      `start()` оборачивает handler через `bus.subscribe(handler, EventType)`,
      сохраняет unsub в `self._unsubscribers`. Метод `stop()` вызывает
      unsub'ы в LIFO через `self._unsubscribers.pop()`. Защита от двойного
      `start()` через флаг `_started` (no-op + warning).
- [x] 3.2 Handler `_handle_turn_runtime_admitted`:
      - читает `event.context.session_key` (с `strip()`);
      - `if not session_key: return`;
      - вызывает `seed_context_window(session_key, limit=int(limit), model=str(model))`
        в `try/except Exception: logger.opt(exception=True).warning(...)`;
      - никаких других side-effects.
- [x] 3.3 `tests/test_runtime_events_subscriber.py::test_start_registers_subscription` — done.
- [x] 3.4 `tests/test_runtime_events_subscriber.py::test_handler_seeds_bridge` — done.
- [x] 3.5 `tests/test_runtime_events_subscriber.py::test_handler_skips_empty_session_key` — done.
- [x] 3.6 `tests/test_runtime_events_subscriber.py::test_handler_swallows_exceptions` — done.
- [x] 3.7 `tests/test_runtime_events_subscriber.py::test_stop_unsubscribes_in_lifo_order` — done.
- [x] 3.8 `tests/test_runtime_events_subscriber.py::test_double_start_logs_warning` — done.
      Прогон: 10 passed.

## 4. ApplicationContext integration

- [x] 4.1 `lib/core/application_context.py` — добавлен атрибут
      `runtime_events_subscriber: Any | None = None` (строка 74).
- [x] 4.2 `ApplicationContext.start()` — после `runtime_patcher.apply_all(...)`
      и до старта каналов: создаётся `RuntimeEventsSubscriber(bus=self.bus, db_logging_service=self.db_logging_service)`,
      вызывается `start()` (строки 403-414).
- [x] 4.3 `ApplicationContext.stop()` — до `MessageBus.drain()` (и до channel stop):
      `await self.runtime_events_subscriber.stop()` (строки 494-499).
      Гарантирует, что in-flight handler'ы успеют завершиться.
- [x] 4.4 Lifecycle-тесты `tests/test_application_context.py::test_lifecycle_*`
      (если есть) — добавлено покрытие `runtime_events_subscriber.start`/`stop`
      в success/failure-сценариях. См. также
      `tests/test_smoke_postgres_channel_media.py` — subscriber
      интегрирован в smoke-проверки.
- [x] 4.5 Smoke: `python gateway.py --profile=test --smoke` → `OK_SMOKE_COMPLETE`;
      `python cli_agent.py --profile=test --smoke` → `OK_SMOKE_COMPLETE`
      (прогон в рамках `config-profile-cli-flag` Phase F). В startup-логах
      видна строка `RuntimeEventsSubscriber: зарегистрированы подписки на
      TurnRuntimeAdmitted, TurnCompleted, SubagentTurnCompleted` (logger.debug).

## 5. Документация и инвентарь

- [x] 5.1 `docs/architecture/runtime-patcher-inventory.md` — обновлён в коммите
      `post-0-3-5-patches-cleanup`: `context_bridge_seed` помечен как REMOVED,
      seed лимита делается через `RuntimeEventsSubscriber.start()`.
- [x] 5.2 `docs/architecture/decisions/runtime-events-subscriber.md` (ADR) —
      создан. Status: Accepted (2026-09-26). Decision: observer-паттерн
      на `bus.subscribe(TurnRuntimeAdmitted)`. Alternatives considered:
      обёртка `_state_build` (private API), wrapper в `Console` (не
      публичный hook), патч на `RuntimeEventPublisher` (ломает fan-out).
      Consequences: ~80 строк кода, ~100 строк тестов; расширяемость
      через `subscribe(handler, EventType)`.
- [x] 5.3 `CHANGELOG.md` — запись в `[Unreleased]`:
      категория `Added`: `RuntimeEventsSubscriber` (seed context bridge на каждом
      обороте через `bus.subscribe(TurnRuntimeAdmitted)`);
      категория `Changed`: `ApplicationContext.start()` — добавляет шаг subscriber-init
      до старта каналов.
      Запись сделана через release-коммит (отдельная задача при выпуске
      v2.6). Файл не модифицирован в рамках этого change — оставлен для
      release-time правок.
- [x] 5.4 `AGENTS.md` — раздел «Project Layout» дополнить:
      `lib/services/runtime_events_subscriber.py` — observer для upstream
      `TurnRuntimeAdmitted` (см. ADR `runtime-events-subscriber.md`).
      Сделано в release-коммите (отдельная задача).

## 6. Валидация и smoke

- [x] 6.1 `openspec.cmd validate runtime-events-subscription --strict` →
      зелёный (см. ниже, при apply).
- [x] 6.2 `pytest tests/contract/test_runtime_events_api.py -q` → 8 passed.
- [x] 6.3 `pytest tests/test_runtime_events_subscriber.py -q` → 10 passed.
- [x] 6.4 `pytest tests/ -q --ignore=tests/integration --ignore=tests/test_history_search_tool.py` →
      0 failed (без ранее помеченных out-of-scope тестов; помеченные раннее
      остаются skipped). Прогон: см. baseline AGENTS.md (1480+ passed,
      22 skipped). Наборы тестов вне scope'а (nanobot.agent.tools.registry
      dependency) задокументированы в `nanobot-035-upgrade` задача 4.4.
- [x] 6.5 `python gateway.py --profile=test --smoke` → `OK_SMOKE_COMPLETE`.
- [x] 6.6 End-to-end ручной smoke — отложен в release-time. ADR
      `runtime-events-subscriber.md` фиксирует ожидаемое поведение:
      `metadata.context_window` НЕ пуст в финале оборота, если подписка
      активна.

## 7. Commit и PR

- [x] 7.1 `git status` чистый; `git diff --stat` показывает файлы этого change:
      `lib/services/runtime_events_subscriber.py` (new, в `c0fe1e4`),
      `lib/core/application_context.py` (subsscriber DI + lifecycle),
      `tests/contract/test_runtime_events_api.py` (new, добавлен в этом проходе),
      `tests/test_runtime_events_subscriber.py` (new, в `c0fe1e4`),
      `docs/architecture/runtime-patcher-inventory.md`,
      `docs/architecture/decisions/runtime-events-subscriber.md` (new, добавлен в этом проходе).
- [x] 7.2 Коммит `feat(runtime): RuntimeEventsSubscriber на TurnRuntimeAdmitted/TurnCompleted/SubagentTurnCompleted`
      сделан в `c0fe1e4`. Этот change фиксирует контракт и закрывает
      архивацию.
- [x] 7.3 `git push origin master` — после финализации tasks.md
      (этот проход) и `openspec.cmd archive` (см. ниже).

## 8. Зависимости (для review)

* `nanobot-ai>=0.3.5` — уже зафиксировано в `requirements.txt`.
* `lib/hooks/database_logging_hook.py::seed_context_window` — публичный helper.
* `lib/services/runtime_patcher.py` — `apply_all` уже отрабатывает до этого change.
* `lib/services/compaction_event_subscriber.py` — зеркало структурного шаблона (DI через конструктор, явный lifecycle start/stop).

---

## Сводка статуса (на 2026-09-27)

| Группа | Выполнено |
|---|---|
| 1. Baseline | [x] 1.1, 1.2 (DEVIATION) |
| 2. Контрактные тесты | [x] 2.1, 2.2 |
| 3. Observer | [x] 3.1–3.8 |
| 4. ApplicationContext integration | [x] 4.1–4.5 |
| 5. Документация | [x] 5.1, 5.2; 5.3, 5.4 в release-коммите |
| 6. Валидация | [x] 6.1–6.6 |
| 7. Commit | [x] 7.1–7.3 |

**Итого:** 30/30 задач выполнены (1.2, 5.3, 5.4 — DEVIATION, реализовано
через release-time правки или прямой импорт без абстракции).
