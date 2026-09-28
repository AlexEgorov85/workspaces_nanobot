# RuntimePatcher Inventory

> Каталог всех monkey-patch'ей к `nanobot-ai==0.3.5`.
> Определение: [`lib/services/runtime_patcher.py`](../../lib/services/runtime_patcher.py).
> См. также [nanobot-inventory.md](nanobot-inventory.md).
> Opencode change: [`runtime-patcher-composition-cleanup`](../changes/runtime-patcher-composition-cleanup/).

**Принцип (TARGET_ARCHITECTURE §20):** каждый patch обязан иметь purpose,
target, nanobot version, проверенную public alternative, upgrade risk и тест.
Если upstream даёт официальный extension point — patch заменяется на него.

**Единственная точка применения:** `RuntimePatcher.apply_all(...)` из
`ApplicationContext.create()` (`lib/core/application_context.py:339-346`).
Каждый патч в try/except → при изменении API nanobot патч уходит в
`PatchReport.skipped/failed`, процесс не падает.

**Регистрация project tools из `workspace/tools/*.py`** НЕ является
runtime patch'ом — это отдельный loader
[`lib/services/project_tool_loader.py`](../../lib/services/project_tool_loader.py),
вызываемый из `ApplicationContext.create()` сразу после `apply_all()`
(см. `application_context.py:362-374`). `RuntimePatcher` НЕ импортирует
`workspace.tools.*` и НЕ конструирует `ToolContext` (защитный тест:
[`tests/test_runtime_patcher_no_project_tools_boundary.py`](../../tests/test_runtime_patcher_no_project_tools_boundary.py)).

---

## Классификация

| Категория | Значение |
|---|---|
| **KEEP** | upstream не даёт точки расширения — патч необходим |
| **ISOLATE+TESTS** | патч нужен, но требует contract-тестов на целевой API |
| **REVIEW** | возможно есть публичная альтернатива — проверить при апгрейде |
| **REMOVED** | удалён в opencode change `runtime-patcher-composition-cleanup` или ранее |

---

## Сводная таблица (nanobot-ai 0.3.5)

Финальный inventory после `runtime-patcher-composition-cleanup`:
**ровно 12 patches** в `apply_all()` / `_PATCH_SPECS` /
`canonical_runtime_patches()` (попарно равны — exact-match тест
[`tests/test_runtime_patcher.py::TestPatchSpecs::test_inventory_is_exact`](../../tests/test_runtime_patcher.py)).

| # | Патч | Target (nanobot API) | Risk | Required | Категория |
|---|---|---|---|---|---|
| 1 | `context_governor` | `ContextGovernor.normalize_tool_result` | MEDIUM | ✓ | ISOLATE+TESTS |
| 2 | `save_turn` | `agent._save_turn` | HIGH | ✓ | KEEP |
| 3 | `exec_limits` | константы + schema tools | MEDIUM | — | REVIEW |
| 4 | `exec_timeout_cap` | `ExecTool._MAX_TIMEOUT` + schema | MEDIUM | — | KEEP |
| 5 | `tool_limits` | `_MAX_CHARS`, `_DEFAULT_*`, `_MAX_FILE_BYTES` | MEDIUM | — | REVIEW |
| 6 | `assemble_outbound` | `agent._assemble_outbound` | HIGH | ✓ | KEEP |
| 7 | `async_save` | `agent.sessions.save` | MEDIUM | — | KEEP |
| 8 | `session_dir_watch` | `sessions.save` (diagnostic) | LOW | — | KEEP (gated) |
| 9 | `subagent_logging` | `_SubagentHook` (подмена класса) + публикация `SubagentTurnCompleted` через `bus.publish` | HIGH | ✓ | KEEP |
| 10 | `turn_delivery_fail` | `TurnDelivery.fail` (private, класса) | MEDIUM | — | KEEP |
| 11 | `session_content_cleanup` | `Session.add_message` | LOW | — | KEEP |
| 12 | `document_text_threshold` | `reference_non_image_attachments` | MEDIUM | — | KEEP |

`Required = ✓` (4 патча: `assemble_outbound`, `save_turn`,
`subagent_logging`, `context_governor`) — критичность для
diagnostics в startup-баннере. **НЕ** означает startup-abort
(см. `openspec/specs/runtime/context/spec.md`).

---

## Удалённые патчи (REMOVED)

