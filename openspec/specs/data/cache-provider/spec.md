# CacheProvider (Провайдер кэша)

## Purpose

Определение контракта подсистемы кэширования данных: источник истины, жизненный цикл snapshot'ов, публичный API, атомарность обновлений и контракт потребителя. Кэш — локальный DuckDB-слой для быстрого доступа к read-mostly данным, синхронизированным из PostgreSQL.

## Scope

`platform` — подсистема целиком уехала из агента в capability `data`: локального DuckDB-кэша в `lib/services/` больше нет
Реализация: `mcp-platform/libs/enterprise_data/snapshot/store.py` (`DuckDbSnapshotStore`)

## Responsibility

CacheProvider отвечает за:

- предоставление SQL-кэша для read-mostly данных из PostgreSQL
- инкрементальную синхронизацию данных из PostgreSQL в DuckDB
- предоставление единого интерфейса доступа (`query_sql`, `get_schema`, `explain`, `search_vector`, `preload_indexes`, `check_stale`, `refresh`)
- управление snapshot'ами кэша (атомарная публикация)
- прогрев FAISS-индексов в память и выполнение vector search
- контроль целостности векторных индексов (`IndexIntegrityError`)

## Boundary

### Owns

- DuckDB-файлом кэша на локальном ext4 storage
- snapshot'ами таблиц из PostgreSQL
- FAISS-индексами, загружаемыми по требованию
- публичным API `CacheProvider` (ABC + DuckDB/FAISS-реализация)
- протоколом обнаружения stale/невалидных индексов через `IndexIntegrityError`

### Does Not Own

- бизнес-логикой интерпретации результатов (задача вызывающей стороны)
- прямым доступом Skills к DuckDB-файлу
- альтернативными vector storage backends (единственный backend — FAISS)
- NFS storage (явно запрещён)

### May Depend On

- PostgreSQL как источника истины
- конфигурации `gateway.cache.local_path` (путь к DuckDB)
- конфигурации `gateway.vector.*` (параметры эмбеддинга и индексов)
- `TableRegistry` для синхронизации таблиц PG → DuckDB

### Must Not Depend On

- NFS storage для файла кэша
- прямого доступа Skills к DuckDB-файлу
- конкретной реализации Skills

## Public Contract

`CacheProvider` (ABC в `lib/services/cache_provider.py`) предоставляет:

- `is_ready() -> bool` — готов ли кэш к запросам
- `refresh() -> bool` — создать/обновить SQL-кэш из PostgreSQL
- `check_stale() -> dict` — сверить метки изменений у таблиц кэша с источником
- `preload_indexes() -> list[dict]` — прогреть FAISS-индексы в память
- `search_vector(query, index_name, index_path, top_k, threshold) -> list[SearchResult]` — семантический поиск
- `query_sql(sql, params) -> dict` — выполнить SELECT-запрос к SQL-кэшу
- `explain(sql) -> dict` — EXPLAIN без выполнения
- `get_schema(schema_name, table_names) -> dict` — структура таблиц кэша
- `close()` — закрыть открытые ресурсы

Дополнительные типы:

- `SearchResult` — результат vector search (content, score, source, table, pk_value, chunk, matched_chunks, row, signature_status, signature_reason)
- `IndexIntegrityError` — векторный индекс не прошёл проверку signature (STALE/INVALID)

## Inputs

Разделы ниже описывают предмет **на стороне платформы**, куда он переехал
(см. `## Scope`): он в активном переписывании изменениями
`2026-10-04-close-cache-provider-canon-gap` и
`2026-10-05-vector-indexes-canon-gap`. Пути, которые эти изменения снимают
(агентские `gateway.cache.*`, `lib/services/cache_*`), здесь не утверждаются.

Хранилище не создаётся само — его открывает единственная фабрика
`open_snapshot_store(path, mode=READ_ONLY, *, schema="main", tables=None,
vector_db_table="", index_accessor=None, verify=True)`
(`libs/enterprise_data/snapshot/store.py`):

