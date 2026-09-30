# Proposal: nanobot-0.3.5-upgrade

## Why

Проект собран поверх `nanobot-ai 0.3.0` через плотный слой monkey-патчей в `lib/services/runtime_patcher.py`. Upstream выпустил `0.3.5` (Sep 15, 2026, **809 коммитов** с 0.3.0), в котором:

1. Переписана state-машина `AgentLoop` — `_state_build/_state_compact/_state_restore/_state_save/_state_respond` удалены, заменены на `_dispatch` + `_compact_session` + `TurnContext` (`nanobot/agent/loop.py`).
2. Изменена сигнатура `_assemble_outbound` (6 → 4 позиционных + `log_content` kwarg) и `_save_turn` (добавлены `summary_checkpoint`/`input_persisted_early`).
3. Изменён `ToolContext.__init__` — добавлен `runtime_control: RuntimeControl | None = None` (`nanobot/agent/tools/context.py:79`), `file_state_store: FileStates | None = None` сохранён, `runtime_events` удалён (не было в сигнатуре).
4. Удалены оба `extract_documents` (`nanobot.utils.document.extract_documents` и `nanobot.agent.loop.extract_documents`) — патч становится no-op.
5. `Consolidator.maybe_consolidate_by_tokens` удалён — token-budget compaction идёт через `ContextCompactionEvent` (`nanobot/agent/events.py:17`).
6. `nanobot.command.builtin.cmd_compact` (`builtin.py:348`) теперь регистрирует `/compact` встроенно; наш `patch_compact_command` дублирует функционал.
7. Появился `RuntimeControl` (`nanobot/agent/tools/runtime_control.py:151`), `RuntimeContextBlock` (`nanobot/runtime_context.py:25`), `LLMUsageStore` (`nanobot/llm_usage/store.py:159`), `FileEditActivityHook`, `AgentProgressHook`, встроенный `EventSink` + типизированные `bus/outbound_events.py`.

Без правок: 9 контрактных тестов + 17 юнит-тестов runtime_patcher (вместо заявленных 59 — часть старая сигнатура, которую мы обновляем) падают, 4 патча ломают прод в первом turn'е (`patch_assemble_outbound`, `patch_project_tools`, `patch_document_text_threshold`, `patch_exec_limits.WriteStdinTool`). Текущая версия `nanobot-ai==0.3.5` уже зафиксирована в `requirements.txt`. Дополнительно: `AgentLoop.from_config` в 0.3.5 требует новый kwarg-only `tool_registry` (см. design §7 task 7.1 — поправлен `lib/core/agent_factory.py`).

Параллельно часть нашего самописного кода стала избыточной: upstream теперь сам делает auto-compact-idle guard, даёт `/compact`, даёт runtime event publisher. Цель — **использовать upstream, где он заменил наш обход, и сохранить нашу уникальную ценность (PG-персистентность, file-storage policy, SQL guard)**.

## What Changes

### BREAKING: исправление сломанных патчей

- **`patch_assemble_outbound`** — обёртка `_assemble_outbound` переписана под новую сигнатуру `(msg, final_content, stop_reason, streamed_content, *, log_content=True, turn_latency_ms=None)`. Удалены параметры `all_msgs` и `had_injections`. Учёт `metadata.context_window` переносится в подписку на `TurnRuntimeAdmitted` (см. D7 в design.md).
- **`patch_project_tools`** — `ToolContext(...)` вызов переписан: сохранён kwargs `file_state_store`, убран `runtime_events` (не в сигнатуре), добавлен `runtime_control=agent._runtime_control`. DI-расширения (`_agent_ref`, `_settings_ref`, `_cache_store_ref`, `_db_logging_service`) сохраняются через `setattr` после конструктора.
- **`patch_document_text_threshold`** — обёртка перенесена с `extract_documents` (нет в 0.3.5) на `nanobot.utils.document.reference_non_image_attachments` (`utils/document.py:681`). Семантика: bounded-extraction до `channels.document_text_threshold` символов с маркером обрезки `[text omitted (len=… > threshold=…)]` и обязательным `[Attachment: <path>]` блоком.

### MEDIUM: миграция на встроенные upstream-механизмы

- **`patch_compaction_tracking`** — обёртка `Consolidator.maybe_consolidate_by_tokens` удалена (метод отсутствует в 0.3.5). `ContextCompactionService.notify_session_compacted` подписывается на `ContextCompactionEvent` через выделенный `CompactionEventSubscriber` — событие приходит и для token-budget, и для idle-compact пути.
- **`patch_compact_command` + `lib/commands/compact_command.py`** — удалены. Upstream `/compact` (`builtin.py:348`) уже делает нужное через `loop.consolidator.compact_idle_session(...)`. Наш `ContextCompactionService.notify_session_compacted` вызывается из `CompactionEventSubscriber` при получении `OutboundMessage.event` типа `ContextCompactionEvent`, чтобы history-notice в `agent_conversation_messages` остался.
- **`patch_auto_compact_idle_guard`** — удалён. Upstream `_is_expired` (`autocompact.py:39–55`) уже short-circuit'ит при `_ttl <= 0`.
- **`patch_context_bridge_seed`** — удалён. Подписка на `TurnRuntimeAdmitted` (`turn_delivery.py:242`) через `bus.subscribe` — приоритетный путь для Streamlit-UI (см. D7 в design.md).

