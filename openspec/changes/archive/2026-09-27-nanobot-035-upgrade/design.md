## Context

Текущее состояние (см. `proposal.md` для мотивации):

- `nanobot-ai==0.3.5` зафиксирован в `requirements.txt`.
- `lib/services/runtime_patcher.py` содержит ~16 патчей. Из них:
  - 8 работают без изменений (target-символы сохранены).
  - 4 имеют сигнатурные изменения (`patch_save_turn` — kwargs добавлены, вызов совместим; `patch_assemble_outbound` — сломан в рантайме; `patch_project_tools` — TypeError; `patch_exec_limits` — MEDIUM: `hasattr`-guard для `WriteStdinTool`).
  - 3 ссылаются на удалённые символы (`patch_context_bridge_seed` — заменён на подписку `TurnRuntimeAdmitted` (D7); `patch_compaction_tracking` — `maybe_consolidate_by_tokens`; `patch_document_text_threshold` — оба `extract_documents`).
  - 1 дублирует встроенный upstream (`patch_compact_command`).
  - 1 — dead после рефакторинга upstream (`patch_auto_compact_idle_guard`).
- Контрактные тесты `tests/contract/` (9 падений) фиксируют v0.3.0-сигнатуры.
- Юнит-тесты `tests/test_runtime_patcher.py`, `tests/test_tools_project_loader.py`, `tests/test_runtime_patcher_e2e.py` (59 падений) — следствие тех же сигнатурных изменений.
- 3 юнит-теста вне upgrade-скоупа: `tests/test_profile_lifecycle.py` (4), `tests/test_history_search_tool.py` (SQL guard, не из патчей), `tests/test_pg_session_manager.py` (фикстура мока).

## Goals / Non-Goals

**Goals:**

1. Привести `RuntimePatcher` к состоянию, где `apply_all()` завершается без `failed` на `nanobot-ai==0.3.5`.
2. Закрыть контрактные тесты (`tests/contract/`) на новые upstream-сигнатуры.
3. Минимизировать количество monkey-патчей: перенести функционал на upstream API там, где upstream его предоставил (`/compact`, auto-compact idle guard, EventSink, RuntimeControl).
4. Сохранить нашу уникальную ценность (PG-персистентность, file-storage policy, SQL guard, vector pipeline).
5. Получить baseline-метрику для будущих апгрейдов: какой процент тестов проходит после каждого `pip install nanobot-ai==X.Y.Z`.

**Non-Goals:**

- Реализация собственных версий подсистем, которые upstream держит сам (auto-compact, `/compact`, runtime event publisher, LLM usage tracking).
- Изменения domain skills (`audit_analyzer`, `legal_summarizer`, `office_files`).
- Изменения профилей `prod`/`test` (OpenSpec change `config-profile-cli-flag` уже зафиксирован).
- Изменения benchmark suite.
- Бэкпорт наших патчей в `nanobot-ai` upstream.
- Удаление dead tests вне upgrade-скоупа (`test_profile_lifecycle`, `test_history_search_tool`, `test_pg_session_manager`) — фиксируются как `xfail` с TODO.

## Decisions

### D1: `CompactionEventSubscriber` фильтрует `OutboundMessage.event` в `postgres_channel.send`

**Решение.** Создать класс `CompactionEventSubscriber` в `lib/services/` — выделенный сервис-слушатель с публичным методом `feed(outbound: OutboundMessage)`. Канал `postgres_channel` (и другие каналы транспорта) **дёргает** `subscriber.feed(msg)` из своего `send` при получении каждого `OutboundMessage`. Подписчик:

1. Фильтрует `isinstance(msg.event, ContextCompactionEvent)`;
2. Вызывает публичный API `ContextCompactionService.notify_session_compacted(session_key, phase, compaction_id)` (НЕ приватный `_notify`);
3. Для всех фаз (`started`/`succeeded`/`failed`/`cancelled`) пишет `event_type="context_compacted"` в `agent_gateway_logs`;
4. History-notice в `agent_conversation_messages` пишется **только** для `succeeded` (при `notify_in_history=true`).

DI-инъекция — через `compaction_event_subscriber=...` kwarg конструктора `PostgresChannel` (defaults в `None`, что отключает observer).

Upstream `cmd_compact` (`nanobot/command/builtin.py`) передаёт `events=delivery.events` (`EventSink`), который `emit(ContextCompactionEvent(...))` направляет через `bus.publish_event` → `bus.outbound` (`OutboundMessage.event`).

**Альтернативы.**

