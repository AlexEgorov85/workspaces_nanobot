## Purpose

Lets the project observe nanobot 0.3.5 runtime events through `MessageBus.subscribe(handler, EventType)` without ad-hoc monkey-patches and without losing the per-event contract (who publishes it, which fields are stable, which fields are explicitly out of scope). It defines the lifecycle of event subscribers (start order, stop order, isolation) and their cooperation with `DatabaseLoggingHook` so that observer data flows into `agent_gateway_logs` through a single seam.

## ADDED Requirements

### Requirement: Подписки на runtime-события регистрируются через единый observer-сервис

Система MUST ДОЛЖНА регистрировать `bus.subscribe(handler, EventType)` для runtime-событий nanobot 0.3.5 через один `RuntimeEventsSubscriber` (расширение существующего из `openspec/changes/runtime-events-subscription`). Новые подписки MUST ДОЛЖНЫ добавляться методами `_subscribe_to(handler, EventType)` этого сервиса, а не отдельными модулями с собственным `bus.subscribe(...)`.

#### Scenario: TurnCompleted подписка добавлена через observer
- **WHEN** `ApplicationContext.start()` завершает инициализацию подписчиков
- **THEN** `bus.subscribe(handler, TurnCompleted)` уже зарегистрирована
- **AND** handler доступен через `RuntimeEventsSubscriber.handlers()` для тестов

#### Scenario: SubagentTurnCompleted подписка изолирована от main-loop
- **WHEN** subagent публикует `SubagentTurnCompleted` через `bus.publish(event)` (см. `runtime_patcher.py::patch_subagent_logging`)
- **THEN** подписчик `_handle_subagent_turn_completed` изолирован от `DatabaseLoggingHook.after_run` для main-loop и пишет в `agent_gateway_logs` только `event_type="subagent_run_finished"`

### Requirement: Lifecycle подписок детерминирован относительно apply_all и каналов

Система MUST ДОЛЖНА гарантировать порядок: `RuntimePatcher.apply_all()` → `RuntimeEventsSubscriber.start()` → старт каналов. На shutdown: подписчики MUST ДОЛЖНЫ быть остановлены до того, как `MessageBus.drain()` прекратит обработку исходящих событий. Если `MessageBus.drain()` отсутствует в текущем `ApplicationContext.stop()`, change MUST ДОЛЖЕН его добавить.

#### Scenario: Подписки стартуют после apply_all
- **WHEN** `ApplicationContext.start()` вызывается
- **THEN** `RuntimePatcher.apply_all()` завершён
- **AND** `RuntimeEventsSubscriber.start()` вызван
- **AND** каналы ещё НЕ запущены (никаких inbound-сообщений до того, как подписки активны)

