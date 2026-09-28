# Application Entrypoints (CLI / Gateway / Utility)

## Purpose

Определяет контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`, standalone utilities) поверх единого composition root. Все entrypoint'ы вызывают `ApplicationContext.create(role=...)` с одной и той же сигнатурой; различие только в `role` и опциональных CLI-runtime-флагах. AgentLoop остаётся transport-agnostic: transport (CLI = in-memory bus / gateway = PostgresChannel) живёт ниже AgentLoop, не в нём. DuckDB — runtime-resource, владение sync'ом определяется через PG-level ownership claim, не через `role`.

## ADDED Requirements

### Requirement: Единая сигнатура ApplicationContext.create с обязательным role

`ApplicationContext.create(...)` MUST принимать обязательный kwarg `role: Literal["gateway", "cli", "utility"]` и опциональные CLI-runtime-флаги `storage_override`, `session_override`. Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MUST NOT входить в публичную сигнатуру `create(...)` — эти значения читаются внутри фабрики из `SETTINGS["gateway"].*`, если соответствующий kwargs не передан.

CLI и gateway MUST вызывать `ApplicationContext.create(...)` с **одной и той же сигнатурой**; различие только в `role` и runtime-флагах (`storage_override`, `session_override` — только из CLI).

#### Scenario: CLI и gateway используют одну сигнатуру

- **WHEN** `cli_agent.py` и `gateway.py` инициализируют runtime
- **THEN** оба entrypoint'а вызывают `ApplicationContext.create(script_dir=..., workspace_dir=..., role="cli" | "gateway")`
- **AND** оба не передают `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` в kwargs
- **AND** поведение каждого сервиса определяется только конфигом `gateway.*` + `role`

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

### Requirement: role определяет composition инфраструктуры, не AgentLoop

`role` MUST определять, какие инфраструктурные сервисы создаются внутри `ApplicationContext.create()`:

| Сервис | `role="gateway"` | `role="cli"` | `role="utility"` |
|---|---|---|---|
| `AgentLoop` (hooks, runtime patches, skills, tools, memory) | ✅ | ✅ | ✅ |
| `DbLoggingService` (если `gateway.enable_db_logging=True`) | ✅ | ✅ | ✅ |
| `SessionManager` / `PGSessionManager` | ✅ | ✅ | ✅ |
| `RuntimeEventsSubscriber` | ✅ | ✅ | ✅ |
| `RuntimePatcher.apply_all()` | ✅ | ✅ | ✅ |
| `DuckDbCacheStore` (открывает `<local_path>/cache.duckdb`) | ✅ | ✅ | ❌ |
| `PostgresChannel` (worker pool) | ✅ | ❌ | ❌ |
| `PgDuckDbSyncService` (sync — если claim выдан) | ✅ | ✅ | ❌ |
| `CronService` (если `gateway.enable_cron=True`) | ✅ | ✅ | ❌ |
| WebSocket port check (вызывается из entrypoint) | ✅ | ❌ | ❌ |
| Console I/O (in-memory bus) | ❌ | ✅ | ❌ |

`role` MUST NOT влиять на `AgentLoop` (и его hooks, runtime patches, skills, tools, memory) — это контракт `runtime/context`. `role` MAY влиять только на transport-инфраструктуру (PostgresChannel) и entrypoint-specific services (CronService, WebSocket check, Console I/O). DuckDB — runtime-resource, открывается в обоих `role="gateway"` и `role="cli"`, владение sync'ом определяется отдельно (см. ниже).

#### Scenario: role="gateway" создаёт PostgresChannel

- **WHEN** `gateway.py` вызывает `ApplicationContext.create(role="gateway", ...)`
- **THEN** `ApplicationContext` MUST создать `PostgresChannel` и зарегистрировать его lifecycle в `ctx.start()` / `ctx.stop()`
- **AND** worker pool MUST опрашивать `agent_conversation_messages` через `agent_worker_claims`
- **AND** gateway-specific pre-startup check `WebSocket port availability` MUST быть вызван ДО `ApplicationContext.start()` из `gateway.py` (НЕ внутри `ApplicationContext`)

#### Scenario: role="cli" НЕ создаёт PostgresChannel

- **WHEN** `cli_agent.py` вызывает `ApplicationContext.create(role="cli", ...)`
- **THEN** `ApplicationContext` MUST NOT создавать `PostgresChannel`
- **AND** CLI REPL MUST публиковать сообщения через `bus.publish_inbound(InboundMessage(channel="cli", ...))` напрямую (текущее поведение `lib/cli/console_loop.py`)
- **AND** CLI REPL MUST читать outbound через `bus.consume_outbound()`
- **AND** `AgentLoop` MUST быть подписан на in-memory bus в обоих режимах одинаково

#### Scenario: role="utility" минимальный контекст

- **WHEN** standalone utility вызывает `ApplicationContext.create(role="utility", ...)`
- **THEN** `ApplicationContext` MUST NOT создавать `PostgresChannel`, `PgDuckDbSyncService`, `CronService`, `DuckDbCacheStore`
- **AND** utility получает `AgentLoop` с подключенными runtime-патчами, hooks, skills, tools — но без transport-инфраструктуры

### Requirement: AgentLoop MUST быть transport-agnostic

`AgentLoop` MUST NOT знать о CLI, PostgreSQL, HTTP, WebSocket, Telegram, terminal. `AgentLoop` взаимодействует только с in-memory `MessageBus` через `bus.publish_inbound(InboundMessage(...))` и `bus.publish_outbound(OutboundMessage(...))`.

#### Scenario: AgentLoop получает одно и то же сообщение независимо от источника

- **WHEN** пользователь вводит сообщение в CLI REPL
- **THEN** CLI вызывает `bus.publish_inbound(InboundMessage(channel="cli", chat_id="cli:<session>", content=...))` — AgentLoop обрабатывает через bus
- **WHEN** HTTP-запрос приходит в gateway через `PostgresChannel`
- **THEN** `PostgresChannel` вызывает `bus.publish_inbound(InboundMessage(channel="postgres", chat_id=..., content=...))` — тот же AgentLoop обрабатывает через bus
- **AND** AgentLoop MUST вести себя идентично в обоих случаях

### Requirement: Удаление Streamlit

`gateway.py` MUST NOT импортировать, спавнить или каким-либо образом инициализировать `streamlit_app.py` или `SubprocessManager`. `streamlit_app.py` (файл) MUST быть удалён. `lib/services/subprocess_manager.py::spawn_streamlit` MUST быть удалён. `gateway._streamlit_enabled()` MUST быть удалён.

#### Scenario: gateway не спавнит Streamlit subprocess

- **WHEN** запускается `gateway.py`
- **THEN** `gateway.py` MUST NOT импортировать `streamlit_app` или `SubprocessManager`
- **AND** `subprocess_manager.spawn_streamlit(...)` MUST NOT вызываться

### Requirement: WebSocket port check остаётся server-only

`gateway._check_websocket_port_available()` MUST оставаться в `gateway.py` (server-only pre-startup проверка). Эта проверка MUST NOT присутствовать в CLI и MUST NOT вызываться из `ApplicationContext`.

#### Scenario: Gateway проверяет занятость WebSocket-порта

- **WHEN** запускается `gateway.py` и порт `127.0.0.1:8765` занят
- **THEN** gateway MUST exit 1 до подъёма runtime с понятной диагностикой

#### Scenario: CLI не проверяет WebSocket-порт

- **WHEN** запускается `cli_agent.py`
- **THEN** CLI MUST NOT выполнять socket-check на `127.0.0.1:8765` или любой другой WS-порт

### Requirement: CLI-специфичные runtime-параметры

CLI entrypoint MUST принимать runtime-флаги, которые не относятся к gateway: `--storage` (выбор `storage_mode` для `SessionStorageService`: `auto`/`postgres`/`file`), `--session` (имя сессии для `chat_id`). Эти параметры MUST передаваться как kwargs в `ApplicationContext.create(...)`.

#### Scenario: --storage=file в CLI

- **WHEN** пользователь запускает `cli_agent.py --storage=file`
- **THEN** `storage_override="file"` MUST передаваться в `ApplicationContext.create(storage_override="file", ...)`
- **AND** `SessionStorageService` MUST использовать file-storage вместо Postgres-storage

### Requirement: Slash-команда /compact в CLI остаётся локальной

CLI MUST обрабатывать slash-команду `/compact` как локальный shortcut: вызов `ContextCompactionService.compact(session_key, force=True)` напрямую из REPL.

#### Scenario: /compact сжимает сессию немедленно

- **WHEN** пользователь в CLI вводит `/compact`
- **THEN** CLI MUST вызвать `ContextCompactionService.compact(session_key="cli:<session>", idle=True, force=True)` локально
- **AND** событие `context_compacted` MUST быть записано в `agent_gateway_logs` через `DbLoggingService`

### Requirement: Cron в обоих режимах (config-driven)

Cron-сервис MUST создаваться и в `role="gateway"`, и в `role="cli"`, если `gateway.enable_cron=True` в SETTINGS. `role="utility"` MUST NOT создавать CronService.

Если одновременно работают и CLI, и gateway, cron fires из обоих процессов — это **документированное поведение**, не bug (разные процессы имеют свои `cron/jobs.json`).

#### Scenario: Cron в CLI и gateway

- **WHEN** `cli_agent.py` или `gateway.py` запущен с `gateway.enable_cron=True`
- **THEN** `CronService` MUST быть подключен к `AgentLoop`
- **AND** scheduled jobs MUST выполняться при наступлении cron-тайминга

## ADDED Requirements (DuckDB runtime-resource)

### Requirement: DuckDB — runtime-resource с PG-level ownership claim

DuckDB-кэш (`<local_path>/cache.duckdb`) MUST быть единым runtime-ресурсом, открываемым в `ApplicationContext.create()` для `role="gateway"` и `role="cli"`. Никаких role-based путей (`cli.duckdb`, `gateway.duckdb`) — только `cache.duckdb`.

Владение синком (`PgDuckDbSyncService`) MUST определяться через PG-level ownership claim: первый процесс, успешно зарегистрировавший себя в PG как owner, становится producer'ом (запускает sync); все остальные процессы становятся consumer'ами (открывают cache read-only, sync не запускают).

Producer периодически обновляет claim (TTL heartbeat). Если heartbeat отсутствует дольше установленного TTL, claim считается stale, и следующий процесс может его перехватить.

#### Scenario: Только CLI запущен — CLI = producer

- **WHEN** запускается `cli_agent.py` с `gateway.enable_audit=True`, и никакого active claim в `agent_cache_ownership` нет
- **THEN** `PgDuckDbSyncService` MUST зарегистрировать новый claim в PG (worker_id=`cli_<pid>`)
- **AND** CLI MUST запустить background sync из PG → DuckDB
- **AND** Skills (`audit_analyzer`, `legal_summarizer`) MUST работать, читая свежий cache.duckdb

#### Scenario: Только gateway запущен — gateway = producer

- **WHEN** запускается `gateway.py` с `gateway.enable_audit=True`, и никакого active claim нет
- **THEN** `PgDuckDbSyncService` MUST зарегистрировать новый claim (worker_id=`gateway_<pid>`)
- **AND** gateway MUST запустить background sync

#### Scenario: CLI и gateway запущены одновременно — один producer, второй consumer

- **WHEN** запускается `gateway.py`, успешно регистрирует claim, и затем запускается `cli_agent.py`
- **THEN** `PgDuckDbSyncService` в CLI MUST обнаружить существующий active claim в PG
- **AND** CLI MUST NOT запускать background sync
- **AND** CLI MUST открыть cache read-only и skills MUST работать с тем же snapshot

#### Scenario: Producer умер — следующий процесс claims

- **WHEN** producer-процесс завершается (clean shutdown или kill)
- **THEN** через `cache_ownership_ttl_sec` (default 60 сек) claim считается stale
- **AND** следующий процесс при старте MAY перехватить ownership

#### Scenario: CLI и gateway пишут в один и тот же файл

- **WHEN** оба процесса работают с `gateway.enable_audit=True`
- **THEN** только один процесс (owner) пишет в `<local_path>/cache.duckdb`
- **AND** остальные процессы открывают snapshot read-only
- **AND** DuckDB flock MUST NOT конфликтовать (один writer, остальные readers)

## Forbidden Behavior

- Передавать `enable_*`-флаги через kwargs в `ApplicationContext.create()` из нового кода.
- Спавнить `streamlit_app.py` из `gateway.py` или `cli_agent.py`.
- Писать в `cli.duckdb` или другой role-based snapshot (только `cache.duckdb`).
- Использовать `PostgresChannel` для I/O в CLI (CLI = in-memory bus).
- Создавать отдельные `ApplicationContext` для каждой роли (только одна фабрика с параметром `role`).
- Делать `AgentLoop` aware о CLI, PostgreSQL, HTTP, WebSocket, terminal (transport-agnostic).
- Использовать один `PostgresChannel`-инстанс одновременно в CLI и gateway.
- Создавать `PgDuckDbSyncService` без предварительного `try_claim_cache_ownership()` (sync без ownership claim запрещён).

## Dependencies

- `openspec/specs/runtime/context/spec.md` — `ApplicationContext` как composition root
- `openspec/specs/data/cache-provider/spec.md` — snapshot path resolution (`resolve_publish_path`)
- `openspec/specs/logging-db/spec.md` — `DbLoggingService` для записи `context_compacted` events
- `lib/core/application_context.py:ApplicationContext`
- `lib/channels/postgres_channel.py:PostgresChannel` (gateway-only)
- `lib/services/pg_duckdb_sync_service.py:PgDuckDbSyncService`
- `lib/services/cache_ownership.py` (новый) — PG-level claim mechanism

## Verification

Валидация включает:

1. `tests/test_application_context_role.py` — composition matrix.
3. `tests/test_cli_uses_in_memory_bus.py` — REPL использует `bus.publish_inbound/consume_outbound`, `PostgresChannel` НЕ создаётся.
4. `tests/test_cache_ownership_claim.py` (новый) — первый процесс — producer, второй — consumer; stale claim takeover.
5. `tests/test_cache_provider_role_paths.py` — `resolve_publish_path(role="cli") == resolve_publish_path(role="gateway") == cache.duckdb`.
6. `tests/test_agent_loop_transport_agnostic.py` — AgentLoop работает только через bus.
7. `tests/test_streamlit_removed.py` — отсутствие импортов Streamlit/SubprocessManager.