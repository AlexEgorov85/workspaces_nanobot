# Application Entrypoints (CLI / Gateway)

## Purpose

Определяет контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`) поверх единого composition root. Оба entrypoint'а вызывают `ApplicationContext.create(role=...)` с одной и той же typed signature; различие только в `role` и опциональных CLI-runtime-флагах. AgentLoop остаётся transport-agnostic: transport (CLI = in-memory bus / gateway = PostgresChannel) живёт ниже AgentLoop, не в нём. DuckDB — runtime-resource, владение sync'ом определяется через PG-level ownership claim через отдельную таблицу `agent_cache_ownership` с фиксированным ownership key `duckdb_cache`.

## ADDED Requirements

### Requirement: Единая typed signature ApplicationContext.create с role

`ApplicationContext.create(...)` MUST иметь typed signature:

```python
def create(
    script_dir: Path,
    workspace_dir: Path,
    *,
    role: Literal["gateway", "cli"],
    storage_override: str | None = None,
    session_override: str | None = None,
    **kwargs,  # deprecated: enable_db_logging, enable_audit, enable_cron, print_llm_calls
) -> ApplicationContext: ...
```

`profile` MUST NOT присутствовать как параметр в `def create(..., *, ...):` — профиль MUST быть разрешён ДО `create()` через `config._initialize_settings(profile=...)`.

Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MUST NOT присутствовать как именованные параметры в typed signature. Они MAY приниматься ТОЛЬКО через `**kwargs` для backward compat с существующими вызовами.

CLI и gateway MUST вызывать `ApplicationContext.create(...)` с **одной и той же сигнатурой**; различие только в `role` и runtime-флагах (`storage_override`, `session_override` — только из CLI).

#### Scenario: CLI и gateway используют одну typed signature

- **WHEN** `cli_agent.py` и `gateway.py` инициализируют runtime
- **THEN** оба entrypoint'а вызывают `ApplicationContext.create(script_dir=..., workspace_dir=..., role="cli" | "gateway", ...)`
- **AND** оба НЕ передают `profile` как параметр
- **AND** оба не передают `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` как именованные параметры
- **AND** поведение каждого сервиса определяется только конфигом `gateway.*` + `role`

#### Scenario: Profile resolved ДО ApplicationContext.create()

- **WHEN** `cli_agent.py` или `gateway.py` стартует
- **THEN** он MUST вызвать `config._initialize_settings(profile=...)` ДО `ApplicationContext.create(...)`
- **AND** `ApplicationContext.create(...)` MUST NOT принимать `profile` как параметр

#### Scenario: Defaults для enable_* берутся из конфига

- **WHEN** в `project.json` отсутствуют ключи `gateway.enable_db_logging`, `gateway.enable_audit`, `gateway.enable_cron`, `gateway.print_llm_calls`
- **THEN** `ApplicationContext.create()` MUST использовать значения: `enable_db_logging=True`, `enable_audit=True`, `enable_cron=True`, `print_llm_calls=False`

### Requirement: Deprecated kwargs с явной compatibility boundary

`ApplicationContext.create(...)` MAY принимать kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` через `**kwargs` для backward compat с существующими тестами и интеграционным кодом. Эти kwargs MUST NOT быть именованными параметрами в typed signature (физическая форма compatibility boundary). Deprecated kwargs MUST быть удалены в MINOR релизе после раскрытия этого change.

#### Scenario: Deprecated kwargs через **kwargs продолжают работать

- **WHEN** существующий тест вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **THEN** система MUST использовать переданное значение `enable_audit=False`, игнорируя конфиг `gateway.enable_audit`
- **AND** система MUST логировать `DeprecationWarning` с указанием на новый путь конфигурации
- **AND** сигнатура системы ДОЛЖНА оставаться НЕИЗМЕННОЙ после MINOR релиза, который удаляет эти kwargs

### Requirement: role определяет composition инфраструктуры, не AgentLoop

`role` MUST определять, какие инфраструктурные сервисы создаются внутри `ApplicationContext.create()`. `role` MUST NOT представлять environment, profile, deployment mode, storage ownership или runtime behavior. `role` MUST NOT определять DuckDB producer/consumer status — это ответственность `CacheOwnershipCoordinator`.

