## MODIFIED Requirements

### Requirement: локальный ext4 storage

Система ДОЛЖНА хранить snapshot-файл только на локальной filesystem с поддержкой требуемых cache storage locking semantics (POSIX `fcntl` flock, etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён (NFS эмпирически fails with PID 0 errors на свежем файле). Конкретная FS не специфицируется (ext4 — Linux default, APFS — macOS, NTFS — Windows); термин «ext4» в заголовке requirement сохранён для backward compat с существующими ссылками в тестах и документации, но требование портативно на любую local FS.

Concrete cache storage MUST проверять локальность filesystem **на всех поддерживаемых платформах** (Linux / macOS / Windows), а не только на Linux, и MUST отклонять unsupported network/shared filesystem путь ДО открытия storage. Проверка выполняется детектирующим слоем файловой системы (например, `lib/utils/filesystem_prober.py`), а не предположением об ОС: проверка macOS/mount и Windows (`DRIVE_REMOTE`) обязательна.

Concrete cache storage MUST сообщать о неуспешном открытии типизированной ошибкой, различающей как минимум три причины: файл отсутствует, файл залочен другим владельцем, filesystem не поддерживается. Тихое подавление исключения с возвратом «не удалось открыть, продолжим без кэша» запрещено.

Snapshot-путь MUST быть единым для всех процессов:

- `cache.duckdb` — единый runtime-resource, открываемый composition root'ом в режиме, определяемом `CacheOwnershipCoordinator.try_claim()`.
- Никаких role-based путей. Функция `resolve_publish_path(workspace_path, cache_cfg)` MUST NOT принимать параметр способа запуска (role / entrypoint): у неё такого параметра нет, и добавление запрещено — один и тот же вызов MUST возвращать один и тот же путь независимо от того, кто вызвал.

**`gateway.cache.local_path` MUST быть shared runtime resource**, не profile-specific value. Если CLI работает с одним профилем, а gateway с другим — оба процесса MUST резолвить snapshot в один и тот же физический путь. Профили НЕ ДОЛЖНЫ переопределять `gateway.cache.local_path`.

**Cache lifecycle MUST быть отделён от `gateway.enable_audit`.** Cache runtime (`CacheProvider`, concrete implementation, `CacheOwnershipCoordinator`) создаётся, если `gateway.cache` секция настроена (наличие `gateway.cache.local_path`). `gateway.enable_audit` MUST NOT определять существование cache — он контролирует ТОЛЬКО audit sync (`CacheSyncService`).

Режим открытия (`READ_WRITE` или `READ_ONLY`) MUST определяться через `CacheOwnershipCoordinator.try_claim(worker_id)` ДО создания `CacheProvider`. Нормативный контракт ownership, heartbeat и producer fencing — в capability `data/cache-runtime-lifecycle`.

Доступ потребителей к данным MUST идти через `CacheProvider` — **это
единственный путь**. К файлу кэша обращаются runtime, skills и любые другие
компоненты одинаково; компонент, открывающий storage самостоятельно или
называющий concrete-класс хранилища, нарушает контракт. Способ доставки
данных в skill-процесс (in-process provider против отдельного процесса) этим
requirement НЕ определяется и отдельной capability не имеет.

#### Scenario: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount или другую network/shared filesystem
- **ТОГДА** concrete cache storage MUST reject путь ДО открытия cache с явной ошибкой (PID 0 locking errors эмпирически)

#### Scenario: macOS и Windows отклоняют сетевую FS

- **GIVEN** `platform.system()` — `Darwin` или `Windows`
- **AND** `gateway.cache.local_path` указывает на NFS/SMB share
- **WHEN** выполняется открытие storage
- **THEN** MUST быть выброшена `UnsupportedFilesystemError`
- **AND** открытие storage connection MUST NOT выполняться

#### Scenario: сетевая FS отклоняется, локальная принимается

- **GIVEN** `platform.system()` — `Darwin` или `Windows`
- **AND** путь указывает на локальную FS (APFS / NTFS)
- **WHEN** выполняется открытие storage
- **THEN** открытие MUST завершиться успешно, без `UnsupportedFilesystemError`

#### Scenario: CLI и gateway используют один и тот же snapshot

- **КОГДА** запущены `cli_agent.py` и `gateway.py` (возможно, с разными профилями)
- **ТОГДА** `resolve_publish_path(...)` MUST вернуть один и тот же путь
- **AND** функция MUST NOT принимать параметр role/entrypoint

#### Scenario: Профили не переопределяют gateway.cache.local_path

- **КОГДА** `profiles/test.jsonc` и `profiles/prod.jsonc` имеют разные значения `gateway.cache.local_path`
- **ТОГДА** ConfigurationResolver MUST reject это как ошибку конфигурации

#### Scenario: enable_audit=False НЕ отключает cache

- **КОГДА** `gateway.enable_audit=False`, но `gateway.cache` секция настроена
- **ТОГДА** `CacheProvider` MUST быть создан (cache runtime существует)
- **AND** `CacheSyncService` MUST NOT быть создан (sync отключён)
- **AND** если процесс получил ownership cache resource → `READ_WRITE` access НЕЗАВИСИМО от `enable_audit` (другие runtime-компоненты MAY выполнять cache mutations через `CacheProvider`)

#### Scenario: ошибка открытия файла диагностируема

- **WHEN** открытие `cache.duckdb` завершается неудачей
- **THEN** MUST быть выброшена типизированная ошибка, различающая «файл отсутствует», «файл залочен» и «filesystem не поддерживается»
- **AND** исключение MUST NOT быть подавлено с возвратом нейтрального значения

### Requirement: CacheProvider как интерфейс без конкретной СУБД

`CacheProvider` MUST быть runtime-интерфейсом доступа к cache-хранилищу. `CacheProvider` MUST NOT:

- знать имя или тип конкретной СУБД;
- содержать DuckDB-specific или SQLite-specific API в публичных методах;
- управлять ownership PostgreSQL resource (это `CacheOwnershipCoordinator`);
- самостоятельно выполнять ownership takeover;
- создавать concrete storage implementation.

`CacheProvider` НЕ ИМЕЕТ метода `open()`. Точка создания провайдера — **единственная
функция в слое интерфейса** (`open_cache_provider(*, mode) -> CacheProvider`),
которая разрешает путь через `resolve_publish_path()` и делегирует concrete-реализации.
Ни gateway, ни skills, ни tools, ни composition root MUST NOT называть concrete-класс
хранилища: тип возврата функции — `CacheProvider`, и какая стоит за ним реализация
вызывающему неизвестна.

**Полиморфизм ограничен data-access слоем.** Публичный контракт `CacheProvider` MUST состоять только из методов доступа к данным, единственной мутации и работы с ресурсами индекса: `query_sql`, `explain`, `get_schema`, `search_vector`, `preload_indexes`, `is_ready`, `upsert_records`, `close`. Репликация PostgreSQL → кэш (`refresh`, `check_stale`) в контракт **не входит**: это ответственность sync-слоя (`PgDuckDbSyncService`), а не свойство файла кэша, и оба метода не имеют ни одного production-вызова. В ABC MUST NOT входить и MUST NOT появляться в подклассах: `open()`, `open_cache()`, `refresh()`, `check_stale()`, `try_claim()`, `heartbeat()`, `acquire_write_fence()`, `release()`, `start_heartbeat()`, `stop_heartbeat()`, `start()`, `stop()`.

Обоснование запрета: подкласс, добавляющий метод открытия или владения, превращает ABC в hybrid «интерфейс данных + lifecycle/factory», из-за чего у composition root'а появляется несколько равноправных способов получить provider, а enforcement доступа к storage расходится между ветками. Открытие — ответственность concrete factory; владение — ответственность `CacheOwnershipCoordinator`.

#### Scenario: Замена concrete implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется (например, `DuckDbCacheStore` → `SQLiteCacheStore`)
- **THEN** `CacheOwnershipCoordinator` MUST NOT require changes
- **AND** `CacheSyncService` MUST NOT require changes
- **AND** runtime consumers MUST NOT require changes

#### Scenario: runtime-consumer код не импортирует concrete cache implementation

- **WHEN** проверяются `AgentLoop`, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator`, `ApplicationContext` и standalone-инструменты на импорт concrete cache class
- **THEN** НЕ ДОЛЖНО быть импортов `DuckDbCacheStore` (или любой другой concrete-реализации) вне модулей самой реализации и тестов, проверяющих её напрямую
- **AND** эти компоненты работают только через `CacheProvider` интерфейс

#### Scenario: у ABC нет метода открытия

- **WHEN** выполняется `inspect.getattr_static(CacheProvider, "open")`
- **THEN** MUST быть выброшен `AttributeError`

#### Scenario: репликация не входит в контракт интерфейса

- **WHEN** выполняется инвентаризация abstract-методов `CacheProvider`
- **THEN** `refresh` и `check_stale` MUST отсутствовать
- **AND** репликация PostgreSQL → кэш MUST оставаться ответственностью sync-слоя

#### Scenario: подкласс не добавляет lifecycle

- **GIVEN** класс `X`, наследующий `CacheProvider`
- **WHEN** проверяется наличие методов `open`, `open_cache`, `try_claim`, `heartbeat`, `acquire_write_fence`, `release`, `start_heartbeat`, `stop_heartbeat`
- **THEN** ни один из них MUST NOT быть объявлен в `X`

## ADDED Requirements

### Requirement: ровно одна concrete-реализация `CacheProvider` в runtime

В runtime MUST существовать ровно одна concrete-реализация `CacheProvider`. Второй provider, дублирующий `CacheProvider`, запрещён: дубликат расходится с основной реализацией по enforcement'у `query_sql`, по диагностике open-ошибок и по правилам доступа, и со временем становится неиспользуемым.

- `PostgresDuckDbProvider` MUST быть удалён вместе со своим дублем открытия файла; standalone-инструменты сборки индексов MUST получать провайдера из той же точки создания, что и runtime.
- Concrete-реализация `CacheProvider` MUST быть объявлена наследником `CacheProvider`; сегодня `DuckDbCacheStore` наследования не объявляет, а `ApplicationContext.cache_provider` типизирован как `Any` — интерфейс для runtime существует только в docstring'ах.
- `lib/core/skill_config.py::build_cache_provider` и skill-side обёртка MUST быть **сохранены** как тонкие делегаты в точку создания провайдера с возвращаемым типом `CacheProvider`: skill-side CLI сохраняет точку входа (`docs/skill-tool-architecture.md` §8) и MUST иметь способ получить `CacheProvider`. Их удаление оставляло бы CLI без доступа к кэшу.
- Build-слой, не имеющий ни одного production-вызывающего, MUST быть удалён, а не сохранён «на будущее»; embedding-хелпер, который он реэкспортировал, MUST быть перенесён или сохранён по месту реального использования.

Нарушение этого требования — не stylistic issue, а расхождение enforcement'а: при `READ_ONLY` основная реализация поднимает `ReadOnlyAssertionError` до выполнения, а дубликат полагается только на storage engine.

#### Scenario: в репозитории один provider

- **WHEN** выполняется инвентаризация классов, наследующих `CacheProvider`
- **THEN** MUST быть ровно один concrete-класс
- **AND** `PostgresDuckDbProvider` MUST отсутствовать

#### Scenario: реализация действительно реализует интерфейс

- **WHEN** проверяется `issubclass(<concrete cache class>, CacheProvider)`
- **THEN** MUST вернуться `True`
- **AND** `ApplicationContext.cache_provider` MUST быть типизирован `CacheProvider | None`, а не `Any | None`

#### Scenario: standalone-инструмент открывает кэш через интерфейс

- **WHEN** `tools/build_vectors.py` открывает кэш для чтения
- **THEN** он MUST использовать точку создания провайдера с `mode=READ_ONLY` и типом `CacheProvider`
- **AND** MUST NOT называть concrete-класс хранилища и MUST NOT использовать удалённый `PostgresDuckDbProvider`

### Requirement: enforcement `query_sql` идентичен для всех реализаций

Правила `query_sql` MUST быть одинаковыми для любой реализации `CacheProvider`, независимо от concrete storage: DDL (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `CREATE INDEX`, `DROP INDEX`) → `UnsupportedSqlError` в любом режиме; `INSERT`/`UPDATE`/`DELETE` в `READ_ONLY` → `ReadOnlyAssertionError` **до выполнения**. Проверка MUST NOT возлагаться исключительно на storage engine.

#### Scenario: DDL отклоняется до обращения к storage

- **WHEN** в любом режиме вызывается `query_sql` с DDL-запросом
- **THEN** MUST быть выброшена `UnsupportedSqlError`
- **AND** storage engine MUST NOT быть вызван

#### Scenario: DML в READ_ONLY отклоняется приложением

- **WHEN** в `READ_ONLY` вызывается `query_sql("INSERT ...")`
- **THEN** MUST быть выброшена `ReadOnlyAssertionError` до выполнения запроса

### Requirement: ровно один файл кэша, без публикации снимка

В runtime MUST существовать **ровно один физический файл кэша**. Рабочий
файл и «файл для читателей» MUST NOT быть разными сущностями: разделение
снимков запрещено.

- Механизм публикации копированием (`ATTACH` временного файла + `os.replace`) MUST быть удалён. Сегодня `DuckDbCacheStore.publish()` копирует файл **сам в себя**, поскольку `_cache_path` и `_publish_path` указывают на один и тот же путь.
- Поле `_publish_path` и его присваивание из composition root MUST быть удалены.
- Runtime MUST писать в файл кэша напрямую через интерфейс; отдельного шага «опубликовать снимок» MUST NOT существовать.
- Тиражированное владение файлом MUST считаться дефектом: оно роняет и заново захватывает блокировку файла каждый цикл, создавая недетерминированное окно, в которое читатель попадает или не попадает.

Обоснование: «in-memory mirror → snapshot file» — наследие модели, сменившейся на ownership (Stage C/D) и не доведённой до конца. Она противоречит коду и документирует несуществующее поведение.

#### Scenario: у провайдера нет шага публикации

- **WHEN** выполняется инвентаризация методов concrete-реализации `CacheProvider`
- **THEN** `publish` MUST отсутствовать
- **AND** MUST NOT существовать временных файлов `cache.duckdb.*.tmp` рядом с файлом кэша

#### Scenario: рабочий файл и файл для читателей — один

- **GIVEN** composition root создал провайдера через точку создания
- **WHEN** сравниваются пути, по которым провайдер пишет и которые видны потребителям
- **THEN** MUST совпадать один и тот же физический файл
