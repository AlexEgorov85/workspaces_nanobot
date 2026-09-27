# RuntimePatcher Inventory

> Каталог всех monkey-patch'ей к `nanobot-ai==0.3.5`.
> Определение: [`lib/services/runtime_patcher.py`](../../lib/services/runtime_patcher.py).
> См. также [nanobot-inventory.md](nanobot-inventory.md).

**Принцип (TARGET_ARCHITECTURE §20):** каждый patch обязан иметь purpose,
target, nanobot version, проверенную public alternative, upgrade risk и тест.
Если upstream даёт официальный extension point — patch заменяется на него.

**Единственная точка применения:** `RuntimePatcher.apply_all(...)` из
`ApplicationContext.create()` (`lib/core/application_context.py`). Каждый патч
в try/except → при изменении API nanobot патч уходит в `PatchReport.skipped/failed`,
процесс не падает.

---

## Классификация

| Категория | Значение |
|---|---|
| **KEEP** | upstream не даёт точки расширения — патч необходим |
| **ISOLATE+TESTS** | патч нужен, но требует contract-тестов на целевой API |
| **REVIEW** | возможно есть публичная альтернатива — проверить при апгрейде |
| **DEPRECATED** | функционал перенесён на upstream-механизм в nanobot 0.3.5; патч оставлен как no-op для совместимости со `PatchReport`/логами |

---

## Сводная таблица (nanobot-ai 0.3.5)

| # | Патч | Target (nanobot API) | Risk | Категория |
|---|---|---|---|---|
| 1 | `context_bridge_seed` | **УДАЛЁН** в opencode change post-0.3.5-patches-cleanup (коммит 2163f05). Seed лимита делается подпиской на `TurnRuntimeAdmitted` через `RuntimeEventsSubscriber.start()`. | — | REMOVED |
| 2 | `context_governor` | `ContextGovernor.normalize_tool_result` | HIGH | ISOLATE+TESTS |
| 3 | `save_turn` | `agent._save_turn` | HIGH | KEEP |
| 4 | `session_content_cleanup` | `Session.add_message` | MEDIUM | KEEP |
| 5 | `async_save` | `agent.sessions.save` | MEDIUM | KEEP |
| 6 | `session_dir_watch` | `sessions.save` (diagnostic) | LOW | KEEP (gated) |
| 7 | `exec_limits` | константы + schema tools | HIGH | REVIEW |
| 8 | `exec_timeout_cap` | `ExecTool._MAX_TIMEOUT` + schema | MEDIUM | KEEP |
| 9 | `tool_limits` | `_MAX_CHARS`, `_DEFAULT_*`, `_MAX_FILE_BYTES` | HIGH | REVIEW |
| 10 | `assemble_outbound` | `agent._assemble_outbound` | HIGH | KEEP |
| 11 | `subagent_logging` | `_SubagentHook` (подмена класса) + публикация `SubagentTurnCompleted` через `bus.publish` | HIGH | KEEP |
| 12 | `project_tools` | `ToolContext(...)` + setattr DI | HIGH | KEEP |
| 13 | `compact_tracking` | — | — | DEPRECATED |
| 14 | `compact_command` | — | — | DEPRECATED |
| 15 | `idle_guard` | — | — | DEPRECATED |
| 16 | `document_text_threshold` | `reference_non_image_attachments` | MEDIUM | KEEP |

---

## Deprecation Notes (Что заменили в 0.3.5)

### 13. `compact_tracking`

Удалён. В nanobot 0.3.5:
* `Consolidator.maybe_consolidate_by_tokens` отсутствует — token-budget
  компакция идёт через `ContextCompactionEvent`
  (`nanobot.events.ContextCompactionEvent`).
* `_write_history_notice`/`_record_event_log` теперь вызываются
  из `CompactionEventSubscriber` (`lib/services/compaction_event_subscriber.py`),
  который дёргается каналом при получении `OutboundMessage.event` типа
  `ContextCompactionEvent`.

Подробности: `openspec/changes/nanobot-035-upgrade/design.md`.

### 14. `compact_command`

Удалён вместе с `lib/commands/compact_command.py`. Upstream
`cmd_compact` (`nanobot/command/builtin.py`) делает то же:
`loop.consolidator.compact_idle_session(ctx.key, runtime=runtime,
events=delivery.events)`. Принудительное сжатие остаётся публичным
через `ContextCompactionService.compact(force=True)`.

### 15. `idle_guard`

Удалён. Upstream `AutoCompact._is_expired`
(`nanobot/agent/autocompact.py`) возвращает `False` при `_ttl <= 0`,
поэтому при `idleCompactAfterMinutes=0` патч был избыточен.

---

## Детальный каталог

### 2. `patch_context_governor(config, settings, workspace_dir)`