| Сервис | `role="gateway"` | `role="cli"` |
|---|---|---|
| `AgentLoop` (hooks, runtime patches, skills, tools, memory) | ✅ | ✅ |
| `DbLoggingService` (если `gateway.enable_db_logging=True`) | ✅ | ✅ |
| `SessionManager` / `PGSessionManager` | ✅ | ✅ |
| `RuntimeEventsSubscriber` | ✅ | ✅ |
| `RuntimePatcher.apply_all()` | ✅ | ✅ |
| `DuckDbCacheStore` (открывает `<local_path>/cache.duckdb` в режиме claim) | ✅ | ✅ |
| `PostgresChannel` (worker pool) | ✅ | ❌ |
| `PgDuckDbSyncService` (sync — только если claim = OWNER) | ✅ | ✅ (если OWNER) |
| `CronService` (если `gateway.enable_cron=True`) | ✅ | ❌ |
| WebSocket port check (вызывается из entrypoint) | ✅ | ❌ |
| Console I/O (in-memory bus) | ❌ | ✅ |

`role` MUST NOT влиять на `AgentLoop` (и его hooks, runtime patches, skills, tools, memory). `role` MAY влиять только на transport-инфраструктуру (PostgresChannel, Console I/O) и entrypoint-specific services (CronService, WebSocket check). DuckDB — runtime-resource, открывается в обоих `role="gateway"` и `role="cli"` в режиме, определяемом `CacheOwnershipCoordinator`.

#### Scenario: role="gateway" создаёт PostgresChannel и CronService

- **WHEN** `gateway.py` вызывает `ApplicationContext.create(role="gateway", ...)`
- **THEN** `ApplicationContext` MUST создать `PostgresChannel` и зарегистрировать его lifecycle
- **AND** `ApplicationContext` MUST создать `CronService` если `gateway.enable_cron=True`
- **AND** gateway-specific pre-startup check `WebSocket port availability` MUST быть вызван ДО `ApplicationContext.start()` из `gateway.py`

#### Scenario: role="cli" НЕ создаёт PostgresChannel и CronService

- **WHEN** `cli_agent.py` вызывает `ApplicationContext.create(role="cli", ...)`
- **THEN** `ApplicationContext` MUST NOT создавать `PostgresChannel`
- **AND** `ApplicationContext` MUST NOT создавать `CronService` (cron = gateway-only)
- **AND** CLI REPL MUST публиковать сообщения через `bus.publish_inbound(InboundMessage(channel="cli", ...))` напрямую
- **AND** CLI REPL MUST читать outbound через `bus.consume_outbound()`

### Requirement: AgentLoop MUST быть transport-agnostic

`AgentLoop` MUST NOT знать о CLI, PostgreSQL, HTTP, WebSocket, Telegram, terminal. `AgentLoop` взаимодействует только с in-memory `MessageBus` через `bus.publish_inbound(InboundMessage(...))` и `bus.publish_outbound(OutboundMessage(...))`.

#### Scenario: AgentLoop получает одно и то же сообщение независимо от источника

- **WHEN** пользователь вводит сообщение в CLI REPL
- **THEN** CLI вызывает `bus.publish_inbound(InboundMessage(channel="cli", chat_id="cli:<session>", content=...))` — AgentLoop обрабатывает через bus
- **WHEN** HTTP-запрос приходит в gateway через `PostgresChannel`
- **THEN** `PostgresChannel` вызывает `bus.publish_inbound(InboundMessage(channel="postgres", chat_id=..., content=...))` — тот же AgentLoop обрабатывает через bus
- **AND** AgentLoop MUST вести себя идентично в обоих случаях

### Requirement: CacheOwnershipCoordinator MUST определять режим DuckDB ДО открытия

DuckDB-кэш (`<local_path>/cache.duckdb`) MUST быть единым runtime-resource. Режим открытия (`READ_WRITE` или `READ_ONLY`) MUST определяться через `CacheOwnershipCoordinator.try_claim(worker_id)` ДО создания `DuckDbCacheStore`.

Lifecycle MUST быть строго:

```text
1. resolve cache.duckdb path (через resolve_publish_path)
2. attempt atomic PG ownership claim (CacheOwnershipCoordinator.try_claim)
3. determine access mode:
   - READ_WRITE (OWNER)
   - READ_ONLY (READER)
4. open DuckDB using the determined access mode (DuckDbCacheStore.open(mode=...))
5. create PgDuckDbSyncService only for OWNER (READ_WRITE)
```

`DuckDbCacheStore` MUST открываться в режиме, возвращённом `CacheOwnershipCoordinator`. `PgDuckDbSyncService` MUST NOT решать DuckDB access mode — это ответственность `CacheOwnershipCoordinator`.

