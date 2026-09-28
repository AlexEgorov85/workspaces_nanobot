## MODIFIED Requirements

### Requirement: локальный ext4 storage

Система ДОЛЖНА хранить DuckDB-файл кэша только на локальной filesystem с поддержкой требуемой DuckDB locking semantics (POSIX `fcntl` flock, etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён (NFS эмпирически fails with PID 0 errors на свежем файле). Конкретная FS не специфицируется (ext4 — Linux default, APFS — macOS, NTFS — Windows; все поддерживают DuckDB locking); термин «ext4» в заголовке requirement сохранён для backward compat с существующими ссылками в тестах и документации, но требование портативно на любую local FS.

`CacheProvider` MUST reject путь, расположенный на NFS или другой network filesystem, ДО открытия DuckDB — fail-fast с явной ошибкой.

Snapshot-путь MUST быть единым для всех процессов:

- `cache.duckdb` — единый runtime-resource, открывается в `ApplicationContext.create()` для `role="gateway"` и `role="cli"` в режиме, определяемом `CacheOwnershipCoordinator`.
- Никаких role-based путей (`cli.duckdb`, `gateway.duckdb`). Параметр `role` в `resolve_publish_path(role)` сохранён для backward compat, но `role="cli"` и `role="gateway"` MUST возвращать **`<local_path>/cache.duckdb`**.

Режим открытия (`READ_WRITE` или `READ_ONLY`) MUST определяться через `CacheOwnershipCoordinator.try_claim(worker_id)` ДО создания `DuckDbCacheStore`. См. подробный контракт в `runtime/entrypoints`.

Skills (`audit_analyzer`, `legal_summarizer`) MUST открывать `cache.duckdb` через `CacheProvider` (без `role`-based path), читать свежий snapshot независимо от того, какой процесс является owner'ом.

#### Scenario: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount или другую network filesystem
- **ТОГДА** `CacheProvider` MUST reject путь ДО открытия DuckDB с явной ошибкой (PID 0 locking errors эмпирически)
- **И НЕ ДОЛЖЕН** пытаться открыть DuckDB на NFS

#### Scenario: CLI и gateway используют один и тот же snapshot

- **КОГДА** запущены `cli_agent.py` и `gateway.py` одновременно с `gateway.enable_audit=True`
- **ТОГДА** `resolve_publish_path(role="cli")` MUST возвращать тот же путь, что и `resolve_publish_path(role="gateway")` — `<local_path>/cache.duckdb`
- **И НЕ ДОЛЖНО** происходить race condition на DuckDB flock
- **И ДОЛЖНО** быть задокументировано в `docs/VECTOR_INDEXES.md` или эквивалентном разделе

## ADDED Requirements

### Requirement: CacheProvider с явным mode

`CacheProvider` MUST предоставлять API с явным параметром `mode: CacheAccessMode` (enum `READ_WRITE` или `READ_ONLY`). В `READ_ONLY` режиме все мутации (INSERT/UPDATE/DELETE) MUST быть запрещены.

Режим определяется `CacheOwnershipCoordinator.try_claim(worker_id)` ДО открытия `CacheProvider`. См. `runtime/entrypoints`.

#### Scenario: CacheProvider.open(READ_ONLY) запрещает мутации

- **WHEN** `CacheProvider.open(mode=READ_ONLY)` вызван
- **THEN** любая попытка INSERT/UPDATE/DELETE MUST поднять `ReadOnlyAssertionError` или `PermissionError`

#### Scenario: CacheProvider.open(READ_WRITE) разрешает мутации

- **WHEN** `CacheProvider.open(mode=READ_WRITE)` вызван
- **THEN** SELECT/INSERT/UPDATE/DELETE MUST работать нормально