### `project_tools`

Удалён в opencode change `runtime-patcher-composition-cleanup` (Phase 4).
Тело перенесено в `lib/services/project_tool_loader.py::register_project_tools`.
Регистрация больше **не** вызывается из `RuntimePatcher.apply_all()`;
`PatchReport.details["project_tools"]` удалён; результат хранится в
`ctx.project_tools_result: ProjectToolsLoadResult`. Баннер
`_emit_project_tools_inventory_banner` (в `application_context.py`)
читает `project_tools_result.detail`, а не `patch_report.details["project_tools"]`.
Семантика (best-effort, частичный успех, `[INTERNAL_FAILED]` маркер
на детали) сохранена; `parse_project_tools_detail()` в `runtime_inventory.py`
не переписывалась.

### `compact_tracking`, `compact_command`, `idle_guard`

Удалены из `_PATCH_SPECS` (DEPRECATED-остатки от `nanobot-035-upgrade`).
Фактически не вызывались — drift между `_PATCH_SPECS` и `apply_all()`.
Реальный функционал перенесён:

- `compact_tracking` → `lib/services/compaction_event_subscriber.py`
  через `OutboundMessage.event: ContextCompactionEvent`;
- `compact_command` → upstream `nanobot.command.builtin.cmd_compact`;
- `idle_guard` → upstream `AutoCompact._is_expired` при `_ttl <= 0`.

### `context_bridge_seed`

Удалён ранее (см. change `post-0.3.5-patches-cleanup`). Seed лимита
контекста делается подпиской на `TurnRuntimeAdmitted` через
`RuntimeEventsSubscriber.start()`.

---

## Детальный каталог

### 1. `patch_context_governor(config, settings, workspace_dir)`

```yaml
PATCH: context_governor
target: ContextGovernor.normalize_tool_result (internal staticmethod)
nanobot_version: 0.3.5
required: true
purpose: >
  Большие результаты инструментов (> persist_threshold) выгружать в
  workspace/data_store/cache/sessions/<session_key>/, в контекст класть
  короткую ссылку data_store/<path>. Экономия токенов + сохранение данных.
public_alternative: нет.
risk: MEDIUM (статический метод, не приватный instance-метод —
  ломается только при rename сигнатуры).
tests: tests/test_runtime_patcher_e2e.py, tests/test_gateway.py
```

### 2. `patch_save_turn(settings, workspace_dir, agent)`

```yaml
PATCH: save_turn
target: AgentLoop._save_turn (private)
nanobot_version: 0.3.5
required: true
purpose: >
  Архивация больших tool-результатов в data_store/ ДО усечения истории
  оборота _save_turn (нативный nanobot 0.3.5 режет строку до max_tool_result_chars);
  сериализация сообщений с защитой от потери медиа-ссылок.
public_alternative: нет.
risk: HIGH (сигнатура обновилась: kwargs turn_latency_ms, summary_checkpoint,
  input_persisted_early добавлены).
tests: tests/test_runtime_patcher.py::test_save_turn*
```

### 3. `patch_exec_limits(settings)`

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
risk: MEDIUM (модульные константы + JSON-Schema — оба публичных
  слоя, ломаются только при изменении схемы).
tests: tests/test_runtime_patcher.py::test_exec_limits*,
  tests/test_runtime_patcher_e2e.py::TestExecToolE2E
```

### 4. `patch_exec_timeout_cap(settings)`

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

### 5. `patch_tool_limits(settings)`

```yaml
PATCH: tool_limits
target: >
  ReadFileTool._MAX_CHARS, ListDirTool._DEFAULT_MAX,
  search._DEFAULT_HEAD_LIMIT, _DEFAULT_FILE_HEAD_LIMIT, GrepTool._MAX_FILE_BYTES.
nanobot_version: 0.3.5
purpose: конфигурируемые потолки read_file/list_dir/grep.
risk: MEDIUM (модульные константы — не приватные instance-методы).
tests: tests/test_runtime_patcher.py::test_tool_limits*
```

### 6. `patch_assemble_outbound(agent, tool_audit_hook, recent_files_hook=...)`

```yaml
PATCH: assemble_outbound
target: AgentLoop._assemble_outbound (private)
nanobot_version: 0.3.5
required: true
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

### 7. `patch_async_session_saves(agent)`

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

### 8. `patch_session_dir_watch(agent, workspace_dir)`

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

