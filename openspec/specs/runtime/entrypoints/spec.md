# runtime/entrypoints Specification

## Purpose
Определяет контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`) поверх единого composition root. Оба entrypoint'а вызывают `ApplicationContext.create(role=...)` с одной и той же typed signature; различие только в `role` и опциональных CLI-runtime-флагах. AgentLoop остаётся transport-agnostic: transport (CLI = in-memory bus / gateway = PostgresChannel) живёт ниже AgentLoop, не в нём. Cache runtime — abstract (`CacheProvider` interface), владение sync'ом определяется через PG-level ownership claim через отдельную таблицу `agent_cache_ownership` с фиксированным ownership key `local_cache`. Concrete cache implementation (`DuckDbCacheStore`) находится только в composition root и в разделе concrete adapter; runtime consumers работают только через `CacheProvider` interface.

## Scope

`agent` — точки входа и lifecycle агента
Реализация: `gateway.py`, `cli_agent.py`, `lib/lifecycle/`

## Requirements

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

`profile` MUST NOT присутствовать как параметр в `def create(..., *, ...):` — профиль MUST быть разрешён ДО `create()` через `config._initialize_settings(profile=...)`. `profile` MUST NOT также проходить через `**kwargs`: его передача SHALL приводить к `TypeError`.

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
- **AND** `ApplicationContext.create(..., profile=...)` MUST приводить к `TypeError`

#### Scenario: Defaults для enable_* берутся из конфига

- **WHEN** в `config.json` отсутствуют ключи `gateway.enable_db_logging`, `gateway.enable_audit`, `gateway.enable_cron`, `gateway.print_llm_calls`
- **THEN** `ApplicationContext.create()` MUST использовать значения: `enable_db_logging=True`, `enable_audit=True`, `enable_cron=False`, `print_llm_calls=False`

### Requirement: Deprecated kwargs с явной compatibility boundary

Deprecated kwargs являются временной compatibility boundary. Они MUST приниматься только через `**kwargs` до выполнения отдельного change `remove-deprecated-enable-kwargs`. До этого change production code MUST NOT использовать эти kwargs. После применения `remove-deprecated-enable-kwargs`:

- `enable_db_logging`
- `enable_audit`
- `enable_cron`
- `print_llm_calls`

MUST NOT приниматься `ApplicationContext.create()`; их передача MUST приводить к `TypeError`.

Перечень deprecated kwargs SHALL состоять ровно из этих четырёх
параметров. `profile` MUST NOT входить в этот перечень: у него нет
migration path в `config.json`, и он не является deprecated API.

#### Scenario: Deprecated kwargs через **kwargs продолжают работать

- **WHEN** существующий тест вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **THEN** система MUST использовать переданное значение `enable_audit=False`, игнорируя конфиг `gateway.enable_audit`
- **AND** система MUST логировать `DeprecationWarning` с указанием на новый путь конфигурации

#### Scenario: После remove-deprecated-enable-kwargs — TypeError на deprecated kwargs

- **WHEN** change `remove-deprecated-enable-kwargs` реализован
- **AND** код вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **THEN** MUST быть поднят `TypeError`

#### Scenario: profile не входит в перечень deprecated kwargs

- **WHEN** проверяется перечень deprecated compatibility kwargs
- **THEN** `profile` MUST NOT входить в него
- **AND** профиль MUST NOT обрабатываться через `**kwargs` с
  `DeprecationWarning`

### Requirement: ApplicationContext.create MUST NOT принимать profile

`ApplicationContext.create()` MUST NOT принимать `profile` ни как
именованный параметр, ни через `**kwargs`. Профиль MUST быть разрешён
ДО вызова `create()` через lifecycle-gate `_initialize_settings(profile=...)`,
после чего `ApplicationContext` читает его только из
`SETTINGS["profile"]`.

`profile` MUST NOT входить в перечень deprecated compatibility kwargs.
Deprecated compatibility boundary (`DEPRECATED_ENABLE_KWARGS`) предназначена
исключительно для `enable_db_logging`, `enable_audit`, `enable_cron`,
`print_llm_calls` — параметров с определённым migration path в
`config.json` (`gateway.*`). У `profile` такого migration path нет: он не
является deprecated API, а уже не является API `ApplicationContext` вовсе.

`ApplicationContext` MUST NOT знать, каким способом был выбран профиль.
Единственный канал получения профиля — `SETTINGS["profile"]`.

#### Scenario: Передача profile в create() приводит к TypeError

- **WHEN** код вызывает `ApplicationContext.create(..., profile="test")`
- **THEN** вызов SHALL привести к `TypeError`
- **AND** `ApplicationContext` SHALL NOT быть создан

#### Scenario: profile отсутствует в сигнатуре create()

- **WHEN** проверяется `inspect.signature(ApplicationContext.create)`
- **THEN** `profile` MUST NOT присутствовать в `parameters`
- **AND** `profile` MUST NOT входить в перечень deprecated kwargs

#### Scenario: ApplicationContext читает профиль из SETTINGS

- **WHEN** `ApplicationContext.create()` выполняется после успешного
  `_initialize_settings(profile=...)`
- **THEN** активный профиль SHALL быть прочитан из `SETTINGS["profile"]`
- **AND** значение SHALL совпадать с профилем, переданным в
  `_initialize_settings`

#### Scenario: ApplicationContext не влияет на выбор профиля

- **WHEN** `ApplicationContext.create()` вызывается
- **THEN** он MUST NOT изменять, пересобирать или переключать
  разрешённый профиль
- **AND** он MUST NOT выполнять повторное разрешение профиля

### Requirement: Production entrypoints MUST NOT передавать profile в composition root

Production application entrypoints MUST NOT передавать `profile` в
`ApplicationContext.create()`. Профиль определяется entrypoint'ом и
публикуется через `_initialize_settings(profile=...)` до вызова
`create()`; после этого профиль доступен исключительно через
`SETTINGS["profile"]`.

#### Scenario: Entrypoints не передают profile

- **WHEN** проверяются production-файлы `gateway.py` и `cli_agent.py`
- **THEN** вызовы `ApplicationContext.create()` в них MUST NOT содержать
  аргумент `profile`

#### Scenario: Профиль CLI определён до composition

- **WHEN** `cli_agent.py` стартует
- **THEN** он MUST вызвать `_initialize_settings` с фиксированным
  профилем `test` ДО вызова `ApplicationContext.create()`
- **AND** вызов `ApplicationContext.create()` SHALL NOT содержать `profile`

### Requirement: role определяет composition инфраструктуры, не AgentLoop

`role` MUST определять, какие инфраструктурные сервисы создаются внутри `ApplicationContext.create()`. `role` MUST NOT представлять environment, profile, deployment mode, storage ownership или runtime behavior. `role` MUST NOT определять cache producer/consumer status — это ответственность `CacheOwnershipCoordinator`.

| Сервис | `role="gateway"` | `role="cli"` |
|---|---|---|
| `AgentLoop` (hooks, runtime patches, skills, tools, memory) | ✅ | ✅ |
| `DbLoggingService` (если `gateway.enable_db_logging=True`) | ✅ | ✅ |
| `SessionManager` (поверх `SanitizingSessionStore`) | ✅ | ✅ |
| `RuntimeEventsSubscriber` | ✅ | ✅ |
| `RuntimePatcher.apply_all()` | ✅ | ✅ |
| `CacheProvider` (если `gateway.cache` настроен) | ✅ | ✅ |
| `CacheOwnershipCoordinator` (если `gateway.cache` настроен) | ✅ | ✅ |
| concrete cache factory (`DuckDbCacheStore.open(path, mode)`) | ✅ | ✅ |
| `PostgresChannel` (worker pool) | ✅ | ❌ |
| `CacheSyncService` (sync — если `enable_audit=True` И OWNER) | ✅ | ✅ (если OWNER) |
| `CronService` (если `gateway.enable_cron=True`) | ✅ | ❌ |
| WebSocket port check (вызывается из entrypoint) | ✅ | ❌ |
| Console I/O (in-memory bus) | ❌ | ✅ |

`role` MUST NOT влиять на `AgentLoop` (и его hooks, runtime patches, skills, tools, memory). `role` MAY влиять только на transport-инфраструктуру (PostgresChannel, Console I/O) и entrypoint-specific services (CronService, WebSocket check). **Cache runtime** (`CacheProvider`, concrete implementation, `CacheOwnershipCoordinator`) — shared runtime-resource, открывается в обоих `role="gateway"` и `role="cli"` если `gateway.cache` секция настроена. **Sync runtime** (`CacheSyncService`) — controlled by `gateway.enable_audit`, separate concern.

`role` MUST NOT влиять на `AgentLoop` (и его hooks, runtime patches, skills, tools, memory). `role` MAY влиять только на transport-инфраструктуру (PostgresChannel, Console I/O) и entrypoint-specific services (CronService, WebSocket check). Cache runtime — abstract (`CacheProvider` interface), открывается в обоих `role="gateway"` и `role="cli"` в режиме, определяемом `CacheOwnershipCoordinator`.

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

### Requirement: CacheOwnershipCoordinator MUST определять режим cache ДО открытия

Cache-snapshot (через concrete adapter — текущая реализация: `<local_path>/cache.duckdb`) MUST быть единым runtime-resource. Режим открытия (`READ_WRITE` или `READ_ONLY`) MUST определяться через `CacheOwnershipCoordinator.try_claim(worker_id)` ДО создания concrete `CacheProvider` implementation.

Lifecycle MUST быть строго:

```text
1. resolve snapshot path (через resolve_publish_path)
2. attempt atomic PG ownership claim (CacheOwnershipCoordinator.try_claim → ClaimResult)
3. determine access mode from ClaimResult:
   - acquired=True → READ_WRITE (OWNER)
   - acquired=False → READ_ONLY (READER)
