# Proposal: post-0-3-5-hook-migration

## Why

После upgrade на `nanobot-ai 0.3.5` lib/hooks/* хуки проекта (`DatabaseLoggingHook`, `ToolAuditHook`, `TerminalToolPrintHook`) перестали работать корректно, потому что upstream изменил lifecycle-протокол:

1. **`AgentHook` стал полноценным базовым классом** с no-op методами `emit_reasoning`, `emit_reasoning_end`, `on_stream`, `on_stream_end`, `finalize_content`, `on_error`, `on_finally`. Раньше наши хуки были plain classes без этих методов; `CompositeHook._for_each_hook_safe` через `getattr(h, method_name)` ловил `AttributeError`, глотал его и продолжал — отсюда ERROR-спам в логах и потерянные callback'и.

2. **`usage` стал `LLMUsage` dataclass** (frozen, с `to_turn_dict()`/`to_dict()`), а не `dict`. Прямое обращение `dict(context.usage)` и `.get("prompt_tokens")` падают с `TypeError`.

3. **`AgentLoop.close_mcp` удалён**, новый метод — `aclose()` (без параметров). Старые вызовы дают `AttributeError`.

4. **`AgentLoop._save_turn` получил kwarg-only параметры** `summary_checkpoint: SessionSummaryCheckpoint | None` и `input_persisted_early: bool = False`. Wrapper `patch_save_turn._wrap` падает с `TypeError: unexpected keyword argument 'summary_checkpoint'`.

Этот change делает миграцию хуков на новый протокол, фиксит тип usage и обновляет сигнатуры.

## What Changes

### MODIFIED

- **`lib/hooks/database_logging_hook.py`** — наследует `nanobot.agent.hook.AgentHook`; добавлен `_usage_to_dict()` (адаптер `LLMUsage`/dict → per-turn dict через `to_turn_dict()`); `after_iteration`/`_store_iteration_usage`/`_make_run_event`/`finalize_content` используют `_usage_to_dict` вместо `dict(...)` / `.get()`.
- **`lib/hooks/tool_audit_hook.py`** — наследует `nanobot.agent.hook.AgentHook`. Без поведенческих изменений.
- **`lib/hooks/terminal_tool_print_hook.py`** — наследует `nanobot.agent.hook.AgentHook`. Без поведенческих изменений.
- **`gateway.py`** (`_run`, shutdown-блок) — `ctx.agent.close_mcp()` → `await ctx.agent.aclose()`.
- **`lib/cli/console_loop.py`** (REPL shutdown-блок) — `agent.close_mcp()` → `await agent.aclose()`.
- **`benchmarks/runner.py`** (финальный cleanup) — `ctx.agent.close_mcp()` → `await ctx.agent.aclose()`.
- **`lib/services/runtime_patcher.py::patch_save_turn._wrap`** — принимает и проксирует новые kwargs `summary_checkpoint=None`, `input_persisted_early=False` в `AgentLoop._save_turn`.

### NEW

- **Allowlist в `lib/cli/hook_loader.py::_allowed_hook_names()`** — расширен значением `"debug_stream_diag"` (временный диагностический хук, в этой change помечен как REMOVED в tasks.md).
- **`workspace/hooks/debug_stream_diag.py`** — диагностический хук для stream-trace (`emit_reasoning`/`on_stream`/`finalize_content` чанки → `data_store/cache/debug_stream.log`). Использовался при диагностике обрезания ответа. Помечен как REMOVED (см. tasks.md).

### REMOVED

- **`workspace/hooks/debug_stream_diag.py`** — после подтверждения диагноза (обрезание — upstream-проблема провайдера `api.minimax.io`, не наш код) хук удаляется вместе с записью в allowlist.

### Capabilities

#### New Capabilities

- `runtime/agent-hooks`: нормативный контракт lib/hooks/* хуков в 0.3.5:
  - Все хуки ДОЛЖНЫ наследовать `nanobot.agent.hook.AgentHook` (получают no-op для новых lifecycle-методов).
  - `usage` в `AgentHookContext` ДОЛЖЕН обрабатываться через адаптер (`LLMUsage` в 0.3.5 — frozen dataclass, не dict).
  - `AgentLoop.close_mcp`/`aclose`/`_save_turn` сигнатуры зафиксированы для 0.3.5.

#### Modified Capabilities

- `runtime/context` — добавлено требование: `lib/hooks/*` хуки ДОЛЖНЫ быть совместимы с lifecycle-протоколом `AgentHook` из текущей установленной версии `nanobot-ai`; правки протокола оформляются через OpenSpec change `post-0-3-5-hook-migration` или его преемника.

### Impact

- **Код:** 6 prod-файлов + 1 временный debug-файл + 1 строка в allowlist. ~80 строк изменений суммарно. Никаких новых модулей.
- **API:** нет. Все правки — адаптация к upstream-изменениям в nanobot 0.3.5.
- **Зависимости:** `nanobot-ai==0.3.5` (уже зафиксировано).
- **Тесты:** правки тестовых моков в `tests/test_cli_agent.py`, `tests/test_gateway.py`, `tests/test_runtime_patcher.py` для совместимости с новыми сигнатурами (`aclose`, `summary_checkpoint`).
- **Документация:** `docs/architecture/runtime-patcher-inventory.md` — обновить статус `patch_save_turn` (добавлены kwargs).
- **Производительность:** нулевое влияние. Хуки теперь no-op по умолчанию для новых методов, поведение идёт через runtime-patcher и конкретные хуки.

### Вне scope

- Локальный фикс обрезания ответа на стороне провайдера `api.minimax.io`. Диагностировано: провайдер теряет префикс content при переходе reasoning→content через `response.reasoning_text.delta` + content-канал. Это upstream-проблема провайдера; локальный фикс через overlap не сработает для случаев overlap=0. Оформлено как upstream-bug-report (вне этого change).
- Изменение формата `DbLoggingService.log_event` payload. Это независимая работа.
- Дополнительные хуки (например, `RateLimitHook`, `CostLimitHook`). Не востребованы.

### Зависимости

- `nanobot-ai==0.3.5` (зафиксировано).
- `openspec/changes/nanobot-035-upgrade` — контекст изменений upstream, которые обуславливают эту миграцию.
- `openspec/changes/post-0-3-5-patches-cleanup` — параллельная работа, переход на `RuntimeEventsSubscriber`; этот change **не пересекается** с патчами хуков.

### Связанные спеки

- `openspec/changes/nanobot-035-upgrade` — описание upstream-изменений в nanobot 0.3.5 (`AgentHook` API, `LLMUsage`, `aclose`, `_save_turn` kwargs).
