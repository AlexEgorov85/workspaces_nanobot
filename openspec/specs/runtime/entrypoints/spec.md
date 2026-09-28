# runtime/entrypoints Specification

## Purpose
Определяет контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`) поверх единого composition root. Оба entrypoint'а вызывают `ApplicationContext.create(role=...)` с одной и той же typed signature; различие только в `role` и опциональных CLI-runtime-флагах. AgentLoop остаётся transport-agnostic: transport (CLI = in-memory bus / gateway = PostgresChannel) живёт ниже AgentLoop, не в нём. Cache runtime — abstract (`CacheProvider` interface), владение sync'ом определяется через PG-level ownership claim через отдельную таблицу `agent_cache_ownership` с фиксированным ownership key `local_cache`. Concrete cache implementation (`DuckDbCacheStore`) находится только в composition root и в разделе concrete adapter; runtime consumers работают только через `CacheProvider` interface.

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

Deprecated kwargs являются временной compatibility boundary. Они MUST приниматься только через `**kwargs` до выполнения отдельного change `remove-deprecated-enable-kwargs`. До этого change production code MUST NOT использовать эти kwargs. После применения `remove-deprecated-enable-kwargs`:

- `enable_db_logging`
- `enable_audit`
- `enable_cron`
- `print_llm_calls`

MUST NOT приниматься `ApplicationContext.create()`; их передача MUST приводить к `TypeError`.

#### Scenario: Deprecated kwargs через **kwargs продолжают работать

- **WHEN** существующий тест вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **THEN** система MUST использовать переданное значение `enable_audit=False`, игнорируя конфиг `gateway.enable_audit`
- **AND** система MUST логировать `DeprecationWarning` с указанием на новый путь конфигурации

#### Scenario: После remove-deprecated-enable-kwargs — TypeError на deprecated kwargs

- **WHEN** change `remove-deprecated-enable-kwargs` реализован
- **AND** код вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **THEN** MUST быть поднят `TypeError`

### Requirement: role определяет composition инфраструктуры, не AgentLoop

`role` MUST определять, какие инфраструктурные сервисы создаются внутри `ApplicationContext.create()`. `role` MUST NOT представлять environment, profile, deployment mode, storage ownership или runtime behavior. `role` MUST NOT определять cache producer/consumer status — это ответственность `CacheOwnershipCoordinator`.

| Сервис | `role="gateway"` | `role="cli"` |
|---|---|---|
| `AgentLoop` (hooks, runtime patches, skills, tools, memory) | ✅ | ✅ |
| `DbLoggingService` (если `gateway.enable_db_logging=True`) | ✅ | ✅ |
| `SessionManager` / `PGSessionManager` | ✅ | ✅ |
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

`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из env. CLI MUST hardcode `profile="test"` при вызове `config._initialize_settings(profile="test")`. Gateway MAY принимать `--profile`.

"test" в контексте CLI НЕ означает урезанный runtime: CLI MUST иметь тот же AgentLoop, Skills, Tools, Vector search, Memory, Logging, Prompts, Runtime patches, что и gateway (плюс CacheProvider interface). Различие только в profile (CLI == "test" hardcoded) и transport (CLI == in-memory bus).

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