4. concrete_factory.open(path, mode=ClaimResult.mode) → CacheProvider instance
   (concrete_factory = `DuckDbCacheStore.open` для текущей реализации; future: `SQLiteCacheStore.open` или эквивалентный)
5. if ClaimResult.acquired AND gateway.enable_audit=True:
   create CacheSyncService (only for OWNER in audit mode)
```

`ApplicationContext` является composition root и MAY использовать concrete factory (например, `DuckDbCacheStore.open(path, mode)`) для сборки текущей реализации `CacheProvider`. После создания runtime:

- `ApplicationContext.cache_provider` MUST иметь тип `CacheProvider`;
- runtime consumers (AgentLoop, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator`) MUST зависеть только от `CacheProvider`;
- concrete implementation MUST NOT использоваться как тип runtime dependency.

Зависимость `ApplicationContext` от `DuckDbCacheStore` допускается ТОЛЬКО в composition code, который создаёт concrete implementation.

#### Scenario: Lifecycle ordering (claim → mode → open → optional sync)

- **WHEN** `ApplicationContext.create()` инициализирует cache runtime
- **THEN** последовательность MUST быть: `resolve_publish_path(...)` → `coord.try_claim()` → concrete_factory.open(mode=ClaimResult.mode) → (если OWNER И `enable_audit=True`) `CacheSyncService.start()`
- **AND** concrete factory НЕ ДОЛЖЕН быть вызван до получения `ClaimResult`
- **AND** `CacheSyncService` MUST NOT быть создан без `ClaimResult.acquired=True`
- **AND** `CacheSyncService` MUST NOT быть создан если `gateway.enable_audit=False`

### Requirement: Ownership contract — atomic claim + real fencing через advisory lock

