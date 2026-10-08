# data/cache-provider Specification — дельта

## Purpose

Закрыть разрыв между каноном и кодом. Канон `data/cache-provider` описывает мир,
в котором агент владел локальным DuckDB-кэшем, слоем владения и фоновой
синхронизацией. Этого кода в репозитории нет уже давно, а нормативный текст
остался. Дыра старая, но после пересборки `unify-runtime-channels` она становится
**вторым** противоречием между двумя канонами: `runtime/entrypoints` уже записал,
чем именно снятые требования заменены
(`openspec/specs/runtime/entrypoints/spec.md:815-820`), а `data/cache-provider`
по-прежнему требует `CacheOwnershipCoordinator.try_claim()` и
`resolve_publish_path(role)`.

Поэтому дельта состоит из двух разных вещей, и смешивать их нельзя:

- **прошлый мир** (слой владения, fencing, `role` в путях, `gateway.cache.local_path`,
  `enable_audit` как переключатель кэша) — кода нет, требования снимаются или
  переписываются;
- **настоящее и всё ещё нужное, чего в каноне нет** (отделение владельца индексов
  от владельца снимка, отсутствие удержания файла между операциями, единственный
  писатель, отказ операций вместо падения сервера) — это `ADDED`.

## Scope

`platform` — снимок DuckDB целиком принадлежит capability `data`:
`mcp-platform/libs/enterprise_data/snapshot/`, объявление пути —
`mcp-platform/platform.json → data.snapshot_path`, владение векторными индексами —
capability `vectors` (`mcp-platform/libs/vectors/`).

В агенте снимка нет: полей `cache_loader` / `cache_provider` / `cache_store` /
`ownership_coordinator` у `ApplicationContext` не существует
(`lib/core/application_context.py:173-181`), секции `gateway.cache` нет в
`config.json` (0 совпадений), `gateway.cache.local_path` не встречается ни в
`config.json`, ни в `profiles/test.jsonc` (0 совпадений).

Не в объёме этого change: **правка кода**. Снятые требования — это ложь в
нормативном тексте, и её достаточно убрать из канона. Реализация отсутствующих
требований (см. `## Известные смежные расхождения` в `proposal.md`) — отдельные
change'ы платформы.

Операции дельт, нацеленные на требования, уже лежащие в каноне, приведены к
тексту канона: 8 требований под `MODIFIED` взяты телом из
`openspec/specs/data/cache-provider/spec.md` — архив при таком состоянии не
может ни добавить второй заголовок с тем же именем, ни переписать живое
требование. Дублирующих `ADDED` на живые требования в этом файле нет.
`REMOVED` в этом файле объявлен для требования, которого в каноне нет, и
оставлен как есть. Причина — расхождение дельты с каноном; незавершённое
остаётся в `tasks.md`.

## ADDED Requirements

### Requirement: Владелец векторных индексов отделён от владельца снимка

Модуль снимка MUST NOT владеть векторными индексами, импортировать FAISS и
строить индексы. `search_vector` / `preload_indexes` MUST делегировать
внедрённому владельцу индексов (`VectorIndexAccessor`), который собирает
composition root платформы.

Направление зависимостей потому одностороннее: `libs/vectors` берёт `SearchResult` и
`IndexIntegrityError` из `snapshot/contracts.py`, а снимок не знает про FAISS.
Обратная ссылка создала бы цикл импорта
(`mcp-platform/libs/enterprise_data/snapshot/store.py:9-17`, `:273-295`).

- Постройка индекса MUST выполняться **на старте сервера платформы, до event loop**
  (`VectorIndexOwner.ensure_index`, зовётся из
  `servers/enterprise/server.py::_prepare_capabilities`), а не по первому поиску:
  холодный поиск не должен платить за чужую подготовку. Строит владелец
  индексов, а не хранилище (`VectorIndexAccessor`, `store.py:273-295`).
- `preload_indexes()` MUST вызываться не хранилищем напрямую, а владельцем
  индексов capability `vectors`; хранилище отдаёт его вызов
  (`store.py:813-826` — докстринг: «сервер вызывает этот путь не напрямую, а через
  владельца индексов capability `vectors`»).
- Без подключённого владельца `preload_indexes()` MUST вернуть пустой список, а
  `search_vector` MUST поднять отказ с кодом `index_not_built`, а не `AttributeError`
  (`store.py:803-808`, `:824-825`).