- `path` — **путь к файлу**, а не каталог; разбирается
  `resolve_snapshot_setting()` (`~` → домашний каталог). Пустое значение →
  не ошибка: сервер отдаёт `UnavailableSnapshot`, и операции со снимком
  отвечают `snapshot_unavailable`. NFS / SMB / сетевые ФС отвергаются
  `reject_unsupported_filesystem` **до** открытия файла;
- `mode` — `READ_ONLY` у читателя (обычное состояние процесса) и
  `READ_WRITE` у стадии пересоздания снимка;
- `vector_db_table` — `schema.table` векторного хранилища; без него векторных
  чтений нет;
- `index_accessor` — владелец FAISS-индексов (`libs/vectors`), а не сам
  снимок; без него `search_vector` отказывает с кодом `index_not_built`;
- `verify` — открыть файл сразу и проверить читаемость (fail-fast на старте).

Данные на вход: SQL и параметры вызова, имя схемы и перечень таблиц для
`get_schema`, аргументы векторного поиска (`query`, `index_name`, `top_k`,
`threshold`) и порция записей для стадии загрузки (`upsert_records`,
`replace_records`). Никаких путей, DSN и имён таблиц вызывающая сторона не
присылает: путь приходит из `platform.json → data.snapshot_path`, состав
таблиц объявляет capability `data`.

## Outputs

- `query_sql(sql, params)` → `{"status", "row_count", "columns", "rows"}` при
  успехе и `{"status": "error", ..., "error"}` при ошибке запроса — ошибка
  возвращается **значением**, чтобы отличать «запрос невалиден» от «снимок
  недоступен» (второе — исключение);
- `explain(sql)` → `{"valid": True, "plan": [...]}` либо
  `{"valid": False, "error": ...}` — синтаксическая проверка без выполнения;
- `get_schema(...)` → описание таблиц снимка;
- `search_vector(...)` → список `SearchResult`;
- `preload_indexes()` → список прогретых индексов; без `index_accessor` —
  пустой список, и это значит «владелец не подключён», а не «индексов нет»;
- `is_ready()` → признак готовности; `close()` — освобождение ресурсов;
- при недоступном снимке — `UnavailableSnapshot`, чей код (`cache_busy`,
  `cache_open_error`) доезжает до клиента без искажений.

Метода `publish()` нет: снимка «для читателей» не существует, загрузчик пишет
в файл напрямую. Наружу нормализованные словари и `SearchResult` — деталь
модуля, за которой потребитель операций платформы не обязан следить: он
работает с операциями capability `data`.

## Requirements

### Requirement: PostgreSQL — источник истины

Система ДОЛЖНА рассматривать PostgreSQL как единственный источник истины для всех кэшируемых таблиц.

#### Scenario: обновление данных

- **КОГДА** в PostgreSQL изменились строки кэшируемой таблицы
- **ТОГДА** `PgDuckDbSyncService` ДОЛЖЕН подхватить изменение (по track-колонке) и инкрементально обновить DuckDB-снапшот
- **И НЕ ДОЛЖЕН** использовать dual-write или иной механизм записи в обе БД одновременно

### Requirement: локальный ext4 storage