#### Scenario: Lifecycle ordering (claim → mode → open)

- **WHEN** `ApplicationContext.create()` инициализирует DuckDB
- **THEN** последовательность MUST быть: `resolve_publish_path(...)` → `coord.try_claim()` → `DuckDbCacheStore.open(mode=...)` → (если OWNER) `PgDuckDbSyncService.start()`
- **AND** `DuckDbCacheStore` MUST NOT быть открыт до получения результата `coord.try_claim()`
- **AND** `PgDuckDbSyncService` MUST NOT быть создан до открытия `DuckDbCacheStore`

### Requirement: Ownership contract — atomic claim + fencing token

Ownership MUST определяться через отдельную таблицу `agent_cache_ownership` в PostgreSQL (НЕ расширение `agent_worker_claims`):

```sql
CREATE TABLE agent_cache_ownership (
    resource_key VARCHAR PRIMARY KEY,           -- фиксированное значение: 'duckdb_cache'
    owner_id VARCHAR NOT NULL,                    -- worker_id текущего владельца
    generation BIGINT NOT NULL DEFAULT 1,         -- fencing token (монотонно растёт при takeover)
    acquired_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL
);
```

**Ownership key** = `'duckdb_cache'` (фиксированная строка, НЕ per-process, НЕ per-role). MUST быть ровно **один** активный claim на ресурс `<local_path>/cache.duckdb`.

**Atomic claim MUST различать два outcomes:**

1. **Claim acquired → OWNER (READ_WRITE).** Процесс получил ownership.
2. **Claim not acquired → READER (READ_ONLY).** Другой процесс уже владеет ресурсом (его `expires_at > NOW()`).

Контракт `try_claim()` MUST NOT опираться на `RETURNING` для определения "кто сейчас owner, если мы НЕ выиграли claim". Реализация MAY использовать `INSERT ... ON CONFLICT (resource_key) DO UPDATE ... WHERE agent_cache_ownership.expires_at < NOW() RETURNING (xmax = 0) AS inserted, owner_id, generation`:

- Если `result` имеет `inserted=true` → claim acquired → OWNER → use the returned `generation` as our `my_generation`.
- Если `result` имеет `inserted=false` (или 0 rows) → claim not acquired → отдельный SELECT для чтения текущего owner metadata (owner_id, generation) для логирования.

PG row-level locking на `INSERT ... ON CONFLICT` гарантирует атомарность: два процесса одновременно делают claim → ровно один получает `inserted=true` (OWNER), остальные получают `inserted=false` (READER).

**Fencing token (generation):** каждый takeover (новый owner захватывает stale claim) MUST инкрементировать `generation` на 1. Процесс, получивший OWNER, MUST запомнить `my_generation` и использовать его для каждой записи в DuckDB. Sync-поток MUST атомарно проверять:

```sql
-- перед каждой записью:
SELECT generation FROM agent_cache_ownership
WHERE resource_key = 'duckdb_cache' AND expires_at > NOW();
-- если возвращённое generation != my_generation → ownership LOST → stop sync
```

Этот check MUST быть атомарным с самой записью в идеале; реализация может использовать:
- **Generation check + advisory lock** (PG `pg_try_advisory_xact_lock(key)` с тем же `key`, что и `resource_key`).
- **Generation check в одной транзакции** с write transaction в DuckDB.

Любая запись при `generation != my_generation` MUST быть отклонена — старый owner НЕ ДОЛЖЕН иметь возможность продолжить синхронизацию после takeover.

Heartbeat каждые 30 сек (`UPDATE last_heartbeat_at = NOW(), expires_at = NOW() + INTERVAL '60 seconds' WHERE resource_key = $1 AND owner_id = $2 AND generation = $3`). TTL = 60 сек. Stale claim (без heartbeat > 60 сек) MAY быть перехвачен следующим процессом.

**`release()` contract:** MUST удалять ownership ТОЛЬКО если `(resource_key, owner_id, generation)` совпадают с текущим значением в таблице. A consumer MUST NOT release чужой ownership. `release()` MUST be a no-op при несовпадении (и логировать WARNING).

```sql
DELETE FROM agent_cache_ownership
WHERE resource_key = $1 AND owner_id = $2 AND generation = $3
RETURNING resource_key;
-- Если RETURNING 0 rows → ownership уже не наш → no-op + warning
-- Если RETURNING 1 row → успешно released
```

