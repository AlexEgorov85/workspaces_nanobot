## Context

После upgrade до `nanobot-ai 0.3.5` (см. `openspec/changes/nanobot-035-upgrade`)
проект получил доступ к нативному pub-sub API `MessageBus.subscribe(handler, EventType)`
(`nanobot/bus/queue.py:92-116`), но не использует его для нескольких рутинных
задач: `TurnCompleted` сейчас игнорируется, subagent пишет `subagent_run_finished`
через monkey-patch напрямую в БД, `_attach_context_window` имеет fallback,
который маскирует отсутствие подписки. Параллельно `ActiveFilesHook`
полностью мёртв в 0.3.5 (метод `AgentHook.before_user_turn` отсутствует в
`nanobot/agent/hook.py:66-151`), а его side-channel-ключи `user_attachments`/
`agent_files` не читаются ни одним потребителем.

`runtime-events-subscription` change уже спроектирован (см. `openspec/changes/
runtime-events-subscription/proposal.md`), но покрывает только `TurnRuntimeAdmitted`.
Этот change расширяет подписки на `TurnCompleted` и `SubagentTurnCompleted`,
заменяет monkey-patch-логику в subagent на нативный pub-sub, удаляет мёртвый
код ActiveFilesHook и лишний fallback.

Текущая версия `nanobot-ai` подтверждена как `0.3.5` через `pip show nanobot-ai`
(установлен в `C:\Users\Алексей\AppData\Roaming\Python\Python314\site-packages\
nanobot\`).

## Goals / Non-Goals

**Goals:**

* Заменить ручную запись `run_finished`-метрик (`latency_ms`) на подписку
  `bus.subscribe(handler, TurnCompleted)` с новым event_type `turn_completed`
  в `agent_gateway_logs`.
* Перенести логику `_SubagentLoggingHook._finalize` в подписку
  `bus.subscribe(handler, SubagentTurnCompleted)`, где `SubagentTurnCompleted`
  публикуется самим `_SubagentLoggingHook.after_run` через `bus.publish(event)`.
* Удалить fallback `agent._last_usage` в `RuntimePatcher._attach_context_window`,
  полагаясь на `RuntimeEventsSubscriber` (seed `TurnRuntimeAdmitted`).
* Удалить мёртвый код `ActiveFilesHook` и его side-channel-ключи.
* Удалить `RuntimePatcher.patch_context_bridge_seed` (no-op с 0.3.5).
* Удалить устаревший исторический комментарий `runtime_patcher.py:2043-2057`.

**Non-Goals:**

* Полная замена `DatabaseLoggingHook.after_run` на подписку `TurnCompleted`
  (невозможно: `TurnCompleted` не несёт `final_content`/`tools_used`).
* Реализация нового pipeline для side-channel активных файлов
  (отдельный change при появлении требований; см. ADR).
* Расширение upstream `nanobot.events` (наш namespace для
  `SubagentTurnCompleted`).
* Изменение `nanobot-ai` (внешняя зависимость).

## Decisions

### D1: `SubagentTurnCompleted` живёт в нашем namespace, не в `nanobot.events`

**Решение:** `lib/events/subagent.py::SubagentTurnCompleted(AgentEvent)`.
Наследует `nanobot.events.AgentEvent` (импорт из `nanobot`), но
определяется в нашем коде.

**Почему:** `nanobot.events.SubagentTurnCompleted` не существует в 0.3.5 и не
планируется upstream. Расширение `nanobot.events` — внешняя зависимость,
контроля над ней нет. Наш dataclass принимается `bus.subscribe(handler, ...)`
потому что `MessageBus.publish` принимает любой `AgentEvent`
(`nanobot/bus/queue.py:118-127`).

**Альтернативы:**

* Расширить `nanobot.events` через monkey-patch — отклонено, ломает
  восходящую совместимость и требует правки при апгрейдах.
* Переиспользовать `nanobot.events.TurnCompleted` через атрибут —
  отклонено, `TurnCompleted` уже имеет жёсткую сигнатуру (`outcome`,
  `failure_*`), расширение полей через monkey-patch нарушит контракт.

### D2: Подписки на runtime-события — расширение `RuntimeEventsSubscriber`

**Решение:** `lib/services/runtime_events_subscriber.py` расширяется
новым методом `_subscribe_turn_completed()` и `_subscribe_subagent_turn_completed()`
в дополнение к существующему `_subscribe_turn_runtime_admitted()`
(см. `openspec/changes/runtime-events-subscription/design.md`).
Каждая подписка возвращает `unsubscribe`, который собирается в
`self._unsubscribers: list[Callable]`. `stop()` вызывает их в обратном
порядке.

**Почему:** single seam для observer-подписок упрощает lifecycle и тесты.
Шаблон уже валидирован в `runtime-events-subscription`.

**Альтернативы:**

* Отдельный `TurnCompletedSubscriber` и `SubagentLoggingSubscriber`
  — отклонено: размножает lifecycle и тестовые подставки.
* Глобальный модуль `lib/services/observers.py` с auto-register —
  отклонено: неявная инициализация, трудно тестировать.

### D3: Subagent публикует `SubagentTurnCompleted` через monkey-patch на `_SubagentLoggingHook.after_run`

**Решение:** `lib/services/runtime_patcher.py::patch_subagent_logging` уже
подменяет `nanobot.agent.subagent._SubagentHook` на `_SubagentLoggingHook`.
`_SubagentLoggingHook.__init__` расширяется параметром `bus: MessageBus`.
`after_run` после завершения `runner.run` вызывает
`await self._bus.publish(SubagentTurnCompleted(...))` (ДО `_finalize`),
затем `_finalize` остаётся для совместимости (пишет `subagent_run_finished`,
как и раньше — чтобы не сломать контракт при потере события).

**Почему:** subagent идёт через `AgentRunner.run`, минуя `TurnDelivery`
(`nanobot/agent/subagent.py:427`), поэтому `TurnCompleted` НЕ публикуется.
Patch на `_SubagentLoggingHook` уже есть и работает в 0.3.5; расширение —
минимальный риск.

**Альтернативы:**

* Расширить upstream `nanobot.agent.subagent` — отклонено (внешняя зависимость).
* Подписаться на `bus.publish_inbound(msg)` в `_announce_result`
  (`subagent.py:530`) — отклонено: `inbound` не идёт через `bus.subscribe`.
* Перехватить `_announce_result` через патч — отклонено: хрупко, ломается при
  добавлении новых параметров в 0.4.x.

### D4: Подписчик `_handle_subagent_turn_completed` пишет `subagent_run_finished`

**Решение:** новый файл `lib/services/subagent_logging_subscriber.py` с
классом `SubagentLoggingSubscriber(RuntimeEventsSubscriber)`. Подписывается
на `SubagentTurnCompleted`. Handler пишет `LogEvent(event_type=
"subagent_run_finished", payload={task_id, task, final_content, tools_used,
stop_reason, request_id, parent_request_id, parent_user_id, usage_tokens,
had_error})` через `DbLoggingService.log_event`. Контракт
`subagent_run_finished` (включая `parent_user_id` security boundary)
НЕ меняется.

**Почему:** полная идентичность с текущим payload `_SubagentLoggingHook.
_finalize` (`runtime_patcher.py:1712-1737`); тесты `tests/test_subagent_
logging.py` остаются зелёными без изменений.

**Альтернативы:**

* Оставить запись в `_finalize` — отклонено: цель change — переход на
  pub-sub. Хук `_SubagentLoggingHook` остаётся, но только как publisher.
* Выделить `_extract_task` и `payload_assembly` в helper, тестировать —
  не приоритет, делается в следующих change'ах.

### D5: `_attach_context_window` без fallback `_last_usage`

**Решение:** удалить `usage = getattr(agent, "_last_usage", None) or {}`
из `runtime_patcher.py:103`. Если `DatabaseLoggingContextBridge.get_iteration_
usage(session_key)` возвращает пустой dict (подписка не отработала),
поднимать `ContextWindowNotSeededError`. UI/CLI получают явное сообщение
вместо пустой метрики.

**Почему:** fallback маскирует дефекты подписки — UI показывает
`used=0`/`pct=0%` без сигнала об ошибке. Это нарушает принцип
`docs/TARGET_ARCHITECTURE.md §26` (observability: явные ошибки лучше
тихих fallback'ов).

**Альтернативы:**

* Оставить fallback как fail-safe — отклонено: пользователь явно
  попросил «fallback скрывает проблемы, не нужен».
* Fallback на `agent.context_window_tokens` (без `usage`) — отклонено:
  даёт `limit` без `used`, что хуже чем явная ошибка.

### D6: `ActiveFilesHook` удаляется без замены

**Решение:** удалить `workspace/hooks/active_files_hook.py` целиком (370
строк). Защита от регрессии — `lib/cli/hook_loader.py::scan_and_register`
переходит на явный allowlist `["active_files_hook", "recent_files_hook",
"session_file_redirect_hook"]` вместо wildcard-импорта. ADR
`docs/architecture/decisions/active-files-hook-removal.md` фиксирует:

* Метод `AgentHook.before_user_turn` отсутствует в 0.3.5
  (`nanobot/agent/hook.py:66-151`).
* Потребителей `session.metadata["user_attachments"|"agent_files"]` нет ни
  в `lib/`, ни в `workspace/`, ни в `tests/`, ни в `openspec/`, ни в `sql/`.
* `render_active_files_section` нигде не вызывается.
* `patch_active_files_in_context` (на который ссылается docstring)
  физически не существует.
* Инцидент 2026-08-27 (consolidator archives file references)
  остаётся открытым — отдельный change при появлении требований.

**Почему:** side-channel не работает в 0.3.5 уже сейчас (метод
`before_user_turn` не вызывается), `after_execute_tool` пишет в
`session.metadata` без `session.save()` (`_sessions=None` в auto-scan
через `lib/cli/hook_loader.py`). Удаление безопасно.

**Альтернативы:**

* Переименовать `before_user_turn` → `before_iteration` — отклонено:
  семантически неверно (вызывается на каждой итерации LLM, не на
  user-turn), `session.messages[-1]` не содержит media в формате
  `InboundMessage.media`.
* Реализовать через `bus.subscribe(UserInputAccepted)` — отклонено:
  +80-100 строк, scope расширяется; ADR рекомендует отдельный change
  при реальной потребности.

### D7: Lifecycle-порядок: apply_all → subscribers → channels → drain

**Решение:** добавить `MessageBus.drain()` в `ApplicationContext.stop()`
после остановки каналов, ПЕРЕД `RuntimeEventsSubscriber.stop()`. Это даёт
in-flight handler'ам шанс завершиться (например, последняя подписка
`SubagentTurnCompleted` пишет в БД через `DbLoggingService`, flush
происходит с интервалом `logging.db.flush_interval_sec`).

**Почему:** `nanobot/bus/queue.py:145-147` (`async def drain`) уже
существует, но в текущем `ApplicationContext.stop` не вызывается.
Подтверждение через `grep "bus.drain\|MessageBus.drain"` в `lib/` —
0 совпадений.

**Альтернативы:**

* Не добавлять `drain()` — отклонено: in-flight handler может быть
  прерван в середине `seed_context_window` или `_handle_turn_completed`,
  что приведёт к частичной записи.
* Drain перед каналами — отклонено: каналы могут ещё публиковать
  события, drain их потеряет.

## Risks / Trade-offs

| Риск | Митигация |
|---|---|
| Подписка на `TurnCompleted` пишет `turn_completed` БЕЗ `final_content` — пользователь может думать, что там есть полный ответ | Чёткое описание в `history_search.description` (обновляется в tasks.md 4.4); `turn_completed` помечается как метрика, не user-visible content |
| Удаление `ActiveFilesHook` без нового pipeline для side-channel — инцидент 2026-08-27 остаётся открытым | ADR явно фиксирует: side-channel не работает в 0.3.5, его удаление — технический долг, решение проблемы архивации PDF — отдельная задача |
| Subagent публикует `SubagentTurnCompleted` через monkey-patch `patch_subagent_logging` — хрупко для 0.4.x | Patch уже существует и работает в 0.3.5; новые подписки переиспользуют его сигнатуру |
| Подписки стартуют после `apply_all`, но `MessageBus.drain()` в текущем `ApplicationContext.stop` отсутствует | D7 добавляет `drain()`; tasks 6.1-6.3 тестируют lifecycle |
| Удаление `_last_usage` fallback'а до применения `runtime-events-subscription` сломает UI на first turn | tasks 1.1 имеет **строгую зависимость** от применения `runtime-events-subscription`; tasks 5.1-5.2 добавляют regression-тест «first turn без tool-вызовов имеет context_window» |
| Подписчик `_handle_subagent_turn_completed` дублирует логику `_SubagentLoggingHook._finalize` (если оставить обе записи) | `_finalize` остаётся для совместимости, но пишет ТОЛЬКО при отсутствии подписки (через флаг `subscriber_registered`); при подписке запись идёт через подписчика, `_finalize` пропускает `log_event` |
| Allowlist в `lib/cli/hook_loader.py` ломает добавление новых hooks | Allowlist дополняется документированным списком; новые hooks добавляются явно через PR |
| Изменение сигнатуры `_SubagentLoggingHook.__init__(bus)` ломает другие потребители (если они есть) | `bus` параметр опционален (`bus: MessageBus | None = None`); если `None` — публикация `SubagentTurnCompleted` пропускается (subagent_run_finished пишется через `_finalize`) |

## Migration Plan

**Pre-conditions (должны быть выполнены до старта):**

* `openspec/changes/runtime-events-subscription` применён (есть в master).
  Без него подписка на `TurnRuntimeAdmitted` не работает → удаление
  `_last_usage` fallback'а сломает UI.

**Шаги:**

1. `tasks.md` группы 1-2 — добавление `SubagentTurnCompleted` и новых
   подписок в `RuntimeEventsSubscriber` (additive, без удаления).
2. `tasks.md` группа 3 — расширение `_SubagentLoggingHook` параметром
   `bus`. Контракт `subagent_run_finished` сохраняется, оба пути
   записи работают параллельно (для regression-теста).
3. `tasks.md` группа 4 — добавление `turn_completed` в history_search
   enum. Контракт `run_finished` не меняется.
4. `tasks.md` группа 5 — удаление `_last_usage` fallback'а.
   Регрессионный тест на first turn обязателен.
5. `tasks.md` группа 6 — добавление `bus.drain()` в `ApplicationContext.stop`.
6. `tasks.md` группа 7 — удаление `ActiveFilesHook`, `patch_context_bridge_seed`,
   устаревшего комментария. ADR создаётся до удаления (документация следа).
7. `tasks.md` группа 8 — финальная валидация (`openspec validate`,
   `pytest`, smoke gateway).

**Rollback:**

* Группы 1-4 additive — rollback = revert commit.
* Группа 5 (удаление fallback) — rollback = восстановить 3 строки
  в `runtime_patcher.py:103` (git revert).
* Группа 7 (удаление ActiveFilesHook) — rollback = `git revert`,
  но ADR остаётся в репо как знание о нерешённой проблеме.
* Группа 6 (bus.drain) — откат безопасен (drain ничего не делает,
  если подписчики уже остановлены).

**Cutover:**

* Один merge-коммит после прохождения `pytest` (1480 passed) +
  `openspec validate` (зелёный) + smoke-тест gateway (один user-turn
  без tool-вызовов показывает `metadata.context_window != 0`).
* Никаких флаговых переключений: change либо применён, либо нет.

## Open Questions

* Q1: Какой минимальный payload у `SubagentTurnCompleted`, чтобы
  существующие тесты `tests/test_subagent_logging.py` остались
  зелёными без изменений?
  * **Ответ:** payload = полная копия `_SubagentLoggingHook._finalize`
    payload + поля `usage`/`error` (см. design D4). Тесты не меняются.

* Q2: Где живёт `SubagentTurnCompleted` — `lib/events/subagent.py`
  или `lib/events/__init__.py`?
  * **Ответ:** `lib/events/subagent.py` (отдельный файл, чтобы не
    загрязнять `__init__.py`). Импорт через
    `from lib.events.subagent import SubagentTurnCompleted` и
    реэкспорт через `nanobot.events`-обёртку НЕ требуется —
    `bus.subscribe(handler, SubagentTurnCompleted)` принимает
    dataclass-наследник `AgentEvent` напрямую.

* Q3: Нужно ли делать `bus.drain()` опциональным через конфиг?
  * **Ответ:** нет, он нужен всегда для graceful shutdown.
    Если `MessageBus` пуст, `drain()` — no-op
    (`nanobot/bus/queue.py:145-147`).