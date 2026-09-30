## REMOVED Requirements

### Requirement: CacheOwnershipCoordinator MUST определять режим cache ДО открытия

**Reason**: Требование задавало режим открытия кэша результатом claim'а в
PostgreSQL: `try_claim()` → `ClaimResult` → `READ_WRITE` при `acquired=True`,
`READ_ONLY` при `acquired=False`, и запрещало открывать кэш до получения claim'а.
Координация владения несколькими writer'ами не нужна: файл кэша открывается на
запись ровно один раз за жизненный цикл процесса — на стадии загрузки. Режим
определяется стадией жизненного цикла, а не claim'ом.

**Migration**: в `application_context._make_sync_services` удаляются создание
координатора, `coord.try_claim()`, логирование claim'а, выбор режима от claim'а
и ветка «READER, сборка не создаётся». Стадия загрузки открывает кэш с
`mode=CacheAccessMode.READ_WRITE`; все прочие стадии и потребители — с
`mode=CacheAccessMode.READ_ONLY`. Функция возвращает
`(CacheStore | None, None)`.

### Requirement: Ownership contract — atomic claim + real fencing через advisory lock

**Reason**: Требование закрепляло распределённый контракт владения: таблица
`agent_cache_ownership` с `resource_key='local_cache'`, монотонно растущую
`generation` как fencing token, heartbeat каждые 30 сек с TTL 60 сек, atomic
claim через `INSERT ... ON CONFLICT` и real fencing на
`pg_advisory_xact_lock(hashtext(resource_key))`.

Каждый элемент защищал от гонки между writer'ами: claim'а — «кто владелец, если
их несколько»; heartbeat'а — stale claim после падения владельца;
`generation` — устаревшего producer'а; advisory lock — mutual exclusion между
takeover и producer write. Writer'ов нет: кэш — снимок, загружаемый самим
процессом при старте.

Отдельно: heartbeat каждые 30 секунд — два запроса к PostgreSQL в минуту
бессрочно, в системе с общим пулом из четырёх соединений. Координатор сам
расходовал ресурс, ради экономии которого кэш существует.

**Migration**: удаляются `lib/services/cache_ownership.py` (438 строк) и
`sql/migrations/V005__create_agent_cache_ownership.sql`; fencing-обёртки
`_initial_load_with_fence`, `_sync_cycle_with_fence`, `_handle_ownership_lost`
и kw-only параметр `ownership_coordinator` убираются из
`pg_duckdb_sync_service.py`. `CacheBusyError` **сохраняется**: файл может быть
занят на запись другим процессом, и это штатная ошибка открытия, а не
недостающая координация.

## MODIFIED Requirements

### Requirement: CacheProvider API с явным mode + layered architecture

API MUST быть layered:

```text
CacheAccessMode                ← READ_WRITE / READ_ONLY (enum, cache_provider.py)
        ↓
CacheProvider (ABC)            ← ЧТЕНИЕ: query_sql / search_vector / get_schema /
                                 explain / preload_indexes / is_ready / close
        ↓
CacheIngestion (ABC)           ← ЗАПИСЬ: replace_records / ensure_schema
        ↓
CacheStore (ABC)               ← наследует обе роли; держит только сборщик
        ↓
Concrete implementation        ← текущая: DuckDbCacheStore
```

**Единственная точка создания провайдера** — `open_cache_provider(*, mode)`.
Прямой вызов concrete factory из любого другого места MUST NOT
существовать. Значение `mode` MUST определяться стадией жизненного цикла, а не
координацией владения: стадия загрузки кэша — `READ_WRITE`, все прочие стадии и
внешние потребители — `READ_ONLY`.

**`CacheProvider` MUST NOT иметь метода `open()`.** Это ответственность фабрики.

`CacheProvider` MUST NOT объявлять операций записи: навыки получают тип только
для чтения. Запись объявлена в `CacheIngestion` и доступна сборщику, который
держит `CacheStore`.

