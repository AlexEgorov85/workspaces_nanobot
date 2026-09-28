# Application Entrypoints (CLI / Gateway)

## Purpose

Определяет контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`) поверх единого runtime: одна `ApplicationContext`, один транспорт (`PostgresChannel` через таблицу `agent_messages`), один пул воркеров. CLI и gateway отличаются только рендерером и сервисами, специфичными для серверного режима.

## ADDED Requirements

### Requirement: Единая сигнатура ApplicationContext.create

`ApplicationContext.create(...)` MUST принимать **только** runtime-флаги, специфичные для вызывающей стороны (`storage_override`, `session_override`), и параметр роли `role: Literal["gateway", "cli", "utility"]`. Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MUST NOT входить в публичную сигнатуру `create(...)` — эти значения читаются внутри фабрики из `SETTINGS["gateway"].*` (`gateway.enable_db_logging`, `gateway.enable_audit`, `gateway.enable_cron`, `gateway.print_llm_calls`).

#### Scenario: CLI и gateway вызывают create с одной сигнатурой

- **WHEN** `cli_agent.py` и `gateway.py` инициализируют runtime
- **THEN** оба entrypoint'а вызывают `ApplicationContext.create(script_dir=..., workspace_dir=..., role="cli" | "gateway")`
- **AND** оба не передают `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` в kwargs
- **AND** поведение каждого сервиса (db logging, audit sync, cron, print_llm_calls) определяется только конфигом `gateway.*`

#### Scenario: Defaults для enable_* берутся из конфига

- **WHEN** в `project.json` отсутствуют ключи `gateway.enable_db_logging`, `gateway.enable_audit`, `gateway.enable_cron`, `gateway.print_llm_calls`
- **THEN** `ApplicationContext.create()` MUST использовать значения: `enable_db_logging=True`, `enable_audit=True`, `enable_cron=True`, `print_llm_calls=False`
- **AND** эти defaults MUST быть едиными для CLI и gateway

### Requirement: Deprecated kwargs с compatibility boundary

`ApplicationContext.create(...)` MAY принимать kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` для обратной совместимости с существующими тестами и интеграционным кодом. Эти kwargs MUST быть помечены как deprecated в docstring и MUST быть удалены в следующем MINOR релизе после раскрытия этого change.

#### Scenario: Deprecated kwargs продолжают работать

- **WHEN** существующий тест вызывает `ApplicationContext.create(enable_audit=False, ...)`
- **THEN** система MUST использовать переданное значение `enable_audit=False`, игнорируя конфиг `gateway.enable_audit`
- **AND** система MUST логировать `DeprecationWarning` с указанием на новый путь конфигурации

#### Scenario: Deprecated kwargs удалены после deprecation period

- **WHEN** MINOR релиз раскрыт после deprecation period
- **THEN** `ApplicationContext.create(enable_audit=...)` MUST поднимать `TypeError` с сообщением об удалённом параметре

### Requirement: CLI публикует сообщения через общий transport

CLI entrypoint (`cli_agent.py`) MUST публиковать и потреблять сообщения через **тот же `Channel`-transport**, что и gateway (`PostgresChannel` через таблицу `agent_messages`). CLI MUST NOT публиковать сообщения напрямую в in-memory `MessageBus` для input/output; `MessageBus` остаётся только для внутренней работы `agent.run()` внутри процесса.

#### Scenario: REPL публикует user-input через таблицу

- **WHEN** пользователь вводит сообщение в CLI REPL
- **THEN** REPL MUST вызвать `channel.publish_inbound(InboundMessage(channel="cli", chat_id="cli:<session>", content=...))` (или эквивалент, публикующий в `agent_messages`)
- **AND** воркер из общего пула MUST забрать задачу через `agent_worker_claims` и обработать её
- **AND** REPL MUST подождать outbound-сообщение через `channel.consume_outbound()` (или эквивалент, читающий из `agent_messages`)
- **AND** REPL MUST отрендерить полученное сообщение в stdout (typewriter)

#### Scenario: CLI и gateway используют общий worker pool