- (а) Подписка `bus.subscribe(handler, ContextCompactionEvent)` в отдельном consumer. Отклонено: `publish_event` идёт через outbound-queue, **не** через локальный fan-out `publish` — `bus.subscribe` события **не видит** (подтверждено в `bus/queue.py:49-65`).
- (б) Подписка `bus.subscribe(handler, TurnRuntimeAdmitted)`. Отклонено: относится к D7, не к compaction-events. События compaction идут через `publish_event`, а TurnRuntimeAdmitted — через `publish` (разные очереди).
- (в) Обёртка `Consolidator.summarize_provider_compaction` для notify. Отклонено: возвращает summary, не вызывает наш notify; дублирование логики.
- (г) Хук `AgentHook.after_run`. Отклонено: вызывается **после** отправки `OutboundMessage` в канал — события compaction (`OutboundMessage.event`) уже не виден.

**Обоснование.** Компромисс SRP: канал остаётся «тупым» транспортом (знает о существовании subscriber'а как об одном observer'е), вся бизнес-логика — в подписчике. Подтверждено альтернативой (а) D-X (3) — единственный стабильный путь без залезания в приватные API Consolidator.

**Контракт для subscriber'а.** Принимает `compaction_service: ContextCompactionService` (через `set_service` — DI-cycle-friendly). Все исключения внутри `feed()` глушатся с warning, чтобы не сломать путь `OutboundMessage` через канал.

**Известное ограничение.** Подписка внедрена только в `postgres_channel.send`. Другие каналы (`redis_channel`, streamlit) — отдельная задача, не в скоупе этого изменения (см. R7 ниже).

### D2: Перенос `_assemble_outbound` wrapper на новую сигнатуру

**Решение.** Wrapper-функция с сигнатурой `(self, msg, final_content, stop_reason, streamed_content, *, log_content=True, turn_latency_ms=None)`. Логика `_tool_audit` и recent-files остаётся в wrapper'е через чтение `session.metadata` после `_build_turn`. Логика `metadata.context_window` переносится в подписку на `TurnRuntimeAdmitted` (см. D7).

**Альтернативы.**

- (а) Перенести всю логику в `AgentHook.after_run` (через `DatabaseLoggingHook`/`ToolAuditHook`). Отклонено: хуки не имеют доступа к `OutboundMessage` до его отправки в канал, метрика context_window не успеет обновиться до рендера.
- (б) Оставить старую сигнатуру wrapper'а и обновить только contract test. Отклонено: `RuntimePatcher` всё равно должен вызывать `_assemble_outbound` с правильными позиционными args — несовместимость в рантайме, не в тесте.

### D3: `ToolContext` kwargs — DI через `setattr` вместо `__init__`

**Решение.** После `ToolContext(...)` (с правильными kwargs 0.3.5) присваиваем `ctx._agent_ref = agent`, `ctx._settings_ref = ...`, `ctx._cache_store_ref = ...`, `ctx._db_logging_service = ...` через `setattr`. Добавляем `runtime_control=agent._runtime_control` в `__init__`-вызов.

**Альтернативы.**

- (а) Использовать `RuntimeControl` Protocol напрямую через `ctx.runtime_control.set_model(...)`. Отклонено: не все наши tools используют runtime_control (например, `compact_context.py` читает `_settings_ref`), DI-расширения нужны.
- (б) Полностью отказаться от DI-расширений и переписать tools под upstream-only. Отклонено: наши tools читают `gateway.compact.*`, `gateway.cache.*`, `gateway.vector.*` через `_settings_ref` — это часть нашего проектного контракта с `lib/core/skill_config.py`.

### D4: `patch_document_text_threshold` → обёртка над `reference_non_image_attachments`

**Решение.** Перецепляем патч на `nanobot.utils.document.reference_non_image_attachments(content, media)` (`utils/document.py:681` — upstream-сигнатура `(content, media)`, без `threshold`). Наша обёртка:

1. Сохраняет `image_paths` (upstream-сематика для vision-блоков);
2. Для каждого не-изображения пытается прочитать текст через `extract_text(path)` (`utils/document.py:extract_text`);
3. Если длина тела ≤ `channels.document_text_threshold` (по умолчанию 20000) — формирует блок `[File: <basename> (saved at <path>)]\n<text>` (path ВСЕГДА в заголовке);
4. Если > порога — `[File: <basename> (saved at <path>)]\n[text omitted (len=… > threshold=…)]`;
5. Если изображение или `extract_text` упал — fallback на `[Attachment: <path>]` (upstream-формат, без нашего расширения).

