## MODIFIED Requirements

### Requirement: локальный ext4 storage

Система ДОЛЖНА хранить DuckDB-файл кэша только на локальной filesystem с поддержкой требуемой DuckDB locking semantics (POSIX `fcntl` flock, etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён (NFS эмпирически fails with PID 0 errors на свежем файле). Конкретная FS не специфицируется (ext4 — Linux default, APFS — macOS, NTFS — Windows; все поддерживают DuckDB locking); термин «ext4» в заголовке requirement сохранён для backward compat с существующими ссылками в тестах и документации, но требование портативно на любую local FS.

`CacheProvider` MUST reject путь, расположенный на NFS или другой network filesystem, ДО открытия DuckDB — fail-fast с явной ошибкой.

Snapshot-путь MUST быть единым для всех процессов:

- `cache.duckdb` — единый runtime-resource, открывается в `ApplicationContext.create()` для `role="gateway"` и `role="cli"` в режиме, определяемом `CacheOwnershipCoordinator`.
- Никаких role-based путей (`cli.duckdb`, `gateway.duckdb`). Параметр `role` в `resolve_publish_path(role)` сохранён для backward compat, но `role="cli"` и `role="gateway"` MUST возвращать **`<local_path>/cache.duckdb`**.

**`gateway.cache.local_path` MUST быть shared runtime resource**, не profile-specific value. Если CLI работает с `profile="test"`, а gateway с `profile="prod"` — оба процесса MUST резолвить `cache.duckdb` в один и тот же физический путь. Профили НЕ ДОЛЖНЫ переопределять `gateway.cache.local_path`.

**Cache lifecycle MUST быть отделён от `gateway.enable_audit`.** Cache runtime (`CacheProvider`, `DuckDbCacheStore`, `CacheOwnershipCoordinator`) создаётся, если `gateway.cache` секция настроена (наличие `gateway.cache.local_path`). `gateway.enable_audit` MUST NOT определять существование cache — он контролирует ТОЛЬКО audit sync (`PgDuckDbSyncService`).

Режим открытия (`READ_WRITE` или `READ_ONLY`) MUST определяться через `CacheOwnershipCoordinator.try_claim(worker_id)` ДО создания `CacheProvider`. См. подробный контракт в `runtime/entrypoints`.

Skills (`audit_analyzer`, `legal_summarizer`) MUST открывать `cache.duckdb` через `CacheProvider` (без `role`-based path), читать свежий snapshot независимо от того, какой процесс является owner'ом.

#### Scenario: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount или другую network filesystem
- **ТОГДА** `CacheProvider` MUST reject путь ДО открытия DuckDB с явной ошибкой (PID 0 locking errors эмпирически)
- **И НЕ ДОЛЖЕН** пытаться открыть DuckDB на NFS

#### Scenario: CLI и gateway используют один и тот же snapshot

- **КОГДА** запущены `cli_agent.py` (profile=test) и `gateway.py --profile=prod` одновременно
- **ТОГДА** оба процесса MUST резолвить `cache.duckdb` в один и тот же физический путь
- **И НЕ ДОЛЖНО** происходить race condition на DuckDB flock

#### Scenario: Профили не переопределяют gateway.cache.local_path

- **КОГДА** `profiles/test.jsonc` и `profiles/prod.jsonc` имеют разные значения `gateway.cache.local_path`
- **ТОГДА** ConfigurationResolver MUST reject это как ошибку конфигурации
- **AND** CLI и gateway MUST всегда видеть один и тот же физический путь

#### Scenario: enable_audit=False НЕ отключает cache

- **КОГДА** `gateway.enable_audit=False`, но `gateway.cache` секция настроена
- **ТОГДА** `CacheProvider` MUST быть создан (cache runtime существует)
- **AND** `PgDuckDbSyncService` MUST NOT быть создан (sync отключён)
- **AND** Skills (`audit_analyzer`, `legal_summarizer`) MUST иметь доступ к `cache.duckdb` через `CacheProvider`

## ADDED Requirements

### Requirement: CacheProvider API с явным mode + layered architecture + query_sql semantics

API MUST быть layered:

```text
CacheOwnershipCoordinator     ← try_claim / heartbeat / release (только ownership)
        ↓
CacheAccessMode              ← READ_WRITE / READ_ONLY (enum)
        ↓
CacheProvider (ABC)          ← open(path, mode) → CacheProvider instance; query_sql / search_vector / get_schema / close
        ↓
DuckDbCacheStore             ← concrete implementation CacheProvider (factory: open() → CacheProvider)
```

**`CacheProvider`** MUST быть абстрактным интерфейсом с classmethod/staticmethod `open(path: str, mode: CacheAccessMode) -> CacheProvider`. **`DuckDbCacheStore.open(...)`** — concrete factory, возвращающий `CacheProvider` instance. **ApplicationContext MUST зависеть только от `CacheProvider` (НЕ от `DuckDbCacheStore`)** — `DuckDbCacheStore` не должен появляться в полях `ctx`.

**`query_sql()` контракт:** executes any SQL statement; mutation statements (INSERT/UPDATE/DELETE) allowed only in `READ_WRITE` mode.

В `READ_ONLY` режиме все мутации MUST быть запрещены через **двухуровневую защиту**:
1. **DuckDB connection MUST быть открыт через `duckdb.connect(path, read_only=True)`** (сама DuckDB не позволит мутации).
2. **`CacheProvider.query_sql(...)` MUST поднять `ReadOnlyAssertionError`** при INSERT/UPDATE/DELETE.

`CacheProvider` MUST reject путь на NFS (или другую network filesystem с неподдерживаемым locking) до открытия DuckDB — fail-fast с явной ошибкой.

#### Scenario: Layered API — ApplicationContext зависит только от CacheProvider

- **WHEN** `ApplicationContext.create()` создаёт cache runtime
- **THEN** `ctx.cache_provider` MUST быть типизирован как `CacheProvider` (ABC)
- **AND** `ctx.cache_provider = DuckDbCacheStore.open(path, mode)` (factory)
- **AND** `ApplicationContext` MUST NOT содержать `DuckDbCacheStore` в полях

#### Scenario: CacheProvider.open(READ_ONLY) — реальный DuckDB read-only connection

- **WHEN** `CacheProvider.open(path, mode=READ_ONLY)` вызван
- **THEN** DuckDB connection MUST быть создан через `duckdb.connect(path, read_only=True)`
- **AND** попытки INSERT/UPDATE/DELETE на уровне SQL MUST быть отклонены DuckDB

#### Scenario: CacheProvider.open(READ_ONLY) — assertion guard

- **WHEN** `CacheProvider.open(mode=READ_ONLY)` вызван
- **AND** через `CacheProvider.query_sql(...)` вызывается INSERT/UPDATE/DELETE
- **THEN** MUST поднять `ReadOnlyAssertionError`

#### Scenario: CacheProvider.open(READ_WRITE) разрешает мутации

- **WHEN** `CacheProvider.open(mode=READ_WRITE)` вызван
- **THEN** SELECT/INSERT/UPDATE/DELETE MUST работать нормально

#### Scenario: query_sql() принимает любые SQL в READ_WRITE

- **WHEN** `CacheProvider.open(mode=READ_WRITE)` и вызов `query_sql("INSERT INTO ...")` или `query_sql("UPDATE ...")` или `query_sql("DELETE ...")`
- **THEN** операция MUST выполниться нормально

#### Scenario: CacheProvider reject NFS path

- **WHEN** `gateway.cache.local_path` указывает на NFS mount или другую network filesystem
- **THEN** `CacheProvider` MUST fail-fast с явной ошибкой