**`ApplicationContext` является composition root** и MAY использовать фабрику
для сборки текущей реализации `CacheProvider`. После создания runtime:

- `ApplicationContext.cache_provider` MUST иметь тип `CacheProvider`;
- runtime consumers (AgentLoop, Skills, Tools) MUST зависеть только от `CacheProvider`;
- concrete implementation MUST NOT использоваться как тип runtime dependency.

В `READ_ONLY` режиме все мутации (INSERT/UPDATE/DELETE) MUST быть запрещены через **двухуровневую защиту**:

1. **Concrete cache adapter MUST открывать storage connection в реальном read-only режиме** (для текущей реализации DuckDB: `duckdb.connect(path, read_only=True)`). Сам storage engine не позволит мутации.
2. **`CacheProvider.query_sql(...)` MUST поднять `ReadOnlyAssertionError`** при INSERT/UPDATE/DELETE.

`query_sql()` контракт: MUST принимать только следующие SQL statement types: `SELECT`, `INSERT`, `UPDATE`, `DELETE`. DDL и другие schema-changing statements (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `CREATE INDEX`, `DROP INDEX`) MUST быть отклонены с `UnsupportedSqlError` в любом mode (включая READ_WRITE).

Concrete cache storage MUST reject unsupported network/shared filesystem paths before opening the storage.

#### Scenario: Layered API — ApplicationContext хранит cache через CacheProvider

- **WHEN** `ApplicationContext.create()` создаёт cache runtime
- **THEN** `ctx.cache_provider` MUST быть типизирован как `CacheProvider` (ABC)
- **AND** `ctx.cache_provider` MUST быть создан через `open_cache_provider(*, mode)` — это composition-time code
- **AND** `ApplicationContext` MUST NOT содержать `DuckDbCacheStore` (или другую concrete implementation) как поле runtime consumer
- **AND** runtime consumers (AgentLoop, Skills, Tools) MUST зависеть только от `CacheProvider`

#### Scenario: Единственная точка создания провайдера

- **WHEN** выполняется поиск по репозиторию мест создания кэша
- **THEN** MUST существовать ровно одна функция создания — `open_cache_provider()`
- **AND** прямой вызов `DuckDbCacheStore.open(...)` из кода вне composition root MUST NOT существовать
- **AND** самостоятельное открытие файла кэша любым компонентом MUST NOT существовать

#### Scenario: Роль чтения не содержит операций записи

- **WHEN** навык получает доступ к кэшу
- **THEN** он MUST получать тип `CacheProvider`
- **AND** этот тип MUST NOT объявлять операций записи

#### Scenario: Concrete factory открывает READ_ONLY cache — реальный read-only connection

- **WHEN** `open_cache_provider(mode=READ_ONLY)` вызван
- **THEN** concrete adapter MUST открывать storage connection в реальном read-only режиме (для DuckDB: `duckdb.connect(path, read_only=True)`)
- **AND** попытки INSERT/UPDATE/DELETE на уровне SQL MUST быть отклонены storage engine

#### Scenario: Concrete factory открывает READ_ONLY cache — assertion guard

- **WHEN** `open_cache_provider(mode=READ_ONLY)` вызван
- **AND** через `CacheProvider.query_sql(...)` вызывается INSERT/UPDATE/DELETE
- **THEN** MUST поднять `ReadOnlyAssertionError`

#### Scenario: Concrete factory открывает READ_WRITE cache — мутации разрешены

- **WHEN** `open_cache_provider(mode=READ_WRITE)` вызван на стадии загрузки кэша
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

> Заголовок требования сохранён дословно из действующей спеки: дельта
> `MODIFIED` заменяет требование целиком, и архив отклоняет молчаливое удаление.
> Содержание переписано: постоянной службы синхронизации больше не существует.

