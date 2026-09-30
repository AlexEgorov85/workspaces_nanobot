## REMOVED Requirements

### Requirement: CacheOwnershipCoordinator как абстрагированный ownership

**Reason**: Требование обосновывало `CacheOwnershipCoordinator` координацией
**нескольких writer'ов** одного локального файла кэша: claim в PostgreSQL,
heartbeat, `generation` как fencing token, advisory-lock fencing boundary и
таблица `agent_cache_ownership`. Кэш — снимок, пересоздаваемый при старте
процесса, поэтому writer у него один и он известен: это сам процесс сборки.
Каждый элемент координатора защищал от гонки, которой нет.

Отдельно: heartbeat каждые 30 секунд — это два запроса в PostgreSQL в минуту
бессрочно, в системе с общим пулом из четырёх соединений. Координатор сам
расходовал тот ресурс, ради экономии которого кэш существует.

**Migration**: `CacheAccessMode` (READ_WRITE / READ_ONLY) **сохраняется** —
стадии сборки нужен `READ_WRITE` — и переезжает в
`lib/services/cache_provider.py`, рядом с `CacheProvider`, чей режим
доступа он описывает. Удаляются `lib/services/cache_ownership.py` (438 строк)
и `sql/migrations/V005__create_agent_cache_ownership.sql`; fencing-обёртки
`_initial_load_with_fence`, `_sync_cycle_with_fence`, `_handle_ownership_lost`
и kw-only параметр `ownership_coordinator` убираются из
`pg_duckdb_sync_service.py`; claim, выбор режима по claim'у и передача
координатора убираются из `application_context._make_sync_services`, который
возвращает `(CacheStore, None)`. Импорты `CacheAccessMode` обновляются в
`duckdb_cache_store.py`, `skill_config.py`, `vector_index_service.py`,
`tools/check_indexes.py`, `tools/build_vectors.py` и тестах. Тест
`tests/test_cache_ownership_claim.py` удаляется.

## MODIFIED Requirements

### Requirement: PostgreSQL — источник истины

Система MUST рассматривать PostgreSQL как единственный источник истины (SHALL)
для всех кэшируемых таблиц. Локальный кэш производен от PostgreSQL и не
является вторым источником.

Данные ядра (`agent_gateway_logs`, `agent_question_runs`, сессии, сообщения)
локально **не кэшируются** и читаются из PostgreSQL напрямую.

Актуальность локальной копии обеспечивается **снимком**: при загрузке берётся
всё, что есть в PostgreSQL в этот момент. Инкрементальное отслеживание
изменений MUST NOT применяться.

#### Scenario: обновление данных

- **КОГДА** в PostgreSQL изменились строки кэшируемой таблицы
- **ТОГДА** изменения MUST быть учтены следующей загрузкой кэша
- **И НЕ ДОЛЖЕН** использоваться dual-write или иной механизм записи в обе БД одновременно

#### Scenario: Данные ядра не кэшируются

- **КОГДА** потребителю нужны события журнала, question runs, сессии или сообщения
- **ТОГДА** он MUST читать их из PostgreSQL
- **И НЕ ДОЛЖЕН** получать их из локального кэша
- **AND** локальный кэш MUST NOT содержать таблиц данных ядра

### Requirement: локальный ext4 storage