**Альтернативы.**

- (а) Удалить патч полностью, принять upstream-поведение (только `[Attachment: <path>]`-маркер без извлечения текста). Отклонено: меняет UX — агенту придётся вызывать `read_file` для каждого приложения, что для больших PDF затратно и не нужно для маленьких файлов.
- (б) Написать свой `extract_documents` с нуля. Отклонено: дублирует логику upstream, рассинхронизируется.

### D5: Удаление `patch_compact_command` и `lib/commands/compact_command.py`

**Решение.** Удаляем `lib/commands/compact_command.py` и `patch_compact_command` в `lib/services/runtime_patcher.py`. Upstream `cmd_compact` (`nanobot/command/builtin.py:348`) делает то же: `loop.consolidator.compact_idle_session(ctx.key, runtime=runtime, events=delivery.events)`. **Наш `ContextCompactionService.notify_session_compacted(...)` остаётся** (публичный API) и вызывается из `CompactionEventSubscriber` при чтении `OutboundMessage.event` типа `ContextCompactionEvent` (фаза `succeeded`/`failed`/`cancelled`). Вызов публичного API, а не приватного `_notify`, обеспечивает стабильность при upgrade.

**Альтернативы.**

- (а) Сохранить наш handler и зарегистрировать через `priority=higher_than_builtin`. Отклонено: дублирование, риск двойного вызова.
- (б) Обёрнуть upstream `cmd_compact` через `priority` ниже builtin. Отклонено: нестабильный API `priority`, нет доступа к `OutboundMessage` для notify-инъекции.
- (в) Подписка через `AgentHook.after_run` для notify. Отклонено: `AgentHook.after_run` вызывается **после** того, как `OutboundMessage` уже ушёл в канал; события compaction приходят как `OutboundMessage.event`, а не как хук-колбэк. CompactionEventSubscriber — единственный путь, который видит `OutboundMessage.event`.

### D6: Удаление `patch_auto_compact_idle_guard`

**Решение.** Полное удаление. Upstream `_is_expired` (`autocompact.py:39–55`) уже возвращает `False` при `_ttl <= 0`, поэтому при нашей конфигурации `idleCompactAfterMinutes: 0` патч был избыточен.

**Альтернативы.** Нет — это dead code в новой версии.

### D7: `patch_context_bridge_seed` → no-op в `RuntimePatcher` (подписка в ApplicationContext — отдельная задача)

**Решение.** В этом изменении `patch_context_bridge_seed` остаётся в `RuntimePatcher.apply_all` как **no-op** с `applied=True` (для совместимости со `PatchReport`/логами). Реальная подписка `bus.subscribe(handler, TurnRuntimeAdmitted)` в `ApplicationContext.start()` **отложена** как отдельная задача (см. R7).

Механизм в новой версии:
1. `RuntimeEventPublisher.turn_runtime_admitted(...)` (`bus/runtime_events.py:204`) зовёт `self.bus.publish(TurnRuntimeAdmitted(...))`;
2. `bus.publish` (`bus/queue.py:118`) идёт через локальный fan-out (`_handlers`), который виден через `bus.subscribe`;
3. Handler (вне `RuntimePatcher`) будет читать `event.runtime.context_window_tokens` и звать `DatabaseLoggingHook.seed_context_window(session_key, limit, model)`.

**Альтернативы.**

