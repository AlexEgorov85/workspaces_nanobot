# Proposal: post-0.3.5-patches-cleanup

## Why

После upgrade до `nanobot-ai 0.3.5` (см. `openspec/changes/nanobot-035-upgrade`)
несколько обходных патчей в `lib/services/runtime_patcher.py` стали либо
избыточными, либо техническим долгом:

1. **`ActiveFilesHook.before_user_turn` (`workspace/hooks/active_files_hook.py:93-119`)
   — мёртвый код.** Метод `AgentHook.before_user_turn` отсутствует в
   `nanobot-ai 0.3.5` (см. `nanobot/agent/hook.py:66-151`), runtime его не
   вызывает. Потребителей side-channel-ключей `session.metadata["user_attachments"|"agent_files"]`
   нет ни в `lib/`, ни в `workspace/`, ни в `tests/`, ни в `openspec/`,
   ни в `sql/`. Метод `render_active_files_section` нигде не вызывается.
   Патч `patch_active_files_in_context`, на который ссылается docstring
   файла (`active_files_hook.py:67, 290`), физически не реализован.
   Хук целиком — артефакт нерешённой проблемы 2026-08-27 (архивация
   PDF через Consolidator теряет упоминание файлов), но в 0.3.5 side-channel
   не работает даже частично.

2. **`RuntimePatcher._attach_context_window` fallback `agent._last_usage`
   (`runtime_patcher.py:103`) — скрывает дефекты.** Когда
   `DatabaseLoggingHook._store_iteration_usage` ещё не отработал (например,
   первый оборот без tool-вызовов) или `DatabaseLoggingHook` отключён,
   fallback маскирует отсутствие данных — UI/CLI показывают пустую метрику
   `metadata.context_window`. С приходом `RuntimeEventsSubscriber`
   (см. `openspec/changes/runtime-events-subscription`, фиксирует seed
   на каждом `TurnRuntimeAdmitted`) fallback перестаёт быть нужен: bridge
   заполняется **до** первой итерации, и `used`/`limit`/`model` известны
   сразу.

3. **`DatabaseLoggingHook.after_run` (`lib/hooks/database_logging_hook.py:474-527`)
   — пишет `run_finished` в БД через приватный API `AgentHook`**, хотя
   в 0.3.5 есть штатное событие `TurnCompleted` (`nanobot/bus/runtime_events.py:60-74`).
   Однако `TurnCompleted` НЕ несёт `final_content`/`tools_used`/`stop_reason`/
   `had_injections`/`messages`, которые критичны для
   `history_search(event_type="run_finished")` (`workspace/tools/history_search_tool.py:101-220`).
   Полная замена сломает контракт history_search.

4. **`_SubagentLoggingHook._finalize` (`runtime_patcher.py:1690-1747`)
   пишет `subagent_run_finished` через monkey-patch на приватный
   `nanobot.agent.subagent._SubagentHook`**, потому что subagent в 0.3.5
   идёт через `AgentRunner.run` (минуя `TurnDelivery`) и не публикует
   `TurnCompleted`. `bus.subscribe(handler, TurnCompleted)` НЕ ловит
   subagent-события — это известное архитектурное ограничение.

5. **No-op-комментарии в `runtime_patcher.py:580-597` (метод
   `patch_context_bridge_seed`) и `runtime_patcher.py:2040-2058`** —
   устарели после применения `runtime-events-subscription`. Docstring
   метода ссылается на план миграции, который уже реализован в отдельном
   change, и продолжает сбивать grep-поиск.

Этот change выполняет **безопасный cleanup**, опираясь на подтверждённые
факты (см. impact-анализ в `openspec/changes/post-0.3.5-patches-cleanup/design.md`):
удаляет мёртвый код `ActiveFilesHook`, убирает скрывающий дефекты fallback
после подключения `RuntimeEventsSubscriber`, добавляет **параллельную**
подписку на `TurnCompleted` для нового event_type `turn_completed` (не
трогая `run_finished`), заменяет subagent-mock-publish на архитектурно
чистую публикацию через `bus.publish`, и убирает устаревшие комментарии.

