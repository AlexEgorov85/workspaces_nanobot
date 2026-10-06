# runtime/agent-hooks Specification

## Purpose
Описывает нормативные требования к `AgentHook`-совместимости хуков проекта
после апгрейда upstream `nanobot-ai 0.3.5`. Цель — зафиксировать
контракт наследования, корректную работу с `LLMUsage` dataclass,
новый API shutdown (`aclose`) и расширенную сигнатуру `patch_save_turn`.
Allowlist-проверка плагинов в `lib/cli/hook_loader.py` фиксируется
отдельным требованием в `runtime/context` (MODIFIED).

## Scope

`agent` — хуки подключаются фабрикой агента
Реализация: `lib/core/agent_factory.py`, `lib/hooks/`

## Requirements

### Requirement: Хуки обязаны наследовать `nanobot.agent.hook.AgentHook`

Все классы хуков в `lib/hooks/*.py` ДОЛЖНЫ прямо или через промежуточный базовый класс наследовать `nanobot.agent.hook.AgentHook`. Только так `CompositeHook._for_each_hook_safe` корректно диспатчит lifecycle-методы (`emit_reasoning`, `emit_reasoning_end`, `on_stream`, `on_stream_end`, `finalize_content`, `on_error`, `on_finally`, `before_run`, `after_run`, `before_iteration`, `after_iteration`, `before_execute_tool`, `after_execute_tool`, `on_execute_tool_error`, `before_execute_tools`, `on_provider_tool_event`, `on_finally`, `on_error`) без `AttributeError`.

#### Сценарий: Хук с lifecycle-методом, унаследованным от AgentHook

- **КОГДА** `AgentLoop` диспатчит `emit_reasoning` через `CompositeHook._for_each_hook_safe`
- **ТОГДА** `AgentHook`-наследованный хук ДОЛЖЕН либо реализовать метод, либо использовать no-op базового класса, без выбрасывания `AttributeError`

#### Сценарий: Не-наследованный хук ломает стрим

- **КОГДА** хук НЕ наследует `AgentHook` и `CompositeHook._for_each_hook_safe` дёргает `getattr(h, "emit_reasoning", ...)`
- **ТОГДА** возникает `AttributeError` и шум ERROR-логов; рассылка reasoning-чанков прерывается или замедляется

### Requirement: `usage` адаптер для `LLMUsage` dataclass

`DatabaseLoggingHook` и любой другой хук, использующий `context.usage`/`response.usage`, MUST конвертировать `LLMUsage` (frozen dataclass из `nanobot.llm_usage.models`, поля `input_tokens`/`output_tokens`/`total_tokens`/`cache_read_tokens`/...) в dict через публичный метод `to_turn_dict()` или `to_dict()`. Прямое `dict(usage)` или `usage.get("prompt_tokens")` запрещены — они падают с `TypeError` на dataclass.

#### Сценарий: Получение prompt/completion token count

- **КОГДА** хук пишет в `DbLoggingService.log_event` или `log_llm_call` и нужно поле `prompt_tokens`
- **ТОГДА** хук ДОЛЖЕН использовать `usage.to_turn_dict()["prompt_tokens"]` или эквивалент через адаптер `_usage_to_dict`
- **И НЕ ДОЛЖЕН** использовать `dict(usage)`, `usage.get("prompt_tokens")`, `usage["prompt_tokens"]` — эти вызовы падают с `TypeError` в nanobot 0.3.5+

### Requirement: Завершение работы `AgentLoop`

Код завершения работы (`gateway.py::_run`, `lib/cli/console_loop.py`) MUST вызывать `await agent.aclose()`, а НЕ `agent.close_mcp()`. Метод `close_mcp` удалён в nanobot 0.3.5; корректный shutdown API — `aclose()` (без параметров, async).

#### Сценарий: Cleanup MCP на shutdown

- **КОГДА** gateway/CLI завершают работу
- **ТОГДА** они ДОЛЖНЫ `await agent.aclose()`
- **И НЕ ДОЛЖНЫ** вызывать `agent.close_mcp()` — это приведёт к `AttributeError` и неконтролируемому падению процесса

### Requirement: Wrapper `patch_save_turn` принимает новые kwargs

`RuntimePatcher.patch_save_turn._wrap` (proxy вокруг `AgentLoop._save_turn`) MUST принимать и проксировать kwarg-only параметры, добавленные в nanobot 0.3.5: `summary_checkpoint: SessionSummaryCheckpoint | None = None` и `input_persisted_early: bool = False`. Без них wrapper падает с `TypeError: unexpected keyword argument 'summary_checkpoint'` при первом `_persist_turn`.

#### Сценарий: _save_turn вызывается с новыми kwargs