#### Scenario: Atomic claim — ровно один OWNER при concurrent calls

- **WHEN** два процесса одновременно вызывают `CacheOwnershipCoordinator.try_claim()`
- **THEN** ровно один MUST получить `READ_WRITE` (его `try_claim` returns OWNER + `my_generation`)
- **AND** остальные MUST получить `READ_ONLY` (их `try_claim` returns READER; current owner metadata читается отдельным SELECT для логирования)
- **AND** это гарантируется PG row-level lock на `INSERT ... ON CONFLICT (resource_key) DO UPDATE WHERE expires_at < NOW() RETURNING (xmax=0) AS inserted`

#### Scenario: Generation инкрементируется при takeover

- **WHEN** producer A владеет cache с `generation=5`, затем producer A heartbeat expires
- **AND** producer B вызывает `try_claim()` и получает OWNER
- **THEN** producer B MUST получить `my_generation=6`
- **AND** `agent_cache_ownership.generation` MUST быть `6`

#### Scenario: Fencing — старый producer прекращает записи при изменении generation

- **WHEN** producer A владеет cache с `my_generation=5`
- **AND** producer B захватывает ownership (generation становится 6)
- **AND** producer A пытается записать в DuckDB
- **THEN** sync-поток producer A MUST атомарно проверить `generation == my_generation`
- **AND** при `generation != my_generation` (т.е. generation=6 ≠ my_generation=5) sync MUST отклонить запись
- **AND** sync MUST остановиться с логированием "ownership lost to generation N"
- **AND** producer A MUST NOT создавать новых строк в `cache.duckdb`

#### Scenario: release() только для matching ownership

- **WHEN** process A владеет cache с `generation=5`
- **AND** process A вызывает `release()`
- **THEN** release MUST удалить claim (RETURNING 1 row)
- **WHEN** process B (READER) вызывает `release()` с чужими `(owner_id, generation)`
- **THEN** release MUST быть no-op (RETURNING 0 rows) + WARNING лог

#### Scenario: kill -9 producer — следующий owner открывает существующий cache

- **WHEN** producer-процесс был killed через `kill -9` (no graceful shutdown, no release)
- **THEN** heartbeat останавливается
- **AND** через `claim_ttl_seconds` (60 сек) claim становится stale
- **AND** следующий процесс при старте MAY перехватить ownership через `try_claim()` (получит `my_generation > previous_generation`)
- **AND** следующий процесс MUST иметь возможность reopen существующий `cache.duckdb` если DuckDB считает БД recoverable (через штатный DuckDB ATTACH / WAL replay / auto-recovery)
- **AND** acceptance criterion: после unclean termination + takeover новый owner может продолжить работу без ручного восстановления

### Requirement: CacheProvider API с явным mode

`CacheProvider` MUST предоставлять API с явным параметром `mode: CacheAccessMode` (enum `READ_WRITE` или `READ_ONLY`). В `READ_ONLY` режиме все мутации (INSERT/UPDATE/DELETE) MUST быть запрещены (raise `ReadOnlyAssertionError` или `PermissionError`). `READ_WRITE` режим разрешает все мутации.

`CacheProvider` MUST reject путь на NFS (или другую network filesystem с неподдерживаемым locking) до открытия DuckDB — fail-fast с явной ошибкой.

#### Scenario: CacheProvider.open(READ_ONLY) запрещает мутации

- **WHEN** `CacheProvider.open(mode=READ_ONLY)` вызван
- **THEN** любая попытка INSERT/UPDATE/DELETE MUST поднять `ReadOnlyAssertionError`
- **AND** SELECT MUST работать нормально

#### Scenario: CacheProvider.open(READ_WRITE) разрешает мутации

- **WHEN** `CacheProvider.open(mode=READ_WRITE)` вызван
- **THEN** SELECT/INSERT/UPDATE/DELETE MUST работать нормально

#### Scenario: CacheProvider reject NFS path

- **WHEN** `gateway.cache.local_path` указывает на NFS mount или другую network filesystem
- **THEN** `CacheProvider` MUST fail-fast с явной ошибкой (PID 0 locking errors эмпирически)

### Requirement: Cron = gateway-only

`CronService` MUST создаваться ТОЛЬКО при `role="gateway"` (если `gateway.enable_cron=True`). CLI MUST NOT создавать `CronService`. Если одновременно работают CLI и gateway, cron fires ТОЛЬКО из gateway — нет дублирования `jobs.json`. Это BREAKING для пользователей, у которых сейчас cron работал в CLI.