## What Changes

### REMOVED

* `workspace/hooks/active_files_hook.py` целиком (370 строк мёртвого кода).
  Вместе с ним удаляются ссылки в `docs/ARCHITECTURE.md:1544` и
  `docs/architecture/nanobot-inventory.json:893`. Запись в
  `openspec/changes/nanobot-035-upgrade/proposal.md:44` (список
  сохраняемых файлов) обновляется — хук выпадает из списка.
* `RuntimePatcher._attach_context_window` fallback
  `usage = getattr(agent, "_last_usage", None) or {}` —
  `runtime_patcher.py:103`. Метод остаётся, читает только из
  `DatabaseLoggingContextBridge.get_iteration_usage` + `agent.context_window_tokens`.
* `RuntimePatcher.patch_context_bridge_seed` (`runtime_patcher.py:580-597`)
  целиком: метод, запись в `_PATCH_SPECS` (`runtime_patcher.py:231-245`),
  вызов в `apply_all` (`runtime_patcher.py:510`). В startup-логе
  `apply_all` этот spec исчезает.
* Устаревший исторический комментарий `runtime_patcher.py:2040-2058`
  (19 строк). Логика комментария уже отражена в
  `openspec/changes/nanobot-035-upgrade/design.md` и
  `openspec/changes/runtime-events-subscription/proposal.md`.

### MODIFIED

* `lib/hooks/database_logging_hook.py::_make_run_event`
  (`database_logging_hook.py:494-527`) — `latency_ms` берётся
  из нового `turn_completed` event'а через подписку, **не**
  измеряется вручную через `time.time()` в `after_run`.
  Поведение `run_finished` (payload, event_type, history_search)
  НЕ меняется.
* `lib/services/runtime_events_subscriber.py` — расширяется
  дополнительной подпиской `bus.subscribe(handler, TurnCompleted)`
  в `start()`. Handler пишет новый `LogEvent(event_type="turn_completed")`
  через `DbLoggingService.log_event`.
* `lib/services/runtime_patcher.py::patch_subagent_logging` —
  `_SubagentLoggingHook.after_run` (`runtime_patcher.py:1682-1683`)
  перед вызовом `_finalize` публикует новое кастомное событие
  `SubagentTurnCompleted` через `self.bus.publish(event)`.
  `_finalize` упрощается: убирается публикация, остаётся только запись
  в БД. `LogEvent(subagent_run_finished)` пишется **из подписки**
  на `SubagentTurnCompleted`, а не напрямую из `after_run`. Контракт
  `subagent_run_finished` (включая `parent_user_id` security boundary)
  НЕ меняется.
* `workspace/tools/history_search_tool.py` (строки 103-111 и описание
  в `description`, строка 124-126) — enum `event_type` расширяется
  значением `"turn_completed"`. Контракт `run_finished` и
  `subagent_run_finished` НЕ меняется.
* `lib/cli/hook_loader.py::scan_and_register` — больше не пытается
  импортировать `active_files_hook` (его нет, но текущий код
  `attr(workspace_dir=...)` будет падать на отсутствующем классе,
  если файл удалить без правки loader).

### NEW

* Новый файл `lib/events/subagent.py` (определение dataclass
  `SubagentTurnCompleted(AgentEvent)` с полями
  `task_id: str`, `parent_request_id: str | None`, `final_content: str`,
  `tools_used: list[str]`, `stop_reason: str | None`,
  `usage: LLMUsage | None`, `had_error: bool`, `error: str | None`).
  Реэкспортируется через `nanobot.events` (без monkey-patch — наш
  собственный namespace, потому что `nanobot.events.SubagentTurnCompleted`
  не существует и не планируется в 0.3.x).
* В `lib/services/runtime_events_subscriber.py` — два новых handler'а:
  `_handle_turn_completed` и `_handle_subagent_turn_completed`.
* В `lib/services/subagent_logging_subscriber.py` —
  подписчик на `SubagentTurnCompleted`, пишущий
  `LogEvent(subagent_run_finished)` в `agent_gateway_logs`.