Система ДОЛЖНА хранить snapshot-файл только на локальной filesystem с поддержкой требуемых cache storage locking semantics (POSIX `fcntl` flock, etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён (NFS эмпирически fails with PID 0 errors на свежем файле). Конкретная FS не специфицируется (ext4 — Linux default, APFS — macOS, NTFS — Windows); термин «ext4» в заголовке requirement сохранён для backward compat с существующими ссылками в тестах и документации, но требование портативно на любую local FS.

Concrete cache storage MUST reject unsupported network/shared filesystem paths before opening the storage. Для текущей DuckDB implementation NFS/SMB и другие network/shared filesystems MUST быть rejected. Для будущих реализаций правила аналогичны (storage без локального filesystem locking semantics недопустим).

Snapshot-путь MUST быть единым для всех процессов:

- `cache.duckdb` — единый runtime-resource, открывается в `ApplicationContext.create()` для `role="gateway"` и `role="cli"` в режиме, определяемом `CacheOwnershipCoordinator`.
- Никаких role-based путей. Параметр `role` в `resolve_publish_path(role)` сохранён для backward compat, но `role="cli"` и `role="gateway"` MUST возвращать **`<local_path>/cache.duckdb`**.

**`gateway.cache.local_path` MUST быть shared runtime resource**, не profile-specific value. Если CLI работает с `profile="test"`, а gateway с `profile="prod"` — оба процесса MUST резолвить snapshot в один и тот же физический путь. Профили НЕ ДОЛЖНЫ переопределять `gateway.cache.local_path`.

**Cache lifecycle MUST быть отделён от `gateway.enable_audit`.** Cache runtime (`CacheProvider`, concrete implementation, `CacheOwnershipCoordinator`) создаётся, если `gateway.cache` секция настроена (наличие `gateway.cache.local_path`). `gateway.enable_audit` MUST NOT определять существование cache — он контролирует ТОЛЬКО audit sync (`CacheSyncService`).

Режим открытия (`READ_WRITE` или `READ_ONLY`) MUST определяться через `CacheOwnershipCoordinator.try_claim(worker_id)` ДО создания `CacheProvider`. См. подробный контракт в `runtime/entrypoints`.

Skills (`audit_analyzer`, `legal_summarizer`) MUST открывать cache через `CacheProvider` (без `role`-based path), читать свежий snapshot независимо от того, какой процесс является owner'ом.

#### Scenario: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount или другую network/shared filesystem
- **ТОГДА** concrete cache storage MUST reject путь ДО открытия cache с явной ошибкой (PID 0 locking errors эмпирически)

#### Scenario: CLI и gateway используют один и тот же snapshot

- **КОГДА** запущены `cli_agent.py` (profile=test) и `gateway.py --profile=prod` одновременно
- **ТОГДА** оба процесса MUST резолвить cache в один и тот же физический путь

#### Scenario: Профили не переопределяют gateway.cache.local_path

- **КОГДА** `profiles/test.jsonc` и `profiles/prod.jsonc` имеют разные значения `gateway.cache.local_path`
- **ТОГДА** ConfigurationResolver MUST reject это как ошибку конфигурации

#### Scenario: enable_audit=False НЕ отключает cache

- **КОГДА** `gateway.enable_audit=False`, но `gateway.cache` секция настроена
- **ТОГДА** `CacheProvider` MUST быть создан (cache runtime существует)
- **AND** `CacheSyncService` MUST NOT быть создан (sync отключён)
- **AND** Skills (`audit_analyzer`, `legal_summarizer`) MUST иметь доступ к cache через `CacheProvider`
- **AND** если процесс получил ownership cache resource → `READ_WRITE` access НЕЗАВИСИМО от `enable_audit` (другие runtime-компоненты MAY выполнять cache mutations через `CacheProvider`)

### Requirement: единый интерфейс доступа

Система ДОЛЖНА предоставлять доступ к кэшу только через `CacheProvider`. Прямой доступ к DuckDB-файлу из кода Skills запрещён.

#### Scenario: Skill запрашивает данные

- **КОГДА** Skill нуждается в SQL-запросе к кэшу
- **ТОГДА** он ДОЛЖЕН вызвать `CacheProvider.query_sql()` (или другой метод интерфейса)
- **И НЕ ДОЛЖЕН** открывать DuckDB-файл напрямую

### Requirement: vector search только через `CacheProvider.search_vector`

Система ДОЛЖНА выполнять vector search исключительно через `CacheProvider.search_vector`. Прямая загрузка FAISS-индексов из Skills запрещена.

#### Scenario: Skill выполняет vector search

- **КОГДА** Skill нуждается в vector similarity query
- **ТОГДА** он ДОЛЖЕН вызвать `CacheProvider.search_vector` с указанием `index_name`
- **И НЕ ДОЛЖЕН** открывать FAISS-файлы напрямую

### Requirement: контроль целостности индексов

Система ДОЛЖНА проверять signature индекса (модель эмбеддингов, размерность, колонки, chunk-параметры) перед использованием и поднимать `IndexIntegrityError` при несовпадении.

#### Scenario: stale индекс

- **КОГДА** сигнатура сохранённого индекса не совпадает с текущей конфигурацией
- **ТОГДА** `search_vector` ДОЛЖЕН поднять `IndexIntegrityError` со статусом `STALE` или `INVALID`
- **И НЕ ДОЛЖЕН** возвращать «тихую» деградацию результатов

### Requirement: Storage implementation isolation

Конкретный тип локального cache-хранилища является implementation detail. Нормативные runtime-контракты НЕ ДОЛЖНЫ использовать конкретное имя или API текущей storage implementation, кроме разделов, описывающих соответствующий concrete adapter.

Компоненты, которые MUST NOT зависеть от `DuckDbCacheStore` (или любого другого concrete имени):

- `AgentLoop`
- Skills
- Tools
- `CacheSyncService`
- `CacheOwnershipCoordinator`
- Runtime consumers `CacheProvider`

`DuckDbCacheStore` MAY фигурировать только в:
- Concrete adapter specification
- Composition root (`ApplicationContext.create`)
- Configuration/factory
- Tests, проверяющих DuckDB-specific behavior

Замена `DuckDbCacheStore` на другую реализацию `CacheProvider` (например, `SQLiteCacheStore`) НЕ ДОЛЖНА требовать изменений в `AgentLoop`, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator` или других runtime consumers `CacheProvider`. Изменения MAY потребоваться только в concrete adapter, composition root, configuration/factory и integration tests конкретной реализации.

#### Scenario: Замена concrete cache implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется (например, `DuckDbCacheStore` → `SQLiteCacheStore`)
- **THEN** `AgentLoop` MUST NOT require changes
- **AND** Skills MUST NOT require changes
- **AND** Tools MUST NOT require changes
- **AND** `CacheSyncService` MUST NOT require changes
- **AND** `CacheOwnershipCoordinator` MUST NOT require changes
- **AND** изменения MAY потребоваться только в: concrete adapter, composition root, configuration/factory, integration tests конкретной реализации

#### Scenario: runtime-consumer код не импортирует concrete cache implementation

- **WHEN** проверяется `AgentLoop`, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator` на импорт concrete cache class
- **THEN** НЕ ДОЛЖНО быть импортов `DuckDbCacheStore` (или любой другой concrete реализации)
- **AND** эти компоненты работают только через `CacheProvider` интерфейс

### Requirement: CacheProvider как интерфейс без конкретной СУБД

`CacheProvider` MUST быть runtime-интерфейсом доступа к cache-хранилищу. `CacheProvider` MUST NOT:

- знать имя или тип конкретной СУБД;
- содержать DuckDB-specific или SQLite-specific API в публичных методах;
- управлять ownership PostgreSQL resource (это `CacheOwnershipCoordinator`);
- самостоятельно выполнять ownership takeover;
- создавать concrete storage implementation.

`CacheProvider` НЕ ИМЕЕТ метода `open()`. Concrete factory (например, `DuckDbCacheStore.open(path, mode)` или эквивалентный для другой реализации) вызывается composition root'ом `ApplicationContext`.

#### Scenario: Замена concrete implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется (например, `DuckDbCacheStore` → `SQLiteCacheStore`)
- **THEN** `CacheOwnershipCoordinator` MUST NOT require changes
- **AND** `CacheSyncService` MUST NOT require changes
- **AND** runtime consumers MUST NOT require changes

#### Scenario: runtime-consumer код не импортирует concrete cache implementation

- **WHEN** проверяется `AgentLoop`, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator` на импорт concrete cache class
- **THEN** НЕ ДОЛЖНО быть импортов `DuckDbCacheStore` (или любой другой concrete реализации)
- **AND** эти компоненты работают только через `CacheProvider` интерфейс

### Requirement: CacheOwnershipCoordinator как абстрагированный ownership

`CacheOwnershipCoordinator` MUST отвечать за ownership общего **логического** cache resource. Coordinator MUST NOT зависеть от конкретной реализации локального cache-хранилища.

Ownership определяется для одного логического cache resource, который может быть реализован DuckDB, SQLite или другой локальной реализацией.

Coordinator отвечает только за:
- `try_claim()` (atomic claim)
- `heartbeat()`
- `release()`
- `acquire_write_fence()` (PG advisory lock для fencing)
- проверку текущего owner (через `ClaimResult.current_owner_id` / `current_generation`)
- generation (fencing token)

Coordinator MUST NOT выполнять операций чтения или записи cache-хранилища.

#### Scenario: Замена concrete cache implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется
- **THEN** `CacheOwnershipCoordinator` MUST NOT require changes
- **AND** runtime consumers MUST NOT require changes
- **AND** изменения MAY потребоваться только в: concrete `CacheProvider` implementation, composition root, configuration/factory, integration tests конкретной реализации

#### Scenario: Coordinator не делает cache I/O

- **WHEN** `CacheOwnershipCoordinator` выполняет любую операцию (`try_claim`, `heartbeat`, `release`, `acquire_write_fence`)
- **THEN** он НЕ ДОЛЖЕН делать read/write в cache storage
- **AND** он работает только с PostgreSQL `agent_cache_ownership` table

### Requirement: query_sql mode semantics

`query_sql()` MUST принимать **только следующие SQL statement types**: `SELECT`, `INSERT`, `UPDATE`, `DELETE`. DDL statements MUST быть отклонены в любом режиме.

DDL включает как минимум: `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `CREATE INDEX`, `DROP INDEX`, и эквивалентные schema-changing statements.

In `READ_ONLY` режиме:
- `SELECT` MUST выполняться нормально.
- `INSERT`/`UPDATE`/`DELETE` MUST поднимать `ReadOnlyAssertionError` **до выполнения**.

In `READ_WRITE` режиме:
- `SELECT`/`INSERT`/`UPDATE`/`DELETE` MUST выполняться нормально.

#### Scenario: query_sql() отклоняет DDL в любом mode

- **WHEN** `query_sql("CREATE TABLE ...")` или `query_sql("DROP TABLE ...")` или `query_sql("ALTER TABLE ...")` или `query_sql("TRUNCATE TABLE ...")` или `query_sql("CREATE INDEX ...")` или `query_sql("DROP INDEX ...")` вызван (в любом mode)
- **THEN** MUST поднять `UnsupportedSqlError` или эквивалентную dedicated validation error (НЕ `ReadOnlyAssertionError`, поскольку DDL запрещён даже в READ_WRITE)

#### Scenario: query_sql() в READ_WRITE принимает DML

- **WHEN** CacheProvider в mode=READ_WRITE и `query_sql("INSERT INTO ...")` или `query_sql("UPDATE ...")` или `query_sql("DELETE ...")`
- **THEN** операция MUST выполниться нормально

#### Scenario: query_sql() в READ_ONLY блокирует DML

- **WHEN** CacheProvider в mode=READ_ONLY и `query_sql("INSERT INTO ...")`
- **THEN** MUST поднять `ReadOnlyAssertionError` до выполнения

#### Scenario: query_sql() в READ_ONLY принимает SELECT

- **WHEN** CacheProvider в mode=READ_ONLY и `query_sql("SELECT ...")`
- **THEN** операция MUST выполниться нормально

### Requirement: CacheAccessMode и двухуровневая защита

`CacheAccessMode` — абстрактный enum: `READ_WRITE` или `READ_ONLY`. Ownership определяет режим:

- `ClaimResult.acquired=True` → `READ_WRITE`
- `ClaimResult.acquired=False` → `READ_ONLY`

Concrete adapter (например, `DuckDbCacheStore`) сам реализует, как открыть своё хранилище в этих режимах.

В `READ_ONLY` режиме все мутации MUST быть запрещены через **двухуровневую защиту**:

1. **Concrete adapter MUST открыть storage connection в реальном read-only режиме** (например, для DuckDB: `duckdb.connect(path, read_only=True)`). Сам storage engine не позволит мутации.
2. **`CacheProvider.query_sql(...)` MUST поднять `ReadOnlyAssertionError`** при INSERT/UPDATE/DELETE.

Оба уровня защиты MUST присутствовать одновременно (defense in depth).

#### Scenario: Concrete adapter открывает storage в реальном read_only режиме

- **WHEN** CacheProvider создан в mode=READ_ONLY (например, `DuckDbCacheStore.open(path, mode=READ_ONLY)`)
- **THEN** concrete adapter MUST открыть storage connection в реальном read-only режиме (для DuckDB: `duckdb.connect(path, read_only=True)`)
- **AND** попытки INSERT/UPDATE/DELETE на уровне SQL MUST быть отклонены storage engine

#### Scenario: CacheProvider поднимает ReadOnlyAssertionError при INSERT/UPDATE/DELETE в READ_ONLY

- **WHEN** CacheProvider в mode=READ_ONLY
- **AND** через `CacheProvider.query_sql(...)` вызывается INSERT/UPDATE/DELETE
- **THEN** MUST поднять `ReadOnlyAssertionError` до выполнения

#### Scenario: query_sql() отклоняет DDL в любом mode

- **WHEN** вызов `query_sql("CREATE TABLE ...")` или `query_sql("DROP TABLE ...")` (в любом mode)
- **THEN** MUST поднять `UnsupportedSqlError` (DDL запрещён даже в READ_WRITE)

## Forbidden Behavior

Система НЕ ДОЛЖНА:

- записывать DuckDB-файл кэша напрямую на NFS mount (эмпирически fails with `PID 0` locking errors)
- вводить второе хранилище кэша помимо `cache_provider.py`
- молча fallback на PostgreSQL при невалидном кэше; потребители ДОЛЖНЫ быть уведомлены
- обходить `CacheProvider` из кода Skills
- дублировать состояние кэша вне единственного пути к файлу кэша
- открывать FAISS-индексы из кода Skills напрямую
- создавать альтернативный vector storage backend рядом с FAISS без явного OpenSpec change

## Dependencies

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `lib/services/cache_provider.py:CacheProvider` — ABC интерфейс
- `lib/services/cache_provider_impl.py` — DuckDB + FAISS реализация
- `lib/services/duckdb_cache_store.py:DuckDbCacheStore` — низкоуровневый слой DuckDB
- `lib/services/pg_duckdb_sync_service.py:PgDuckDbSyncService` — инкрементальный sync PG → DuckDB
- `lib/services/vector_index_service.py:VectorIndexService` — сборка FAISS-индексов
- `lib/services/table_registry.py:TableRegistry` — реестр таблиц для синхронизации
- `tools/build_vectors.py` — CLI для сборки FAISS-индексов

## Configuration

- `gateway.cache.local_path` — путь к локальному DuckDB-файлу (ext4, НЕ NFS).
- `gateway.vector.index.*` — параметры FAISS-индексов (см. `openspec/specs/data/vector-indexes/spec.md`).
- `gateway.vector.index.storage_table` — PG-таблица для хранения эмбеддингов (формат `schema.table`, например `oarb.audit_vectors`).

## Lifecycle

1. **Инициализация**: проверка пути к хранилищу (fail-fast на NFS).
2. **Refresh**: `CacheProvider.refresh()` создаёт/обновляет SQL-кэш из PostgreSQL.
3. **Background sync**: `PgDuckDbSyncService` инкрементально подтягивает изменения по track-колонкам.
4. **Preload**: `preload_indexes()` прогревает FAISS-индексы в память.
5. **Обслуживание**: обработка запросов `query_sql` / `search_vector` / `get_schema` / `explain`.
6. **Stale check**: `check_stale()` сверяет метки изменений.
7. **Закрытие**: `close()` освобождает ресурсы (соединения, файлы).

## State

CacheProvider хранит:

- DuckDB-файл кэша по пути `gateway.cache.local_path`.
- Snapshot'ы таблиц PostgreSQL.
- FAISS-индексы, загруженные в память (`preload_indexes`).
- Кэш сигнатур индексов для контроля целостности.

## Invariants

- PostgreSQL — источник истины для кэшируемых таблиц.
- Кэш хранится только на локальном ext4 storage.
- Единый интерфейс доступа через `CacheProvider`.
- Все векторные индексы FAISS-backed.
- Сигнатура индекса проверяется перед каждым использованием.

## Data Ownership

Владеет:

- **файлом снимка DuckDB** — тем самым путём, который объявлен в
  `platform.json → data.snapshot_path`. Путь объявляет платформа, агент его не
  вычисляет и не присылает;
- содержимым снимка в пределах стадии загрузки: `upsert_records` и
  `replace_records` принадлежат загрузчику capability `data`, который и
  является **единственным писателем** этого файла;
- открытием файла на время вызова: соединение не удерживается между
  операциями, а читать может только проверенное хранилище (`_is_ready`).

Не владеет:

- FAISS-индексами: они принадлежат capability `vectors`
  (`libs/vectors`), а снимок получает к ним доступ только через
  `VectorIndexAccessor`. «Прогрев индексов» в этом слое — делегация, а не
  владение;
- PostgreSQL: база остаётся источником истины, загрузка идёт отдельной
  стадией, а файла снимка в базе не существует;
- жизненным циклом снимка для читателей: атомарной публикации нет,
  «снимок для читателей» не создаётся, и метод `publish()` отсутствует;
- владением файлом между процессами и fencing-логикой — слой владения снят
  (change `drop-local-cache-read-from-pg`): в системе один gateway, writer
  один и известен заранее.

## Error Behavior

- **NFS path**: fail fast при старте с явной ошибкой.
- **Ошибка синхронизации**: логирование, кэш остаётся со stale данными до следующей успешной синхронизации (явная retry-политика `PgDuckDbSyncService`).
- **Ошибка запроса**: возврат ошибки потребителю; молчаливый fallback на PostgreSQL запрещён.
- **Stale/invalid index**: `IndexIntegrityError` с статусом `STALE`/`INVALID` и описанием `reason`; вызывающая сторона обязана обработать (например, пересобрать индекс через `tools/build_vectors.py`).
- **Config missing**: fail fast при старте (`ConfigurationError`).

## Consumers

- Skills (через `CacheProvider`) — SQL-запросы и vector search.
- `VectorIndexService` (через `search_vector`).
- Инфраструктурные сервисы (`ApplicationContext` для refresh/preload).
- `tools/build_vectors.py` — сборка FAISS-индексов.
- Тесты (`tests/test_duckdb_cache_store.py`, `tests/test_pg_duckdb_sync_service.py`).

## Implementation

Все пути ниже — от корня репозитория. Кластер локального кэша агента **снят**
(change `drop-local-cache-read-from-pg`, фаза 5 миграции
`enterprise-mcp-platform`, 2026-10-01), поэтому его файлы перечислены как
**снятые**: ни одного из них в репозитории нет, а предмет живёт в capability
`data` и `vectors` платформы.

Снято в дереве агента, где предмет живёт теперь:

- `lib/services/cache_provider.py` (`CacheProvider`, ABC) — **снят**; контракт
  переехал на платформу:
  `mcp-platform/libs/enterprise_data/snapshot/contracts.py:195` (`CacheProvider`),
  там же `:285` (`CacheIngestion`) и `:358` (`CacheStore`);
- `lib/services/cache_provider_impl.py` (общие помощники эмбеддингов и сигнатур,
  а не реализация интерфейса) — **снят**; помощники живут в
  `mcp-platform/libs/vectors/config.py:36` и
  `mcp-platform/libs/vectors/signature.py:38`;
- `lib/services/duckdb_cache_store.py` (`DuckDbCacheStore`) — **снят**;
  реализация переехала в
  `mcp-platform/libs/enterprise_data/snapshot/store.py:301`, где класс переименован
  в `DuckDbSnapshotStore`, а фабрика открытия — `open_snapshot_store`
  (`mcp-platform/libs/enterprise_data/snapshot/store.py:1414`); разбор пути —
  `resolve_snapshot_path`
  (`mcp-platform/libs/enterprise_data/snapshot/store.py:259`), отказ сетевой ФС
  до открытия файла — `reject_unsupported_filesystem`
  (`mcp-platform/libs/enterprise_data/snapshot/store.py:145`);
- `lib/services/pg_duckdb_sync_service.py` (`PgDuckDbSyncService`) — **снят**;
  загрузку снимка делает `mcp-platform/libs/enterprise_data/loader.py:149`
  (`SnapshotLoadService`): разовая синхронная загрузка, единственный писатель
  файла. Инкрементальный опрос, колбэки и периодический full-resync сняты вместе
  с машинерией, а не перенесены;
- `lib/services/vector_index_service.py` (`VectorIndexService`) — **снят**: FAISS-индексы
  принадлежат capability `vectors` (`mcp-platform/libs/vectors/builder.py:239`,
  `VectorBuilder`), а чтение их из снимка идёт через протокол
  `VectorIndexAccessor` (`mcp-platform/libs/enterprise_data/snapshot/store.py:274`);
- `lib/services/table_registry.py` (`TableRegistry`) — **снят**: состав снимка
  объявляет платформа, `mcp-platform/platform.json → audit.tables`;
- `lib/core/infra_registration.py` (`register_vector_storage`) — **снят**: состав
  индексов объявляет `mcp-platform/platform.json → vectors.indexes`;
- `tools/build_vectors.py` — **снят**: операция сборки индексов
  `mcp-platform/servers/enterprise/build_index.py`, сборщик —
  `mcp-platform/libs/vectors/builder.py:239`.

Связанные компоненты:

- `mcp-platform/platform.json` — `data.snapshot_path` (путь файла снимка),
  `audit.tables` (состав снимка), `vectors.indexes` (состав индексов)
- `docs/ARCHITECTURE.md` — описание реализации
- `docs/DATABASE.md` — слой данных и границы P0
- `docs/VECTOR_INDEXES.md` — детали vector-инфраструктуры

## Verification

Все пути ниже — от корня репозитория. Стражи переехали вместе с предметом в
`mcp-platform/tests/`, агентские тесты кэша сняты вместе с ним.

1. Границы capability: файл снимка принадлежит capability `data`, индексы строит
   и читает capability `vectors`
   (`mcp-platform/tests/test_architecture_boundaries.py`). Это то, чем заменена
   проверка «Skill не ходит в DuckDB/FAISS напрямую» — на стороне агента её
   больше носить нечему.
2. Контракт хранилища снимка: фабрика, `query_sql`, `explain`, `get_schema`,
   векторные чтения (`mcp-platform/tests/test_snapshot_store.py`) — порт
   `tests/test_duckdb_cache_store.py`, который снят.
3. Разовая загрузка снимка, отказ вместо тишины и пересоздание файла
   (`mcp-platform/tests/test_snapshot_load_service.py`,
   `mcp-platform/tests/test_load_snapshot_entry.py`). `tests/test_pg_duckdb_sync_service.py`
   снят вместе с инкрементальной синхронизацией, и покрытие удалено с машинерией,
   а не перенесено.
4. Контракт ABC и исключений снимка, включая `IndexIntegrityError`
   (`mcp-platform/tests/test_snapshot_contracts.py`).
5. Отказ сетевой ФС до открытия файла и правило «файл не удерживается между
   операциями» (`mcp-platform/tests/test_snapshot_no_file_hold.py`). Путь снимка
   в конфигурации объявляет платформа (`mcp-platform/platform.json → data.snapshot_path`);
   ключа `gateway.cache.local_path` в дереве агента нет — ни в `./config.json`,
   ни в `lib/`.
6. В дереве агента: `lib/services` и `lib/utils` не импортируют skills, не ветвятся
   по вызывающему и не хранят доменную схему по умолчанию
   (`tests/test_core_infrastructure_independence.py`). После снятия локального
   кэша этот страж про DuckDB и FAISS не проверяет ничего и не должен читаться
   как их страж.