- **КОГДА** `AgentLoop._persist_turn` (или эквивалентный stage) вызывает `_save_turn(session, messages, skip, turn_latency_ms=..., summary_checkpoint=..., input_persisted_early=...)`
- **ТОГДА** wrapper `_wrap` ДОЛЖЕН принять все kwargs и пробросить их в оригинальный `AgentLoop._save_turn`

## Responsibility

Сбор и упорядочивание хуков, подключаемых к `AgentLoop`, и выдача этого
списка конвейеру. Владелец — `AgentFactory.create` (`lib/core/agent_factory.py:76`),
единственный вызывающий — `ApplicationContext`
(`lib/core/application_context.py:468`). Фабрика не решает, что делает хук,
и не знает про домен навыков: она заказывает инстансы и кладёт их в два
списка разной длительности жизни.

## Boundary

- **Внутри:** порядок сборки, выбор общего или per-turn инстанса,
  отказоустойчивый импорт опциональных хуков.
- **Снаружи:** тела самих хуков (у `RepeatGuardHook` отдельная спека —
  `runtime/anti-loop`), загрузка плагинов `workspace/hooks/` с allowlist'ом
  (`lib/cli/hook_loader.py:31`, спека `runtime/context`), схема и запись в БД
  (`runtime/db-logging`).

## Public Contract

`AgentFactory.create(...) -> tuple[agent, hooks, hook_factories]`
(`lib/core/agent_factory.py:90`).

- `hooks` → `AgentLoop.hooks=`; порядок: плагины `workspace/hooks/`
  (`lib/core/agent_factory.py:196-197`) → `ToolAuditHook` (`:152-155`) →
  `TerminalToolPrintHook` (`:164-166`) → `RepeatGuardHook` (`:174-182`) →
  `McpIdentityHook` (`:190-192`) → дополнительные `framework_hooks` (`:203-204`).
- `hook_factories` → `AgentLoop.hook_factories=`; `DatabaseLoggingHook`
  добавляется сюда, а не в `hooks` (`lib/core/agent_factory.py:206-212`).

Все фреймворковые хуки обязаны наследовать `nanobot.agent.hook.AgentHook`;
иначе `CompositeHook._for_each_hook_safe` диспатчит им несуществующие
lifecycle-методы.

## Inputs

- `settings` — `ProjectSettings`; из него читается блок
  `gateway.repeat_guard` (`lib/core/agent_factory.py:174`).
- `db_logging_service` — `DbLoggingService` или `None`; при `None`
  per-turn фабрики не создаются вовсе (`lib/core/agent_factory.py:212`).
- `project_hooks` — готовые инстансы плагинов, собранные
  `lib.cli.hook_loader.scan_and_register` на стороне вызывающего
  (`lib/core/application_context.py:442-448`).
- `framework_hooks` — готовые инстансы, добавленные вызывающим кодом.

## Outputs

Два списка объектов, передаваемых в `AgentLoop`. Ни файлов, ни сетевых
вызовов, ни записей в БД фабрика не производит: единственный её побочный
эффект — импорт модулей хуков.

## State

Фабрика состояния не хранит: `create` собирает списки заново на каждый
вызов. Состояние между оборотами живёт в двух других местах: per-turn
инстансы `DatabaseLoggingHook` (по одному на оборот, изолируют состояние
вопроса между конкурентными сессиями — `lib/core/agent_factory.py:206-208`)
и общий `McpIdentityHook`, который личность оборота не хранит, а читает из
контекста вызова (`lib/core/agent_factory.py:184-189`).

## Dependencies

- `nanobot.agent.hook.AgentHook`, `nanobot.agent.loop.AgentLoop` (upstream);
- `lib/hooks/tool_audit_hook.py`, `lib/hooks/terminal_tool_print_hook.py`,
  `lib/hooks/repeat_guard_hook.py`, `lib/hooks/mcp_identity_hook.py`,
  `lib/hooks/database_logging_hook.py` — каждый импортируется через
  отдельный `_import_*` с `try/except`, чтобы форк nanobot без модуля не
  ронял старт (`lib/core/agent_factory.py:164`);
- `lib/services/turn_delivery_factory.py` (`lib/core/agent_factory.py:144`);
- `lib/cli/hook_loader.py` — на стороне composition root, не здесь.

## Configuration

- `gateway.repeat_guard.*` (`lib/core/project_settings.py:223-256`) — блок
  единственного читаемого из `settings` хука; `mode` по умолчанию `off`
  (`:253`).
- Прямых настроек самой сборки списка нет: ни allowlist, ни порядок, ни
  набор фреймворковых хуков не выносятся в `config.json`.
- Allowlist имён плагинов принадлежит загрузчику
  (`lib/cli/hook_loader.py:118`), не фабрике.

## Lifecycle

1. `ApplicationContext` вызывает `scan_and_register`
   (`lib/core/application_context.py:442-448`).
