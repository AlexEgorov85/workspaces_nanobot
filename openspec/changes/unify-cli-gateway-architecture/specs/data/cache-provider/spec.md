## MODIFIED Requirements

### Requirement: локальный ext4 storage

Система ДОЛЖНА хранить DuckDB-файл кэша только на локальном ext4 storage. NFS — явно запрещён. Snapshot-путь MUST резолвиться через `resolve_publish_path(role)` с явным параметром роли:

- `role="gateway"` → `<local_path>/cache.duckdb`
- `role="cli"` → `<local_path>/cli.duckdb` (отдельный файл, чтобы избежать конфликта DuckDB flock при одновременной работе CLI и gateway)
- `role="utility"` → `<local_path>/cache.duckdb`

Решение о роли MUST быть явным в `ApplicationContext.create(role=...)` и НЕ ДОЛЖНО определяться runtime-проверкой «запущен ли другой процесс». CLI и gateway MUST писать в разные файлы, чтобы избежать race condition на DuckDB flock. Подробный контракт см. в `runtime/entrypoints`.

#### Scenario: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount
- **ТОГДА** старт `CacheProvider` ДОЛЖЕН fail-fast с явной ошибкой (PID 0 locking errors эмпирически)

#### Scenario: CLI и gateway пишут в разные snapshot-файлы

- **КОГДА** одновременно запущены `cli_agent.py` и `gateway.py` с `gateway.enable_audit=True`
- **ТОГДА** `resolve_publish_path(role="cli")` MUST возвращать путь, отличный от `resolve_publish_path(role="gateway")`
- **И НЕ ДОЛЖНО** происходить race condition на DuckDB flock
- **И ДОЛЖНО** быть задокументировано в `docs/VECTOR_INDEXES.md` или эквивалентном разделе