Ownership MUST определяться через отдельную таблицу `agent_cache_ownership` в PostgreSQL (НЕ расширение `agent_worker_claims`):

```sql
CREATE TABLE agent_cache_ownership (
    resource_key VARCHAR PRIMARY KEY,           -- фиксированное значение: 'local_cache'
    owner_id VARCHAR NOT NULL,                    -- worker_id текущего владельца
    generation BIGINT NOT NULL DEFAULT 1,         -- fencing token (starts at 1, монотонно растёт при takeover)
    acquired_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL
);
```

**Ownership key** = `'local_cache'` — идентификатор логического cache resource. НЕ per-process, НЕ per-role, НЕ per-storage. Этот ключ НЕ ДОЛЖЕН содержать название СУБД или concrete adapter. MUST быть ровно **один** активный claim на ресурс `local_cache`.

**Generation semantics:** `generation` стартует с 1 при первой вставке строки; инкрементируется на 1 при каждом takeover. Strictly monotonically increasing. Generation — fencing token, а НЕ самостоятельный write barrier.

**`try_claim()` MUST возвращать `ClaimResult`:**

```python
class ClaimResult:
    acquired: bool                            # True → мы OWNER; False → другой процесс OWNER
    generation: int                           # my_generation (если acquired=True) или current_generation (если False)
    owner_id: str                             # наш worker_id (если acquired=True) или current owner_id (если False)
    current_owner_id: str | None = None       # для логирования при False
    current_generation: int | None = None      # для логирования при False
```

**Atomic claim MUST различать два outcomes:**

1. **Claim acquired → `ClaimResult(acquired=True, generation=N, owner_id=self.worker_id)`.** Процесс получил ownership.
2. **Claim not acquired → `ClaimResult(acquired=False, current_generation=N, current_owner_id=X)`.** Другой процесс уже владеет ресурсом.

Реализация MAY использовать `INSERT ... ON CONFLICT (resource_key) DO UPDATE ... WHERE agent_cache_ownership.expires_at < NOW() RETURNING ...`. Конкретные PG-детали (например, `(xmax = 0) AS inserted`) — implementation detail, не architectural contract.

PG row-level locking на `INSERT ... ON CONFLICT` гарантирует атомарность: два процесса одновременно делают claim → ровно один получает `acquired=True` (OWNER), остальные получают `acquired=False` (READER).

**Real fencing MUST использовать coordination lock shared между ownership takeover и producer write.** Generation check alone НЕДОСТАТОЧЕН — это TOCTOU race (старый producer проверяет generation → takeover инкрементирует → старый пишет).

Mandatory contract:

```text
Fencing MUST обеспечивать mutual exclusion между:
1. Ownership takeover (try_claim() DO UPDATE branch);
2. Producer mutation critical section (validate generation + execute mutation через CacheProvider).

Рекомендуемый механизм — PG advisory lock:

  BEGIN PG transaction;
    SELECT pg_advisory_xact_lock(hashtext($resource_key));  -- serialize
    -- (в try_claim branch: check generation + INSERT/UPDATE; в producer mutation: check generation + execute mutation через CacheProvider)
  COMMIT;

Lock MUST быть held для полного критического раздела:
  ownership validation AND corresponding CacheProvider mutation.

PostgreSQL transaction MUST NOT быть committed или closed между шагами ownership validation и execution of mutation.

Если ownership не совпадает:
- mutation через CacheProvider НЕ выполняется;
- ownership НЕ изменяется;
- transaction завершается без producer mutation.
```

Реализация MAY использовать `pg_advisory_xact_lock(key=hashtext(resource_key))` (автоматически освобождается при COMMIT/ROLLBACK). Альтернативные механизмы с той же семантикой mutual exclusion допустимы.

**Generation НЕ является самостоятельным write barrier.** Только в комбинации с advisory lock generation обеспечивает fencing.

Heartbeat каждые 30 сек (`UPDATE last_heartbeat_at = NOW(), expires_at = NOW() + INTERVAL '60 seconds' WHERE resource_key = $1 AND owner_id = $2 AND generation = $3`). TTL = 60 сек. Stale claim (без heartbeat > 60 сек) MAY быть перехвачен следующим процессом.

**`release()` contract:** MUST удалять ownership ТОЛЬКО если `(resource_key, owner_id, generation)` совпадают. No-op + WARNING при несовпадении.

```sql
DELETE FROM agent_cache_ownership
WHERE resource_key = $1 AND owner_id = $2 AND generation = $3
RETURNING resource_key;
-- Если RETURNING 0 rows → ownership уже не наш → no-op + warning
```

#### Scenario: Atomic claim — ровно один OWNER при concurrent calls

- **WHEN** два процесса одновременно вызывают `CacheOwnershipCoordinator.try_claim()`
- **THEN** ровно один MUST получить `ClaimResult(acquired=True)`
- **AND** остальные MUST получить `ClaimResult(acquired=False)` с `current_owner_id` первого процесса
- **AND** это гарантируется PG row-level lock на `INSERT ... ON CONFLICT`

#### Scenario: Generation инкрементируется при takeover

- **WHEN** producer A владеет cache с `generation=5`, затем producer A heartbeat expires
- **AND** producer B вызывает `try_claim()` и получает `ClaimResult(acquired=True)`
- **THEN** producer B MUST получить `generation=6`
- **AND** `agent_cache_ownership.generation` MUST быть `6`

#### Scenario: Fencing через advisory lock — старый producer прекращает записи

- **WHEN** producer A хочет выполнить mutation через `CacheProvider`
- **THEN** producer A MUST атомарно захватить `pg_advisory_xact_lock(hashtext('local_cache'))`
- **AND** внутри lock MUST проверить `(owner_id=A, generation=my_generation)` match текущему `agent_cache_ownership` row
- **AND** только при match → execute mutation через `CacheProvider`
- **AND** при несовпадении (B уже takeover) → lock release при COMMIT → A MUST NOT выполнить mutation
- **AND** B НЕ МОЖЕТ инкрементировать generation пока A держит lock (PG advisory lock mutual exclusion)

