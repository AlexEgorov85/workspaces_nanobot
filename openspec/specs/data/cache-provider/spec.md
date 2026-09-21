# CacheProvider (Провайдер кэша)

## Назначение

Определение контракта подсистемы кэширования данных: источник истины, жизненный цикл snapshot'ов, публичный API, атомарность обновлений и контракт потребителя. Кэш — локальный DuckDB-слой для быстрого доступа к read-mostly данным, синхронизированным из PostgreSQL.

## Ответственность

CacheProvider отвечает за:

- предоставление SQL-кэша для read-mostly данных из PostgreSQL
- инкрементальную синхронизацию данных из PostgreSQL в DuckDB
- предоставление единого интерфейса доступа (`query_sql`, `get_schema`, `explain`, `search_vector`, `preload_indexes`, `check_stale`, `refresh`)
- управление snapshot'ами кэша (атомарная публикация)
- прогрев FAISS-индексов в память и выполнение vector search
- контроль целостности векторных индексов (`IndexIntegrityError`)

## Граница

### Владеет

- DuckDB-файлом кэша на локальном ext4 storage
- snapshot'ами таблиц из PostgreSQL
- FAISS-индексами, загружаемыми по требованию
- публичным API `CacheProvider` (ABC + DuckDB/FAISS-реализация)
- протоколом обнаружения stale/невалидных индексов через `IndexIntegrityError`

### Не владеет

- бизнес-логикой интерпретации результатов (задача вызывающей стороны)
- прямым доступом Skills к DuckDB-файлу
- альтернативными vector storage backends (единственный backend — FAISS)
- NFS storage (явно запрещён)

### Может зависеть от

- PostgreSQL как источника истины
- конфигурации `gateway.cache.local_path` (путь к DuckDB)
- конфигурации `gateway.vector.*` (параметры эмбеддинга и индексов)
- `TableRegistry` для синхронизации таблиц PG → DuckDB

### Не должен зависеть от

- NFS storage для файла кэша
- прямого доступа Skills к DuckDB-файлу
- конкретной реализации Skills

## Публичный контракт

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

## Требования

### Требование: PostgreSQL — источник истины

Система ДОЛЖНА рассматривать PostgreSQL как единственный источник истины для всех кэшируемых таблиц.

#### Сценарий: обновление данных

- **КОГДА** в PostgreSQL изменились строки кэшируемой таблицы
- **ТОГДА** `PgDuckDbSyncService` ДОЛЖЕН подхватить изменение (по track-колонке) и инкрементально обновить DuckDB-снапшот
- **И НЕ ДОЛЖЕН** использовать dual-write или иной механизм записи в обе БД одновременно

### Требование: локальный ext4 storage

Система ДОЛЖНА хранить DuckDB-файл кэша только на локальном ext4 storage. NFS — явно запрещён.

#### Сценарий: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount
- **ТОГДА** старт `CacheProvider` ДОЛЖЕН fail-fast с явной ошибкой (PID 0 locking errors эмпирически)

### Требование: единый интерфейс доступа

Система ДОЛЖНА предоставлять доступ к кэшу только через `CacheProvider`. Прямой доступ к DuckDB-файлу из кода Skills запрещён.

#### Сценарий: Skill запрашивает данные

- **КОГДА** Skill нуждается в SQL-запросе к кэшу
- **ТОГДА** он ДОЛЖЕН вызвать `CacheProvider.query_sql()` (или другой метод интерфейса)
- **И НЕ ДОЛЖЕН** открывать DuckDB-файл напрямую

### Требование: vector search только через `CacheProvider.search_vector`

Система ДОЛЖНА выполнять vector search исключительно через `CacheProvider.search_vector`. Прямая загрузка FAISS-индексов из Skills запрещена.

#### Сценарий: Skill выполняет vector search

- **КОГДА** Skill нуждается в vector similarity query
- **ТОГДА** он ДОЛЖЕН вызвать `CacheProvider.search_vector` с указанием `index_name`
- **И НЕ ДОЛЖЕН** открывать FAISS-файлы напрямую

### Требование: контроль целостности индексов

Система ДОЛЖНА проверять signature индекса (модель эмбеддингов, размерность, колонки, chunk-параметры) перед использованием и поднимать `IndexIntegrityError` при несовпадении.

#### Сценарий: stale индекс

- **КОГДА** сигнатура сохранённого индекса не совпадает с текущей конфигурацией
- **ТОГДА** `search_vector` ДОЛЖЕН поднять `IndexIntegrityError` со статусом `STALE` или `INVALID`
- **И НЕ ДОЛЖЕН** возвращать «тихую» деградацию результатов

## Запрещённое поведение

Система НЕ ДОЛЖНА:

- записывать DuckDB-файл кэша напрямую на NFS mount (эмпирически fails with `PID 0` locking errors)
- вводить второе хранилище кэша помимо `cache_provider.py`
- молча fallback на PostgreSQL при невалидном кэше; потребители ДОЛЖНЫ быть уведомлены
- обходить `CacheProvider` из кода Skills
- дублировать состояние кэша вне единственного пути к файлу кэша
- открывать FAISS-индексы из кода Skills напрямую
- создавать альтернативный vector storage backend рядом с FAISS без явного OpenSpec change