```yaml
PATCH: context_governor
target: ContextGovernor.normalize_tool_result (internal staticmethod)
nanobot_version: 0.3.5
purpose: >
  Большие результаты инструментов (> persist_threshold) выгружать в
  workspace/data_store/cache/sessions/<session_key>/, в контекст класть
  короткую ссылку data_store/<path>. Экономия токенов + сохранение данных.
public_alternative: нет.
risk: HIGH.
tests: tests/test_runtime_patcher_e2e.py, tests/test_gateway.py:261-266
```

### 3. `patch_save_turn(settings, workspace_dir, agent)`

```yaml
PATCH: save_turn
target: AgentLoop._save_turn (private)
nanobot_version: 0.3.5
purpose: >
  Архивация больших tool-результатов в data_store/ ДО усечения истории
  оборота _save_turn (нативный nanobot 0.3.5 режет строку до max_tool_result_chars);
  сериализация сообщений с защитой от потери медиа-ссылок.
public_alternative: нет.
risk: HIGH (сигнатура обновилась: kwargs turn_latency_ms, summary_checkpoint,
  input_persisted_early добавлены).
tests: tests/test_runtime_patcher.py::test_save_turn*
```

### 4. `patch_session_content_cleanup()`

```yaml
PATCH: session_content_cleanup
target: Session.add_message (nanobot.session.manager)
nanobot_version: 0.3.5
purpose: >
  Санитизация content/kwargs от NUL-символов на источнике — иначе
  psycopg2 падает "A string literal cannot contain NUL".
public_alternative: перенос в PGSessionManager.save (обсуждается).
risk: MEDIUM.
tests: tests/test_runtime_patcher.py::test_session_content_cleanup*
```

### 5. `patch_async_session_saves(agent)`

```yaml
PATCH: async_save
target: agent.sessions.save (наш PGSessionManager)
nanobot_version: 0.3.5
purpose: >
  Обёртка save() через ThreadPoolExecutor(max_workers=1): синхронный
  psycopg2-вызов не блокирует event loop и не взаимно-блокируется
  с postgres channel.
risk: MEDIUM.
tests: tests/test_runtime_patcher.py::test_async_session_saves*
```

### 6. `patch_session_dir_watch(agent, workspace_dir)`

```yaml
PATCH: session_dir_watch
target: agent.sessions.save
nanobot_version: 0.3.5
purpose: >
  Диагностическое логирование FileNotFoundError вокруг save
  (см. session_dir_watch). Гейт: gateway.runtime_diagnostics.session_dir_watch=true.
risk: LOW.
tests: tests (нет — гейт выключен по умолчанию, проверяется вручную).
```

### 7. `patch_exec_limits(settings)`

```yaml
PATCH: exec_limits
target: >
  exec_session.MAX_OUTPUT_CHARS / DEFAULT_MAX_OUTPUT_CHARS,
  shell.MAX_OUTPUT_CHARS / ExecTool._MAX_OUTPUT (+ JSON-Schema maximum).
  WriteStdinTool удалён в 0.3.5 — getattr-guard.
nanobot_version: 0.3.5
purpose: поднять потолок вывода exec/shell-tools (дефолт ~50K символов);
  значения из gateway.tool_result_limits.* в project.json.
public_alternative: проверять tools.exec секцию config.json на каждом апгрейде.
risk: HIGH.
tests: tests/test_runtime_patcher.py::test_exec_limits*,
  tests/test_runtime_patcher_e2e.py::TestExecToolE2E
```

### 8. `patch_exec_timeout_cap(settings)`

```yaml
PATCH: exec_timeout_cap
target: shell.ExecTool._MAX_TIMEOUT + JSON-Schema параметра timeout
nanobot_version: 0.3.5
purpose: >
  Поднять потолок таймаута exec (хардкод 600с) выше для долгих навыков
  (legal_summarizer 7–10 мин на ГК РФ).
risk: MEDIUM.
tests: tests/test_runtime_patcher.py
```

### 9. `patch_tool_limits(settings)`

```yaml
PATCH: tool_limits
target: >
  ReadFileTool._MAX_CHARS, ListDirTool._DEFAULT_MAX,
  search._DEFAULT_HEAD_LIMIT, _DEFAULT_FILE_HEAD_LIMIT, GrepTool._MAX_FILE_BYTES.
nanobot_version: 0.3.5
purpose: конфигурируемые потолки read_file/list_dir/grep.
risk: HIGH.
tests: tests/test_runtime_patcher.py::test_tool_limits*
```

### 10. `patch_assemble_outbound(agent, tool_audit_hook, recent_files_hook=...)`

