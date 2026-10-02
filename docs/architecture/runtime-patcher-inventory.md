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

Состояние после change `enterprise-mcp-platform`, фаза 6: **ровно 6 patches**
в `apply_all()` / `_PATCH_SPECS` / `canonical_runtime_patches()` (попарно
равны — exact-match тест
[`tests/test_runtime_patcher.py::TestPatchSpecs::test_inventory_is_exact`](../../tests/test_runtime_patcher.py)).
Количество намеренно не выписано числом в тесте: проверяются правила, а не
выписка.

| # | Патч | Target (nanobot API) | Risk | Required | Категория | Условие удаления |
|---|---|---|---|---|---|---|
| 1 | `context_governor` | `ContextGovernor.normalize_tool_result` | MEDIUM | ✓ | **REMOVE** | **Уже не нужен.** `workspace` и `max_tool_result_chars` уже прокинуты: `AgentLoop` → `AgentRunSpec` → `ContextGovernanceConfig` → `maybe_persist_tool_result`. Патч переписывает работающую функцию |
| 2 | `exec_limits` | глобалы `exec_session.MAX_OUTPUT_CHARS` + import-frozen схема | MEDIUM | — | KEEP | upstream даст конфигурацию лимитов вывода. Подкласс не достаёт: потолок в глобале модуля, схема заморожена `deepcopy` в `base.py:336` |
| 3 | `exec_timeout_cap` | `ExecTool._MAX_TIMEOUT` + схема параметра | MEDIUM | — | **KEEP (спорно)** | Снимать нельзя, пока у навыков нет своего таймаута: патч поднимает потолок `600` до `gateway.exec_timeout_cap_sec` (по умолчанию 3600). Обоснование в спецификации — «legal 7–10 мин» — отпадает только вместе с переездом `legal_summarizer` в платформу (фаза 11). До тех пор правдивая категория — **KEEP**, а не REMOVE: `tools.exec.timeout=0` снимает лимит только когда агент **не** передаёт явный `timeout`, а `TOOLS.md` учит его передавать |
| 4 | `tool_limits` | `_MAX_CHARS`, `_DEFAULT_*`, `_MAX_FILE_BYTES` | MEDIUM | — | **PARTIAL** | 3 из 5 целей читаются как `self.<attr>` → подкласс `Tool` под тем же именем. `search._DEFAULT_HEAD_LIMIT` и `_DEFAULT_FILE_HEAD_LIMIT` — голые глобалы, подкласс не перехватывает; они лишь значения по умолчанию (per-call `head_limit` есть) |
| 5 | `assemble_outbound` | `agent._assemble_outbound` | HIGH | ✓ | **PARTIAL** | `TurnEndEvent` в 0.3.5 **не существует** (проверено инспекцией пакета, см. ADR `turn-delivery-public-extension.md`), поэтому пункт «`_final_turn` → `TurnEndEvent`» плана нереализуем в этой формулировке. Остаётся перенос на существующие `EventSink` / `RuntimeEventPublisher` либо решение оставить патч — это отдельное решение владельца, а не молчаливое |
| 6 | `subagent_logging` | `_SubagentHook` (подмена класса) | HIGH | ✓ | KEEP | upstream даст параметр хука у `SubagentManager` и передаст `events` в `AgentRunSpec` субагента. Сейчас нет ни того, ни другого: `events` → `NO_EVENTS`, событий ноль. Проверить, запускаются ли субагенты в деплое — если нет, патч удаляется |
| 7 | `repeat_guard_block` | `nanobot.agent.tools.execution._execute_tool_call` | MEDIUM | — | KEEP | upstream даст способ **отклонить** tool-вызов из хука. Hook-API возвращаемого значения не имеет, а `before_execute_tool` в `_execute_tool_call` вызывается вне `try`, поэтому без патча режим `block` непригоден: с `reraise=False` он молчаливый no-op, с `reraise=True` — обрыв оборота и отмена соседних вызовов батча через `asyncio.gather`. Пока отказа нет — патч нужен |

Колонка и каталог внесены в рамках openspec change
[`enterprise-mcp-platform`](../../openspec/changes/enterprise-mcp-platform/).
**Доказательная база по каждому пункту — в
[`nanobot-reuse-catalog.md`](nanobot-reuse-catalog.md).** Перед добавлением
нового патча каталог просматривается обязательно: он отвечает на вопрос
«а не поставляет ли это Nanobot уже?».