#### Scenario: Cron в gateway

- **WHEN** `gateway.py` запущен с `gateway.enable_cron=True`
- **THEN** `CronService` MUST быть подключен к `AgentLoop`
- **AND** scheduled jobs MUST выполняться при наступлении cron-тайминга

#### Scenario: Cron НЕ в CLI

- **WHEN** `cli_agent.py` запущен
- **THEN** `CronService` MUST NOT создаваться
- **AND** scheduled jobs MUST NOT выполняться из CLI-процесса

### Requirement: WebSocket port check остаётся server-only

`gateway._check_websocket_port_available()` MUST оставаться в `gateway.py`. CLI MUST NOT выполнять её.

#### Scenario: Gateway проверяет занятость WebSocket-порта

- **WHEN** запускается `gateway.py` и порт `127.0.0.1:8765` занят
- **THEN** gateway MUST exit 1 до подъёма runtime

### Requirement: CLI-специфичные runtime-параметры

CLI entrypoint MUST принимать runtime-флаги: `--storage` (выбор `storage_mode`: `auto`/`postgres`/`file`), `--session` (имя сессии для `chat_id`).

#### Scenario: --storage=file в CLI

- **WHEN** пользователь запускает `cli_agent.py --storage=file`
- **THEN** `storage_override="file"` MUST передаваться в `ApplicationContext.create(storage_override="file", ...)`
- **AND** `SessionStorageService` MUST использовать file-storage

### Requirement: Slash-команда /compact в CLI остаётся локальной

CLI MUST обрабатывать `/compact` как локальный shortcut: вызов `ContextCompactionService.compact(session_key, force=True)` напрямую.

#### Scenario: /compact сжимает сессию немедленно

- **WHEN** пользователь в CLI вводит `/compact`
- **THEN** CLI MUST вызвать `ContextCompactionService.compact(session_key="cli:<session>", idle=True, force=True)` локально
- **AND** событие `context_compacted` MUST быть записано в `agent_gateway_logs`

### Requirement: Запрет Streamlit в runtime-коде

`gateway.py`, `cli_agent.py` и весь runtime-код MUST NOT импортировать, спавнить или каким-либо образом инициализировать `streamlit_app` или `streamlit` модуль. Удаление `streamlit_app.py` и `SubprocessManager.spawn_streamlit` — отдельный change `remove-streamlit-runtime`.

#### Scenario: runtime-код не импортирует streamlit

- **WHEN** выполняется `grep -r "import streamlit\|from streamlit" lib/ workspace/ tools/`
- **THEN** НЕ ДОЛЖНО быть результатов в runtime-коде

### Requirement: CLI имеет фиксированный профиль test