#### Scenario: Снимок не строит индекс и не импортирует FAISS

- **WHEN** проверяется модуль снимка на владение индексами
- **THEN** `import faiss` в `mcp-platform/libs/enterprise_data/snapshot/` MUST
  отсутствовать: FAISS принадлежит capability `vectors`
  (`store.py:1` — «единственное место `import duckdb` в платформе»; `store.py:9` —
  «DuckDB принадлежит этому модулю. FAISS — владельцу `libs/vectors`»)
- **AND** модуль MUST NOT содержать построения индекса: подпись индекса и её
  проверка живут в `libs/vectors/signature.py`, а не в снимке
  (`store.py:33-38` — докстринг: «проверка индекса и подпись индекса писателю
  снимка не принадлежат»)

#### Scenario: Владелец индексов не подключён — отказ назван, а не AttributeError

- **WHEN** хранилище открыто без `index_accessor` и вызван `search_vector`
- **THEN** MUST быть поднят `InfrastructureError` с кодом `index_not_built`
  (`store.py:803-808`)
- **AND** `preload_indexes()` MUST вернуть пустой список, а не упасть
  (`store.py:824-825`)
- **AND** причина MUST доезжать до вызывающего как отказ операции, а не как
  `AttributeError`: неподключённый владелец — настроечное состояние, а не дефект
  кода (проверяется
  `mcp-platform/tests/test_snapshot_optional_startup.py::TestClientGetsTheRealCause::test_vector_reads_fail_with_the_reason_not_attribute_error`)

#### Scenario: Индекс собран на старте, до первого поиска

- **WHEN** процесс платформы поднялся
- **THEN** построение индекса MUST быть уже выполнено для каждого объявленного
  и включённого индекса (`_prepare_capabilities` → `ensure_index` в
  `servers/enterprise/server.py`), то есть к обслуживанию запросов FAISS в памяти
  уже есть
- **AND** `search_vector` MUST NOT быть путём нормальной постройки: он зовёт
  владельца только как восстановление, если индекса нет
  (`store.py:793-800` — «на старте сервера он уже собран, а здесь остаётся путь
  восстановления»)

### Requirement: Файл снимка не удерживается между операциями, а провайдер не бывает полуготовым

В режиме `READ_ONLY` файл снимка MUST NOT удерживаться между операциями:
соединение открывается на время вызова и закрывается сразу после него. Хранение
постоянного соединения означало бы, что читатель снимка живёт все время работы
процесса и держит блокировку файла, а это ровно то состояние, из-за которого
загрузчик не может был пересоздать снимок.

Неудача открытия MUST быть исключением всегда. Наружу MUST NOT отдаваться
провайдер с `is_ready() is False`, полученный из неудачного открытия: вызывающий
не отличил бы «снимка нет» от «снимок сломан».

- Соединение MUST открываться и закрываться внутри вызова
  (`_read_conn()` в `store.py:380`).
- Неудача открытия MUST классифицироваться: занятый файл, неоткрываемый файл,
  неподдерживаемая ФС — разные ошибки
  (`_classify_open_error()` в `store.py:444`;
  `UnsupportedFilesystemError` — `store.py:113`, `CacheBusyError`,
  `CacheOpenError` — `snapshot/contracts.py:140,168`).
- `CacheOpenError` MUST быть `ConfigurationError`, потому что провал открытия
  файла снимка — сбой startup-инфраструктуры, а не ошибка вызова
  (`snapshot/contracts.py:168`; `store.py:1444-1453` — «Наружу „полуготовый“
  провайдер не отдаётся»).

#### Scenario: Между чтениями файл не удерживается

- **WHEN** выполнено несколько `query_sql` подряд в режиме `READ_ONLY`
- **THEN** соединение MUST открываться на время вызова и закрываться сразу после
  него, а не жить между вызовами
  (`store.py:22-24` — докстринг; `store.py:380` — `_read_conn()`)
- **AND** проверяется
  `mcp-platform/tests/test_snapshot_no_file_hold.py::TestNoFileHold::test_file_not_held_between_reads`
- **AND** падение запроса MUST NOT оставлять соединение открытым
  (`::test_connection_closed_even_when_query_fails`)