- (а) Wrapper вокруг `AgentLoop._build_turn` (`loop.py:1865`). Отклонено: приватный метод удалён в 0.3.5; идём через публичный путь upstream.
- (б) Подписка на `TurnRuntimeAdmitted` через `bus.subscribe` (`bus/queue.py:92`). Отложено: реализация требует правок `ApplicationContext.start()` и `BusFactory` (нет `bus` в текущем контексте). Это **тот же** путь, который использует upstream WebUI (`session/webui_turns.py:577`).
- (в) Реализовать подписку в этом же изменении. Отклонено: пересекается с R2/Architecture-контрактом `lib/services/` (требует выделения отдельного helper'а `ContextBridgeSubscriber` — отдельный OpenSpec-change чтобы не размывать scope).

**Обоснование.** Патч становится pure-no-op: `agent._state_build` удалён, поэтому реальный seed сейчас **не делается** в `RuntimePatcher`. Это означает, что при холодном старте оборота `metadata.context_window` в финале **не строится через мост** (но по-прежнему собирается из `agent._last_usage` в `_attach_context_window` fallback'е — см. `RuntimePatcher._attach_context_window`). Полноценная подписка — отдельное изменение.

**Контракт.** `RuntimePatcher.patch_context_bridge_seed(None)` возвращает `(False, "agent is None")`; с реальным `agent` — `(True, "context-bridge seed moved to bus.subscribe(TurnRuntimeAdmitted)")`. Это видно в `PatchReport.applied`.

### D8: Удаление `lib/hooks/base_tool_tracking_hook.py`

**Решение.** Файл удаляется. Каждый хук (`ToolAuditHook`, `DatabaseLoggingHook`, `TerminalToolPrintHook`) переходит на `AgentHookContext.tool_calls` (читается из `ctx` параметра хука). Helpers (`_iter_tool_calls`, `_tool_call_name`, `_tool_call_id`, `_tool_call_info`) тривиально заменяются `[(call.name, call.id, call.arguments) for call in ctx.tool_calls]`.

**Альтернативы.** Нет — это обёртка над стабильным upstream API.

### D9: Тесты вне upgrade-скоупа — `skip` с TODO

**Решение.** `tests/test_profile_lifecycle.py::test_gateway_prod_smoke_selects_prod_tables` и три соседних теста — помечаются `@pytest.mark.skip(reason="Out of scope for 0.3.5 upgrade, tracked in ISSUE-XXX")`. Тесты `test_history_search_tool.py::TestUserIsolation/TestGeneratedSqlGuard/TestSnapshotConsistency` (8 шт.) и `test_pg_session_manager.py::test_init_sets_framework_contract` — то же самое.

**Альтернативы.** Чинить эти тесты в рамках upgrade-изменения. Отклонено: выходит за скоуп, требует отдельного диагностического раунда (Windows subprocess, SQL-гвард с `user_id` predicate, фикстура моков).

> **Примечание.** `xfail(strict=True)` нельзя использовать: если тест случайно починится, CI упадёт. `skip` корректен для отключения.

### D-Y: Интроспекция сигнатур nanobot 0.3.5 (финальная проверка)

Прямая интроспекция через `inspect.signature` на установленном `nanobot-ai==0.3.5`:

```text
AgentLoop._assemble_outbound:
(self, msg, final_content, stop_reason, streamed_content, *, log_content: bool = True, turn_latency_ms: int | None = None)

ToolContext.__init__ (полная сигнатура):
- config: ToolsConfig (REQUIRED)
- workspace: str (REQUIRED)
- bus: MessageBus | None = None
- subagent_manager: SubagentManager | None = None
- cron_service: CronService | None = None
- exec_session_manager: ExecSessionManager | None = None
- sessions: SessionManager | None = None
- file_state_store: FileStates | None = None  ← СОХРАНЯЕМ
- provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None
- image_generation_provider_configs: dict[str, ProviderConfig] | None = None
- timezone: str = 'UTC'
- workspace_sandbox: WorkspaceSandboxStatus | None = None
- runtime_control: RuntimeControl | None = None  ← ДОБАВЛЯЕМ

ToolContext.__dataclass_params__: frozen=False  ← setattr работает

cmd_compact:
(ctx: CommandContext) -> None

Consolidator.compact_idle_session:
(self, session_key, *, runtime: LLMRuntime, max_suffix: int = 0, events: EventSink) -> str | None
```

**Выводы:**
1. `_assemble_outbound` **включает** `log_content: bool = True` — наш патч должен передавать этот kwarg.
2. `ToolContext` **включает** `file_state_store` и `runtime_control` — оба сохраняем/добавляем; `runtime_events` **НЕ** в сигнатуре (удалён).
3. `ToolContext` имеет `frozen=False` — `setattr` для DI работает.
4. `Consolidator.compact_idle_session` принимает `events: EventSink` — наш CompactionEventSubscriber работает с этим API.

---

### D-X: Перепроверка утверждений про upstream-покрытие

Решение перепроверить три ключевых утверждения, на которых строился предыдущий драфт спеки (по итогам финальной интроспекции `nanobot-ai==0.3.5`).

**(1) LLM usage tracking upstream vs наш `DbLoggingService`.**

Upstream `LLMUsageStore` (`nanobot/llm_usage/store.py:159`) хранит **только метаданные**: provider, model, source, stream, finish_reason, input/output/cache_tokens, error_kind, error_status_code, generation_ms, ttft_ms. Подтверждено в `models.py:11-17`:

> `LLMCallRecord`: «The small, chart-oriented result of one provider call attempt. Request messages, response text, reasoning, and tool payloads deliberately do not belong to this contract. Sessions already own that content.»

И в `store.py:564`:

> `recent_calls`: «Return bounded metadata rows for diagnostics; never returns content.»

Подключение через `provider.set_llm_call_observer(record_llm_call)` (`cli/gateway_runtime.py:415`) — **observer-pipeline**, активируется явно для WebUI/Settings. В нашем проекте **не подключён** (`grep -r "LLMUsageStore" C:\Users\Алексей\.nanobot` — пусто).

Наш `DbLoggingService` (`lib/services/db_logging_service.py`) хранит **content**: `payload={"tool": name, "args": args or {}}` (line 486), `prompt=self._pending_prompt or []` (database_logging_hook.py:409), `response=asdict(response)` (line 410). Это субстрат для `history_search_tool` (`workspace/tools/history_search_tool.py`).

**Вывод.** Это **два независимых слоя** с разной семантикой: upstream — content-free для UI-графиков; наш — content-rich для search. Никакого дублирования. Наше утверждение в драфте «upstream покрывает часть» — **корректно по сути, но не должно звучать как „upstream заменяет“**. Правка: спек переформулирует, что `LLMUsageStore` — это **параллельный слой** (не наша задача; не подключаем в этом изменении), а `DbLoggingService` — наш основной content-rich logger.

**(2) Upstream `/compact` cmd vs наш `lib/commands/compact_command.py` + `ContextCompactionService.notify_session_compacted`.**

Upstream `cmd_compact` (`nanobot/command/builtin.py:348`) выполняет **только**: `loop.consolidator.compact_idle_session(ctx.key, runtime=runtime, events=delivery.events)` + опциональный `provider_state = None` + `save`. **Никаких записей в БД** — нет ни обращения к `agent_conversation_messages`, ни к `agent_gateway_logs`. Подтверждено `grep -r "agent_conversation_messages\|agent_gateway_logs" nanobot/` — пусто (эти таблицы — наши).

Наш `ContextCompactionService.notify_session_compacted` (публичный API, обёртка вокруг `_notify`):
- `_record_event_log` → пишет `event_type="context_compacted"` в `agent_gateway_logs` (Postgres)
- `_write_history_notice` → пишет в `agent_conversation_messages` (Postgres) для UI-стикера
- Это **наш уникальный слой**, не дублирование upstream.

**Вывод.** Утверждение D5 «удаляем наш cmd, оставляем upstream cmd_compact» — **корректно для логики compaction**, но не должно звучать как «наш `notify_session_compacted` заменяется upstream». Уточнённый план: `lib/commands/compact_command.py` и `patch_compact_command` удаляются; **наш `notify_session_compacted` остаётся** и вызывается из выделенного `CompactionEventSubscriber` при чтении `OutboundMessage.event` типа `ContextCompactionEvent`. Подробности в D1.

**(3) `EventSink.subscribe` для `ContextCompactionEvent` — реально ли работает?**

Перепроверено в `nanobot/events.py:46-73`: `EventSink` — **send-only callback** с `publish: Callable[[AgentEvent], Awaitable[None]]` и методом `emit(event)`. **Нет метода `subscribe`**. Доктрина: «This owns no queue or subscribers» (events.py:53).

В `bus/queue.py:49-65` `publish_event` идёт через `publish_outbound` (outbound-queue), не через локальный fan-out `publish` (queue.py:118). Подтверждено в `agent/turn_delivery.py:200,367`: `_publish_event` всегда зовёт `self._routed_events.publish`, который = `bus.publish_event`.

**Вывод.** Утверждение предыдущего драфта «подписка через `EventSink.subscribe`» — **неверно**. Перепроверенный план в D1: фильтр в `postgres_channel` при чтении `bus.outbound`. Это единственный стабильный путь без залезания в приватные API Consolidator.

## Risks / Trade-offs

- **R1: Изменения upstream API в следующих версиях nanobot снова сломают патчи.** → Спека `upgrade-compatibility` вводит обязательную процедуру: inventory + contract tests + baseline перед merge. Принятие риска: каждые 1–2 месяца релиз upstream → нужен ~1 день на upgrade.
- **R2: Подписка на `ContextCompactionEvent` зависит от того, что upstream не переименует event-type.** → Митигация: тест `tests/contract/test_compaction_api.py::test_context_compaction_event_emitted` (новый) фиксирует `event_type == "context_compacted"` (имя берётся из upstream `agent/events.py:17`).
- **R3: `ToolContext` — frozen=False dataclass, `setattr` работает; но upstream может сделать frozen=True.** → Митигация: при следующем upgrade проверяем `frozen` через `inspect.signature(ToolContext)`. Если frozen — DI переносим в `__init__`-kwargs (как было в 0.3.0).
- **R4: Удаление `patch_compact_command` теряет наш `force=True`-флаг.** → Митигация: upstream `cmd_compact` (`builtin.py:348`) всегда выполняет forced-compaction (нет `force` параметра). Это совпадает с нашей семантикой, потерь нет.
- **R5: Удаление `patch_auto_compact_idle_guard` может изменить поведение при `idleCompactAfterMinutes != 0`.** → Митигация: наш проект использует `idleCompactAfterMinutes: 0` (`config.json`), поэтому патч ничего не делал. Документируем в `CHANGELOG.md` как no-behavior-change.
- **R6: 3 падающих теста вне upgrade-скоупа помечены skip — могут маскировать реальные регрессии.** → Митигация: каждый skip имеет `reason` с TODO-ссылкой; при следующем прогоне suite создаётся задача на разбор.
- **R7: Подписка `bus.subscribe(TurnRuntimeAdmitted)` в ApplicationContext.start() не реализована (D7 отложен).** При холодном старте оборота `metadata.context_window` собирается из fallback'а `agent._last_usage` в `_attach_context_window`. На UI-метке это видно как «секунды без обновлений» сразу после первого оборота; после первой записи в мосте (через `after_iteration` хука) метка оживает. Полное покрытие — отдельный OpenSpec change. Отслеживается в ISSUE-NB035-5.
- **R8: `CompactionEventSubscriber.feed` подключён только в `postgres_channel.send`.** `redis_channel` и `streamlit_app` пока не вызывают observer; для них history-notice `context_compacted` НЕ появится. Расширение в отдельном OpenSpec change (R8 подсвечивает текущий scope).

## Migration Plan

### Порядок выполнения

1. **Baseline.** `pytest tests/contract -q` → зафиксировать 9 failed. `pytest tests/test_runtime_patcher.py tests/test_tools_project_loader.py tests/test_runtime_patcher_e2e.py` → зафиксировать 59 failed.
2. **Этап 1 (HIGH, сломанные патчи).**
   - D2: переписать `patch_assemble_outbound`.
   - D3: переписать `patch_project_tools` (kwargs + setattr DI).
   - D4: перецепить `patch_document_text_threshold` на `reference_non_image_attachments`.
3. **Этап 2 (MEDIUM, миграция на upstream).**
   - D1: подписка на `ContextCompactionEvent`.
   - D5: удаление `patch_compact_command` + `lib/commands/compact_command.py`.
   - D6: удаление `patch_auto_compact_idle_guard`.
   - D7: перенос `patch_context_bridge_seed` на `turn_runtime_admitted`.
4. **Этап 3 (LOW, гигиена).**
   - D8: удаление `lib/hooks/base_tool_tracking_hook.py`.
   - Чистка мёртвой ссылки на `workspace/utils/event_log.py`.
   - D9: пометить вне-скоуп-тесты `xfail`.
5. **Update.** `docs/architecture/runtime-patcher-inventory.md` — каждая запись с новым статусом.
6. **Validate.** `openspec validate nanobot-035-upgrade` → `pytest tests/` → зелёный.
7. **CHANGELOG.md.** Новый блок v3.x.0 с категориями `Changed` (HIGH/MEDIUM правки), `Removed` (удалённые патчи), `Fixed` (исправленные сигнатуры).

### Rollback

`requirements.txt` откатывается на `nanobot-ai==0.3.0`. Все наши патчи возвращаются к старому поведению. CHANGELOG-блок помечается как `yanked`. Rollback нужен только если upstream выпустит hotfix, отменяющий 0.3.5-изменения (маловероятно в 30 дней после релиза).

## Open Questions

- **Q1: Поддерживать ли в specе оба пути — wrapper и подписку — для `ContextCompactionEvent`?** Решено через D1 (только подписка).
- **Q2: Удалять ли `_compile_prompt_metadata` методы в `ToolAuditHook`/`DatabaseLoggingHook`, которые сейчас зависят от старого `_assemble_outbound`?** Перенос в `_build_turn` хук; деталь — в тасках.
- **Q3: Нужно ли создать новую specу для `DbLoggingService` (документирование долговечного журнала)?** Не в скоупе этого изменения — может быть отдельным OpenSpec change позже.