```yaml
PATCH: assemble_outbound
target: AgentLoop._assemble_outbound (private)
nanobot_version: 0.3.5
purpose: >
  Главный интеграционный патч. Внедряет в metadata финального outbound:
  - _tool_audit (аудит вызовов из ToolAuditHook.drain);
  - media (auto-attach созданных файлов из RecentFilesHook);
  - _final_turn=True (маркер финализации оборота для postgres-канала);
  плюс синтетический OutboundMessage при возврате None штатным кодом.
  Сигнатура 0.3.5: (msg, final_content, stop_reason, streamed_content,
  *, log_content=True, turn_latency_ms=None) — обёртка следует за ней.
why_not_hook: ни один хук не вызывается ПОСЛЕ сборки outbound.
public_alternative: OutboundMessage.event покрывает часть, но не даёт
  injection в metadata.
risk: HIGH — getattr-защита даёт graceful skip.
tests: tests/test_runtime_patcher.py (TestPatchAssembleOutbound),
  tests/test_recent_files_hook.py, tests/test_gateway.py,
  tests/contract/test_agent_loop_api.py::test_assemble_outbound_signature
```

### 11. `patch_subagent_logging(db_logging_service, session_manager=...)`

```yaml
PATCH: subagent_logging
target: nanobot.agent.subagent._SubagentHook (подмена ЦЕЛОГО КЛАССА в модуле)
nanobot_version: 0.3.5
purpose: >
  БД-логирование запусков подагентов: tool-события → DbLoggingService,
  итог subagent_run_finished, персист истории subagent:<task_id>.
  Штатный _SubagentHook пишет только debug в loguru.
public_alternative: следить за hook-factory для subagents.
risk: HIGH (CRITICAL пересмотрен до HIGH — публичные атрибуты Task/Context
  стабильны в 0.3.5).
tests: tests/test_runtime_patcher.py::test_subagent_logging*
```

### 12. `patch_project_tools(agent, workspace_dir, settings=...)`

```yaml
PATCH: project_tools
target: ToolContext.__init__ (~13 kwargs в 0.3.5) + agent.tools.register
nanobot_version: 0.3.5
purpose: >
  Auto-discover workspace/tools/*.py (pkgutil), создание ToolContext со всеми
  зависимостями AgentLoop, регистрация tool'ов через штатный registry.
  DI: setattr(ctx, "_agent_ref"/"_settings_ref"/"_cache_store_ref"/"_db_logging_service").
  В 0.3.5:
    - file_state_store — сохранён в ToolContext.__init__ kwargs;
    - runtime_events — УДАЛЁН (не в сигнатуре);
    - runtime_control — ДОБАВЛЕН (ToolContext.__init__);
  ToolContext frozen=False → setattr для DI работает.
public_alternative: частично есть (config-based tools).
risk: HIGH (сигнатура ToolContext — до 13 kwargs).
tests: tests/test_runtime_patcher.py, tests/test_tools_project_loader.py,
  tests/contract/test_tools_and_context.py
```

### 16. `patch_document_text_threshold(settings)`

```yaml
PATCH: document_text_threshold
target: nanobot.utils.document.reference_non_image_attachments
nanobot_version: 0.3.5
purpose: >
  Единый механизм встраивания документов в user-промпт (все каналы/навыки).
  В 0.3.5 upstream `extract_documents` удалён — патч оборачивает
  `reference_non_image_attachments`:
    маленький (≤ channels.document_text_threshold):
      [File: <basename> (saved at <path>)]
      <text>
    большой (> порога):
      [File: <basename> (saved at <path>)]
      [text omitted (len=… > threshold=…)]
  Изображения НЕ формируют текстовых блоков (путь идёт в image_paths).
  Нечитаемые файлы — fallback на upstream [Attachment: <path>].
public_alternative: ПРОВЕРИТЬ при апгрейде.
risk: MEDIUM (оборачиваем публичную функцию, fallback на upstream при сбое).
tests: tests/test_runtime_patcher.py::TestPatchDocumentTextThreshold
```

---

## Политика ведения

1. **Новый patch без записи здесь** = архитектурное нарушение (TARGET §20).
2. **При апгрейде nanobot:** пройти таблицу сверху вниз, для каждого патча сверить
   целевой API по changelog upstream; контракт каждого целевого API фиксируется
   в `tests/contract/`.
3. **Кандидаты на удаление:** №7, №8, №9 (если upstream даст конфигурацию лимитов).
   Отслеживать в [Unreleased] CHANGELOG.
4. **DEPRECATED (#1, #13, #14, #15)** — сохраняются как no-op + `applied=True`
   для совместимости со `PatchReport`. Реальный функционал перенесён:
   - #1 → `bus.subscribe(TurnRuntimeAdmitted)` в `ApplicationContext.start()`
   - #13 → `CompactionEventSubscriber.feed` через `OutboundMessage.event`
   - #14 → upstream `nanobot.command.builtin.cmd_compact`
   - #15 → upstream `AutoCompact._is_expired`
5. **Запрещено** добавлять патчи вне этого класса (единственные исторические
   исключения: `BusFactory._wrap` для MessageBus и Jinja2-loader в
   `consolidator_locale` — оба задокументированы в nanobot-inventory.md §3.2).
