# runtime/agent-hooks Specification

## Purpose
Описывает нормативные требования к `AgentHook`-совместимости хуков проекта
после апгрейда upstream `nanobot-ai 0.3.5`. Цель — зафиксировать
контракт наследования, корректную работу с `LLMUsage` dataclass,
новый API shutdown (`aclose`) и расширенную сигнатуру `patch_save_turn`.
Allowlist-проверка плагинов в `lib/cli/hook_loader.py` фиксируется
отдельным требованием в `runtime/context` (MODIFIED).

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

`DatabaseLoggingHook` и любой другой хук, использующий `context.usage`/`response.usage`, ДОЛЖЕН конвертировать `LLMUsage` (frozen dataclass из `nanobot.llm_usage.models`, поля `input_tokens`/`output_tokens`/`total_tokens`/`cache_read_tokens`/...) в dict через публичный метод `to_turn_dict()` или `to_dict()`. Прямое `dict(usage)` или `usage.get("prompt_tokens")` запрещены — они падают с `TypeError` на dataclass.

#### Сценарий: Получение prompt/completion token count

- **КОГДА** хук пишет в `DbLoggingService.log_event` или `log_llm_call` и нужно поле `prompt_tokens`
- **ТОГДА** хук ДОЛЖЕН использовать `usage.to_turn_dict()["prompt_tokens"]` или эквивалент через адаптер `_usage_to_dict`
- **И НЕ ДОЛЖЕН** использовать `dict(usage)`, `usage.get("prompt_tokens")`, `usage["prompt_tokens"]` — эти вызовы падают с `TypeError` в nanobot 0.3.5+

### Requirement: Завершение работы `AgentLoop`

Код завершения работы (`gateway.py::_run`, `lib/cli/console_loop.py`, `benchmarks/runner.py`) ДОЛЖЕН вызывать `await agent.aclose()`, а НЕ `agent.close_mcp()`. Метод `close_mcp` удалён в nanobot 0.3.5; корректный shutdown API — `aclose()` (без параметров, async).

#### Сценарий: Cleanup MCP на shutdown

- **КОГДА** gateway/CLI/benchmarks завершают работу
- **ТОГДА** они ДОЛЖНЫ `await agent.aclose()`
- **И НЕ ДОЛЖНЫ** вызывать `agent.close_mcp()` — это приведёт к `AttributeError` и неконтролируемому падению процесса

### Requirement: Wrapper `patch_save_turn` принимает новые kwargs

`RuntimePatcher.patch_save_turn._wrap` (proxy вокруг `AgentLoop._save_turn`) ДОЛЖЕН принимать и проксировать kwarg-only параметры, добавленные в nanobot 0.3.5: `summary_checkpoint: SessionSummaryCheckpoint | None = None` и `input_persisted_early: bool = False`. Без них wrapper падает с `TypeError: unexpected keyword argument 'summary_checkpoint'` при первом `_persist_turn`.

#### Сценарий: _save_turn вызывается с новыми kwargs

- **КОГДА** `AgentLoop._persist_turn` (или эквивалентный stage) вызывает `_save_turn(session, messages, skip, turn_latency_ms=..., summary_checkpoint=..., input_persisted_early=...)`
- **ТОГДА** wrapper `_wrap` ДОЛЖЕН принять все kwargs и пробросить их в оригинальный `AgentLoop._save_turn`