Система MUST хранить snapshot-файл локального кэша только на локальной
filesystem с поддержкой cache storage locking semantics (POSIX `fcntl` flock,
etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён (NFS эмпирически
fails with PID 0 errors на свежем файле). Термин «ext4» в заголовке сохранён
для backward compat с существующими ссылками в тестах и документации, но
требование портативно на любую local FS.

Concrete cache storage MUST reject unsupported network/shared filesystem paths
до открытия storage.

**Назначение локального кэша — экономия соединений PostgreSQL.** Чтения
навыков MUST обслуживаться из локального кэша и MUST NOT расходовать слот
общего пула соединений. Кэш MUST NOT занимать слот пула в рабочем режиме:
единственный момент, когда кэш обращается к PostgreSQL, — загрузка при старте
процесса.

**Локальных кэшей ровно один.** Он содержит исключительно доменные данные
навыков и их векторное хранилище, и **не содержит данных ядра**.

**Кэш используется навыками только на чтение.** Навыки MUST получать тип
`CacheProvider`, который не объявляет операций записи; запись доступна только
роли `CacheIngestion`, которую держит сборщик. Это MUST быть структурным
свойством типов, а не соглашением.

Snapshot-путь MUST быть единым для всех процессов: и CLI, и gateway MUST
получать один и тот же путь. `gateway.cache.local_path` MUST быть shared
runtime resource, не profile-specific value.

**Cache lifecycle MUST быть отделён от `gateway.enable_audit`.** Режим открытия
(`READ_WRITE` или `READ_ONLY`) MUST определяться стадией жизненного цикла:
`READ_WRITE` — только на стадии загрузки кэша при старте, `READ_ONLY` — во
всех остальных случаях.

#### Scenario: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount или другую network/shared filesystem
- **ТОГДА** concrete cache storage MUST reject путь ДО открытия cache с явной ошибкой (PID 0 locking errors эмпирически)

#### Scenario: Чтения навыков не расходуют пул PostgreSQL

- **КОГДА** навык выполняет запрос к доменным данным через `CacheProvider`
- **THEN** он MUST NOT обращаться к PostgreSQL
- **AND** MUST NOT занимать слот общего пула соединений
- **AND** чтение MUST обслуживаться из локального файла кэша

#### Scenario: Кэш не держит слот пула в рабочем режиме

- **WHEN** загрузка кэша завершена и процесс перешёл в рабочий режим
- **THEN** кэш MUST NOT удерживать слот общего пула соединений
- **AND** MUST NOT выполнять фоновых обращений к PostgreSQL
- **AND** единственным обращением к PostgreSQL MUST оставаться загрузка при старте

#### Scenario: CLI и gateway используют один и тот же snapshot

- **КОГДА** запущены `cli_agent.py` (profile=test) и `gateway.py --profile=prod` одновременно
- **ТОГДА** оба процесса MUST резолвить cache в один и тот же физический путь

#### Scenario: Профили не переопределяют gateway.cache.local_path

- **КОГДА** `profiles/test.jsonc` и `profiles/prod.jsonc` имеют разные значения `gateway.cache.local_path`
- **ТОГДА** ConfigurationResolver MUST reject это как ошибку конфигурации

#### Scenario: enable_audit=False НЕ отключает cache

- **КОГДА** `gateway.enable_audit=False`, но `gateway.cache` секция настроена
- **ТОГДА** `CacheProvider` MUST быть создан (cache runtime существует)
- **AND** Skills (`audit_analyzer`, `legal_summarizer`) MUST иметь доступ к cache через `CacheProvider`

#### Scenario: Навык не получает роль записи

- **WHEN** навык получает доступ к кэшу
- **THEN** он MUST получать объект типа `CacheProvider`
- **AND** тип MUST NOT объявлять операций записи
- **AND** роль `CacheIngestion` MUST NOT быть доступна коду навыков

### Requirement: Storage implementation isolation

Конкретный тип локального cache-хранилища является implementation detail.
Нормативные runtime-контракты MUST NOT использовать конкретное имя или API
текущей storage implementation, кроме разделов, описывающих соответствующий
concrete adapter.

Компоненты, которые MUST NOT зависеть от `DuckDbCacheStore` (или любого другого
concrete имени):

- `AgentLoop`
- Skills
- Tools
- Сборщик кэша
- Runtime consumers `CacheProvider`

`DuckDbCacheStore` MAY фигурировать только в:

- Concrete adapter specification
- Composition root (`ApplicationContext.create`)
- Configuration/factory
- Tests, проверяющих DuckDB-specific behavior

Замена `DuckDbCacheStore` на другую реализацию `CacheProvider` MUST NOT
требовать изменений в `AgentLoop`, Skills, Tools, сборщике кэша или других
runtime consumers `CacheProvider`.

#### Scenario: Замена concrete cache implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется (например, `DuckDbCacheStore` → `SQLiteCacheStore`)
- **THEN** `AgentLoop` MUST NOT require changes
- **AND** Skills MUST NOT require changes
- **AND** Tools MUST NOT require changes
- **AND** сборщик кэша MUST NOT require changes
- **AND** изменения MAY потребоваться только в: concrete adapter, composition root, configuration/factory, integration tests конкретной реализации

#### Scenario: runtime-consumer код не импортирует concrete cache implementation

- **WHEN** проверяется `AgentLoop`, Skills, Tools, сборщик кэша на импорт concrete cache class
- **THEN** НЕ ДОЛЖНО быть импортов `DuckDbCacheStore` (или любой другой concrete реализации)
- **AND** эти компоненты работают только через `CacheProvider` интерфейс

### Requirement: CacheProvider как интерфейс без конкретной СУБД

`CacheProvider` MUST быть runtime-интерфейсом **доступа на чтение** к
cache-хранилищу. `CacheProvider` MUST NOT:

- знать имя или тип конкретной СУБД;
- содержать DuckDB-specific или SQLite-specific API в публичных методах;
- объявлять операции записи;
- координировать владение ресурсом в PostgreSQL;
- создавать concrete storage implementation.

Операции записи MUST быть объявлены в отдельной роли `CacheIngestion`, а
`CacheStore` MUST наследовать обе роли. Код навыков MUST зависеть только от
`CacheProvider`.

`CacheProvider` НЕ ИМЕЕТ метода `open()`. Concrete factory вызывается
composition root'ом `ApplicationContext`.

#### Scenario: Замена concrete implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется (например, `DuckDbCacheStore` → `SQLiteCacheStore`)
- **THEN** сборщик кэша MUST NOT require changes
- **AND** runtime consumers MUST NOT require changes

#### Scenario: Роль записи отделена от роли чтения

- **WHEN** проверяется контракт доступа к кэшу
- **THEN** `CacheProvider` MUST NOT объявлять операций записи
- **AND** операции записи MUST быть объявлены в `CacheIngestion`
- **AND** `CacheStore` MUST наследовать обе роли

#### Scenario: runtime-consumer код не импортирует concrete cache implementation

- **WHEN** проверяется `AgentLoop`, Skills, Tools, сборщик кэша на импорт concrete cache class
- **THEN** НЕ ДОЛЖНО быть импортов `DuckDbCacheStore` (или любой другой concrete реализации)
- **AND** эти компоненты работают только через `CacheProvider` интерфейс

### Requirement: CacheAccessMode и двухуровневая защита

`CacheAccessMode` — абстрактный enum: `READ_WRITE` или `READ_ONLY`. Он
описывает **режим доступа**, а не результат координации владения, и объявлен
рядом с `CacheProvider` в `lib/services/cache_provider.py`.

Режим определяется стадией жизненного цикла:

- стадия загрузки кэша при старте → `READ_WRITE`;
- все прочие стадии и все потребители → `READ_ONLY`.

Единственность объявления MUST соблюдаться: определение `CacheAccessMode`
MUST существовать в единственном экземпляре. Два определения одного и того же
enum означают, что сравнение значения на границе модулей молча перестаёт
срабатывать, а защита `READ_ONLY` — молча перестаёт действовать.

Concrete adapter сам реализует, как открыть своё хранилище в этих режимах.

В `READ_ONLY` режиме все мутации MUST быть запрещены через **двухуровневую
защиту**:

1. **Concrete adapter MUST открывать storage connection в реальном read-only режиме** (например, для DuckDB: `duckdb.connect(path, read_only=True)`). Сам storage engine не позволит мутации.
2. **`CacheProvider.query_sql(...)` MUST поднять `ReadOnlyAssertionError`** при INSERT/UPDATE/DELETE.

Оба уровня защиты MUST присутствовать одновременно (defense in depth).

#### Scenario: Concrete adapter открывает storage в реальном read_only режиме

- **WHEN** CacheProvider создан в mode=READ_ONLY (например, `DuckDbCacheStore.open(path, mode=READ_ONLY)`)
- **THEN** concrete adapter MUST открывать storage connection в реальном read-only режиме (для DuckDB: `duckdb.connect(path, read_only=True)`)
- **AND** попытки INSERT/UPDATE/DELETE на уровне SQL MUST быть отклонены storage engine

#### Scenario: CacheProvider поднимает ReadOnlyAssertionError при INSERT/UPDATE/DELETE в READ_ONLY

- **WHEN** CacheProvider в mode=READ_ONLY
- **AND** через `CacheProvider.query_sql(...)` вызывается INSERT/UPDATE/DELETE
- **THEN** MUST поднять `ReadOnlyAssertionError` до выполнения

#### Scenario: query_sql() отклоняет DDL в любом mode

- **WHEN** вызов `query_sql("CREATE TABLE ...")` или `query_sql("DROP TABLE ...")` (в любом mode)
- **THEN** MUST поднять `UnsupportedSqlError` (DDL запрещён даже в READ_WRITE)

#### Scenario: CacheAccessMode определён в единственном экземпляре

- **GIVEN** в кодовой базе определён `CacheAccessMode`
- **WHEN** выполняется поиск определений этого типа по репозиторию
- **THEN** MUST существовать ровно одно определение
- **AND** все импорты `CacheAccessMode` MUST указывать на него

## ADDED Requirements

### Requirement: Базовый интерфейс доступа к кэшу

Система MUST предоставлять **единый базовый интерфейс** доступа к кэшу, и
конкретная реализация этого интерфейса MUST оставаться деталью реализации.

```text
        навык (отдельный процесс)                    рантайм
  ┌──────────────────────────────┐          ┌────────────────────────────┐
  │  CacheProvider  ← интерфейс  │          │  DuckDbCacheStore          │
  │  query_sql / search_vector   │  ──────► │  ← реализация интерфейса   │
  │  get_schema / preload_indexes│          │  работает с файлом кэша     │
  │  explain / is_ready / close  │          │  (DuckDB — не виден навыку) │
  └──────────────────────────────┘          └────────────────────────────┘
```

Навык MUST общаться с кэшем **только** через базовый интерфейс и MUST NOT
знать, какая СУБД лежит под ним. Конкретная реализация, имя файла кэша,
формат файла и язык конкретного хранилища MUST NOT просачиваться в код навыков,
в их документацию и в имена их функций.

Граница проходит **между процессами**: навык запускается отдельным процессом и
получает доступ к интерфейсу через фабрику, возвращающую тип интерфейса.
Прямое открытие файла кэша навыком запрещено.

Правило, однозначно разделяющее роли: `mode=READ_ONLY` возвращает
`CacheProvider` (только чтение), `mode=READ_WRITE` возвращает `CacheStore`
(чтение и запись). Запись недоступна потребителям по типу, а не по соглашению.

#### Scenario: Навык не знает реализацию

- **WHEN** проверяется код навыка, его документация и имена функций
- **THEN** в них MUST NOT встречаться имена конкретных реализаций
      (`DuckDbCacheStore`, `SQLiteCacheStore`)
- **AND** MUST NOT встречаться путь или имя файла кэша
- **AND** MUST NOT требоваться знание СУБД для корректной работы навыка

#### Scenario: Навык получает интерфейс, а не реализацию

- **WHEN** навык получает доступ к кэшу
- **THEN** он MUST получать объект типа `CacheProvider`
- **AND** смена конкретной реализации MUST NOT требовать изменений в навыке

#### Scenario: Роль определяется режимом

- **WHEN** запрошен режим `READ_ONLY`
- **THEN** MUST быть возвращён объект роли `CacheProvider`
- **WHEN** запрошен режим `READ_WRITE`
- **THEN** MUST быть возвращён объект роли `CacheStore`

#### Scenario: Замена реализации не затрагивает навыки

- **GIVEN** кэш реализован через базовый интерфейс
- **WHEN** concrete implementation заменяется (например, `DuckDbCacheStore` → `SQLiteCacheStore`)
- **THEN** код навыков MUST NOT require changes
- **AND** skill-контракт MUST NOT требовать релизов

### Requirement: локальный кэш не содержит данных ядра

Локальный кэш MUST содержать исключительно ресурсы, объявленные skill'ами и
зарегистрированные инфраструктурой (доменные таблицы навыка и таблица сырых
эмбеддингов).

Локальный кэш MUST NOT содержать таблиц данных ядра. Создание локального буфера
журнала, журнала выгрузки и флашера в PostgreSQL запрещено: журналирование
пишет в PostgreSQL напрямую и не имеет локальной копии.

#### Scenario: Состав кэша ограничен доменными ресурсами

- **WHEN** кэш загружается при старте
- **THEN** он MUST содержать только таблицы, зарегистрированные skill'ами и через infra-регистрацию
- **AND** ни одна из таблиц данных ядра MUST NOT попасть в кэш

#### Scenario: Журналирование не имеет локального буфера

- **WHEN** `DbLoggingService` записывает событие
- **THEN** запись MUST выполняться напрямую в PostgreSQL
- **AND** локальный буфер, журнал выгрузки и флашер MUST NOT существовать
- **AND** контракт `try_log_event` MUST остаться без изменений

### Requirement: Кэш — снимок на момент загрузки

Кэш навыков MUST загружаться полностью при каждом запуске процесса и MUST
представлять собой снимок состояния PostgreSQL, зафиксированный в момент
загрузки. Инкрементальное отслеживание изменений MUST NOT применяться.

Свежесть кэша определяется временем загрузки. Время последней загрузки MUST
публиковаться **одним** каналом: событием `event_type="cache_load_done"` с
полем `payload.loaded_at` в журнале `agent_gateway_logs`. Время загрузки MUST
NOT дублироваться в выдаче запроса и в health-summary: кэш — снимок, а не
живая проекция, поэтому повторение того же признака во втором месте создало бы
два источника истины для одного факта.

Загрузка требует доступного PostgreSQL. При его недоступности кэш MUST
оставаться пустым, а потребитель MUST получать явную ошибку отсутствия
данных, а не устаревшие значения.

#### Scenario: Полная загрузка на старте

- **WHEN** запускается процесс
- **THEN** кэш MUST быть загружен полностью из PostgreSQL
- **AND** его состав MUST соответствовать состоянию PostgreSQL на момент загрузки
- **AND** время загрузки MUST быть сохранено и опубликовано событием `cache_load_done`

#### Scenario: Изменения после загрузки не отслеживаются

- **WHEN** в PostgreSQL изменились данные после загрузки кэша
- **THEN** локальный кэш MUST NOT обновляться до следующей загрузки
- **AND** система MUST NOT выполнять фоновых обращений к PostgreSQL ради отслеживания изменений
- **AND** потребитель MUST иметь возможность узнать время загрузки из события `cache_load_done` и оценить актуальность данных

#### Scenario: PostgreSQL недоступен на старте

- **WHEN** процесс стартует при недоступном PostgreSQL
- **THEN** кэш MUST остаться пустым
- **AND** потребитель MUST получать явную ошибку отсутствия данных
- **AND** устаревшие значения MUST NOT подставляться молча

### Requirement: Процесс не удерживает файл кэша

Файл кэша MUST открываться на запись ровно один раз за жизненный цикл
процесса — на стадии загрузки — и MUST закрываться сразу после неё.

Чтение MUST открывать файл **на время операции** и закрывать сразу после
получения результата. Между операциями чтения процесс MUST NOT удерживать
файл открытым.

Основание: DuckDB допускает параллельное чтение и блокирует только writer
(проверено на DuckDB 1.5.4 двумя реальными процессами: RW-владелец с вторым RO
даёт `IOException`, два RO открываются одновременно). Поскольку writer'ов
после загрузки не остаётся, файл свободен всегда.

#### Scenario: Файл не удерживается между операциями

- **WHEN** процесс завершил загрузку кэша
- **THEN** файл MUST быть закрыт
- **AND** MUST NOT открываться на запись до следующего перезапуска процесса
- **AND** между операциями чтения процесс MUST NOT удерживать файл открытым

#### Scenario: Чтение открывает файл на время операции

- **WHEN** выполняется чтение из кэша
- **THEN** файл MUST открываться в read-only режиме на время операции
- **AND** MUST закрываться сразу после получения результата
- **AND** постоянное соединение MUST NOT удерживаться

#### Scenario: Файл свободен для внешнего процесса

- **WHEN** процесс работает с кэшем
- **THEN** внешний процесс MUST иметь возможность открыть файл кэша на чтение
- **AND** MUST NOT получать отказ из-за занятости файла процессом