**Каждый патч обязан иметь заполненное условие удаления.** Патч без него —
архитектурное нарушение наравне с патчем без записи в этом документе.


`Required = ✓` (3 патча: `assemble_outbound`, `subagent_logging`,
`context_governor`) — критичность для
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
| 23 события | `TurnCompleted` (несёт `outcome`, `failure_kind`, `failure_error_kind`, `failure_attempts`), `SessionTurnPersisted`, `SessionTurnStarted`, `ContextCompactionEvent`, `RecoveryStateEvent`, `RetryStatusEvent`, `RetryWaitEvent` | замена инъекции в `metadata` подпиской |
| ~~`TurnEndEvent`~~ | **не существует в 0.3.5** | проверено инспекцией установленного пакета: в `nanobot.agent` есть `AgentEvent`, `StreamDeltaEvent`, `StreamEndEvent`, `StreamedResponseEvent`, `TurnContext`, `EventSink`, `TurnRoute`, `RetryStatusEvent`. Модуля `nanobot.agent.events` нет. Пункты плана, ссылающиеся на `TurnEndEvent`, нереализуемы как написаны (ADR `turn-delivery-public-extension.md`) |

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

7 патчей → **4–5**. `turn_delivery_fail` уже переехал на публичный параметр
`AgentLoop(turn_delivery_factory=...)`; из трёх оставшихся кандидатов на снятие
`context_governor` (уже не нужен по сути), `exec_timeout_cap` (только после
переезда `legal_summarizer` в платформу) и `assemble_outbound` (нужно решение
о переносе на `EventSink` / `RuntimeEventPublisher` — `TurnEndEvent` в 0.3.5
нет). `exec_limits`, `tool_limits`, `subagent_logging` и `repeat_guard_block`
остаются: точки расширения у них структурно нет.

Остаются: `context_governor` — только подстановка ссылки для встроенных
tool'ов, и при переносе тяжёлых запросов в MCP он тоже исчезает;
`subagent_logging` — у `SubagentManager` нет фабрики хуков, точка вставки
структурно отсутствует; `exec_limits` и `tool_limits` — конфигурации upstream
нет; `exec_timeout_cap` — пересматривается вместе с уходом legal в фазу 11;
`repeat_guard_block` — снимается первым среди них, если upstream даст способ
отклонить tool-вызов из хука.

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

### `turn_delivery_fail`

Удалён в change `enterprise-mcp-platform`, фаза 6 (пункт 6.1). Нативная замена —
`lib/services/turn_delivery_factory.py`:
`FallbackTurnDeliveryFactory` внедряется публичным параметром
`AgentLoop(turn_delivery_factory=...)` из `AgentFactory.create()`; переопределение
`fail()` публикует настроенный fallback **сам** и не зовёт `super().fail()`.

Что это дало сверх патча:

- двойная публикация невозможна конструктивно, поэтому
  `_OutboundSilencer` — прокси, подменявший `self.bus` на время вызова
  upstream-метода, — удалён вместе с патчем;
- поломка при апгрейде nanobot громкая и ранняя: `AgentLoop.__init__`
  валидирует переданную фабрику (`factory.bus is bus`);
- маршрутизация не переписана: `create()`/`unrouted()` вызываются через
  `super()`, подменяется только класс собранного экземпляра.

Осознанный размен, зафиксированный в ADR
`docs/architecture/decisions/turn-delivery-public-extension.md`: переопределение
дублирует ~8 строк upstream-логики `fail()` (публикация + `turn_completed`).

Контракт закреплён `tests/test_turn_delivery_factory.py` (17 тестов); запрет
возврата патча — `REMOVED_PATCHES` в `tests/test_runtime_patcher.py`.

Побочный эффект переноса: CLI-путь (`cli_agent.py` → `AgentFactory`) тоже
получил настраиваемый fallback. Раньше патч применялся только в
`ApplicationContext`, и в CLI пользователь видел upstream-литерал
«Sorry, I encountered an error.».

### `save_turn`, `async_save`, `session_content_cleanup`, `session_dir_watch`, `document_text_threshold`

Удалены в change `enterprise-mcp-platform`, фаза 6 (пункты 6.2–6.5, 6.7) — патчи,
которые заменены штатными точками расширения, а не переписаны:

| Патч | Куда ушёл | Пункт плана |
|---|---|---|
| `save_turn` | Хук `after_execute_tool` — архивирование результата происходит раньше, чем upstream усечёт его в `_save_turn` | 6.2 |
| `async_save` | `lib/services/session_storage.py::install_async_save` — обёртка ставится при создании менеджера сессий | 6.3 |
| `session_content_cleanup` | `PGSessionManager.save` через `workspace/utils/clean_text.py`: чистка NUL — забота PostgreSQL, а не фреймворка | 6.4 |
| `session_dir_watch` | Удалён целиком: гейт выключен по умолчанию, тестов не было | 6.5 |
| `document_text_threshold` | Нативный document-tool агента — это наш код, патчить фреймворк не нужно | 6.7 |

### `context_bridge_seed`

Удалён ранее (см. change `post-0.3.5-patches-cleanup`). Seed лимита
контекста делается подпиской на `TurnRuntimeAdmitted` через
`RuntimeEventsSubscriber.start()`.

---

## Детальный каталог

Каталог содержит **только живые патчи** — те, что есть в `apply_all()`,
`_PATCH_SPECS` и `canonical_runtime_patches()`. Удалённые патчи перечислены в
«Удалённые патчи (REMOVED)» выше; исключение — последняя запись, оставленная
указателем на то, что патч делал.

**Условие удаления здесь не дублируется:** оно живёт в сводной таблице выше, и
второе место для него — это расхождение, которое читатель примет за факт.

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

### 2. `patch_exec_limits(settings)`

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

### 3. `patch_exec_timeout_cap(settings)`

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

### 4. `patch_tool_limits(settings)`

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

### 5. `patch_assemble_outbound(agent, tool_audit_hook, recent_files_hook=...)`

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

### 6. `patch_subagent_logging(db_logging_service, session_manager=...)`

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

### 7. `patch_repeat_guard_block()`

```yaml
PATCH: repeat_guard_block
target: nanobot.agent.tools.execution._execute_tool_call (private)
nanobot_version: 0.3.5
required: false
purpose: >
  Превращает RepeatGuardBlocked из RepeatGuardHook в синтетический
  tool-результат вида ("Error: RepeatGuardBlocked: ...", {name, status: "error",
  detail}) и вызывает hook.on_execute_tool_error с исходным контекстом
  вызова. Без него режим gateway.repeat_guard.mode="block" непригоден.
why_not_hook: >
  Hook-API не имеет возвращаемого значения, которое читает раннер.
  _for_each_hook_safe глотает исключение хука при reraise=False
  (молчаливый no-op), а при reraise=True исключение уходит из
  execute_tool_calls наверх, потому что before_execute_tool вызывается
  ВНЕ try-блока; в concurrent-режиме asyncio.gather без
  return_exceptions=True отменяет весь батч соседних вызовов.
public_alternative: >
  ctx.tool_calls не годится: runner.py присваивает
  context.tool_calls = list(response.tool_calls), то есть копию.
risk: >
  MEDIUM — патч ловит ТОЛЬКО собственный тип RepeatGuardBlocked, поэтому
  чужие ошибки хуков не маскируются; идемпотентен (_repeat_guard_patched);
  hook/context извлекаются биндингом по сигнатуре оригинала, потому что
  upstream вызывает _execute_tool_call позиционно и kwargs["hook"] всегда
  был бы None. При несовместимом API возвращает (False, причина).
tests: tests/contract/test_repeat_guard_hook_contract.py,
  tests/test_runtime_patcher.py::test_repeat_guard_block*
```

### 7. ~~`patch_turn_delivery_fail`~~ — УДАЛЁН (фаза 6, п. 6.1)

> Запись сохранена как указатель на то, что патч делал, — по контракту и
> содержимому журнала. Реализации в `runtime_patcher.py` больше нет; см.
> «Удалённые патчи (REMOVED)» выше и ADR
> `docs/architecture/decisions/turn-delivery-public-extension.md`.

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

---

## Политика ведения

1. **Новый patch без записи здесь** = архитектурное нарушение (TARGET §20).
2. **При апгрейде nanobot:** пройти таблицу сверху вниз, для каждого патча сверить
   целевой API по changelog upstream; контракт каждого целевого API фиксируется
   в `tests/contract/`.
3. **Кандидаты на удаление:** `exec_timeout_cap`, `tool_limits`, `assemble_outbound`
   (если upstream даст конфигурацию лимитов). Перечислены именами, а не номерами:
   нумерация каталога меняется при каждом удалении, и ссылка на номер переживает
   изменение молча. Отслеживать в [Unreleased] CHANGELOG.
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