### LOW: гигиена и deprecation

- **`lib/hooks/base_tool_tracking_hook.py`** — удалён. Хелперы (`_iter_tool_calls`, `_tool_call_name`, ...) тривиально заменяются `AgentHookContext.tool_calls` (`nanobot/agent/hook.py`).
- **`workspace/utils/event_log.py`** — мёртвая ссылка в `lib/services/context_compaction.py:323` (файл не существует). Удалить упоминание из docstring/комментария.
- **`patch_exec_limits`** — ссылка на `exec_session.WriteStdinTool` (удалён в 0.3.5) обёрнута в `hasattr`-guard; основные exec-лимиты применяются по-прежнему.

### Сохраняется как было

`patch_context_governor`, `patch_save_turn`, `patch_exec_timeout_cap`, `patch_tool_limits`, `patch_async_session_saves`, `patch_session_dir_watch`, `patch_subagent_logging`, `patch_session_content_cleanup` — сигнатуры и пути в 0.3.5 не изменились, патчи работают как есть.

`DbLoggingService` (`agent_gateway_logs`), `PGSessionManager`, `workspace/hooks/{session_file_redirect,recent_files}_hook.py`, `lib/utils/sql_safety.py`, `workspace/utils/db.py`, `lib/services/runtime_health.py`, channel layer (`lib/channels/postgres_channel.py`), vector pipeline (`lib/services/{pg_duckdb_sync_service,duckdb_cache_store,table_registry,vector_index_service,cache_provider,cache_provider_impl}`), skill layer (`workspace/skills/*`) — **наша уникальная ценность**, в upstream нет эквивалента, остаются без изменений.

### Capabilities

### New Capabilities

- `runtime/runtime-patcher-upgrade` — нормативный контракт на политику совместимости с upstream-патчами: какие патчи обязательны, какие deprecate, как версионировать поверх nanobot. Описывает структуру `lib/services/runtime_patcher.py` и его инварианты.

### Modified Capabilities

- `runtime/context` — добавлено требование: при апгрейде upstream-nanobot `RuntimePatcher.apply_all` ДОЛЖЕН проходить через `report.failed == []` после прохождения smoke `tests/contract/`; `ContextCompactionService.compact()` ДОЛЖЕН писать history-notice и для token-budget, и для idle-пути (через `ContextCompactionEvent` подписку).
- `architecture/skill-tool-boundary` — без изменения требований (не трогаем).

### Impact

- `lib/services/runtime_patcher.py` — основной файл изменений (8 патчей правятся/удаляются/переписываются).
- `lib/services/context_compaction.py` — переход с `Consolidator.maybe_consolidate_by_tokens` на `ContextCompactionEvent` подписку; удаление мёртвой ссылки на `event_log.py`.
- `lib/commands/compact_command.py` — удаление.
- `lib/hooks/base_tool_tracking_hook.py` — удаление; хуки (`tool_audit`, `database_logging`, `terminal_tool_print`) переходят на прямые обращения к `AgentHookContext`.
- `tests/contract/` — обновление expectations под новые сигнатуры (`test_agent_loop_api.py`, `test_command_router.py`, `test_compaction_api.py`).
- `tests/test_runtime_patcher.py`, `tests/test_runtime_patcher_e2e.py`, `tests/test_tools_project_loader.py` — обновление после правок патчей; ожидаем зелёный прогон.
- `tests/test_profile_lifecycle.py`, `tests/test_history_search_tool.py`, `tests/test_pg_session_manager.py` — **не зависят от upgrade**, существующие падения остаются в работе (см. ADR в `design.md`).
- `requirements.txt` — уже зафиксировано `nanobot-ai==0.3.5`.
- Документация: `docs/architecture/runtime-patcher-inventory.md` (обновляется), `AGENTS.md` (раздел про `gateway.compact.*` остаётся), `CHANGELOG.md` (новый блок v3.x.0).

### Вне scope

- Апгрейд на ещё не вышедшие версии nanobot.
- Реализация фич, которые upstream держит у себя (например, добавление собственного `_idle_events`).
- Изменения domain skills (`audit_analyzer`, `legal_summarizer`, `office_files`).
- Изменения профилей `prod`/`test`.
- Benchmark suite.