Загрузка кэша MUST быть **синхронной разовой операцией**, выполняемой
composition root'ом при старте процесса. Фоновый поток, очередь задач и цикл
догоняющих изменений MUST NOT существовать.

```text
PostgreSQL ──► сборка (синхронный вызов) ──► CacheStore (READ_WRITE) ──► close()
```

Требования к механизму загрузки:

- загрузка MUST NOT порождать потоков и MUST NOT выполнять фоновых запросов к
  PostgreSQL после своего завершения;
- загрузка MUST использовать ровно один путь записи — полную загрузку всех
  зарегистрированных таблиц;
- инкрементальные операции записи (доли, догоняющие изменения) MUST NOT
  применяться;
- загрузка MUST брать слот общего пула соединений только на время своего
  выполнения и MUST освобождать его по завершении;
- привязка операций записи к роли `CacheIngestion` MUST выполняться **в одном
  месте** — в composition root. Дублирующая привязка колбэков в
  entrypoints-ах и standalone-инструментах MUST NOT существовать.

Загрузка MUST NOT зависеть от конкретной реализации `CacheProvider` и MUST
работать через роли `CacheProvider` / `CacheIngestion`.

#### Scenario: sync не может обойти fencing

- **GIVEN** процесс выполняет загрузку кэша
- **WHEN** выполняется mutation
- **THEN** mutation MUST проходить через стадию загрузки, открывшую кэш в `READ_WRITE`
- **AND** concrete cache implementation MUST NOT вызываться в обход этой стадии
- **AND** за пределами этой стадии mutation MUST быть отклонена режимом `READ_ONLY`

#### Scenario: ownership generation изменился до выполнения mutation

- **GIVEN** кэш загружен на стадии `READ_WRITE`
- **WHEN** стадия завершилась и соединение закрыто
- **THEN** последующие обращения к кэшу MUST открывать его только в `READ_ONLY`
- **AND** повторное открытие файла на запись MUST NOT выполняться до следующего перезапуска процесса
- **AND** фоновых обращений к PostgreSQL, отслеживающих изменения, MUST NOT быть

#### Scenario: CacheSyncService storage-agnostic

- **WHEN** выполняется загрузка кэша
- **THEN** она работает через роли `CacheProvider` / `CacheIngestion`, НЕ через `DuckDbCacheStore` (или другую concrete реализацию)
- **AND** замена concrete cache implementation НЕ требует изменений в механизме загрузки

#### Scenario: Загрузка не порождает фоновой работы

- **WHEN** загрузка кэша завершилась
- **THEN** процесс MUST NOT иметь фонового потока синхронизации
- **AND** MUST NOT выполнять периодических обращений к PostgreSQL
- **AND** слот общего пула соединений MUST быть освобождён

#### Scenario: Ровно один путь записи

- **WHEN** данные попадают в локальный кэш
- **THEN** это MUST происходить только через полную загрузку при старте
- **AND** инкрементальные пути записи MUST NOT использоваться
- **AND** MUST существовать ровно одно место в коде, где роли записи привязываются к сборщику

#### Scenario: Число потоков загрузки ограничено размером пула

- **WHEN** выполняется загрузка нескольких таблиц
- **THEN** число параллельных загрузок MUST NOT превышать
  `channels.postgres.pool.max_conn`
- **AND** причина ограничения MUST быть выражена в коде, а не следовать из
  совпадения числа таблиц с лимитом
- **AND** каждый поток загрузки MUST брать слот общего пула

#### Scenario: Загрузка фиксирует время снимка

- **WHEN** загрузка завершилась
- **THEN** время загрузки MUST быть сохранено
- **AND** MUST быть опубликовано ровно одним каналом — событием
  `event_type="cache_load_done"` с полем `payload.loaded_at` в
  `agent_gateway_logs`
- **AND** MUST NOT дублироваться в health-summary или в выдаче запроса,
  чтобы для одного факта не существовало двух источников истины