## Зависимости

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `lib/services/cache_provider.py:CacheProvider` — ABC интерфейс
- `lib/services/cache_provider_impl.py` — DuckDB + FAISS реализация
- `lib/services/duckdb_cache_store.py:DuckDbCacheStore` — низкоуровневый слой DuckDB
- `lib/services/pg_duckdb_sync_service.py:PgDuckDbSyncService` — инкрементальный sync PG → DuckDB
- `lib/services/vector_index_service.py:VectorIndexService` — сборка FAISS-индексов
- `lib/services/table_registry.py:TableRegistry` — реестр таблиц для синхронизации
- `tools/build_vectors.py` — CLI для сборки FAISS-индексов

## Конфигурация

- `gateway.cache.local_path` — путь к локальному DuckDB-файлу (ext4, НЕ NFS).
- `gateway.vector.index.*` — параметры FAISS-индексов (см. `openspec/specs/data/vector-indexes/spec.md`).
- `gateway.vector.index.storage_table` — PG-таблица для хранения эмбеддингов (формат `schema.table`, например `oarb.audit_vectors`).

## Жизненный цикл

1. **Инициализация**: проверка пути к хранилищу (fail-fast на NFS).
2. **Refresh**: `CacheProvider.refresh()` создаёт/обновляет SQL-кэш из PostgreSQL.
3. **Background sync**: `PgDuckDbSyncService` инкрементально подтягивает изменения по track-колонкам.
4. **Preload**: `preload_indexes()` прогревает FAISS-индексы в память.
5. **Обслуживание**: обработка запросов `query_sql` / `search_vector` / `get_schema` / `explain`.
6. **Stale check**: `check_stale()` сверяет метки изменений.
7. **Закрытие**: `close()` освобождает ресурсы (соединения, файлы).

## Состояние

CacheProvider хранит:

- DuckDB-файл кэша по пути `gateway.cache.local_path`.
- Snapshot'ы таблиц PostgreSQL.
- FAISS-индексы, загруженные в память (`preload_indexes`).
- Кэш сигнатур индексов для контроля целостности.

## Инварианты

- PostgreSQL — источник истины для кэшируемых таблиц.
- Кэш хранится только на локальном ext4 storage.
- Единый интерфейс доступа через `CacheProvider`.
- Все векторные индексы FAISS-backed.
- Сигнатура индекса проверяется перед каждым использованием.

## Поведение при ошибке

- **NFS path**: fail fast при старте с явной ошибкой.
- **Ошибка синхронизации**: логирование, кэш остаётся со stale данными до следующей успешной синхронизации (явная retry-политика `PgDuckDbSyncService`).
- **Ошибка запроса**: возврат ошибки потребителю; молчаливый fallback на PostgreSQL запрещён.
- **Stale/invalid index**: `IndexIntegrityError` с статусом `STALE`/`INVALID` и описанием `reason`; вызывающая сторона обязана обработать (например, пересобрать индекс через `tools/build_vectors.py`).
- **Config missing**: fail fast при старте (`ConfigurationError`).

## Потребители

- Skills (через `CacheProvider`) — SQL-запросы и vector search.
- `VectorIndexService` (через `search_vector`).
- Инфраструктурные сервисы (`ApplicationContext` для refresh/preload).
- `tools/build_vectors.py` — сборка FAISS-индексов.
- Тесты (`tests/test_duckdb_cache_store.py`, `tests/test_pg_duckdb_sync_service.py`).

## Реализация

Основная реализация:

- `lib/services/cache_provider.py:CacheProvider` (ABC)
- `lib/services/cache_provider_impl.py` — DuckDB + FAISS реализация интерфейса
- `lib/services/duckdb_cache_store.py:DuckDbCacheStore`
- `lib/services/pg_duckdb_sync_service.py:PgDuckDbSyncService`
- `lib/services/vector_index_service.py:VectorIndexService`

Связанные компоненты:

- `lib/services/table_registry.py:TableRegistry`
- `lib/core/infra_registration.py:register_vector_storage`
- `tools/build_vectors.py`
- `docs/ARCHITECTURE.md` — описание реализации
- `docs/DATABASE.md` — слой данных и границы P0
- `docs/VECTOR_INDEXES.md` — детали vector-инфраструктуры

## Проверка

Валидация включает:

1. Архитектурные тесты: проверка отсутствия прямого доступа к DuckDB/FAISS из Skills (`tests/test_core_infrastructure_independence.py`).
2. Тесты синхронизации: проверка актуальности данных кэша (`tests/test_pg_duckdb_sync_service.py`).
3. Тесты cache store: проверка контракта `DuckDbCacheStore` (`tests/test_duckdb_cache_store.py`).
4. Code review: проверка отсутствия NFS paths в конфигурации.
5. Тесты IndexIntegrityError: проверка контроля целостности индексов.