- **WHEN** запущены и `gateway.py`, и `cli_agent.py` одновременно
- **THEN** воркер любого из процессов MAY забрать задачу из `agent_worker_claims` независимо от того, какой процесс её создал (CLI-чат или gateway-чат)
- **AND** это MUST NOT нарушать ownership задачи (задача, обрабатываемая одним воркером, не подхватывается другим)

### Requirement: Различие между CLI и gateway — только рендерер и server-only services

CLI и gateway MUST отличаться только в:

1. **Рендерер**: CLI рисует outbound-сообщения в stdout (typewriter), gateway — отдаёт через HTTP/WebSocket/Redis.
2. **Server-only services**: проверка занятости WebSocket-порта (`gateway._check_websocket_port_available`) — присутствует только в gateway.

Все остальные сервисы (db logging, audit sync, cron, runtime patches, hooks, tools, sessions) MUST быть идентичны.

#### Scenario: CLI не спавнит Streamlit subprocess

- **WHEN** запускается `cli_agent.py`
- **THEN** `cli_agent.py` MUST NOT импортировать, спавнить или каким-либо образом инициализировать `streamlit_app.py` или `SubprocessManager`
- **AND** `subprocess_manager.spawn_streamlit(...)` MUST NOT вызываться

#### Scenario: Gateway не спавнит Streamlit subprocess

- **WHEN** запускается `gateway.py`
- **THEN** `gateway.py` MUST NOT импортировать, спавнить или каким-либо образом инициализировать `streamlit_app.py` или `SubprocessManager`
- **AND** упоминания `streamlit_app.py`, `streamlit.*`-секции в `project.json`, `streamlit.enabled`-флаг MUST NOT оказывать влияния на runtime

### Requirement: Producer/consumer модель для audit-sync snapshot

Когда `gateway.enable_audit=True`, snapshot `cache.duckdb` публикуется через `resolve_publish_path(role)` в зависимости от роли процесса:

- `role="gateway"` → `<local_path>/cache.duckdb`
- `role="cli"` → `<local_path>/cli.duckdb` (отдельный файл, чтобы избежать конфликта DuckDB flock при одновременной работе CLI и gateway)
- `role="utility"` → `<local_path>/cache.duckdb` (тот же путь, что у gateway; standalone утилиты обычно запускаются, когда gateway выключен)

Решение о роли процесса MUST быть явным — передаётся через `ApplicationContext.create(role=...)` и НЕ ДОЛЖНО определяться runtime-проверкой «запущен ли другой процесс».

#### Scenario: Gateway пишет в cache.duckdb

- **WHEN** запускается `gateway.py` с `gateway.enable_audit=True`
- **THEN** `PgDuckDbSyncService` MUST публиковать snapshot в `<local_path>/cache.duckdb`
- **AND** стандартные Skills (`audit_analyzer`, `legal_summarizer`) MUST читать этот файл через `CacheProvider`

#### Scenario: CLI пишет в свой snapshot

- **WHEN** запускается `cli_agent.py` с `gateway.enable_audit=True`
- **THEN** `PgDuckDbSyncService` MUST публиковать snapshot в `<local_path>/cli.duckdb`
- **AND** стандартные Skills MUST читать этот файл через `CacheProvider` (с тем же `role="cli"`)

#### Scenario: CLI и gateway не пишут в один файл

- **WHEN** одновременно запущены `cli_agent.py` и `gateway.py` с `gateway.enable_audit=True`
- **THEN** каждый процесс пишет в свой snapshot (CLI → `cli.duckdb`, gateway → `cache.duckdb`)
- **AND** файлы MUST NOT совпадать (path collision запрещён)
- **AND** DuckDB flock MUST NOT конфликтовать

### Requirement: WebSocket port check остаётся server-only

`gateway._check_websocket_port_available()` MUST оставаться в `gateway.py` (server-only pre-startup проверка). Эта проверка MUST NOT присутствовать в CLI и MUST NOT вызываться из `ApplicationContext`.

#### Scenario: Gateway проверяет занятость WebSocket-порта

- **WHEN** запускается `gateway.py` и порт `127.0.0.1:8765` занят
- **THEN** gateway MUST exit 1 до подъёма runtime с понятной диагностикой (PID процесса-владельца, подсказка про `taskkill`/Ctrl+C)

#### Scenario: CLI не проверяет WebSocket-порт