#### Scenario: release() только для matching ownership

- **WHEN** process A владеет cache с `generation=5`
- **AND** process A вызывает `release()`
- **THEN** release MUST удалить claim
- **WHEN** process B (READER) вызывает `release()` с чужими `(owner_id, generation)`
- **THEN** release MUST быть no-op + WARNING лог

#### Scenario: kill -9 producer — следующий owner открывает существующий cache

- **WHEN** producer-процесс был killed через `kill -9` (no graceful shutdown, no release)
- **THEN** heartbeat останавливается
- **AND** через `claim_ttl_seconds` (60 сек) claim становится stale
- **AND** следующий процесс при старте MAY перехватить ownership через `try_claim()` (получит `generation > previous_generation`)
- **AND** следующий процесс MUST иметь возможность reopen существующий snapshot файл если cache storage считает его recoverable (acceptance criterion, не конкретный механизм)

### Requirement: CacheProvider API с явным mode + layered architecture

API MUST быть layered:

```text
CacheOwnershipCoordinator     ← try_claim / heartbeat / release / acquire_write_fence (только ownership)
        ↓
CacheAccessMode              ← READ_WRITE / READ_ONLY (enum)
        ↓
CacheProvider (ABC)          ← interface: query_sql / search_vector / get_schema / close
        ↓
Concrete implementation      ← текущая: DuckDbCacheStore; future: SQLiteCacheStore
```

**`CacheProvider` MUST NOT иметь метода `open()`.** Это ответственность concrete factory.

Concrete implementation создаётся composition root через concrete factory:

```python
# для текущей реализации:
DuckDbCacheStore.open(path, mode) -> CacheProvider
# для будущей реализации:
SQLiteCacheStore.open(path, mode) -> CacheProvider
```

**`ApplicationContext` является composition root** и MAY использовать concrete factory для сборки текущей реализации `CacheProvider`. После создания runtime:

- `ApplicationContext.cache_provider` MUST иметь тип `CacheProvider`;
- runtime consumers (AgentLoop, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator`) MUST зависеть только от `CacheProvider`;
- concrete implementation MUST NOT использоваться как тип runtime dependency.

Зависимость `ApplicationContext` от `DuckDbCacheStore` допускается ТОЛЬКО в composition code, который создаёт concrete implementation.

В `READ_ONLY` режиме все мутации (INSERT/UPDATE/DELETE) MUST быть запрещены через **двухуровневую защиту**:
1. **Concrete cache adapter MUST открыть storage connection в реальном read-only режиме** (для текущей реализации DuckDB: `duckdb.connect(path, read_only=True)`; для будущей SQLite — соответствующий API). Сам storage engine не позволит мутации.
2. **`CacheProvider.query_sql(...)` MUST поднять `ReadOnlyAssertionError`** при INSERT/UPDATE/DELETE.

`query_sql()` контракт: MUST принимать только следующие SQL statement types:
- `SELECT`
- `INSERT`
- `UPDATE`
- `DELETE`

DDL и другие schema-changing statements (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `CREATE INDEX`, `DROP INDEX`) MUST быть отклонены с `UnsupportedSqlError` в любом mode (включая READ_WRITE).

Concrete cache storage MUST reject unsupported network/shared filesystem paths before opening the storage. Для текущей DuckDB implementation NFS/SMB и другие network/shared filesystems MUST быть rejected. Для будущих реализаций правила аналогичны.

#### Scenario: Layered API — ApplicationContext хранит cache через CacheProvider

- **WHEN** `ApplicationContext.create()` создаёт cache runtime
- **THEN** `ctx.cache_provider` MUST быть типизирован как `CacheProvider` (ABC)
- **AND** `ctx.cache_provider` MAY быть создан через concrete factory (например, `DuckDbCacheStore.open(path, mode)`) — это composition-time code
- **AND** `ApplicationContext` MUST NOT содержать `DuckDbCacheStore` (или другую concrete implementation) как поле runtime consumer
- **AND** runtime consumers (AgentLoop, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator`) MUST зависеть только от `CacheProvider`

#### Scenario: Concrete factory открывает READ_ONLY cache — реальный read-only connection

- **WHEN** concrete factory (например, `DuckDbCacheStore.open(path, mode=READ_ONLY)`) вызван
- **THEN** concrete adapter MUST открыть storage connection в реальном read-only режиме (для DuckDB: `duckdb.connect(path, read_only=True)`)
- **AND** попытки INSERT/UPDATE/DELETE на уровне SQL MUST быть отклонены storage engine

#### Scenario: Concrete factory открывает READ_ONLY cache — assertion guard

- **WHEN** concrete factory (например, `DuckDbCacheStore.open(mode=READ_ONLY)`) вызван
- **AND** через `CacheProvider.query_sql(...)` вызывается INSERT/UPDATE/DELETE
- **THEN** MUST поднять `ReadOnlyAssertionError`

#### Scenario: Concrete factory открывает READ_WRITE cache — мутации разрешены

- **WHEN** concrete factory (например, `DuckDbCacheStore.open(mode=READ_WRITE)`) вызван
- **THEN** `SELECT`/`INSERT`/`UPDATE`/`DELETE` MUST работать нормально

#### Scenario: query_sql() в READ_WRITE принимает SELECT и DML

- **WHEN** CacheProvider создан в READ_WRITE и `query_sql("SELECT ...")` или `query_sql("INSERT INTO ...")` или `query_sql("UPDATE ...")` или `query_sql("DELETE ...")`
- **THEN** операция MUST выполниться нормально