#### Scenario: Отказ DDL не открывает файл

- **WHEN** в режиме `READ_ONLY` вызван `query_sql("CREATE TABLE …")`
- **THEN** файл MUST NOT быть открыт вовсе: проверка типа запроса стоит ДО
  открытия соединения
  (`store.py:575-591` — `_assert_query_allowed()` вызывается до чтения;
  проверяется `::TestNoFileHold::test_ddl_rejection_does_not_open_the_file`)

#### Scenario: Неудачное открытие — исключение, а не «полуготовый» провайдер

- **WHEN** файл отсутствует, повреждён, пуст или путь не передан
- **THEN** MUST быть поднято исключение, а не возвращён объект с
  `is_ready() is False` (`store.py:1444-1453` — `open_snapshot_store` вызывает
  `connect()` при `verify=True`; проверяется
  `mcp-platform/tests/test_snapshot_no_file_hold.py::TestFactoryFailsLoudly::test_no_half_ready_provider_escapes`,
  `::test_read_only_on_missing_file_raises`, `::test_corrupt_file_raises`,
  `::test_empty_file_raises`, `::test_empty_path_raises`)
- **AND** ошибка MUST называть причину: занятый файл, битый файл и пустой путь —
  разные состояния, и молчаливое «не открылось» заставляет вызывающего
  перезапускать то, что не требует перезапуска
  (`::TestBusyErrorNamesTheHolder::test_windows_message`, `::test_linux_message`)

#### Scenario: Режим READ_WRITE держит соединение — это стадия загрузки

- **WHEN** хранилище открыто в режиме `READ_WRITE`
- **THEN** соединение MUST удерживаться, а не открываться на время вызова
  (проверяется `::TestNoFileHold::test_read_write_mode_holds_connection`)
- **AND** такой режим MUST открываться только загрузчиком, а не runtime-путём
  (см. требование «Единственный писатель снимка — стадия загрузки»)

### Requirement: Единственный писатель снимка — стадия загрузки

Запись в снимок MUST выполняться одной стадией жизненного цикла — загрузкой
(`SnapshotLoadService`), которая открывает хранилище в `READ_WRITE`. Runtime-путь
MUST писать через одну операцию полной замены содержимого таблицы
(`replace_records`) и MUST NOT заводить второго писателя того же файла.

Второй writer означал бы, что снимок читают не оттуда, откуда его пишут, поэтому
роль чтения и роль записи разведены на уровне ABC: `CacheProvider` (чтение),
`CacheIngestion` (запись), `CacheStore` — оба
(`mcp-platform/libs/enterprise_data/snapshot/contracts.py:195,285,358`).

- Частичная запись (`upsert_records`) MAY оставаться примитивом хранилища, но из
  runtime-пути MUST NOT вызываться
  (`store.py:28-29`).
- Загрузчик MUST ходить в PostgreSQL сам, а снимку MUST отдавать готовые строки:
  писатель снимка не является читателем базы
  (`mcp-platform/libs/enterprise_data/loader.py:149-157`; `:321` —
  `store.replace_records(table, rows)`).
- Синхронизации по track-колонкам в снимке нет: таблица заменяется целиком
  (`loader.py:321`).

#### Scenario: Runtime-путь не имеет своего писателя

- **WHEN** строится состав операций capability `data`
- **THEN** запись в снимок MUST сводиться к полной замене содержимого таблицы
  (`store.py:25-29` — «Из runtime-пути вызывается одна операция полной замены
  содержимого таблицы»)
- **AND** `upsert_records` MUST оставаться примитивом хранилища и MUST NOT
  становиться вторым runtime-путём записи
  (`store.py:832`; `store.py:28-29`)

#### Scenario: Загрузчик — единственный, кто открывает снимок на запись

- **WHEN** выполняется загрузка снимка
- **THEN** `SnapshotLoadService` MUST открывать хранилище в режиме `READ_WRITE` и
  MUST быть единственным владельцем этого режима
  (`loader.py:149-157` — «store: роль `CacheStore`, открытая в режиме
  `READ_WRITE`»)
- **AND** загрузка MUST использовать `replace_records` / `ensure_schema`, а не
  `query_sql` для записи (`loader.py:153`, `:321`)