### Capabilities

#### New Capabilities

* `runtime/runtime-events-observability` — нормативный контракт на
  observer-подписки на runtime-события nanobot 0.3.5 в нашем проекте.
  Описывает, какие события подписываются через
  `bus.subscribe(handler, EventType)`, какие handler'ы изолированы
  и тестируемы, как они кооперируются с `DatabaseLoggingHook` и
  `RuntimePatcher`. Включает в себя:
  - `TurnRuntimeAdmitted` (seed context-window — из существующего
    `runtime-events-subscription`);
  - `TurnCompleted` (новый — пишет `turn_completed`);
  - `SubagentTurnCompleted` (новый кастомный — пишет
    `subagent_run_finished`).

#### Modified Capabilities

* `tools-history-search` — enum `event_type` расширяется
  значением `"turn_completed"` (новый источник метрик).
  Контракт существующих `run_finished`/`subagent_run_finished`
  НЕ меняется.
* `runtime/context` — добавлено требование: на lifecycle
  `ApplicationContext` подписки `RuntimeEventsSubscriber` и
  `SubagentLoggingSubscriber` ДОЛЖНЫ быть запущены **после**
  `RuntimePatcher.apply_all` и **до** старта каналов, и
  остановлены **до** `MessageBus.drain()`. Уточняет, что
  fallback `_last_usage` в `_attach_context_window` БОЛЬШЕ НЕ
  ДОЛЖЕН существовать (он скрывает дефекты подписки).

### Impact

**Удаляется:**

* `workspace/hooks/active_files_hook.py` (370 строк)
* `RuntimePatcher.patch_context_bridge_seed` (~18 строк + запись в `_PATCH_SPECS`)
* Fallback `_last_usage` (~3 строки)
* Устаревший комментарий `runtime_patcher.py:2040-2058` (19 строк)

**Добавляется:**

* `lib/events/subagent.py` (~50 строк + dataclass `SubagentTurnCompleted`)
* Расширение `lib/services/runtime_events_subscriber.py`
  (`_handle_turn_completed` ~40 строк, `_handle_subagent_turn_completed` ~30 строк)
* `lib/services/subagent_logging_subscriber.py` (~60 строк)
* Правки `lib/hooks/database_logging_hook.py::_make_run_event`
  (~−10 строк: убираем ручной `time.time()` для latency)
* Правки `lib/services/runtime_patcher.py::patch_subagent_logging`
  (~+40 строк: публикация + перенос `_finalize` логики в подписчик)
* Правки `workspace/tools/history_search_tool.py` (enum + description,
  ~5 строк)
* Правки `lib/cli/hook_loader.py` (защита от missing class, ~3 строки)
* Правки `docs/ARCHITECTURE.md:1544`, `docs/architecture/nanobot-inventory.json:893`
  (удаление упоминаний ActiveFilesHook)
* Правки `openspec/changes/nanobot-035-upgrade/proposal.md:44`
  (удаление из списка сохраняемых файлов)
* Новый ADR `docs/architecture/decisions/active-files-hook-removal.md`
  с обоснованием удаления и описанием нерешённой проблемы
  2026-08-27 как отдельной задачи

**Тесты:**

* `tests/test_runtime_events_subscriber.py` (расширение существующего):
  добавить тесты на `_handle_turn_completed` и
  `_handle_subagent_turn_completed`.
* `tests/test_subagent_logging_subscriber.py` (новый, ~80 строк):
  fake-bus + fake-db-service, проверяет, что handler пишет
  правильный `LogEvent(subagent_run_finished)`.
* `tests/test_database_logging_hook.py` (расширение существующего):
  regression на то, что `_make_run_event` не измеряет latency
  руками после подключения подписки.
* `tests/test_runtime_patcher.py` (расширение существующего):
  убрать тесты на `patch_context_bridge_seed`; добавить тест на
  отсутствие fallback `_last_usage`.
* `tests/test_history_search_tool.py` (расширение существующего):
  enum `event_type` теперь содержит `"turn_completed"`.