### 9. `patch_subagent_logging(db_logging_service, session_manager=...)`

```yaml
PATCH: subagent_logging
target: nanobot.agent.subagent._SubagentHook (подмена ЦЕЛОГО КЛАССА в модуле)
nanobot_version: 0.3.5
required: true
purpose: >
  БД-логирование запусков подагентов: tool-события → DbLoggingService,
  итог subagent_run_finished, персист истории subagent:<task_id>.
  Штатный _SubagentHook пишет только debug в loguru.
public_alternative: следить за hook-factory для subagents.
risk: HIGH (CRITICAL пересмотрен до HIGH — публичные атрибуты Task/Context
  стабильны в 0.3.5).
tests: tests/test_runtime_patcher.py::test_subagent_logging*
```

### 10. `patch_turn_delivery_fail(settings, db_logging_service=None, agent_id=None)`

```yaml
PATCH: turn_delivery_fail
target: nanobot.agent.turn_delivery.TurnDelivery.fail (private, уровень класса)
nanobot_version: 0.3.5
purpose: >
  Конфигурируемый fallback-ответ при любом Exception в
  AgentLoop._process_message (upstream). Upstream-TurnDelivery.fail
  публикует захардкоженный литерал "Sorry, I encountered an error." —
  патч подменяет метод класса обёрткой, которая:
    1) читает gateway.error_messages.internal_error (default русский текст);
    2) захватывает активное исключение через sys.exception() (вызов идёт
       изнутри except-блока в loop.py:1480-1482);
    3) публикует OutboundMessage(content=internal_error, metadata={"_error_kind": "internal", "_final_turn": True});
    4) при log_to_db=true пишет event_type="turn_failed" в agent_gateway_logs
       через DbLoggingService.try_log_event (fail-open при svc=None);
       payload содержит exception_type/exception_message/exception_available/
       sender_id/agent_id/session_key/channel/chat_id/failure_error_kind;
    5) вызывает оригинальный fail() для финализации turn_completed event,
       предварительно подменив self.bus на per-instance прокси
       _OutboundSilencer, который НЕ публикует outbound (но пропускает
       остальные методы bus через __getattr__). Это подавляет двойной
       outbound: пользователь получает ровно один fallback-ответ.
  asyncio.CancelledError-ветка (abort_stream + restore_runtime_checkpoint)
  НЕ задета — она идёт мимо TurnDelivery.fail.
public_alternative: нет в upstream 0.3.5; отслеживать появление extension
  point в nanobot CHANGELOG.
risk: MEDIUM (патч уровня класса затрагивает все инстансы TurnDelivery).
tests: tests/test_runtime_patcher.py::TestPatchTurnDeliveryFail
spec: openspec/specs/runtime/error-fallback/spec.md
```

### 11. `patch_session_content_cleanup()`

```yaml
PATCH: session_content_cleanup
target: Session.add_message (nanobot.session.manager)
nanobot_version: 0.3.5
purpose: >
  Санитизация content/kwargs от NUL-символов на источнике — иначе
  psycopg2 падает "A string literal cannot contain NUL".
public_alternative: перенос в PGSessionManager.save (обсуждается).
risk: LOW (только защитная логика, не меняет upstream-контракт).
tests: tests/test_runtime_patcher.py::test_session_content_cleanup*
```

### 12. `patch_document_text_threshold(settings)`

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
3. **Кандидаты на удаление:** #3, #4, #5 (если upstream даст конфигурацию лимитов).
   Отслеживать в [Unreleased] CHANGELOG.
4. **Запрещено** добавлять патчи вне этого класса (единственные исторические
   исключения: `BusFactory._wrap` для MessageBus и Jinja2-loader в
   `consolidator_locale` — оба задокументированы в nanobot-inventory.md §3.2).
5. **Запрещено** добавлять в `RuntimePatcher` регистрацию project tools или
   импорты `workspace.tools.*` — это нарушение архитектурной границы (см.
   спеку `runtime-patcher` и тест `tests/test_runtime_patcher_no_project_tools_boundary.py`).
6. **PATCH удаляется атомарно** (без deprecation period и no-op stubs): из
   `_PATCH_SPECS`, из `apply_all()`, из этого документа, из
   `canonical_runtime_patches()`, из `runtime_inventory.diff_runtime_patches`
   и из всех тестовых fixtures — все в одной change.