#### Scenario: Подписки останавливаются до bus.drain
- **WHEN** `ApplicationContext.stop()` вызывается
- **THEN** `RuntimeEventsSubscriber.stop()` вызван (handler'ы дерегистрированы)
- **AND** `MessageBus.drain()` ожидает завершения in-flight handler'ов (если они были)
- **AND** каналы остановлены ПОСЛЕ `bus.drain()`

### Requirement: TurnCompleted пишет turn_completed без потери run_finished

Система MUST ДОЛЖНА подписаться на `TurnCompleted` (через `bus.subscribe(handler, TurnCompleted)`) и записывать `LogEvent(event_type="turn_completed", payload={latency_ms, outcome, failure_kind, failure_error_kind, failure_attempts, usage_tokens, round_usage_tokens, runtime_model})` в `agent_gateway_logs` через `DbLoggingService.log_event`. Система MUST НЕ ДОЛЖНА удалять или изменять существующий `LogEvent(event_type="run_finished", payload={final_content, tools_used, stop_reason, had_injections, request_id})`, потому что `history_search(event_type="run_finished")` использует `payload.final_content` для отображения прошлых ответов агента.

#### Scenario: Каждый оборот пишет оба события
- **WHEN** агент завершает user-turn
- **THEN** в `agent_gateway_logs` присутствует `event_type="turn_completed"` с полями `latency_ms`, `outcome`, `usage_tokens`
- **AND** в `agent_gateway_logs` присутствует `event_type="run_finished"` с `payload.final_content` (полный текст ответа)

#### Scenario: Ошибка handler'а не ломает TurnCompleted публикацию
- **WHEN** handler `bus.subscribe(handler, TurnCompleted)` бросает исключение
- **THEN** `bus.publish` логирует исключение и продолжает обработку следующих handler'ов (не падает)
- **AND** `run_finished` всё равно пишется через `DatabaseLoggingHook.after_run`

### Requirement: Subagent публикует SubagentTurnCompleted через bus.publish

Система MUST ДОЛЖНА публиковать кастомный `SubagentTurnCompleted(task_id, parent_request_id, final_content, tools_used, stop_reason, usage, had_error, error)` через `bus.publish(event)` сразу после завершения `AgentRunner.run` в `_SubagentLoggingHook.after_run` (расширение существующего патча в `runtime_patcher.py:1682-1683`). Система MUST ДОЛЖНА подписаться на это событие через `bus.subscribe(handler, SubagentTurnCompleted)` и handler MUST ДОЛЖЕН писать `LogEvent(event_type="subagent_run_finished", payload={task_id, task, final_content, tools_used, stop_reason, request_id, parent_request_id, parent_user_id, usage_tokens, had_error})`. Контракт `subagent_run_finished` (включая `parent_user_id` security boundary) MUST НЕ ДОЛЖЕН меняться.

#### Scenario: Subagent завершает успешно
- **WHEN** `AgentRunner.run(AgentRunSpec(hook=_SubagentLoggingHook(...)))` завершается
- **THEN** `_SubagentLoggingHook.after_run` публикует `SubagentTurnCompleted(had_error=False, final_content, tools_used, ...)` через `bus.publish`
- **AND** подписчик пишет `LogEvent(subagent_run_finished, had_error=False)` в `agent_gateway_logs`

#### Scenario: Subagent завершается с ошибкой
- **WHEN** `AgentRunner.run` бросает исключение
- **THEN** `_SubagentLoggingHook.on_error` публикует `SubagentTurnCompleted(had_error=True, error="...")` через `bus.publish`
- **AND** подписчик пишет `LogEvent(subagent_run_finished, had_error=True)` с полем `error` в payload

#### Scenario: Подписчик изолирован от main-loop
- **WHEN** `bus.publish(SubagentTurnCompleted(...))` вызывается
- **THEN** `_handle_subagent_turn_completed` срабатывает ТОЛЬКО для этого события (не для `TurnCompleted` main-loop)
- **AND** `DatabaseLoggingHook.after_run` для main-loop остаётся источником `run_finished`

### Requirement: Fallback _last_usage удалён после подключения подписки

Система MUST НЕ ДОЛЖНА иметь fallback `usage = getattr(agent, "_last_usage", None) or {}` в `RuntimePatcher._attach_context_window` (`runtime_patcher.py:103`). После применения `runtime-events-subscription` `DatabaseLoggingContextBridge.get_iteration_usage(session_key)` MUST ДОЛЖЕН возвращать непустые `usage`/`limit`/`model` к моменту первого `OutboundMessage` с `_final_turn=True` (включая обороты без tool-вызовов), потому что `RuntimeEventsSubscriber` засевает bridge на каждом `TurnRuntimeAdmitted`.

#### Scenario: First turn без tool-вызовов
- **WHEN** агент делает оборот без tool-вызовов (single-step генерация)
- **THEN** `metadata.context_window.used` НЕ равен 0
- **AND** `metadata.context_window.limit` соответствует `runtime.context_window_tokens` из `TurnRuntimeAdmitted`
- **AND** `metadata.context_window.model` соответствует `runtime.model`

#### Scenario: Подписка не зарегистрирована (regression)
- **WHEN** `RuntimeEventsSubscriber.start()` НЕ был вызван
- **THEN** `_attach_context_window` MUST raise `ContextWindowNotSeededError` (новый тип), а НЕ возвращать `used=0` через fallback
- **AND** UI получает явное сообщение об ошибке вместо пустой метрики

### Requirement: ActiveFilesHook удалён, инцидент 2026-08-27 зафиксирован в ADR

Система MUST ДОЛЖНА удалить `workspace/hooks/active_files_hook.py` целиком (370 строк мёртвого кода). Система MUST НЕ ДОЛЖНА вызывать `render_active_files_section` или читать `session.metadata["user_attachments"|"agent_files"]`. Система MUST ДОЛЖНА создать ADR `docs/architecture/decisions/active-files-hook-removal.md`, фиксирующий: (а) почему хук удалён (метод `AgentHook.before_user_turn` отсутствует в 0.3.5, side-channel не работает); (б) что инцидент 2026-08-27 (consolidator archives file references) остаётся открытым; (в) что решение этой проблемы — отдельная задача, не входит в этот change.

#### Scenario: Файл ActiveFilesHook отсутствует
- **WHEN** проект собирается
- **THEN** `workspace/hooks/active_files_hook.py` НЕ существует
- **AND** `lib/cli/hook_loader.py::scan_and_register` НЕ пытается его импортировать (защита через `try/except ImportError` или явный allowlist)
- **AND** ADR `docs/architecture/decisions/active-files-hook-removal.md` присутствует в репо

#### Scenario: history_search не находит user_attachments
- **WHEN** агент ищет прошлые активные файлы через `history_search(query="user_attachments")`
- **THEN** результат пуст (записи не существуют)
- **AND** никакой runtime-ошибки

## REMOVED Requirements

### Requirement: RuntimePatcher.patch_context_bridge_seed как no-op
**Reason**: метод стал no-op после upgrade до nanobot-ai 0.3.5 (см. `openspec/changes/nanobot-035-upgrade/design.md §D7`); его обязанность перенесена в `RuntimeEventsSubscriber` (см. `openspec/changes/runtime-events-subscription`).
**Migration**: ссылка на метод удаляется из `runtime_patcher.py::apply_all` и `_PATCH_SPECS`. В startup-логе этот spec исчезает. Код, который раньше вызывал `patch_context_bridge_seed(agent)`, ничего не делает (no-op stub удалён целиком).

### Requirement: Исторический комментарий runtime_patcher.py:2043-2057
**Reason**: комментарий дублирует информацию из `openspec/changes/nanobot-035-upgrade/design.md` и `openspec/changes/runtime-events-subscription/proposal.md` после их применения. Audit-trail сохранён в OpenSpec, в коде комментарий больше не нужен.
**Migration**: ничего — комментарий удаляется без замены.

### Requirement: runtime/events/turn_completed в payload содержит final_content
**Reason**: `TurnCompleted` в nanobot 0.3.5 НЕ содержит `final_content` (см. `nanobot/bus/runtime_events.py:60-74`); требование ошибочно и не должно существовать.
**Migration**: ничего — требование было ошибочным.