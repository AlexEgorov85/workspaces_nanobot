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

| # | Патч | Target (nanobot API) | Risk | Required | Категория | Условие удаления |
|---|---|---|---|---|---|---|
| 1 | `context_governor` | `ContextGovernor.normalize_tool_result` | MEDIUM | ✓ | ISOLATE+TESTS | хук выгружает результат в файл; остаётся только подстановка ссылки для встроенных tool'ов. Исчезает, когда тяжёлые запросы уйдут в MCP |
| 2 | `save_turn` | `agent._save_turn` | HIGH | ✓ | KEEP | **хуки:** `after_execute_tool` вызывается раньше, чем upstream усечёт результат |
| 3 | `exec_limits` | константы + schema tools | MEDIUM | — | REVIEW | upstream даст конфигурацию лимитов |
| 4 | `exec_timeout_cap` | `ExecTool._MAX_TIMEOUT` + schema | MEDIUM | — | KEEP | legal уезжает в MCP — LLM-вызов уходит из процесса агента |
| 5 | `tool_limits` | `_MAX_CHARS`, `_DEFAULT_*`, `_MAX_FILE_BYTES` | MEDIUM | — | REVIEW | upstream даст конфигурацию лимитов |
| 6 | `assemble_outbound` | `agent._assemble_outbound` | HIGH | ✓ | KEEP | **события:** `_final_turn` → `TurnEndEvent`, `_tool_audit` и `media` → публикация через `turn_context.events` |
| 7 | `async_save` | `agent.sessions.save` | MEDIUM | — | KEEP | **собственный класс:** `agent.sessions` — это `PGSessionManager`, патч не нужен |
| 8 | `session_dir_watch` | `sessions.save` (диагностика) | LOW | — | KEEP (gated) | **удаляется** — гейт выключен по умолчанию, тестов нет |
| 9 | `subagent_logging` | `_SubagentHook` (подмена класса) | HIGH | ✓ | KEEP | upstream даст фабрику хуков для subagent'ов (сейчас `_SubagentHook` конструируется жёстко) |
| 10 | `turn_delivery_fail` | `TurnDelivery.fail` | MEDIUM | — | KEEP | **хуки:** `finalize_content` заменяет текст, `on_error` + `TurnCompleted` дают логирование |
| 11 | `session_content_cleanup` | `Session.add_message` | LOW | — | KEEP | **собственный класс:** `PGSessionManager.save` — `clean_text.py` уже делает это для PostgreSQL |
| 12 | `document_text_threshold` | `reference_non_image_attachments` | MEDIUM | — | KEEP | документы уезжают в `libs/document` |

`Required = ✓` (4 патча: `assemble_outbound`, `save_turn`,
`subagent_logging`, `context_governor`) — критичность для
diagnostics в startup-баннере. **НЕ** означает startup-abort
(см. `openspec/specs/runtime/context/spec.md`).

---

## Поверхность расширения `nanobot-ai==0.3.5`

Проверено чтением установленного пакета. Это основание для колонки
«Условие удаления».

| Механизм | Что даёт | Значение для патчей |
|---|---|---|
| `AgentHook` | 18 методов, в т.ч. `after_execute_tool`, `on_error`, `on_finally`, `before_iteration` | наблюдение и побочные эффекты |
| `AgentTurnHookContext.events: EventSink` | `publish: Callable[[AgentEvent], Awaitable[None]]` + `accepts(type)` для пропуска дорогого производства без потребителя | **публикация событий из хука — штатный механизм** |
| `AgentTurnHookContext.metadata` / `.attributes` | мутабельные `dict`, передаваемые каждой `AgentTurnHookFactory` | второй канал для per-turn данных, чище ключей в `OutboundMessage.metadata` |
| `AgentTurnHookFactory` | `Callable[[AgentTurnHookContext], AgentHook \| None]`; цепочка собирается в `agent/turn_hooks.py` | официальная фабрика per-turn хуков |
| `finalize_content(ctx, content) -> str \| None` | вызывается в `agent/runner.py` в трёх местах | **единственная хук-точка, подменяющая значение** |
| 23 события | `TurnCompleted` (несёт `outcome`, `failure_kind`, `failure_error_kind`, `failure_attempts`), `TurnEndEvent`, `SessionTurnPersisted`, `SessionTurnStarted`, `ContextCompactionEvent`, `RecoveryStateEvent`, `RetryStatusEvent`, `RetryWaitEvent` | замена инъекции в `metadata` подпиской |

Собственный `AgentProgressHook` в наборе `nanobot` публикует события
прогресса через тот же `EventSink` — публикация из хука является задуманным
паттерном, а не обходным путём.

### Что хук может, а что — нет

Хук **может**: наблюдать всё (вызовы, параметры, результаты, ошибки, поток,
итерации), писать в БД, публиковать события в шину хода, менять
`turn_context.metadata` и `.attributes`, и — если метод возвращает значение —
подменять это значение.

Хук **не может только одно**: заменить значение, которое фреймворк передаёт
дальше, не используя возвращаемое значение хука. В 0.3.5 такой случай ровно
один. Проверено в `nanobot/agent/tools/execution.py`:

```python
result = await tool.execute(**params)          # или tools.execute(...)
...
await hook.after_execute_tool(context, tool_call, tool, params, result)
...
return result, {...}                           # возвращается ИСХОДНЫЙ result
```

`after_execute_tool` возвращает `None`, и раннер отдаёт `result` без изменений.
Поэтому хук годится для выгрузки большого результата в файл (побочный
эффект), но **не годится для подстановки короткой ссылки в контекст**.

Отсюда правило — и оно уже, чем звучало раньше:

> Проверять надо не наличие хука, а **может ли он повлиять именно на то, что
> нужно изменить**: побочным эффектом, публикацией события, мутацией
> `metadata` или возвращаемым значением. Возвращаемое значение требуется
> только для подстановки значения.

### Пять правил вместо патча

1. **Патчить наше, а не фреймворк.** Если поведение правится в
   `PGSessionManager`, патч не нужен — правь наш класс (`async_save`,
   `session_content_cleanup`).
2. **Событие вместо инъекции в `OutboundMessage.metadata`.** Хук публикует
   событие через `turn_context.events`, потребитель подписывается. Сработало
   для `compact_tracking`; та же схема применима к `_tool_audit`, `media` и
   `_final_turn`.
3. **Хук вместо патча — почти всегда.** Прямых запретов на хуки нет; единственное
   исключение разобрано выше и касается только подстановки значения.
4. **Проверять наличие конфига до патча константы.** Для exec в 0.3.5 конфига нет.
5. **Контрактные тесты на целевой API** — в `tests/contract/`.

### Ожидаемый результат

12 патчей → **4–5**. Шесть заменяются хуками, событиями или собственным
классом (`1` частично, `2`, `6`, `7`, `10`, `11`), один удаляется (`8`), один
уходит по миграции доменов (`12`).

Остаются: `1` — только подстановка ссылки для встроенных tool'ов, и при
переносе тяжёлых запросов в MCP он тоже исчезает; `9` — у `SubagentManager`
нет фабрики хуков, точка вставки структурно отсутствует; `3` и `5` — нет
конфигурации upstream; `4` — пересматривается вместе с уходом legal в MCP.

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
