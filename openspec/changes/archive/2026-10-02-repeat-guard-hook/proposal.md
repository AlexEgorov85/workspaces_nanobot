## Why

Агент может зацикливаться на повторных tool-вызовах с одинаковыми
аргументами без прогресса — например, `read_file` на той же странице
5 раз, `exec` той же команды или `web_search` одного и того же
запроса. Единственный runtime-guard уровня агента сегодня —
upstream `max_tool_iterations = 200` (`config.json:13`,
`nanobot/agent/runner.py:435`), и узкие throttles `repeated_external_lookup_error`
(`nanobot/utils/runtime.py:109`, hardcoded `_MAX_REPEAT_EXTERNAL_LOOKUPS=2`)
и `repeated_workspace_violation_error` (`nanobot/utils/runtime.py:173`,
hardcoded `_MAX_REPEAT_WORKSPACE_VIOLATIONS=2`). Они покрывают только
web-fetch/web-search и workspace-bypass; повторяющиеся вызовы
`read_file`/`exec`/`history_search`/etc. остаются невидимы до
исчерпания 200 итераций. Старый `RepeatGuardHook` упоминается в
`CHANGELOG.md:3642` (v1.3.0), но был удалён вместе с self-review
(`CHANGELOG.md:3670`).

## What Changes

- Добавить **framework hook `RepeatGuardHook`** в `lib/hooks/`,
  наследующий `nanobot.agent.hook.AgentHook`; сравнивает
  `tool_name + normalized args` (и опционально хеш результата) в
  скользящем окне последних `window_size` вызовов, и при превышении
  `max_repeats_in_window` поднимает синтетическую ошибку
  (`"Error: repeat-guard blocked — identical tool call repeated N
  times in last K iterations"`), которую `_execute_tool_call`
  (`nanobot/agent/tools/execution.py:177-194`) превращает в обычный
  tool-error в conversation — никаких новых путей прерывания.
- Конфигурация — новая опциональная секция `gateway.repeat_guard.*`
  в `project.json` с режимом `off` (по умолчанию NO-OP)/`warn`
  (только лог)/`block` (синтетический tool-error).
- Журнал срабатываний — `agent_gateway_logs.event_type =
  tool_repeat_blocked` через `DbLoggingService.try_log_event`
  (уже канонический путь для всех runtime-событий).
- Подключение через `AgentFactory.create` (`lib/core/agent_factory.py`)
  после `ToolAuditHook`, чтобы аудит уже видел «нормальный» вызов и
  не зависел от того, заблокирован он или нет; регистрация в
  `canonical_framework_hooks()` (`lib/services/runtime_inventory.py`)
  для `diagnose_startup.py`.
- Тесты: `tests/test_repeat_guard_hook.py` + расширение
  `tests/test_runtime_inventory.py` (новая запись в каноническом
  реестре framework-хуков).
- Документация: краткое упоминание в `docs/ARCHITECTURE.md` рядом с
  `tool_result_limits.*` и в `CHANGELOG.md` после применения change.

Никаких изменений SQL/DDL, формата upstream-конфига nanobot,
`SETTINGS["channels"]`/`SETTINGS["logging"]`, `compact_context`
tool или skill-контрактов. **Не затрагивает** upstream `max_tool_iterations`
и `repeated_*_error` — это остаётся fallback верхнего уровня.

## Capabilities

### New Capabilities

- `runtime/anti-loop`: описывает контракт общего runtime-защитника
  от деградирующих циклов одинаковых tool-вызовов внутри одного
  оборота агента: режимы работы, конфигурация, фаза срабатывания,
  журналирование, границы ответственности (что НЕ перекрывается с
  upstream `max_tool_iterations`/throttle'ами), взаимодействие с
  другими фреймворковыми хуками.

### Modified Capabilities

- Нет. Существующие capabilities (`runtime/agent-hooks`,
  `runtime/error-fallback`, `configuration/profiles`,
  `data/cache-provider`) не меняют требования: новый хук лишь
  наследует `AgentHook`-контракт, зафиксированный в
  `runtime/agent-hooks`, и использует публичные артефакты
  (`DbLoggingService.try_log_event`) без изменения их контрактов.

## Impact

- Код:
  - новый модуль `lib/hooks/repeat_guard_hook.py`
    (`RepeatGuardHook(AgentHook)` + хелпер нормализации аргументов),
  - регистрация/импорт в `lib/core/agent_factory.py::create`
    (между `ToolAuditHook` и `TerminalToolPrintHook`),
  - запись в `canonical_framework_hooks()` в
    `lib/services/runtime_inventory.py`,
  - новые pydantic-поля в `GatewayRepeatGuardSettings` в
    `lib/core/project_settings.py` с дефолтами,
  - блок `gateway.repeat_guard.*` в `project.json` (опциональный).
- API: публичный экспорт `RepeatGuardHook` из
  `lib.hooks.repeat_guard_hook`.
- Тесты:
  - `tests/test_repeat_guard_hook.py` — юнит: нормализация args,
    скользящее окно, срабатывание threshold, исключения
    `exempt_tools`, режим `warn`/`block`/`off`;
  - `tests/test_runtime_inventory.py` — добавление `RepeatGuardHook`
    в ожидаемый список framework-хуков;
  - `tests/test_config_keys.py` — если ключи будут обязательными
    (текущий план — опциональные с дефолтами, поэтому без правок).
- SQL/DDL: без изменений.
- Операторы: новые ключи `gateway.repeat_guard.*` — опциональные
  с дефолтами (`off`), существующие деплои без правок `project.json`
  продолжают работать как раньше.