- **AND** запись в снимок, открытый на чтение, MUST быть отвергнута, а не
  «исправлена» повторным открытием (`store.py:1002-1020` — `_assert_writable`;
  проверяется
  `mcp-platform/tests/test_snapshot_load_service.py::TestLoad::test_read_only_store_makes_the_load_fail`)

#### Scenario: Роли чтения и записи разведены на уровне ABC

- **WHEN** проверяется набор абстракций снимка
- **THEN** чтение и запись MUST быть разными ABC, а их соединение — третьим
  (`snapshot/contracts.py:195` — `CacheProvider`; `:285` — `CacheIngestion`;
  `:358` — `CacheStore(CacheProvider, CacheIngestion)`; проверяется
  `mcp-platform/tests/test_snapshot_contracts.py::TestProviderAbcContract::test_write_role_is_separate_abc`)
- **AND** ABC чтения MUST NOT содержать ни одного метода записи — иначе роль
  «читатель» снова сможет стать писателем

### Requirement: Ненастроенный или непригодный снимок отказывает операциям, а не роняет сервер

Путь снимка MUST быть объявлен платформой, а не агентом
(`mcp-platform/platform.json → data.snapshot_path`). Незаданный путь MUST означать
«снимок ненастроен», а не «сервер не запустится»: capability `data` от снимка не
зависит, и её остальные операции MUST работать.

Вместо `None` composition root MUST отдавать объект, который на любой операции
поднимает `InfrastructureError` с кодом и текстом исходной ошибки. Код важен
отдельно от текста: `cache_busy` — «файл держит другой процесс»,
`cache_open_error` — «файл не открывается», и вызывающий различает их по коду, а не
по формулировке.

- Пустой путь MUST давать отказ с текстом «снимок не настроен», а не
  `CacheOpenError` о пустом аргументе (`servers/enterprise/server.py:484-488`).
- Ошибка открытия MUST сохранять свой код доездом до клиента, а не
  подменяться общим «индексы недоступны» (`server.py:496-504`).
- `UnavailableSnapshot` MUST NOT открывать DuckDB, строить FAISS и держать
  соединение — это не второй владелец файла
  (`mcp-platform/libs/enterprise_data/snapshot/unavailable.py:28-32`).

#### Scenario: Путь не задан — сервер поднимается, операции отвечают причиной

- **WHEN** `ENTERPRISE_SNAPSHOT_PATH` пуст
- **THEN** сервер MUST подняться, а capability MUST отдать
  `UnavailableSnapshot` с текстом «снимок не настроен»
  (`server.py:484-488`; `platform.json:95` и `data._about` — «Путь здесь OPTIONAL…
  пустое значение означает „снимок ненастроен"… а не „сервер не запустится"»)
- **AND** остальные capability MUST работать как обычно: снимок от них не зависит
  (проверяется
  `mcp-platform/tests/test_snapshot_optional_startup.py::TestServerSurvivesUnusableSnapshot::test_container_is_built_and_other_capabilities_work`)

#### Scenario: Файл занят или бит — процесс живёт, клиент различает причины

- **WHEN** файл снимка держит другой процесс либо он не открывается
- **THEN** сервер MUST подняться без снимка, залогировав предупреждение
  (`server.py:496-504`; проверяется `::TestServerSurvivesUnusableSnapshot::test_busy_file_does_not_kill_startup`,
  `::test_broken_file_does_not_kill_startup`)
- **AND** клиент MUST получить **свой** код ошибки, а не общий отказ снимка
  (проверяется `::TestClientGetsTheRealCause::test_busy_keeps_its_own_code`,
  `::test_broken_file_keeps_its_own_code`)
- **AND** причина MUST быть читаема и без вызова операции — иначе health-check
  упал бы вместо того, чтобы показать «снимок недоступен»
  (`unavailable.py:59-60` — `get_stats()`; проверяется
  `::TestClientGetsTheRealCause::test_reason_is_readable_without_calling_an_operation`)

#### Scenario: Путь снимка объявляет платформа, а не агент

- **WHEN** определяется, где лежит файл снимка
- **THEN** путь MUST читаться из `mcp-platform/platform.json → data.snapshot_path`
  (`platform.json:95`), а не из `config.json` агента: секции `gateway.cache` в
  `config.json` нет (0 совпадений)
