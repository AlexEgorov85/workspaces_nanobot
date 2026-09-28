## MODIFIED Requirements

### Requirement: локальный ext4 storage

Система ДОЛЖНА хранить DuckDB-файл кэша только на локальном ext4 storage. NFS — явно запрещён. Snapshot-путь MUST быть единым для всех процессов:

- `cache.duckdb` — единый runtime-resource, открывается в `ApplicationContext.create()` для `role="gateway"` и `role="cli"`.
- Никаких role-based путей (`cli.duckdb`, `gateway.duckdb`). Параметр `role` в `resolve_publish_path(role)` сохранён для backward compat, но `role="cli"` и `role="gateway"` MUST возвращать **`<local_path>/cache.duckdb`**.

Владение синком (`PgDuckDbSyncService`) MUST определяться через PG-level ownership claim (см. `runtime/entrypoints`): первый захвативший claim процесс становится producer, остальные — consumer'ами. Никто из entrypoint'ов не назначен жёстко producer'ом.

Skills (`audit_analyzer`, `legal_summarizer`) MUST открывать `cache.duckdb` через `CacheProvider` (без `role`-based path), читать свежий snapshot независимо от того, какой процесс является producer'ом.

#### Scenario: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount
- **ТОГДА** старт `CacheProvider` ДОЛЖЕН fail-fast с явной ошибкой (PID 0 locking errors эмпирически)

#### Scenario: CLI и gateway используют один и тот же snapshot

- **КОГДА** запущены `cli_agent.py` и `gateway.py` одновременно с `gateway.enable_audit=True`
- **ТОГДА** `resolve_publish_path(role="cli")` MUST возвращать тот же путь, что и `resolve_publish_path(role="gateway")` — `<local_path>/cache.duckdb`
- **И НЕ ДОЛЖНО** происходить race condition на DuckDB flock
- **И ДОЛЖНО** быть задокументировано в `docs/VECTOR_INDEXES.md` или эквивалентном разделе