* `tests/test_active_files_hook.py` (новый минимальный): проверяет,
  что файл удалён (защита от регрессии при случайном восстановлении).

**Документация:**

* `docs/architecture/decisions/active-files-hook-removal.md` (новый ADR)
* `docs/architecture/runtime-patcher-inventory.md` — статус
  `context_bridge_seed` удаляется, статус `subagent_logging` обновляется
  с подключением `SubagentTurnCompleted`.
* `workspace/TOOLS.md:49, 186` — описание нового event_type
  `turn_completed`.

### Вне scope

* Полная замена `DatabaseLoggingHook.after_run` подпиской на
  `TurnCompleted` — отклонена по результатам impact-анализа
  (`final_content`/`tools_used`/`stop_reason`/`had_injections` недоступны
  через `TurnCompleted`). Текущий `run_finished` остаётся как есть,
  подписка на `TurnCompleted` пишет **новый** event_type `turn_completed`.
* Покрытие других runtime-событий (`UserInputAccepted`,
  `SessionTurnStarted`, `RecoveryStateEvent`, `RetryWaitEvent`,
  `RetryStatusEvent`) — отдельный change при появлении потребителей.
* Расширение `nanobot.events` upstream-событиями (`SubagentTurnCompleted`
  не предлагается в upstream — наш namespace).
* Полная реализация проблемы 2026-08-27 (consolidator archives file
  references) — отдельный change при необходимости, см. ADR
  `docs/architecture/decisions/active-files-hook-removal.md`.
* Изменение `MessageBus.subscribe` API или порядка маршрутизации
  (используется публичный интерфейс `nanobot/bus/queue.py:92`).

### Зависимости

* `nanobot-ai>=0.3.5` (зафиксировано в `requirements.txt`).
* `openspec/changes/runtime-events-subscription` — change должен быть
  применён **до** этого change, потому что `RuntimeEventsSubscriber.start()`
  нужен как pre-condition для удаления `_last_usage` fallback'а.
  Если `runtime-events-subscription` ещё не в master, объединить два
  change'а в один.
* `lib/events/__init__.py` (или аналогичный реэкспорт) — если
  `nanobot.events` сейчас не имеет `subagent`-subnamespace; проверить,
  что наш dataclass `SubagentTurnCompleted` доступен через
  `from lib.events.subagent import SubagentTurnCompleted` и через
  `bus.subscribe(handler, SubagentTurnCompleted)`.

### Связанные спеки

* `openspec/changes/runtime-events-subscription` — расширяется новыми
  подписками (см. `runtime/runtime-events-observability` capability).
* `openspec/changes/nanobot-035-upgrade` — изменение статуса
  `patch_active_files_in_context` (фигурировал в `design.md` как
  план, сейчас признан нереализуемым через `AgentHook`-механизм).

### Риски (детально — в `design.md`)

| Риск | Митигация |
|---|---|
| Подписка на `TurnCompleted` пишет `turn_completed` БЕЗ `final_content` — пользователи могут думать, что там есть полный ответ | Чёткое описание в `history_search.description`; `turn_completed` помечается как метрика, не user-visible content |
| Удаление `ActiveFilesHook` без нового pipeline для side-channel — инцидент 2026-08-27 остаётся открытым | ADR явно фиксирует: «side-channel не работает в 0.3.5, его удаление — технический долг, решение проблемы архивации PDF — отдельная задача» |
| Subagent публикует `SubagentTurnCompleted` через monkey-patch `patch_subagent_logging` — хрупко для 0.4.x | Patch уже существует и работает в 0.3.5; новые подписки просто переиспользуют его сигнатуру |
| Подписки стартуют после `apply_all`, но `MessageBus.drain()` в текущем `ApplicationContext.stop` отсутствует | Lifecycle проверяется в design §L1; если `drain()` нужен — добавляется в этом change |
| Удаление `_last_usage` fallback'а до применения `runtime-events-subscription` сломает UI на first turn | `tasks.md` форсирует порядок: подписка сначала, fallback удаляется только после smoke-теста |