- **AND** `~` MUST разворачиваться платформой при чтении настройки
  (`resolve_snapshot_setting()` в `store.py:213`; вызов — `server.py:480-482`)
- **AND** путь MUST приниматься как путь **к файлу**, а не как каталог: требование
  разбирать путь обратно на каталог завело бы второе место, где решается, где лежит
  снимок (`server.py:463-468`; форма «каталог» осталась только для
  standalone-утилит — `resolve_snapshot_path()`, `store.py:258`)

## MODIFIED Requirements

### Requirement: PostgreSQL — источник истины
Система MUST рассматривать PostgreSQL как единственный источник истины для всех кэшируемых таблиц.

#### Scenario: обновление данных

- **КОГДА** в PostgreSQL изменились строки кэшируемой таблицы
- **ТОГДА** обновление снимка MUST делать capability `data` платформы одной транзакцией с чтением (`SnapshotLoadService`, `mcp-platform/libs/enterprise_data/loader.py:149`). Инкрементального sync в системе нет: агентский `PgDuckDbSyncService` **снят** вместе с кэш-кластером
- **И НЕ ДОЛЖЕН** использовать dual-write или иной механизм записи в обе БД одновременно

### Requirement: локальный ext4 storage
Система ДОЛЖНА хранить snapshot-файл только на локальной filesystem с поддержкой требуемых cache storage locking semantics (POSIX `fcntl` flock, etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён (NFS эмпирически fails with PID 0 errors на свежем файле). Конкретная FS не специфицируется (ext4 — Linux default, APFS — macOS, NTFS — Windows); термин «ext4» в заголовке requirement сохранён для backward compat с существующими ссылками в тестах и документации, но требование портативно на любую local FS.

Concrete cache storage MUST reject unsupported network/shared filesystem paths before opening the storage. Для текущей DuckDB implementation NFS/SMB и другие network/shared filesystems MUST быть rejected. Для будущих реализаций правила аналогичны (storage без локального filesystem locking semantics недопустим).

Snapshot-путь MUST быть единым для всех процессов:

- `cache.duckdb` — имя файла снимка (`SNAPSHOT_FILENAME`, `mcp-platform/libs/enterprise_data/snapshot/store.py:88`). Путь объявляет платформа: `mcp-platform/platform.json → data.snapshot_path`; резолвится `resolve_snapshot_path()` (`:259`). Агент файл не открывает: `CacheProvider` в дереве агента **не остался**.
- Никаких role-based путей. Функции `resolve_publish_path(role)` в коде нет, и роли она не принимала бы: резолвинг единственный — `resolve_snapshot_path(cache_dir, filename)`.

**Путь снимка MUST быть shared runtime resource**, не profile-specific value. Он объявляется на платформе (`mcp-platform/platform.json → data.snapshot_path`, `platform.json:99`), поэтому оба процесса резолвят его в один и тот же физический путь. Ключа `gateway.cache.local_path` в конфигурации агента **нет** — секция `gateway.cache` снята вместе с кэш-кластером.

**Снимок MUST загружать платформа, а не агент.** Ни `CacheProvider`, ни concrete implementation, ни слой владения в дереве агента не остались; снимком владеет capability `data`. Секции `gateway.cache` в конфигурации агента **нет**, и `gateway.enable_audit` не имеет отношения к её существованию.

Режим открытия (`READ_WRITE` или `READ_ONLY`) определяет владелец файла снимка; `CacheAccessMode` объявлен на платформе (`mcp-platform/libs/enterprise_data/snapshot/contracts.py:46`). Слой владения **снят** (change `drop-local-cache-read-from-pg`): claim, heartbeat, fencing и таблица `agent_cache_ownership` не выполняются и не существуют.

Навык `audit_analyzer` MUST получать данные через операции capability `audit` — модель получает их как `mcp_enterprise_*` с настоящими `inputSchema`. Прямого доступа к файлу снимка у него нет, и открывать его не нужно: файл вообще не открывает агент. Навыка `legal_summarizer` в дереве агента **не осталось** — предмет переехал на платформу (`mcp-platform/libs/legal_summarizer/`, capability `legal_summarizer`); в `workspace/skills/` остались `audit_analyzer` и `enterprise_mcp`.

#### Scenario: обнаружение NFS пути

- **КОГДА** `gateway.cache.local_path` указывает на NFS mount или другую network/shared filesystem
- **ТОГДА** concrete cache storage MUST reject путь ДО открытия cache с явной ошибкой (PID 0 locking errors эмпирически)

#### Scenario: CLI и gateway используют один и тот же snapshot

- **КОГДА** запущены `cli_agent.py` (profile=test) и `gateway.py --profile=prod` одновременно
- **ТОГДА** оба процесса MUST резолвить cache в один и тот же физический путь

#### Scenario: Профили не переопределяют gateway.cache.local_path

- **КОГДА** профили задают разные значения `gateway.cache.local_path`
- **ТОГДА** ConfigurationResolver MUST reject это как ошибку конфигурации

Сегодня у сценария нет предмета: `profiles/prod.jsonc` в дереве агента **не
объявлен** (каталог `profiles/` содержит только `profiles/test.jsonc`, и ключа
`gateway.cache.local_path` в нём нет), а путь снимка объявляет платформа —
`mcp-platform/platform.json → data.snapshot_path` (`platform.json:99`). Правило
сохранено как контракт конфигурации, а не как описание существующих файлов.

#### Scenario: enable_audit=False НЕ отключает cache

- **КОГДА** `gateway.enable_audit=False`, но `gateway.cache` секция настроена
- **ТОГДА** `CacheProvider` MUST быть создан (cache runtime существует)
- **AND** отдельной подсистемы sync в агенте MUST NOT быть создано: `CacheSyncService` **снят** вместе с кэш-кластером
- **AND** навык `audit_analyzer` MUST иметь доступ к данным через операции capability `audit`, а не через интерфейс агента: агентский `CacheProvider` **снят**
- **AND** если процесс получил ownership cache resource → `READ_WRITE` access НЕЗАВИСИМО от `enable_audit` (другие runtime-компоненты MAY выполнять cache mutations через `CacheProvider`)

### Requirement: единый интерфейс доступа
Система MUST предоставлять доступ к кэшу только через `CacheProvider`. Прямой доступ к DuckDB-файлу из кода Skills запрещён.

#### Scenario: Skill запрашивает данные

- **КОГДА** Skill нуждается в SQL-запросе к кэшу
- **ТОГДА** он ДОЛЖЕН вызвать `CacheProvider.query_sql()` (или другой метод интерфейса)
- **И НЕ ДОЛЖЕН** открывать DuckDB-файл напрямую

### Requirement: vector search только через `CacheProvider.search_vector`
Система MUST выполнять vector search исключительно через `CacheProvider.search_vector`. Прямая загрузка FAISS-индексов из Skills запрещена.

#### Scenario: Skill выполняет vector search

- **КОГДА** Skill нуждается в vector similarity query
- **ТОГДА** он ДОЛЖЕН вызвать `CacheProvider.search_vector` с указанием `index_name`
- **И НЕ ДОЛЖЕН** открывать FAISS-файлы напрямую

### Requirement: контроль целостности индексов
Система MUST проверять signature индекса (модель эмбеддингов, размерность, колонки, chunk-параметры) перед использованием и поднимать `IndexIntegrityError` при несовпадении.

#### Scenario: stale индекс

- **КОГДА** сигнатура сохранённого индекса не совпадает с текущей конфигурацией
- **ТОГДА** `search_vector` ДОЛЖЕН поднять `IndexIntegrityError` со статусом `STALE` или `INVALID`
- **И НЕ ДОЛЖЕН** возвращать «тихую» деградацию результатов

### Requirement: Storage implementation isolation
Конкретный тип локального cache-хранилища является implementation detail. Нормативные runtime-контракты НЕ ДОЛЖНЫ использовать конкретное имя или API текущей storage implementation, кроме разделов, описывающих соответствующий concrete adapter.

Компоненты, которые MUST NOT зависеть от concrete-имени хранилища:

- `AgentLoop`
- Skills
- Tools
- Runtime consumers `CacheProvider`

`DuckDbSnapshotStore` MAY фигурировать только в:
- Concrete adapter specification
- Composition root (`ApplicationContext.create`)
- Configuration/factory
- Tests, проверяющих DuckDB-specific behavior

Замена concrete-реализации на другую (например, `SQLiteCacheStore`) НЕ ДОЛЖНА требовать изменений в `AgentLoop`, Skills, Tools или других runtime consumers `CacheProvider`. Изменения MAY потребоваться только в concrete adapter, composition root, configuration/factory и integration tests конкретной реализации.

#### Scenario: Замена concrete cache implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется (например, `DuckDbSnapshotStore` → `SQLiteCacheStore`)
- **THEN** `AgentLoop` MUST NOT require changes
- **AND** Skills MUST NOT require changes
- **AND** Tools MUST NOT require changes
- **AND** изменения MAY потребоваться только в: concrete adapter, composition root, configuration/factory, integration tests конкретной реализации

#### Scenario: runtime-consumer код не импортирует concrete cache implementation

- **WHEN** проверяется `AgentLoop`, Skills, Tools на импорт concrete cache class
- **THEN** НЕ ДОЛЖНО быть импортов `DuckDbSnapshotStore` (или любой другой concrete реализации)
- **AND** эти компоненты работают только через `CacheProvider` интерфейс

### Requirement: CacheProvider как интерфейс без конкретной СУБД
`CacheProvider` MUST быть runtime-интерфейсом доступа к cache-хранилищу. `CacheProvider` MUST NOT:

- знать имя или тип конкретной СУБД;
- содержать DuckDB-specific или SQLite-specific API в публичных методах;
- управлять владением PostgreSQL resource: слоя владения **нет**, в системе один gateway и один известный writer;
- самостоятельно выполнять ownership takeover;
- создавать concrete storage implementation.

`CacheProvider` НЕ ИМЕЕТ метода `open()`. Concrete-реализация `DuckDbSnapshotStore` сама открывает своё хранилище; вызов идёт из composition root.

#### Scenario: Замена concrete implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется (например, `DuckDbSnapshotStore` → `SQLiteCacheStore`)
- **THEN** runtime consumers MUST NOT require changes

#### Scenario: runtime-consumer код не импортирует concrete cache implementation

- **WHEN** проверяется `AgentLoop`, Skills, Tools на импорт concrete cache class
- **THEN** НЕ ДОЛЖНО быть импортов `DuckDbSnapshotStore` (или любой другой concrete реализации)
- **AND** эти компоненты работают только через `CacheProvider` интерфейс

### Requirement: CacheAccessMode и двухуровневая защита
`CacheAccessMode` — enum: `READ_WRITE` или `READ_ONLY` (`mcp-platform/libs/enterprise_data/snapshot/contracts.py:46`). Режим задаёт владелец файла снимка: механизма claim больше нет, и класс `ClaimResult` **снят** вместе со слоем владения.

Concrete adapter `DuckDbSnapshotStore` сам реализует, как открыть своё хранилище в этих режимах.

В `READ_ONLY` режиме все мутации MUST быть запрещены через **двухуровневую защиту**:

1. **Concrete adapter MUST открыть storage connection в реальном read-only режиме** (например, для DuckDB: `duckdb.connect(path, read_only=True)`). Сам storage engine не позволит мутации.
2. **`CacheProvider.query_sql(...)` MUST поднять `ReadOnlyAssertionError`** при INSERT/UPDATE/DELETE.

Оба уровня защиты MUST присутствовать одновременно (defense in depth).

#### Scenario: Concrete adapter открывает storage в реальном read_only режиме

- **WHEN** CacheProvider создан в mode=READ_ONLY (например, `DuckDbSnapshotStore` открыт в READ_ONLY)
- **THEN** concrete adapter MUST открыть storage connection в реальном read-only режиме (для DuckDB: `duckdb.connect(path, read_only=True)`)
- **AND** попытки INSERT/UPDATE/DELETE на уровне SQL MUST быть отклонены storage engine

#### Scenario: CacheProvider поднимает ReadOnlyAssertionError при INSERT/UPDATE/DELETE в READ_ONLY

- **WHEN** CacheProvider в mode=READ_ONLY
- **AND** через `CacheProvider.query_sql(...)` вызывается INSERT/UPDATE/DELETE
- **THEN** MUST поднять `ReadOnlyAssertionError` до выполнения

#### Scenario: query_sql() отклоняет DDL в любом mode

- **WHEN** вызов `query_sql("CREATE TABLE ...")` или `query_sql("DROP TABLE ...")` (в любом mode)
- **THEN** MUST поднять `UnsupportedSqlError` (DDL запрещён даже в READ_WRITE)

## REMOVED Requirements

### Requirement: CacheOwnershipCoordinator как абстрагированный ownership

**Reason**: Требование закрепляло компонент, которого в репозитории нет. Слой
владения локальным кэшем снят: `CacheOwnershipCoordinator`, `try_claim()`,
`heartbeat()`, `release()`, `acquire_write_fence()`, класс `ClaimResult` и таблица
`agent_cache_ownership` в коде не встречаются ни разу (0 совпадений по
`lib/`,
`mcp-platform/libs/`, `mcp-platform/servers/`, `config.json`,
`mcp-platform/platform.json`); имя класса осталось только в прозе —
`snapshot/contracts.py:56` и `mcp-platform/docs/TARGET-ARCHITECTURE.md:51`.
Пока компонент
был, требование было честным; после снятия оно осталось правдой в прошедшем времени.

**Migration**: Контракт переносить нечего. Режим доступа к снимку описывает
`CacheAccessMode`
(`mcp-platform/libs/enterprise_data/snapshot/contracts.py::CacheAccessMode`), и его
роль ограничена свойством доступа к файлу, а не координацией между процессами. Это
же зафиксировано в каноне `runtime/entrypoints`
(`openspec/specs/runtime/entrypoints/spec.md:815-816`).

Точное содержание снятого требования, чтобы снятие было проверяемым, а не
«перепишем позже»:

- `CacheOwnershipCoordinator` MUST отвечать за ownership общего **логического**
  cache resource;
- coordinator MUST отвечать только за `try_claim()` (atomic claim), `heartbeat()`,
  `release()`, `acquire_write_fence()` (PG advisory lock для fencing), проверку
  текущего owner (через `ClaimResult.current_owner_id` / `current_generation`) и
  generation (fencing token);
- coordinator MUST NOT выполнять операций чтения или записи cache-хранилища.

Оба сценария прежней редакции — «Замена concrete cache implementation» и
«Coordinator не делает cache I/O» — снимаются вместе с требованием: оба
описывают замену concrete-реализации и поведение координатора, которых в системе
нет. Проверять нечего, и оставлять их было бы враньём в нормативном тексте.

Причина именно та. Слой владения снимался дважды, по двум разным причинам, и
второе снятие окончательное:

1. Слой владения heartbeat'ом каждые 30 секунд делал 2 запроса в минуту бессрочно
   — то есть расходовал ровно тот ресурс, ради экономии которого кэш и
   существовал (change `2026-10-02-drop-local-cache-read-from-pg`).
2. Гонок за файл не осталось: writer снимка один, известен заранее, и открывает
   его capability `data` платформы. Takeover'а не бывает, а значит не бывает и
   fencing — fencing существует, чтобы отозвать права у takeover'нувшего.

Замена не требуется: режим доступа описывает
`mcp-platform/libs/enterprise_data/snapshot/contracts.py::CacheAccessMode`, объём
его роли ограничен свойством доступа к файлу, а не координацией между
процессами. Это же зафиксировано в каноне `runtime/entrypoints`
(`openspec/specs/runtime/entrypoints/spec.md:815-816` — таблица «Снятые
требования»).

#### Scenario: Слоя владения, который можно было бы заменить, не существует

- **WHEN** ищется механизм выбора режима доступа к снимку
- **THEN** `CacheOwnershipCoordinator` MUST NOT существовать ни в одном дереве
  (в коде 0 совпадений по `lib/`, `mcp-platform/libs/`, `mcp-platform/servers/`,
  `config.json`, `mcp-platform/platform.json`; имя класса осталось только в
  прозе — `snapshot/contracts.py:56` и `mcp-platform/docs/TARGET-ARCHITECTURE.md:51`,
  и ни то ни другое кодом не является)
- **AND** таблицы `agent_cache_ownership` MUST NOT существовать: fencing без
  takeover'а не имеет предмета
  (в `sql/**` — 0 совпадений: таблицу не создаёт ни одна миграция; имя осталось
  в комментариях `V006`/`V007` и в `sql/migrations/README.md`; канон
  `runtime/entrypoints:816` фиксирует то же)
- **AND** режим доступа MUST определяться стадией жизненного цикла, а не
  координацией между процессами
  (`snapshot/contracts.py:46-58`)