- **WHEN** запускается `cli_agent.py`
- **THEN** CLI MUST NOT выполнять socket-check на `127.0.0.1:8765` или любой другой WS-порт

### Requirement: CLI-специфичные runtime-параметры

CLI entrypoint MUST принимать runtime-флаги, которые не относятся к gateway: `--storage` (выбор storage_mode: `auto`/`postgres`/`file`), `--session` (имя сессии для `chat_id`). Эти параметры MUST передаваться как kwargs в `ApplicationContext.create(...)`. Они MUST NOT иметь эффекта на gateway entrypoint (gateway всегда использует `storage="postgres"` и `session=None`).

#### Scenario: --storage=file в CLI

- **WHEN** пользователь запускает `cli_agent.py --storage=file`
- **THEN** `storage_override="file"` MUST передаваться в `ApplicationContext.create(storage_override="file", ...)`
- **AND** `SessionStorageService` MUST использовать file-storage вместо Postgres-storage
- **AND** CLI REPL MUST продолжать работать (offline-режим; сообщения не публикуются в `agent_messages`, а пишутся в локальный session-файл)

#### Scenario: --storage=postgres в CLI (default)

- **WHEN** пользователь запускает `cli_agent.py --storage=postgres` (или без `--storage`, если default = postgres)
- **THEN** CLI MUST публиковать сообщения через `PostgresChannel` (общий transport с gateway)

### Requirement: Slash-команда /compact в CLI остаётся локальной

CLI MUST обрабатывать slash-команду `/compact` как локальный shortcut: вызов `ContextCompactionService.compact(session_key, force=True)` напрямую из REPL, без публикации в `agent_messages`.

#### Scenario: /compact сжимает сессию немедленно

- **WHEN** пользователь в CLI вводит `/compact`
- **THEN** CLI MUST вызвать `ContextCompactionService.compact(session_key="cli:<session>", idle=True, force=True)` локально
- **AND** результат (report) MUST быть отрендерен в stdout сразу после сжатия
- **AND** событие `context_compacted` MUST быть записано в `agent_gateway_logs` через `DbLoggingService`

## Forbidden Behavior

- Передавать `enable_*`-флаги через kwargs в `ApplicationContext.create()` из нового кода (только deprecated-период для совместимости).
- Спавнить `streamlit_app.py` из `gateway.py` или `cli_agent.py`.
- Хранить `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` в CLI-специфичных kwargs.
- Писать в один и тот же snapshot-файл из разных процессов (race condition на DuckDB flock).
- Импортировать `Streamlit`, `streamlit_app`, `subprocess_manager.spawn_streamlit` из runtime-кода.
- Использовать `bus.publish_inbound` для user-input в CLI REPL (только через `Channel`-transport).
- Создавать отдельные `ApplicationContext` для каждой роли (только одна фабрика с параметром `role`).

## Dependencies

- `openspec/specs/runtime/context/spec.md` — `ApplicationContext` как composition root
- `openspec/specs/data/cache-provider/spec.md` — snapshot path resolution (`resolve_publish_path`)
- `openspec/specs/logging-db/spec.md` — `DbLoggingService` для записи `context_compacted` events
- `lib/core/application_context.py:ApplicationContext`
- `lib/channels/postgres_channel.py:PostgresChannel`
- `lib/services/cache_provider_impl.py:resolve_publish_path`

## Verification

Валидация включает:

1. `tests/test_application_context.py` — проверяет, что `create(role="cli")` и `create(role="gateway")` создают идентичный набор сервисов (за исключением `role`-зависимых параметров snapshot).
3. `tests/test_cli_over_postgres_channel.py` (новый) — интеграционный: CLI публикует сообщение → воркер забирает → CLI читает outbound → рендерит.
4. `tests/test_cache_provider_role_paths.py` (новый) — `resolve_publish_path(role="cli")` ≠ `resolve_publish_path(role="gateway")`.
5. `tests/test_streamlit_removed.py` (новый) — `gateway.py` не импортирует Streamlit/SubprocessManager.
6. Code review: проверка отсутствия `enable_*` в новых вызовах `ApplicationContext.create(...)` из `cli_agent.py` / `gateway.py`.