2. `AgentFactory.create` собирает `hooks` и `hook_factories`
   (`lib/core/application_context.py:468`) и возвращает тройку.
3. `AgentLoop` держит `hooks` весь процесс; `hook_factories` вызывает на
   каждом обороте — столько инстансов `DatabaseLoggingHook`, сколько
   оборотов.
4. Завершение: `await ctx.agent.aclose()` (`gateway.py:639`).

## Data Ownership

Никаких данных фабрика не создаёт и не хранит. Записи аудита порождают
хуки через `DbLoggingService` и принадлежат журналу; файловые состояния
принадлежат хукам-владельцам.

## Error Behavior

- Отсутствие опционального модуля хука — не ошибка: импорт обёрнут в
  `try/except`, хук просто не добавляется
  (`lib/core/agent_factory.py:164-166`).
- Отказ `scan_and_register` логируется предупреждением и не прерывает
  старт (`lib/core/application_context.py:444-448`).
- Отказ внутри самого хука — не забота фабрики: его несёт `AgentLoop`.

## Invariants

- `ToolAuditHook` присутствует в `hooks` всегда, безусловно
  (`lib/core/agent_factory.py:152-155`).
- `RepeatGuardHook` регистрируется всегда, включая `mode="off"`, — иначе
  канонический список `runtime_inventory` разошёлся бы с фактом и
  `tools/diagnose_startup.py` сообщал бы о ложном drift'е
  (`lib/core/agent_factory.py:168-182`).
- `McpIdentityHook` идёт последним из фреймворковых, чтобы `ToolAuditHook`
  прочитал аргументы модели, а не инфраструктурные ключи
  (`lib/core/agent_factory.py:184-192`).
- `DatabaseLoggingHook` не попадает в `hooks` ни при каких настройках
  (`lib/core/agent_factory.py:206-212`).
- Порядок «плагины → фреймворковые → дополнительные» не переставляется:
  хук, читающий результат tool'а, держит `after_execute_tool` и должен
  видеть то, что вернул runner (`lib/core/agent_factory.py:199-204`).

## Forbidden Behavior

- Класс хука не наследует `nanobot.agent.hook.AgentHook` — `AgentLoop`
  начнёт диспатчить ему несуществующие методы.
- `usage` (`LLMUsage`) читается как dict: `dict(usage)`, `usage.get(...)`,
  `usage[...]`. Нормализация — через `_usage_to_dict`
  (`lib/hooks/database_logging_hook.py:286`) или публичный
  `to_turn_dict()`.
- Вызов `agent.close_mcp()` — метода нет в nanobot 0.3.5; shutdown только
  `await agent.aclose()` (`gateway.py:639`).
- Один хук зарегистрирован и в `hooks`, и в `hook_factories` — двойной
  вызов на одном обороте.
- `McpIdentityHook` поставлен раньше `ToolAuditHook`.
- Фабрика пишет в БД, в файлы или в stdout — у неё нет таких прав.

## Consumers

- `lib/core/application_context.py:468` — единственный вызывающий;
  результат кладётся в `ctx.agent`, `ctx.hooks`, `ctx.hook_factories`.
- `lib/services/runtime_inventory.py:68` (`canonical_framework_hooks`) —
  сверяет ожидаемый набор фреймворковых хуков с фактом, поэтому состав и
  порядок здесь — контракт.
- `tools/diagnose_startup.py` — показывает состав подключённых хуков.
- Каналы и CLI — рендерят записи `ToolAuditHook`
  (`lib/core/agent_factory.py:147-151`).

## Implementation

Существующие на диске пути, на которых держится этот контракт:

- `lib/core/agent_factory.py` — сборка и порядок;
- `lib/core/application_context.py` — вызов и auto-scan плагинов;
- `lib/cli/hook_loader.py` — загрузка плагинов и allowlist;
- `lib/hooks/tool_audit_hook.py`, `lib/hooks/terminal_tool_print_hook.py`,
  `lib/hooks/repeat_guard_hook.py`, `lib/hooks/mcp_identity_hook.py`,
  `lib/hooks/database_logging_hook.py` — сами хуки;
- `lib/core/project_settings.py` — модели блока настроек;
- `lib/services/runtime_inventory.py` — канонический список для сверки.

## Verification

- `tests/test_hook_allowlist.py` — allowlist и загрузка плагинов;
- `tests/test_hooks_tool_audit_hook.py` — аудит вызовов;
- `tests/test_terminal_tool_print_hook.py` — живой вывод результатов;
- `tests/test_repeat_guard_hook.py` — регистрация и режимы защитника;
- `tests/test_mcp_identity_hook.py` — подстановка личности;
- `tests/test_hooks_database_logging.py` — per-turn фабрика и usage;
- `tests/test_runtime_inventory.py` — соответствие ожидаемого набора
  хуков фактическому;
- `tests/test_application_context.py` — сборка контекста целиком.