#### Scenario: query_sql() в READ_WRITE отклоняет DDL

- **WHEN** CacheProvider создан в READ_WRITE и `query_sql("CREATE TABLE ...")` или `query_sql("DROP TABLE ...")` или `query_sql("ALTER TABLE ...")` или `query_sql("TRUNCATE TABLE ...")` или `query_sql("CREATE INDEX ...")` или `query_sql("DROP INDEX ...")`
- **THEN** MUST поднять `UnsupportedSqlError` (DDL запрещён даже в READ_WRITE)

#### Scenario: query_sql() в READ_ONLY принимает SELECT

- **WHEN** CacheProvider создан в READ_ONLY и `query_sql("SELECT ...")`
- **THEN** операция MUST выполниться нормально

#### Scenario: CacheProvider reject NFS path

- **WHEN** `gateway.cache.local_path` указывает на NFS mount или другую network filesystem
- **THEN** `CacheProvider` MUST fail-fast с явной ошибкой (PID 0 locking errors эмпирически)

### Requirement: CacheSyncService для синхронизации данных

`CacheSyncService` отвечает за синхронизацию данных из PostgreSQL в локальное cache-хранилище. `CacheSyncService` НЕ ДОЛЖЕН зависеть от конкретной реализации `CacheProvider`.

Источник данных синхронизации — PostgreSQL. Destination — локальное cache-хранилище, доступное через `CacheProvider` interface.

`CacheSyncService` НЕ ДОЛЖЕН напрямую импортировать concrete cache implementation (например, `DuckDbCacheStore`). Название и интерфейс сервиса НЕ ДОЛЖНЫ предполагать конкретный тип локального хранилища.

```text
PostgreSQL
    │
    ▼
CacheSyncService (storage-independent)
    │
    ▼
CacheProvider (interface)
    │
    ▼
concrete implementation (DuckDbCacheStore / SQLiteCacheStore)
```

**Кто держит fencing boundary (Variant A — фиксировано):**

```text
CacheOwnershipCoordinator  ← отвечает за ownership и fencing.
CacheProvider              ← отвечает только за cache access.
CacheSyncService           ← является producer; ОБЯЗАН использовать
                              ownership-fenced write boundary
                              перед каждой mutation.
CacheProvider              ← НЕ ДОЛЖЕН самостоятельно выполнять
                              ownership check, heartbeat или takeover.
```

`CacheSyncService` MUST NOT вызывать mutation CacheProvider в обход fenced write boundary.

Все producer mutations через `CacheSyncService` MUST проходить через ownership-fenced write boundary:

```text
1. BEGIN PostgreSQL transaction.
2. Acquire resource-scoped coordination lock.
3. Read current ownership state.
4. Verify owner_id + generation.
5. Execute mutation through CacheProvider.
6. COMMIT PostgreSQL transaction.

PostgreSQL transaction MUST NOT быть committed или closed между шагами 4 и 5.

Если ownership не совпадает:
- mutation через CacheProvider НЕ выполняется;
- ownership НЕ изменяется;
- transaction завершается без producer mutation.
```

#### Scenario: sync не может обойти fencing

- **GIVEN** процесс является producer
- **AND** ownership содержит generation G
- **WHEN** `CacheSyncService` выполняет mutation
- **THEN** mutation MUST проходить через fenced write boundary
- **AND** concrete cache implementation MUST NOT вызываться в обход этого boundary

#### Scenario: ownership generation изменился до выполнения mutation

- **GIVEN** процесс является producer
- **AND** ownership generation изменился (B сделал takeover) ДО выполнения mutation
- **WHEN** `CacheSyncService` пытается выполнить mutation
- **THEN** mutation MUST NOT быть выполнена
- **AND** `OwnershipLostError` MUST быть поднят

#### Scenario: CacheSyncService storage-agnostic

- **WHEN** `CacheSyncService` выполняет mutation
- **THEN** он импортирует `CacheProvider` interface, НЕ `DuckDbCacheStore` (или другую concrete реализацию)
- **AND** замена concrete cache implementation НЕ требует изменений в `CacheSyncService`

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