`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из env. CLI MUST hardcode `profile="test"` при вызове `config._initialize_settings(profile="test")`. Gateway MAY принимать `--profile`.

"test" в контексте CLI НЕ означает урезанный runtime: CLI MUST иметь тот же AgentLoop, Skills, Tools, DuckDB, Vector search, Memory, Logging, Prompts, Runtime patches, что и gateway. Различие только в profile (CLI == "test" hardcoded) и transport (CLI == in-memory bus).

#### Scenario: CLI не принимает --profile

- **WHEN** пользователь запускает `python cli_agent.py --profile=test`
- **THEN** argparse MUST exit с ошибкой `unrecognized arguments: --profile=test`

#### Scenario: CLI hardcodes profile="test"

- **WHEN** пользователь запускает `python cli_agent.py`
- **THEN** CLI MUST вызвать `config._initialize_settings(profile="test")` (hardcoded)
- **AND** `SETTINGS["profile"]` MUST быть `"test"`

#### Scenario: CLI не читает профиль из env

- **WHEN** пользователь запускает `python cli_agent.py` с `NANOBOT_PROFILE=prod` в env
- **THEN** CLI MUST игнорировать переменную окружения
- **AND** `SETTINGS["profile"]` MUST быть `"test"`

#### Scenario: Gateway сохраняет --profile механизм

- **WHEN** пользователь запускает `python gateway.py --profile=prod` или `--profile=test`
- **THEN** gateway MUST принять `--profile`
- **AND** `SETTINGS["profile"]` MUST соответствовать переданному значению

## Forbidden Behavior

- Передавать `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` как именованные параметры в typed signature `ApplicationContext.create(...)`.
- Передавать `profile` как параметр в `ApplicationContext.create(...)` (profile MUST быть resolved ДО через `_initialize_settings(profile=...)`).
- Открывать `DuckDbCacheStore` в режиме `READ_WRITE` без предварительного `CacheOwnershipCoordinator.try_claim()`.
- Создавать `PgDuckDbSyncService` если `CacheAccessMode = READ_ONLY`.
- Создавать `CronService` при `role="cli"`.
- Использовать `PostgresChannel` для I/O в CLI (CLI = in-memory bus).
- Делать `AgentLoop` aware о CLI, PostgreSQL, HTTP, WebSocket, terminal (transport-agnostic).
- Импортировать или спавнить `streamlit_app` из runtime-кода.
- Создавать несколько активных ownership claims (один resource — один owner).
- Использовать один и тот же `CacheProvider` экземпляр одновременно в CLI и gateway (разные process ownership на cache).
- Ветвиться по `profile == "test"` в runtime-компонентах (выбор профиля — на этапе resolution).
- Принимать `--profile` CLI-аргумент в `cli_agent.py`.
- Читать профиль из env-переменных в `cli_agent.py`.
- Определять `gateway.cache.local_path` per-profile (это shared runtime resource, не profile-specific value).
- Создавать новые API компоненты (`CacheOwnershipCoordinator`, `CacheAccessMode`, `DuckDbCacheStore.open(mode=...)`, `ReadOnlyAssertionError`) без явного объявления в design.md как новых контрактов.
- Реализовать `release()` без проверки `(owner_id, generation)` match.
- Использовать только assertion для блокировки мутаций в READ_ONLY режиме (DuckDB connection тоже MUST быть открыт в реальном read_only mode).

## Dependencies

- `openspec/specs/runtime/context/spec.md` — `ApplicationContext` как composition root
- `openspec/specs/data/cache-provider/spec.md` — snapshot path resolution
- `openspec/specs/logging-db/spec.md` — `DbLoggingService` для записи `context_compacted` events
- `openspec/specs/configuration/profiles/spec.md` — CLI fixed profile contract
- `lib/core/application_context.py:ApplicationContext`
- `lib/channels/postgres_channel.py:PostgresChannel` (gateway-only)
- `lib/services/pg_duckdb_sync_service.py:PgDuckDbSyncService`
- `lib/services/cache_ownership.py` (новый) — `CacheOwnershipCoordinator`

## Verification

Валидация включает:

1. `tests/test_application_context_role.py::TestRoleComposition` — composition matrix (D2).
2. `tests/test_application_context_role.py::TestNoProfileInSignature` — `inspect.signature(ApplicationContext.create)` НЕ содержит `profile`.
3. `tests/test_application_context_role.py::TestDeprecatedKwargs` — `enable_*` через `**kwargs` логирует `DeprecationWarning`.
4. `tests/test_cli_uses_in_memory_bus.py` — REPL использует bus, не PostgresChannel.
5. `tests/test_cache_ownership_claim.py`:
   - `test_first_process_becomes_owner`
   - `test_second_process_becomes_reader`
   - `test_concurrent_claim_exactly_one_owner`
   - `test_stale_claim_takeover`
   - `test_heartbeat_updates_claim`
   - `test_release_deletes_claim`
   - `test_old_owner_fencing` — после перехвата ownership старый owner MUST прекратить записи
   - `test_ownership_key_fixed` — только один ряд в `agent_cache_ownership`
6. `tests/test_cache_provider_mode.py` — `READ_ONLY` блокирует мутации; `READ_WRITE` разрешает.
7. `tests/test_cache_provider_role_paths.py` — `resolve_publish_path(role="cli") == resolve_publish_path(role="gateway") == cache.duckdb`.
8. `tests/test_agent_loop_transport_agnostic.py` — AgentLoop работает только через bus.
9. `tests/test_streamlit_imports_removed.py` — `grep` подтверждает отсутствие импортов Streamlit.
10. `tests/test_cli_no_profile.py::TestCLINoProfile` — CLI rejects `--profile`, hardcodes `test`, ignores env.
11. `tests/test_gateway_accepts_profile.py::TestGatewayAcceptsProfile` — gateway принимает `--profile=prod`/`--profile=test`.
12. `tests/test_application_context_role.py::TestDuckDBLifecycleOrdering` — `resolve_publish_path` → `try_claim` → `DuckDbCacheStore.open(mode=...)` → sync (atomic sequence).