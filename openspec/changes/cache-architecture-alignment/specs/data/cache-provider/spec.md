## ADDED Requirements

### Requirement: Cache lifecycle MUST NOT depend on `gateway.enable_audit`

Cache runtime (`CacheProvider`, concrete implementation,
`CacheOwnershipCoordinator`) MUST создаваться при наличии
`gateway.cache` секции в `SETTINGS`, независимо от значения
`gateway.enable_audit`. `enable_audit` MUST контролировать
**только** audit sync (`CacheSyncService` / `PgDuckDbSyncService`),
не существование cache runtime.

Это уточнение существующего requirement #2 о cache lifecycle.

#### Scenario: enable_audit=False → cache runtime создан

- **WHEN** `SETTINGS["gateway"]["cache"]` настроен (есть
  `local_path`)
- **AND** `SETTINGS["gateway"]["enable_audit"]=False`
- **THEN** `ApplicationContext.create()` MUST создать
  `CacheProvider` instance
- **AND** MUST создать `CacheOwnershipCoordinator`
- **AND** MUST попытаться `try_claim()`
- **AND** MUST NOT создать `CacheSyncService` (sync гейтится
  `enable_audit`)

#### Scenario: enable_audit=True → cache runtime + sync оба созданы

- **WHEN** `SETTINGS["gateway"]["cache"]` настроен
- **AND** `SETTINGS["gateway"]["enable_audit"]=True`
- **AND** `ClaimResult.acquired=True`
- **THEN** `ApplicationContext.create()` MUST создать
  `CacheProvider`, `CacheOwnershipCoordinator`, И
  `CacheSyncService`

### Requirement: Producer fencing MUST охватывать initial load и sync callback

`CacheSyncService` (и его текущая реализация
`PgDuckDbSyncService`) MUST оборачивать в
`CacheOwnershipCoordinator.acquire_write_fence()` не только
инкрементальный polling, но также:

- `_do_initial_load()` (или эквивалентный initial sync при старте);
- `_fire_sync_callback()` (publish-событие, инициирующее `publish()`
  на cache).

Это закрывает TOCTOU race: takeover может произойти между
initial load'ом и polling cycle, и без fence старый producer запишет
данные после takeover'а.

#### Scenario: takeover во время initial load — mutation отменена

- **GIVEN** процесс A — OWNER с `generation=N`
- **WHEN** процесс A начинает `_do_initial_load()`
- **AND** в ходе initial load процесс B выполняет `try_claim()` →
  получает `generation=N+1` (A ещё не fence'd)
- **WHEN** процесс A пытается закоммитить мутации initial load
- **THEN** `acquire_write_fence()` MUST перепроверить ownership
- **AND** при `generation != N` MUST raise `OwnershipLostError`
- **AND** мутации MUST NOT применяться к `CacheProvider`

#### Scenario: publish во время takeover — callback отменён

- **GIVEN** процесс A — OWNER с `generation=N`, sync polling
  завершил мутацию через fence
- **AND** `_fire_sync_callback()` стартовал вне fence-блока
- **WHEN** takeover происходит до `_fire_sync_callback()`
- **THEN** `_fire_sync_callback()` MUST выполниться ВНУТРИ
  `acquire_write_fence()`
- **AND** при `generation != N` MUST NOT вызывать `CacheProvider.publish()`

### Requirement: CacheOwnershipCoordinator MUST запускать heartbeat при active ownership

`CacheOwnershipCoordinator.heartbeat()` MUST вызываться
автоматически в фоновом worker-thread'е, когда процесс является
OWNER (`try_claim().acquired=True`). Heartbeat MUST обновлять
`expires_at = NOW() + INTERVAL '60 seconds'` каждые 30 сек, пока
процесс — OWNER.

Heartbeat worker MUST останавливаться в `release()` или `stop()`
(graceful shutdown). Heartbeat MUST NOT запускаться для READER'ов
(`acquired=False`).

#### Scenario: heartbeat обновляет expires_at

- **GIVEN** процесс A — OWNER с `generation=N`
- **AND** worker-thread запущен
- **WHEN** проходит 30 сек
- **THEN** `agent_cache_ownership.expires_at` MUST быть обновлён
  (`UPDATE ... SET last_heartbeat_at = NOW(), expires_at = NOW() + INTERVAL '60 seconds' WHERE resource_key='local_cache' AND owner_id=A AND generation=N`)
- **AND** `agent_cache_ownership.generation` MUST остаться `N`
  (heartbeat НЕ инкрементирует generation)

#### Scenario: takeover после heartbeat expiry

- **GIVEN** процесс A — OWNER, heartbeat worker упал / процесс killed
- **WHEN** проходит `claim_ttl_seconds` (60 сек) без heartbeat
- **AND** процесс B вызывает `try_claim()`
- **THEN** B MUST получить `ClaimResult(acquired=True, generation=N+1)`
  (takeover allowed по expired claim)

#### Scenario: heartbeat не запускается для READER'а

- **GIVEN** процесс A получил `ClaimResult(acquired=False)`
- **THEN** heartbeat worker MUST NOT быть запущен
- **AND** `expires_at` для ресурса в PG MUST NOT обновляться от A

### Requirement: Network filesystem rejection работает на не-Linux платформах

Concrete cache adapter (`DuckDbCacheStore` / будущие реализации)
MUST проверять, что `cache.local_path` лежит на локальной
filesystem с поддержкой требуемых storage locking semantics
(POSIX `fcntl` flock или эквивалент) на **всех поддерживаемых
платформах** (Linux / macOS / Windows), а не только Linux.

#### Scenario: macOS / Windows — NFS path rejected

- **GIVEN** `platform.system() ∈ {"Darwin", "Windows"}`
- **WHEN** `gateway.cache.local_path` указывает на NFS / SMB share
- **THEN** concrete adapter MUST обнаружить сетевую FS (через
  `psutil.disk_partitions()` или эквивалент)
- **AND** MUST fail-fast с `UnsupportedFilesystemError` ДО открытия
  storage

#### Scenario: macOS / Windows — local FS path accepted

- **GIVEN** `platform.system() ∈ {"Darwin", "Windows"}`
- **AND** `gateway.cache.local_path` указывает на APFS / NTFS
- **WHEN** concrete adapter открывает storage
- **THEN** open MUST succeed без `UnsupportedFilesystemError`