`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из env. CLI MUST использовать фиксированный профиль `test` при вызове `config._initialize_settings(profile="test")`. Gateway MAY принимать `--profile`.

"test" в контексте CLI НЕ означает урезанный runtime: CLI MUST иметь тот же AgentLoop, Skills, Tools, Vector search, Memory, Logging, Prompts, Runtime patches, что и gateway (плюс CacheProvider interface). Различие только в profile (CLI == "test" fixed) и transport (CLI == in-memory bus).

#### Scenario: CLI не принимает --profile

- **WHEN** пользователь запускает `python cli_agent.py --profile=test`
- **THEN** CLI MUST отклонить флаг с `ConfigurationError` и завершиться с кодом 2
- **AND** процесс MUST NOT запускать `ApplicationContext`

#### Scenario: CLI hardcodes profile="test"

- **WHEN** пользователь запускает `python cli_agent.py` (без `--profile`)
- **THEN** CLI MUST вызвать `config._initialize_settings(profile="test")` (fixed)
- **AND** `SETTINGS["profile"]` MUST быть `"test"`
- **AND** процесс MUST NOT требовать `--profile` для старта

#### Scenario: CLI не читает профиль из env

- **WHEN** `cli_agent.py` запускается в окружении, содержащем
  произвольные переменные, чьи имена или значения выглядят как выбор
  профиля
- **THEN** CLI MUST игнорировать эти переменные
- **AND** `SETTINGS["profile"]` MUST быть `"test"`
- **AND** этот сценарий ограничивает ТОЛЬКО выбор профиля; секреты и
  `${VAR}` substitution MAY legitimately читать environment

#### Scenario: Gateway сохраняет --profile механизм

- **WHEN** пользователь запускает `python gateway.py --profile=prod` или `--profile=test`
- **THEN** gateway MUST принять `--profile`
- **AND** `SETTINGS["profile"]` MUST соответствовать переданному значению

### Requirement: Невозможность поднятия обязательной зависимости обнаруживается на старте

Процесс `enterprise-mcp` — обязательная зависимость агента, а не опциональный
сервис: со стороны платформы он владеет пулом PostgreSQL и даёт модели входы к
данным. Поэтому `enterprise-mcp` MUST быть опрошен **синхронно и блокирующе на
старте**, ДО того как агент примет хоть один оборот. Отказ подъёма MUST NOT
откладываться до момента, когда зависимость понадобится: «платформа лежит»
обнаруживается на старте, а не посреди оборота пользователя.

Операция рукопожатия — `list_operations()`: она поднимает stdio-сессию и делает
discovery. Второго, отдельного механизма рукопожатия SHALL NOT появляться: если
сервер поднялся, но нужные операции не отдал, подниматься больше нечего, и такой
отказ MUST приводить к тому же отказу запуска, а не проходить дальше.

Рукопожатие MUST выполняться внутри живого event loop, к которому привязана
stdio-сессия. Синхронный подъём ДО `asyncio.run` SHALL NOT использоваться: loop
там ещё не существует. Ленивое создание сессии внутри клиента MAY оставаться, но
ONLY как путь восстановления оборвавшейся сессии — как нормальный путь подъёма
оно SHALL NOT использоваться.

#### Scenario: Платформа не отвечает — отказ на старте, а не на первом вопросе

- **WHEN** поднимается любой из двух entrypoint'ов, а `enterprise-mcp` не отвечает
- **THEN** рукопожатие MUST дать отказ ДО того, как агент станет способен принять
  оборот (каналы в gateway, REPL в CLI)
- **AND** подъём MUST быть блокирующим: продолжение старта без результата
  рукопожатия SHALL NOT происходить
- **AND** отказ MUST NOT откладываться до первого вызова tool'а, которому
  платформа нужна (проверяется поведенчески:
  `tests/test_gateway_enterprise_mcp_startup.py::TestRuntimeOrdering::test_handshake_precedes_channels_start_and_agent`
  и `::TestCliOrdering::test_handshake_runs_before_repl_and_transport`)

#### Scenario: Рукопожатие выполняется внутри живого event loop

- **WHEN** проверяется место вызова рукопожатия в обоих entrypoint'ах
- **THEN** вызов MUST находиться внутри корутины, исполняемой `asyncio.run(...)`
- **AND** подъём сессии ДО `asyncio.run` SHALL NOT использоваться, потому что
  stdio-сессия привязана к loop, который её поднял, и подъём до loop означал бы
  сессию, закрывающуюся вместе с ним

#### Scenario: Сервер поднялся, но операций не отдал

- **WHEN** `list_operations()` завершается пустым набором операций
- **THEN** это SHALL считаться тем же отказом запуска, что и неотвечающий сервер
- **AND** старт SHALL NOT продолжаться «на всякий случай»

### Requirement: Отказ подъёма — это отказ запуска, а не тихая деградация

Отказ рукопожатия MUST NOT деградировать в частичную работу. Ни каналы, ни
агент, ни REPL SHALL NOT стартовать после отказа, а сам отказ MUST NOT быть
проглочен. Тихая деградация здесь была бы единственно неправильным вариантом:
задачи начали бы забираться из очереди, а tool'ы отвечали бы ошибкой — и
«платформа лежит» выглядело бы снаружи как «агент работает».

Правильный порядок сам по себе этого не гарантирует: порядок соблюдён, а
исключение проглочено — и деградация наступила. Отказ MUST доходить до
владельца процесса (`GatewayRunner` в gateway — он логирует и перезапускает с
backoff; `main()` в CLI — он печатает в `stderr` и возвращает ненулевой код).

#### Scenario: Каналы не стартуют после отказа

- **WHEN** `_connect_enterprise_mcp(ctx)` в `gateway._run` падает
- **THEN** `channels.start_all()` SHALL NOT быть вызван
- **AND** `ctx.agent.run()` SHALL NOT быть вызван
- **AND** исключение SHALL дойти до вызывающего кода
  (проверяется `TestHandshakeFailureRefusesStartup::test_channels_never_start_when_handshake_fails`)

#### Scenario: REPL не поднимается после отказа

- **WHEN** рукопожатие в CLI падает
- **THEN** REPL SHALL NOT подняться
- **AND** `ctx.attach_log_transport()` SHALL NOT быть вызван
  (проверяется `TestCliOrdering::test_repl_does_not_start_when_handshake_fails`)

### Requirement: Причина отказа фиксируется ДО отказа наружу

Причина MUST быть названа словами **до** того, как исключение уйдёт наружу. На
старте MUST быть выведена читаемая строка вердикта, называющая упавшую
зависимость и саму причину, и отдельная строка подсказки — что именно
проверять. Для CLI причина MUST дополнительно попасть в журнал запуска
(`logger.error`): строка в консоли не заменяет запись в журнал, из которого
инцидент потом и разбирают.

**Уточнение формулировки.** Раньше это обосновывалось так: «`GatewayRunner`
сообщает лишь `Gateway exited unexpectedly, restarting in 1.0s`, и причина не
читается ни в одном логе». Это неточно: `GatewayRunner` дописывает к уведомлению
само исключение и его трейс (`"... restarting in %.1fs: %s\n%s"`). Что верно и
что остаётся причиной требования: уведомление называет **перезапуск**, а не
**упавшую зависимость**, и не даёт подсказки, что проверять. Строка вердикта —
единственное место, где это сказано.

#### Scenario: Вердикт напечатан до отказа

- **WHEN** рукопожатие в gateway падает
- **THEN** ДО `raise` в вывод SHALL уйти строка, содержащая признак отказа
  (`НЕ ПОДНЯЛСЯ`) и текст исходной причины
  (проверяется `TestHandshakeFailureRefusesStartup::test_reason_is_printed_before_the_refusal`)

#### Scenario: Причина CLI попадает и в консоль, и в журнал

- **WHEN** рукопожатие в CLI падает
- **THEN** в вывод SHALL уйти строка с признаком отказа, исходной причиной и
  подсказкой, что проверять
- **AND** `logger.error` SHALL быть вызван с той же причиной
  (проверяется `TestCliHandshake::test_reason_is_readable_and_stack_trace_free`
  и `::TestCliHandshake::test_reason_reaches_the_log`)

### Requirement: Отказ конфигурации отличается от отказа доступности

Два класса отказа MUST NOT смешиваться, потому что лечатся они по-разному и
значат разное.

- **Отказ конфигурации** — `ConfigurationError`. Типичный случай: оверлей
  профиля объявлен в ДВУХ файлах (`profiles/<режим>.jsonc` агента и
  `mcp-platform/platform.json → profiles.<имя>`), и расхождение приводит к тому,
  что агент ждёт одни имена таблиц, а платформа пишет в другие. Повтор и
  рестарт такое НЕ исправят. Тип MUST сохраняться: ошибка конфигурации MUST NOT
  заворачиваться в отказ доступности, иначе «опечатка в профиле» выдаст себя за
  «сервер не отвечает».
- **Отказ доступности** — процесс `enterprise-mcp` не поднялся, упал или не
  ответил вовремя. В CLI MUST подниматься `CliStartupError`, уносящий исходную
  причину (и в тексте, и в `__cause__`); в gateway `EnterpriseMcpUnavailable`
  MUST уходить наверх на своём месте.

В CLI код выхода MUST различать эти случаи: `2` — ошибка конфигурации,
`1` — не поднялась зависимость. Смешанные коды ломают разбор: по одному числу
нельзя понять, чинить ли профиль или поднимать процесс.

#### Scenario: Расхождение профиля сохраняет тип конфигурации

- **WHEN** рукопожатие (или сверка имён таблиц) даёт `ConfigurationError`
- **THEN** тип MUST сохраниться, без заворачивания в `CliStartupError`
- **AND** в вывод SHALL уйти строка с признаком `КОНФИГУРАЦИЯ`
  (проверяется `TestCliHandshake::test_profile_mismatch_keeps_configuration_error_type`
  и `::TestHandshakeFailureRefusesStartup::test_profile_mismatch_also_refuses_to_start_channels`)

#### Scenario: Отказ доступности сохраняет исходную причину

- **WHEN** рукопожатие падает с `EnterpriseMcpUnavailable`
- **THEN** поднятый `CliStartupError` SHALL нести исходный текст причины
- **AND** `__cause__` SHALL остаться `EnterpriseMcpUnavailable`
  (проверяется `TestCliHandshake::test_failure_keeps_the_original_cause`)

#### Scenario: Коды выхода CLI различают два класса отказа

- **WHEN** отказ конфигурации доходит до `cli_agent.main()`
- **THEN** код выхода SHALL быть `2`
- **WHEN** отказ доступности доходит до `cli_agent.main()`
- **THEN** код выхода SHALL быть `1` и `stderr` SHALL содержать причину
  (проверяется `TestCliStartupBoundary::test_configuration_error_still_exits_two`,
  `::TestCliStartupBoundary::test_configuration_error_from_handshake_still_exits_two`
  и `::TestCliStartupBoundary::test_startup_failure_exits_nonzero_with_reason`)

### Requirement: Порядок касается старта каналов, а не их конструирования

Инвариант порядка — рукопожатие раньше **запуска** — MUST NOT распространяться на
**конструирование**. Разница существенна и легко спутывается: `create_all()`
только конструирует каналы и задач не создаёт, а `start_all()` — это запуск.
Поэтому `create_all` MAY выполняться до рукопожатия, а `start_all` MUST NOT.

Различение MUST быть зафиксировано явно, иначе правка инварианта превратится в
ложное требование «каналы создаются только после рукопожатия» — то есть в
запрет несуществующей проблемы ценой реального усложнения composition.

Между рукопожатием и `start_all` находится `ctx.attach_log_transport()` —
построение writer'а журнала поверх живой MCP-сессии. Оно MUST оставаться после
рукопожатия: writer строится на живой сессии, которой до рукопожатия нет.

#### Scenario: Порядок в исходнике

- **WHEN** в `gateway.py` вызов рукопожатия расположен в тексте ПОСЛЕ вызова
  `channels.start_all()`
- **THEN** проверка MUST это увидеть и провалиться — это отдать первую задачу в
  никуда
  (проверяется `TestOrdering::test_handshake_precedes_channels`, статическая
  сверка порядка в исходнике)

#### Scenario: Фактический порядок вызовов

- **WHEN** выполняется `gateway._run` на заглушках
- **THEN** рукопожатие SHALL наблюдаться раньше `channels.start_all()` и раньше
  `agent.run()`
  (проверяется `TestRuntimeOrdering::test_handshake_precedes_channels_start_and_agent`;
  статическая сверка выше дёшева, но хрупка — она не ловит «рукопожатие вызвано
  правильно, но не оттуда»)

#### Scenario: Конструирование до рукопожатия — не нарушение

- **WHEN** выполняется `gateway._run` на заглушках
- **THEN** `create_all` SHALL наблюдаться раньше рукопожатия, и это SHALL быть
  признано корректным
- **AND** `create_all` SHALL наблюдаться раньше `start_all`
  (проверяется `TestRuntimeOrdering::test_constructing_channels_is_not_starting_them`)

### Requirement: CLI следует тому же контракту с поправкой на интерактивность вывода

Контракт запуска применим к CLI в полном объёме: то же обязательное
рукопожатие, тот же отказ вместо деградации, то же различение классов отказа.
Поправка касается только способа подъёма и подачи:

1. Рукопожатие MUST выполняться внутри живого event loop до старта REPL —
   stdio-сессия привязана к loop, и поднять её синхронно до `asyncio.run`
   нельзя.
2. Причина MUST печататься одной читаемой строкой **без стек-трейса**, а
   подсказка — отдельной строкой. CLI интерактивен: пользователю нужен вердикт
   «не поднялась платформа», а не дамп.
3. Стек-трейс MUST печататься только по явному запросу через
   `NANOBOT_CLI_TRACEBACK=1`. По умолчанию он SHALL NOT выводиться.

**Сверка профильных имён таблиц на CLI.** В gateway рукопожатие дополнено
сводкой по capability и сверкой имён таблиц с платформой. CLI выполняет
**сверку имён таблиц** (паритет): он жёстко прибит к профилю `test`, оверлей
объявлен в двух файлах, и расхождение — ровно тот дефект, который сверка ловит
(агент ждёт `*_test`, платформа пишет в боевые таблицы). Правило сверки
импортируется у владельца контракта (`gateway._verify_platform_table_alignment`),
а не копируется: вторая копия разошлась бы с первой при первой же правке.
Расхождение MUST оставаться `ConfigurationError` (код выхода `2`), а не
`CliStartupError` (`1`), — иначе опечатка в оверлее выглядит как упавшая
платформа.

Чего CLI по-прежнему НЕ делает — сводку по всем capability. Интерактивная
консоль выигрывает от краткости, а список таблиц отдаёт та же проба
`schema_check`, ради которой сверка и делается.

**Граница контракта (что CLI НЕ делает).** Сводка по capability (`vectors` /
`data` / `audit`) на CLI-пути не выполняется намеренно. Это НЕ часть контракта
запуска и не должно выдаваться за него.

#### Scenario: Рукопожатие CLI идёт первым в живом loop

- **WHEN** запускается CLI
- **THEN** рукопожатие SHALL наблюдаться раньше REPL и раньше
  `attach_log_transport()`
  (проверяется `TestCliOrdering::test_handshake_runs_before_repl_and_transport`
  и `TestCliPatchedBranch::test_patched_branch_reaches_the_repl` — обе ветви
  CLI: `--patched` обязана держать тот же контракт)

#### Scenario: Расхождение имён таблиц на CLI отказывает в запуске кодом 2

- **WHEN** CLI запускается, а имена таблиц агента и платформы расходятся
- **THEN** SHALL подниматься `ConfigurationError`, а не `CliStartupError`
- **AND** код выхода SHALL быть `2`, а не `1`
- **AND** REPL SHALL NOT подниматься ни в обычной ветви, ни в `--patched`
  (проверяется `TestCliTableAlignment::test_profile_mismatch_exits_two_and_is_not_masked_as_one`
  и `::TestCliTableAlignment::test_profile_mismatch_refuses_startup_as_configuration_error`,
  обе ветви параметризованы)

#### Scenario: Трейс только по явному запросу

- **WHEN** `NANOBOT_CLI_TRACEBACK` не установлен
- **THEN** `stderr` SHALL NOT содержать `Traceback`
- **WHEN** `NANOBOT_CLI_TRACEBACK=1`
- **THEN** полный трейс SHALL печататься, код выхода SHALL остаться `1`
  (проверяется `TestCliStartupBoundary::test_startup_failure_exits_nonzero_with_reason`
  и `::TestCliStartupBoundary::test_traceback_only_when_explicitly_requested`)

### Requirement: Контракт применим к обоим входам в систему

У системы два application entrypoint'а — `gateway.py` и `cli_agent.py` — и оба
MUST выполнять этот контракт. Отличие между ними — способ подъёма и подача
отказа, а не сам факт отказа. Требование, написанное для одного входа, SHALL NOT
считаться выполненным, пока его не держит второй.

Выключенный раздел `gateway.agent.enterprise_mcp` — единственный случай, когда
рукопожатия не происходит: клиент не создан (`None`) по решению оператора. Это
MUST NOT считаться отказом. Выход SHALL быть явным и печатаемым («не объявлен»
+ предупреждение, что инструменты данных ответят структурной ошибкой), а не
молчаливым, иначе следующий разбер не поймёт, был ли сервер выключен или сломан.

#### Scenario: Выключенный раздел — не отказ

- **WHEN** раздел `gateway.agent.enterprise_mcp` не объявлен
- **THEN** рукопожатие MUST завершиться без исключения в обоих entrypoint'ах
- **AND** в вывод SHALL уйти явная строка о том, что сервер не объявлен
  (проверяется `TestHandshake::test_disabled_section_is_not_an_error` и
  `::TestHandshake::test_disabled_section_is_reported_not_silent`)

#### Scenario: Второй вход не отстаёт от первого

- **WHEN** контракт запуска изменяется
- **THEN** правка MUST применена к обоим entrypoint'ам
- **AND** отсутствие рукопожатия на одном из путей SHALL считаться нарушением
  контракта, а не допустимым упрощением этого пути

#### Scenario: Отказ проб capability не равен отказу запуска

- **WHEN** рукопожатие прошло, но отдельная проба capability не ответила или
  вернула ошибку
- **THEN** старт MUST NOT от этого рушиться: платформа отвечает, а неполнота
  одного capability разбирается отдельно и не должна выглядеть как «шлюз не
  поднялся»
- **AND** при этом расхождение имён таблиц MUST по-прежнему ронять старт
  (проверяется `TestHealthSummary::test_probe_failure_does_not_fail_startup`
  и `::TestHealthSummary::test_misaligned_profile_refuses_to_start`)
