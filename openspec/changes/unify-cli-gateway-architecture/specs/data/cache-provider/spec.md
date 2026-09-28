## MODIFIED Requirements

### Requirement: локальный ext4 storage

Система ДОЛЖНА хранить DuckDB-файл кэша только на локальной filesystem с поддержкой требуемой DuckDB locking semantics (POSIX `fcntl` flock, etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён (NFS эмпирически fails with PID 0 errors на свежем файле). Конкретная FS не специфицируется (ext4 — Linux default, APFS — macOS, NTFS — Windows; все поддерживают DuckDB locking); термин «ext4» в заголовке requirement сохранён для backward compat с существующими ссылками в тестах и документации, но требование портативно на любую local FS.

`CacheProvider` MUST reject путь, расположенный на NFS или другой network filesystem, ДО открытия DuckDB — fail-fast с явной ошибкой.

Snapshot-путь MUST быть единым для всех процессов:

- `cache.duckdb` — единый runtime-resource, открывается в `ApplicationContext.create()` для `role="gateway"` и `role="cli"` в режиме, определяемом `CacheOwnershipCoordinator`.
- Никаких role-based путей (`cli.duckdb`, `gateway.duckdb`). Параметр `role` в `resolve_publish_path(role)` сохранён для backward compat, но `role="cli"` и `role="gateway"` MUST возвращать **`<local_path>/cache.duckdb`**.

**`gateway.cache.local_path` MUST быть shared runtime resource**, не profile-specific value. Если CLI работает с `profile="test"`, а gateway с `profile="prod"` — оба процесса MUST резолвить `cache.duckdb` в один и тот же физический путь. Профили НЕ ДОЛЖНЫ переопределять `gateway.cache.local_path`.

Режим открытия (`READ_WRITE` или `READ_ONLY`) MUST определяться через `CacheOwnershipCoordinator.try_claim(worker_id)` ДО создания `DuckDbCacheStore`. См. подробный контракт в `runtime/entrypoints`.

Skills (`audit_analyzer`, `legal_summarizer`) MUST открывать `cache.duckdb` через `CacheProvider` (без `role`-based path), читать свежий snapshot независимо от того, какой процесс является owner'ом.

#### Scenario: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount или другую network filesystem
- **ТОГДА** `CacheProvider` MUST reject путь ДО открытия DuckDB с явной ошибкой (PID 0 locking errors эмпирически)
- **И НЕ ДОЛЖЕН** пытаться открыть DuckDB на NFS

#### Scenario: CLI и gateway используют один и тот же snapshot

- **КОГДА** запущены `cli_agent.py` (profile=test) и `gateway.py --profile=prod` одновременно с `gateway.enable_audit=True`
- **ТОГДА** оба процесса MUST резолвить `cache.duckdb` в один и тот же физический путь
- **И НЕ ДОЛЖНО** происходить race condition на DuckDB flock
- **И ДОЛЖНО** быть задокументировано в `docs/VECTOR_INDEXES.md`

#### Scenario: Профили не переопределяют gateway.cache.local_path

- **КОГДА** `profiles/test.jsonc` и `profiles/prod.jsonc` имеют разные значения `gateway.cache.local_path`
- **ТОГДА** ConfigurationResolver SHOULD reject это как ошибку конфигурации (или MUST применять только одно значение, default prod)
- **AND** CLI и gateway MUST всегда видеть один и тот же физический путь

## ADDED Requirements

### Requirement: CacheProvider с явным mode + реальный DuckDB read_only connection

`CacheProvider` MUST предоставлять API с явным параметром `mode: CacheAccessMode` (enum `READ_WRITE` или `READ_ONLY`).

`DuckDbCacheStore` MUST открывать DuckDB connection с реальным read-only режимом, когда `mode=READ_ONLY`:

```python
duckdb.connect(path, read_only=True)
```

Это — **первый уровень защиты**: сама DuckDB connection не позволяет INSERT/UPDATE/DELETE.

Дополнительно, `CacheProvider` MUST иметь **второй уровень защиты** (assertion guard): при попытке мутации через `CacheProvider.query_sql(...)` с `mode=READ_ONLY` MUST поднять `ReadOnlyAssertionError` или `PermissionError`. Это защищает код, который работает через `CacheProvider` API (даже если кто-то случайно пытается мутировать).

Режим определяется `CacheOwnershipCoordinator.try_claim(worker_id)` ДО открытия `CacheProvider`. См. `runtime/entrypoints`.

#### Scenario: CacheProvider.open(READ_ONLY) — реальный read-only connection

- **WHEN** `DuckDbCacheStore.open(path, mode=READ_ONLY)` вызван
- **THEN** DuckDB connection MUST быть создан через `duckdb.connect(path, read_only=True)`
- **AND** попытки INSERT/UPDATE/DELETE на уровне SQL MUST быть отклонены DuckDB (не `CacheProvider` assertion, а самой DuckDB)

#### Scenario: CacheProvider.open(READ_ONLY) — assertion guard

- **WHEN** `CacheProvider.open(mode=READ_ONLY)` вызван
- **THEN** любая попытка INSERT/UPDATE/DELETE через `CacheProvider.query_sql(...)` MUST поднять `ReadOnlyAssertionError` или `PermissionError`

#### Scenario: CacheProvider.open(READ_WRITE) разрешает мутации

- **WHEN** `CacheProvider.open(mode=READ_WRITE)` вызван
- **THEN** SELECT/INSERT/UPDATE/DELETE MUST работать нормально