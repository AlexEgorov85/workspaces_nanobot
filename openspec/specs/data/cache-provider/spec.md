# CacheProvider (Провайдер кэша)

## Purpose

Определение контракта подсистемы кэширования данных: источник истины, жизненный цикл snapshot'ов, публичный API, атомарность обновлений и контракт потребителя. Кэш — локальный DuckDB-слой для быстрого доступа к read-mostly данным, синхронизированным из PostgreSQL.

## Scope

`platform` — подсистема целиком уехала из агента в capability `data`: локального DuckDB-кэша в `lib/services/` больше нет
Реализация: `mcp-platform/libs/enterprise_data/snapshot/store.py` (`DuckDbSnapshotStore`)

## Requirements

### Requirement: PostgreSQL — источник истины

PostgreSQL MUST оставаться единственным источником истины для всех кэшируемых
таблиц. Правка касается **механизма**: инкрементального обновления снимка по
track-колонке из сервиса агента больше нет и быть не может — снимок читает и пишет
один процесс платформы, и запись идёт полной заменой содержимого таблицы.

> Правка 2026-10-04. Прежний нормативный текст требовал, чтобы
> `PgDuckDbSyncService` «подхватил изменение (по track-колонке) и инкрементально
> обновил DuckDB-снапшот». Класса `PgDuckDbSyncService` в репозитории нет: файл
> `lib/services/pg_duckdb_sync_service.py` отсутствует, поиск по `lib/`,
> `mcp-platform/libs/`, `mcp-platform/servers/`, `config.json` и
> `mcp-platform/platform.json` даёт 0 совпадений; в `lib/` имя класса осталось
> лишь в двух докстрингах, то есть в прозе, а не в коде
> (`lib/services/db_logging_service.py:209`, `:1504` — см. `proposal.md`
> § «Известные смежные расхождения»). Синхронизацией занимается
> capability `data` (`mcp-platform/libs/enterprise_data/loader.py`), и таблица
> заменяется целиком (`loader.py:321` — `store.replace_records(table, rows)`), а не
> инкрементально по track-колонке.
>
> Не тронуто: запрет dual-write и запрет писать в обе БД одновременно. Он не
> изменился в силе — он стал строже, потому что писатель теперь один и известен
> заранее (см. требование «Единственный писатель снимка — стадия загрузки»).
>
> Заголовок сценария сохранён: архив отказывается выбрасывать сценарий из
> `MODIFIED`-блока.

#### Scenario: обновление данных

- **WHEN** в PostgreSQL изменились строки кэшируемой таблицы
- **THEN** загрузчик снимка MUST пересобрать содержимое этой таблицы в файле
  снимка (`mcp-platform/libs/enterprise_data/loader.py:321` —
  `store.replace_records(table, rows)`)
- **AND** MUST NOT выполняться dual-write: запись в PostgreSQL и запись в снимок
  MUST NOT идти одним механизмом одновременно. Загрузчик читает базу и отдаёт
  снимку готовые строки — это две стадии, а не две записи в одну таблицу
  (`loader.py:149-157`, `:204`)
- **AND** инкрементальный sync по track-колонке MUST NOT восстанавливаться как
  отдельная подсистема агента: писатель снимка один, и второй writer означал бы,
  что снимок читают не оттуда, откуда его пишут
  (`lib/core/application_context.py:173-176` — снятие полей
  `cache_loader`/`cache_provider`/`cache_store` с той же формулировкой)

### Requirement: локальный ext4 storage

Файл снимка MUST храниться на локальной filesystem с поддержкой требуемых cache
storage locking semantics. Network/shared filesystem (NFS, SMB, CIFS) MUST быть
отвергнуты **до** открытия storage. Конкретная FS не специфицируется (ext4 —
Linux default, APFS — macOS, NTFS — Windows); термин «ext4» в заголовке сохранён
для backward compat с существующими ссылками в тестах и документации, но требование
портативно на любую local FS.

Правка 2026-10-04 — **путь объявляет платформа, слой владения снят**:

- `gateway.cache.local_path` в `config.json` **не существует** (0 совпадений; нет и
  секции `gateway.cache`, 0 совпадений). Путь объявляет платформа:
  `mcp-platform/platform.json → data.snapshot_path` (`platform.json:95`).
- Никаких role-based путей. Параметра `role` не существует ни у
  `resolve_snapshot_path()`, ни у чего-либо ещё: функция принимает каталог и имя
  файла (`store.py:258-270`, `store.py:87` — `SNAPSHOT_FILENAME = "cache.duckdb"`).
  Прежнее требование сохраняло `resolve_publish_path(role)` «для backward
  compat» — функции с таким именем в репозитории нет (0 совпадений).
- Агент MUST NOT открывать файл снимка ни в одной роли. Поля `cache_provider` /
  `cache_store` у `ApplicationContext` сняты
  (`lib/core/application_context.py:173-181`, `:761`, `:854`), роль
  `ApplicationContext.create()` — `role="gateway" | "cli"` — к снимку отношения не
  имеет.
- Режим открытия MUST задаваться стадией жизненного цикла, а не claim'ом:
  `READ_WRITE` — только стадия пересоздания при старте процесса, `READ_ONLY` — во
  всех остальных случаях (`snapshot/contracts.py:46-58` — докстринг
  `CacheAccessMode`).
- `gateway.enable_audit` MUST NOT определять существование снимка. Ключа в
  `config.json` нет (0 совпадений), поле `enable_audit` у `ApplicationContext`
  осталось (`lib/core/application_context.py:114`, `:186`) и к снимку отношения не
  имеет.

#### Scenario: обнаружение NFS пути

- **WHEN** объявленный путь снимка указывает на NFS mount или другую
  network/shared filesystem
- **THEN** concrete cache storage MUST отвергнуть путь ДО открытия файла, с
  `UnsupportedFilesystemError`, а не после (PID 0 locking errors эмпирически)
  (`store.py:113-119` — докстринг класса; `reject_unsupported_filesystem()` в
  `store.py:144`; вызов ДО создания хранилища — `store.py:1449`;
  проверяется
  `mcp-platform/tests/test_snapshot_no_file_hold.py::TestFilesystemGuard::test_reject_is_called_before_open`,
  `::test_nfs_mount_is_rejected`, `::test_all_network_fstypes_are_rejected`)

> **Известный пробел в коде, не закрытый этим change:** детектор работает только
> на Linux — на Windows и macOS `reject_unsupported_filesystem()` завершается
> no-op (`store.py:150`, `:160-161`; это закреплено тестом
> `::test_non_linux_is_a_noop`). То есть на этой машине требование НЕ выполняется.
> Требование оставлено в каноне как есть: ослаблять его под фактический пробел —
> значит зафиксировать в каноне ошибку. Починка — отдельный change платформы, см.
> `proposal.md` § «Известные смежные расхождения».

#### Scenario: CLI и gateway используют один и тот же snapshot

- **WHEN** запущены `cli_agent.py` и `gateway.py` одновременно
- **THEN** оба процесса MUST использовать один и тот же файл снимка
- **AND** это MUST следовать из того, что **путь объявлен в одном месте** —
  `mcp-platform/platform.json → data.snapshot_path` (`platform.json:95`), — а не
  из согласования между процессами
- **AND** ни один из процессов MUST NOT открывать файл снимка на запись и MUST NOT
  быть его владельцем: файл открывает и закрывает capability `data` платформы
  (`lib/core/application_context.py:31` — «Снимком (`cache.duckdb`) агент **не
  владеет**»; `:854`)

> Прежняя редакция требовала, чтобы оба entrypoint'а резолвили путь одинаково
> «независимо от того, какой процесс является owner'ом», и добавляла к этому
> `role`-based резолвинг. Слой владения снят (change
> `drop-local-cache-read-from-pg`): в системе один gateway, writer снимка один и
> известен заранее, а гонок за файл не бывает. Условие прежнего сценария —
> два процесса, каждый со своим путём, — больше не возникает, потому что путь
> единственный и объявлен платформой.

#### Scenario: Профили не переопределяют gateway.cache.local_path

- **WHEN** профили контура различаются
- **THEN** профиль MUST NOT переопределять путь снимка: `data.snapshot_path` —
  shared runtime resource, а не profile-specific value
  (`mcp-platform/platform.json:77` — `profiles._about`: «Всё остальное (пул, LLM,
  эмбеддинги, снимок, индексы) профилем НЕ разделяется намеренно: это shared
  runtime resources»; `PROFILE_OWNED_KEYS` в
  `mcp-platform/libs/enterprise_common/settings.py` — ровно три ключа: журнал,
  прогоны вопросов, очередь задач)
- **AND** `gateway.cache.local_path` MUST NOT появиться в `config.json` или в
  профилях как второй источник пути: секции `gateway.cache` в `config.json` нет
  (0 совпадений), в `profiles/test.jsonc` — тоже (0 совпадений)
- **AND** профиль, которого нет в `platform.json`, MUST быть ошибкой, а не поводом
  откатиться к базовым (боевым) именам
  (`platform.json:77`; `mcp-platform/libs/enterprise_common/settings.py`)

> Прежняя редакция требовала, чтобы `ConfigurationResolver` **отвергал** как
> ошибку конфигурации разные `gateway.cache.local_path` в профилях. Отвергать
> нечего: настройки в профилях нет, и её появление в двух местах стало бы вторым
> источником пути к файлу. Поэтому условие перевёрнуто: не «resolver ловит
> расхождение», а «настройка не переопределяется профилем и не возвращается в
> конфигурацию агента».

#### Scenario: enable_audit=False НЕ отключает cache

- **WHEN** `enable_audit` выключен
- **THEN** на существование снимка это MUST NOT влиять ни в одну сторону: снимком
  владеет capability `data` платформы, а не агент
- **AND** `enable_audit` MUST NOT обращаться к снимку, открывать его или решать
  режим доступа к нему (`lib/core/application_context.py:114` — чтение флага;
  `:173-181` — снятие полей кэша; кэш-поля в dataclass не возвращены)
- **AND** навыки MUST получать данные кэша не через файл снимка, а через операции
  платформы: `audit_analyzer` ходит в данные операциями capability `audit`
  (`workspace/skills/audit_analyzer/SKILL.md`), а навыка `legal_summarizer` в
  агенте не осталось (каталог `workspace/skills/legal_summarizer/` отсутствует)

> Прежняя редакция требовала, чтобы `CacheProvider` создавался при наличии
> `gateway.cache.local_path`, чтобы `CacheSyncService` не создавался при
> `enable_audit=False`, и чтобы при ownership `READ_WRITE` был доступен независимо
> от флага. Все три посылки описывают агентский кэш-кластер, которого нет: полей
> `cache_provider` / `cache_store` / `ownership_coordinator` у
> `ApplicationContext` не осталось, а секции `gateway.cache` в `config.json` нет.
> Условие сценария сохранено, а решение перевёрнуто: флаг не управляет ничем,
> что касается снимка, потому что агент снимком не владеет.

### Requirement: единый интерфейс доступа

Доступ к данным кэша MUST идти через объявленный интерфейс, а прямой доступ к
файлу снимка из кода навыков запрещён. Правка 2026-10-04: интерфейс переехал
через границу процесса. Навык агента MUST получать данные вызовом операции
платформы (`mcp_enterprise_*`), а не вызовом метода `CacheProvider` в своём
процессе: `CacheProvider` в агенте не существует и не должен.

- `CacheProvider` MUST оставаться единственным входом к данным снимка на стороне
  платформы (`snapshot/contracts.py:195` — ABC; `store.py:298` —
  `DuckDbSnapshotStore(CacheStore)`)
- Операция MUST доезжать до агента как операция capability, объявленная в
  `config.json → tools.mcpServers.enterprise.enabled_tools`
  (`config.json:982` — список включает `vector_search`, `query_operation`,
  `history_search`; `enterprise_mcp` объявлен в `:…`, `gateway.agent.enterprise_mcp`)
- Навык MUST NOT получать путь к файлу снимка и MUST NOT открывать его: путь
  известен платформе и объявляется в `platform.json` (`platform.json:95`)

#### Scenario: Skill запрашивает данные

- **WHEN** навыку агента нужны данные кэшируемых таблиц
- **THEN** он MUST вызвать операцию платформы (`mcp_enterprise_*`), а не открывать
  файл снимка напрямую
  (`workspace/skills/audit_analyzer/SKILL.md`; объявление операций —
  `config.json → tools.mcpServers.enterprise`)
- **AND** прямой доступ к файлу снимка из кода навыков MUST оставаться
  запрещённым: DuckDB принадлежит capability `data`, и «единственное место
  `import duckdb` в платформе» — это модуль снимка
  (`store.py:1`)
- **AND** агент MUST NOT создавать у себя экземпляр `CacheProvider` и MUST NOT
  знать путь к файлу снимка (`lib/core/application_context.py:31`, `:173-181`)

> Прежняя редакция требовала, чтобы навык вызвал именно
> `CacheProvider.query_sql()`. Такого вызова у навыка быть не может: `CacheProvider`
> — объект в процессе платформы, а навык живёт в процессе агента. Запрет прямого
> доступа к DuckDB-файлу не изменился в силе — он теперь обеспечивается ещё и
> тем, что путь и файл недостижимы из агента.

### Requirement: vector search только через `CacheProvider.search_vector`

Векторный поиск MUST выполняться только через объявленный интерфейс, а прямой
доступ к FAISS-индексам из кода навыков запрещён. Правка 2026-10-04: FAISS
принадлежит capability `vectors`, а не владельцу снимка; `search_vector` в
хранилище делегирует внедрённому владельцу индексов.

- `search_vector` MUST сохраняться в ABC чтения и делегировать, а не строить
  индекс (`snapshot/contracts.py:238`; `store.py:786-811`)
- Параметр `index_path` MUST NOT возвращаться: он обслуживал persist-файлы
  индекса на диске, которых в платформе нет, и его сохранение означало бы второй
  способ указать, где взять индекс (`store.py:798-801`)

#### Scenario: Skill выполняет vector search

- **WHEN** навыку агента нужен vector similarity query
- **THEN** он MUST вызвать операцию платформы vector search
  (`config.json → tools.mcpServers.enterprise.enabled_tools` — `vector_search`;
  `workspace/skills/audit_analyzer/SKILL.md`)
- **AND** прямой доступ к FAISS-файлам из кода навыков MUST оставаться запрещённым
  (проверяется `mcp-platform/tests/test_audit_lib_boundaries.py` и
  `::test_architecture_boundaries.py` на границы capability)
- **AND** на стороне платформы поиск MUST идти через `search_vector` хранилища,
  делегирующий владельцу индексов, а не через прямой `import faiss` в хранилище
  (`store.py:786-811`; `store.py:9-17`)

> Прежняя редакция требовала, чтобы навык вызвал `CacheProvider.search_vector` с
> указанием `index_name` — вызов в том же процессе, где объект `CacheProvider`
> существует. У навыка такого объекта нет, и появиться он не должен: снимок и
> индексы принадлежат платформе. Запрет прямого чтения FAISS-файлов не изменился
> в силе.

### Requirement: контроль целостности индексов

Подпись векторного индекса (модель эмбеддингов, размерность, колонки,
chunk-параметры) MUST проверяться перед использованием, а несовпадение MUST
поднимать `IndexIntegrityError` со статусом `STALE` или `INVALID`. Правка
2026-10-04: проверка принадлежит владельцу индексов, а не хранилищу снимка, но
**поведение остаётся тем же** и по-прежнему не допускает тихой деградации.

- Класс ошибки MUST оставаться один на платформу, а не дублироваться в
  `libs/vectors` (`snapshot/contracts.py:85`; импорт в `libs/vectors/__init__.py:27`;
  проверяется
  `mcp-platform/tests/test_snapshot_contracts.py::TestSingleDefinition::test_index_integrity_error_is_the_same_class_in_both_places`,
  `::test_no_duplicate_definition_in_vectors`)
- Проверка MUST выполняться до эмбеддинга запроса, а не после
  (`libs/vectors/owner.py:344-351` — `verify_index_signature()` и бросок до
  `self._embed(text)`)

#### Scenario: stale индекс

- **WHEN** сохранённая сигнатура индекса не совпадает с текущей конфигурацией
- **THEN** поиск MUST поднять `IndexIntegrityError` со статусом `STALE` или `INVALID`
  (`mcp-platform/libs/vectors/owner.py:346-351` —
  `raise IndexIntegrityError(index_name, status, "index signature mismatch")`)
- **AND** MUST NOT возвращать «тихую» деградацию результатов: агент MUST
  отличать «индекс устарел, пересобери» от «поиск не нашёл» по коду
  (`stale_index` / `invalid_index`, а не «индексы недоступны»;
  `mcp-platform/libs/vectors/signature.py:12`; проверяется
  `mcp-platform/tests/test_snapshot_contracts.py::TestIndexIntegrityError::test_stale_signature_gets_stale_index_code`,
  `::test_invalid_signature_gets_invalid_index_code`)
- **AND** неизвестный статус MUST считаться `INVALID`, а не проходить как
  «индекс в порядке» (`::test_unknown_status_is_invalid_index`)

### Requirement: Storage implementation isolation

Конкретный тип локального cache-хранилища является implementation detail.
Нормативные runtime-контракты MUST NOT использовать конкретное имя или API текущей
storage implementation. Правка 2026-10-04: из перечня потребителей уходят
`CacheSyncService` и `CacheOwnershipCoordinator` — оба класса удалены, а вместе с
ними исчезли все «потребители, которые не должны зависеть от concrete-класса»,
кроме агента и платформенных capability.

Оставшаяся граница стала строже, а не слабее: `import duckdb` в платформе
разрешён **только** в модуле снимка, а FAISS — **только** в capability `vectors`.

- `DuckDbSnapshotStore` MAY фигурировать только в: concrete adapter, composition
  root платформы (`servers/enterprise/server.py`), configuration/factory,
  тесты DuckDB-специфичного поведения
- `AgentLoop`, Skills, Tools и capability, читающие снимок, MUST NOT импортировать
  concrete класс (`store.py:298` — единственное concrete-реализация;
  `snapshot/contracts.py:195` — ABC, от которого зависят потребители)
- `mcp-platform/servers/enterprise/capabilities/data/service/main.py` MUST получать
  хранилище от composition root, а не создавать concrete-реализацию
  (`main.py:530` — вызов `open_snapshot_store`; комментарий `main.py:525-528` —
  «Открывает его composition root (server.py)»)

#### Scenario: Замена concrete cache implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется
- **THEN** `AgentLoop` MUST NOT require changes
- **AND** Skills MUST NOT require changes
- **AND** Tools MUST NOT require changes
- **AND** capability, читающие снимок, MUST NOT require changes
- **AND** изменения MAY потребоваться только в: concrete adapter, composition root
  платформы, configuration/factory, integration tests конкретной реализации
- **AND** замена MUST NOT требовать возвраща в агент слоя владения или
  синхронизации: это были не потребители хранилища, а соседи по файлу

> Две строки прежнего перечня — `CacheSyncService` и
> `CacheOwnershipCoordinator` — сняты вместе с самими классами: в коде
> совпадений нет (по `lib/`, `mcp-platform/libs/`, `mcp-platform/servers/`,
> `config.json`); имя класса осталось только в прозе —
> `snapshot/contracts.py:56`. Оставлены как требование те, кто
> пережил переезд: агент и capability платформы. Проверяется
> `mcp-platform/tests/test_snapshot_contracts.py::TestProviderAbcContract::test_cache_provider_has_no_open`.

#### Scenario: runtime-consumer код не импортирует concrete cache implementation

- **WHEN** проверяются `AgentLoop`, Skills, Tools и capability, читающие снимок, на
  импорт concrete cache class
- **THEN** НЕ ДОЛЖНО быть импортов `DuckDbSnapshotStore` (или любой другой concrete
  реализации) в агентском дереве: в `lib/` нет ни одного совпадения по
  `DuckDbSnapshotStore`, `CacheProvider`, `CacheOwnershipCoordinator`,
  `resolve_publish_path`
- **AND** эти компоненты MUST работать только через объявленный интерфейс —
  операции платформы со стороны агента, `CacheProvider` со стороны capability
  (`snapshot/contracts.py:195`)
- **AND** `import duckdb` в `mcp-platform/**` MUST сосредоточен в модуле снимка
  (`store.py:1` — «единственное место `import duckdb` в платформе»;
  `mcp-platform/tests/test_snapshot_contracts.py::TestProviderAbcContract::test_contracts_module_has_no_duckdb_import`)

### Requirement: CacheProvider как интерфейс без конкретной СУБД

`CacheProvider` MUST быть runtime-интерфейсом доступа к cache-хранилищу.
`CacheProvider` MUST NOT:

- знать имя или тип конкретной СУБД;
- содержать DuckDB-specific или SQLite-specific API в публичных методах;
- управлять владением ресурсом между процессами (такого слоя больше нет);
- самостоятельно выполнять takeover;
- создавать concrete storage implementation.

Правка 2026-10-04: **`CacheProvider` НЕ ИМЕЕТ метода `open()`, и это остаётся
верным** — но composition root, который вызывает concrete factory, больше не
`ApplicationContext` агента, а composition root платформы.

- Метода `open()` в ABC MUST NOT появляться (проверяется
  `mcp-platform/tests/test_snapshot_contracts.py::TestProviderAbcContract::test_cache_provider_has_no_open`)
- Concrete factory MUST быть модульной функцией, а не методом ABC
  (`open_snapshot_store()` в `store.py:1408`; проверяется `::test_factory_is_a_module_function`)
- ABC MUST NOT нести блокировок жизненного цикла: режим доступа передаётся
  аргументом фабрики, а не удерживается провайдером (проверяется
  `::test_cache_provider_has_no_lifecycle_locks`)
- Concrete factory MUST вызываться composition root'ом платформы
  (`servers/enterprise/server.py:491`; `capabilities/data/service/main.py:530`),
  а не агентом (`lib/core/application_context.py:761` — метод `_init_cache_runtime`
  удалён)

#### Scenario: Замена concrete implementation

- **GIVEN** cache runtime реализован через `CacheProvider`
- **WHEN** concrete implementation заменяется
- **THEN** runtime consumers MUST NOT require changes
- **AND** `CacheStore` MUST оставаться единственным concrete-facing типом:
  `CacheProvider` (чтение) и `CacheIngestion` (запись) объявлены раздельно, их
  соединение — `CacheStore` (`snapshot/contracts.py:195,285,358`; проверяется
  `::TestProviderAbcContract::test_write_role_is_separate_abc`)
- **AND** замена MUST NOT требовать в агенте ничего: снимок в агенте не открывается
  (`lib/core/application_context.py:31`, `:173-181`)

> Прежняя редакция перечисляла `CacheOwnershipCoordinator` среди потребителей,
> которые MUST NOT require changes. Класса в коде нет: единственное упоминание
> имени осталось в докстринге `snapshot/contracts.py:56`. Владение снято, и
> его появление означало бы возврат слоя, решающего, кому принадлежит файл, при
> единственном заранее известном владельце.

#### Scenario: runtime-consumer код не импортирует concrete cache implementation

- **WHEN** проверяются runtime consumers на импорт concrete cache class
- **THEN** НЕ ДОЛЖНО быть импортов `DuckDbSnapshotStore` за пределами модуля
  снимка, composition root платформы и DuckDB-специфичных тестов
- **AND** `snapshot/contracts.py` MUST NOT импортировать `duckdb`: контракт не
  знает СУБД (проверяется
  `mcp-platform/tests/test_snapshot_contracts.py::TestProviderAbcContract::test_contracts_module_has_no_duckdb_import`)
- **AND** ABC чтения MUST сохранять методы `is_ready`, `preload_indexes`,
  `search_vector`, `query_sql`, `explain`, `get_schema`, `close` — и MUST NOT
  содержать `refresh()` и `check_stale()`, которых в реализации нет
  (`snapshot/contracts.py:223-280`; поиск `def refresh` / `def check_stale` по
  `mcp-platform/libs/enterprise_data/snapshot/*.py` — 0 совпадений; проверяется
  `::TestProviderAbcContract::test_reading_role_methods_present`)

> Замечание для владельца канона, не правится дельтой: `refresh()` и
> `check_stale()` перечислены в `## Public Contract` канона
> (`openspec/specs/data/cache-provider/spec.md:58-59`), но в ABC их нет, и
> `MODIFIED`-блок на прозу не распространяется. См. `proposal.md` § «Что дельта
> не может закрыть».

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

`CacheAccessMode` — абстрактный enum: `READ_WRITE` или `READ_ONLY`. Правка
2026-10-04 — **режим задаёт стадия жизненного цикла, а не результат claim'а**:
`ClaimResult` и `CacheOwnershipCoordinator` больше не существуют: в коде
совпадений нет (по `lib/`, `mcp-platform/libs/`, `mcp-platform/servers/`,
`config.json`); имя класса осталось только в прозе —
`snapshot/contracts.py:56`. Распределённого владения нет: в системе
один gateway, writer снимка один и известен заранее.

- `READ_WRITE` MUST открываться только на стадии пересоздания снимка при старте
  процесса — то есть загрузчиком
  (`snapshot/contracts.py:46-58` — докстринг `CacheAccessMode`:
  «`READ_WRITE` только на стадии пересоздания при старте процесса,
  `READ_ONLY` во всех остальных случаях»; `open_snapshot_store(..., mode=...)` —
  `store.py:1408`)
- Runtime-путь MUST открывать снимок в `READ_ONLY`
  (`servers/enterprise/server.py:493` — `CacheAccessMode.READ_ONLY`)
- Enum MUST быть объявлен ровно один раз (проверяется
  `mcp-platform/tests/test_snapshot_contracts.py::TestCacheAccessMode::test_defined_exactly_once`,
  `::test_values_are_stable_strings`)

В `READ_ONLY` режиме все мутации MUST быть запрещены через **двухуровневую
защиту**, и этот контракт остаётся в силе без изменений:

1. **Concrete adapter MUST открыть storage connection в реальном read-only режиме**
   (для DuckDB: `duckdb.connect(path, read_only=True)`). Сам storage engine не
   позволит мутации.
2. **`CacheProvider.query_sql(...)` MUST поднять `ReadOnlyAssertionError`** при
   INSERT/UPDATE/DELETE.

Оба уровня защиты MUST присутствовать одновременно (defense in depth).

#### Scenario: Concrete adapter открывает storage в реальном read_only режиме

- **WHEN** хранилище создано в mode=READ_ONLY
- **THEN** concrete adapter MUST открыть соединение DuckDB с `read_only=True`, то
  есть на уровне самого storage engine
  (`store.py:334` — `self._duckdb_read_only: bool = bool(mode == CacheAccessMode.READ_ONLY)`;
  `store.py:427` — `duckdb.connect(str(p), read_only=self._duckdb_read_only)`)
- **AND** попытки INSERT/UPDATE/DELETE на уровне SQL MUST быть отклонены storage
  engine независимо от проверок провайдера

#### Scenario: CacheProvider поднимает ReadOnlyAssertionError при INSERT/UPDATE/DELETE в READ_ONLY

- **WHEN** хранилище в mode=READ_ONLY и через `query_sql(...)` вызывается
  INSERT/UPDATE/DELETE
- **THEN** MUST подняться `ReadOnlyAssertionError` ДО выполнения
  (`store.py:575-591` — `_assert_query_allowed()`; `sql_guard.py:101-120` —
  `assert_query_allowed()`: DML при `read_only` → `ReadOnlyAssertionError`)
- **AND** проверка MUST стоять до открытия соединения, а не после: отклонённый
  запрос не должен открывать файл
  (`mcp-platform/tests/test_snapshot_no_file_hold.py::TestNoFileHold::test_ddl_rejection_does_not_open_the_file`)
- **AND** запись через примитивы хранилища MUST быть отвергнута вторым уровнем
  `_assert_writable` — assertion-guard на случай, если соединение переоткрыто в
  RW или код пишет мимо `query_sql` (`store.py:1002-1020`;
  `sql_guard.py:8-11` — «Второй уровень защиты (assertion guard)»)

#### Scenario: query_sql() отклоняет DDL в любом mode

- **WHEN** вызов `query_sql("CREATE TABLE ...")` или `query_sql("DROP TABLE ...")`
  выполнен в любом mode
- **THEN** MUST подняться `UnsupportedSqlError` — DDL запрещён даже в READ_WRITE
  (`sql_guard.py:71`, `:79`, `:116`; `snapshot/contracts.py:105`)
- **AND** класс MUST наследовать `EnterpriseError`, а не `Exception`, и нести
  транспортный код (проверяется
  `mcp-platform/tests/test_snapshot_contracts.py::TestExceptionClasses::test_unsupported_sql_error_is_exception`,
  `::test_every_error_carries_a_code`)

> Прежняя редакция выводила режим из claim'а: «`ClaimResult.acquired=True` →
> `READ_WRITE`, `ClaimResult.acquired=False` → `READ_ONLY`». Ни `ClaimResult`, ни
> claim'а в репозитории нет. Сама двухуровневая защита не изменилась и остаётся
> в силе дословно.

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


## Responsibility

CacheProvider отвечает за:

- предоставление SQL-кэша для read-mostly данных из PostgreSQL
- инкрементальную синхронизацию данных из PostgreSQL в DuckDB — снята
  (`PgDuckDbSyncService` **не перенесён**), осталась только стадия загрузки
  снимка силами `SnapshotLoadService`
- предоставление единого интерфейса доступа (`query_sql`, `get_schema`, `explain`, `search_vector`, `preload_indexes`, `is_ready`, `close`)
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

`CacheProvider` (ABC в `mcp-platform/libs/enterprise_data/snapshot/contracts.py:195`;
агентский `lib/services/cache_provider.py` **снят**) предоставляет:

- `is_ready() -> bool` — готов ли кэш к запросам
- `preload_indexes() -> list[dict]` — прогреть FAISS-индексы в память
- `search_vector(query, index_name, index_path, top_k, threshold) -> list[SearchResult]` — семантический поиск
- `query_sql(sql, params) -> dict` — выполнить SELECT-запрос к SQL-кэшу
- `explain(sql) -> dict` — EXPLAIN без выполнения
- `get_schema(schema_name, table_names) -> dict` — структура таблиц кэша
- `close()` — закрыть открытые ресурсы

`refresh()` и `check_stale()` в ABC **нет**: они были методами снятого агентского
`CacheProvider`, а загрузку снимка теперь делает отдельная стадия —
`SnapshotLoadService` (`mcp-platform/libs/enterprise_data/loader.py:149`); она же
фиксирует время актуальности снимка (`SnapshotLoadResult.finished_at`,
`per_table[*].max_track`, `mcp-platform/libs/enterprise_data/loader.py:128-132`).
Роль чтения и роль записи разведены:
`CacheProvider` (`mcp-platform/libs/enterprise_data/snapshot/contracts.py:195`) и
`CacheIngestion` (`mcp-platform/libs/enterprise_data/snapshot/contracts.py:285`),
объединённые в `CacheStore` (`mcp-platform/libs/enterprise_data/snapshot/contracts.py:358`).

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
(`mcp-platform/libs/enterprise_data/snapshot/store.py:1414`):

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

## State

CacheProvider хранит:

- DuckDB-файл кэша по пути `gateway.cache.local_path`.
- Snapshot'ы таблиц PostgreSQL.
- FAISS-индексы, загруженные в память (`preload_indexes`).
- Кэш сигнатур индексов для контроля целостности.

## Dependencies

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `mcp-platform/libs/enterprise_data/snapshot/contracts.py:195` (`CacheProvider`) —
  ABC интерфейс; агентский `lib/services/cache_provider.py` **снят**
- `mcp-platform/libs/enterprise_data/snapshot/store.py:301` (`DuckDbSnapshotStore`) —
  DuckDB-реализация, открывается фабрикой `open_snapshot_store`
  (`mcp-platform/libs/enterprise_data/snapshot/store.py:1414`); агентские
  `lib/services/duckdb_cache_store.py` (`DuckDbCacheStore`) и
  `lib/services/cache_provider_impl.py` **сняты**
- `mcp-platform/libs/vectors/config.py:36` и
  `mcp-platform/libs/vectors/signature.py:38` — помощники эмбеддингов и подписи
  индекса; агентский `lib/services/cache_provider_impl.py` **снят**, его
  помощники перенесены сюда
- `mcp-platform/libs/enterprise_data/loader.py:149` (`SnapshotLoadService`) —
  разовая загрузка снимка из PostgreSQL, единственный писатель файла; агентский
  `lib/services/pg_duckdb_sync_service.py` (`PgDuckDbSyncService`, инкрементальный
  sync) **снят** вместе с машинерией, а не перенесён
- `mcp-platform/libs/vectors/builder.py:239` (`VectorBuilder`) — сборка
  векторных строк; агентский `lib/services/vector_index_service.py`
  (`VectorIndexService`) **снят**, чтение индексов из снимка идёт через
  `VectorIndexAccessor` (`mcp-platform/libs/enterprise_data/snapshot/store.py:274`)
- `mcp-platform/platform.json → audit.tables` — состав снимка; реестр таблиц
  агента `lib/services/table_registry.py` (`TableRegistry`) **снят**
- `mcp-platform/servers/enterprise/build_index.py` и
  `mcp-platform/libs/vectors/builder.py:239` — наполнение векторного хранилища;
  агентский `tools/build_vectors.py` **снят**

## Configuration

- `gateway.cache.local_path` — путь к локальному DuckDB-файлу (ext4, НЕ NFS).
- `gateway.vector.index.*` — параметры FAISS-индексов (см. `openspec/specs/data/vector-indexes/spec.md`).
- `gateway.vector.index.storage_table` — PG-таблица для хранения эмбеддингов (формат `schema.table`, например `oarb.audit_vectors`).

## Lifecycle

1. **Инициализация**: проверка пути к хранилищу (fail-fast на NFS).
2. **Загрузка**: `SnapshotLoadService` (`mcp-platform/libs/enterprise_data/loader.py:149`)
   разово перезаписывает снимок из PostgreSQL. Метод `CacheProvider.refresh()` у
   снятого агентского `CacheProvider` **не перенесён**: читатель не пишет.
3. **Инкрементальный sync**: снят вместе с `PgDuckDbSyncService`
   (`lib/services/pg_duckdb_sync_service.py` **снят**) — в системе один writer и он
   известен заранее, гонок за файл не обрабатывается.
4. **Preload**: `preload_indexes()` прогревает FAISS-индексы в память.
5. **Обслуживание**: обработка запросов `query_sql` / `search_vector` / `get_schema` / `explain`.
6. **Stale check**: сравнивать метки не с чем — снимок перезаписывается целиком на
   стадии загрузки, а актуальность его фиксирует загрузчик
   (`SnapshotLoadService`); метода `check_stale()` у `CacheProvider` **нет**.
7. **Закрытие**: `close()` освобождает ресурсы (соединения, файлы).

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
- **Stale/invalid index**: `IndexIntegrityError` с статусом `STALE`/`INVALID` и описанием `reason`; вызывающая сторона обязана обработать (например, пересобрать индекс сборщиком capability — `mcp-platform/servers/enterprise/build_index.py`).
- **Config missing**: fail fast при старте (`ConfigurationError`).

## Invariants

- PostgreSQL — источник истины для кэшируемых таблиц.
- Кэш хранится только на локальном ext4 storage.
- Единый интерфейс доступа через `CacheProvider`.
- Все векторные индексы FAISS-backed.
- Сигнатура индекса проверяется перед каждым использованием.

## Forbidden Behavior

Система НЕ ДОЛЖНА:

- записывать DuckDB-файл кэша напрямую на NFS mount (эмпирически fails with `PID 0` locking errors)
- вводить второе хранилище кэша помимо единственной фабрики `open_snapshot_store` (`mcp-platform/libs/enterprise_data/snapshot/store.py:1414`); агентский `cache_provider.py` **снят** и второй точкой входа не является
- молча fallback на PostgreSQL при невалидном кэше; потребители ДОЛЖНЫ быть уведомлены
- обходить `CacheProvider` из кода Skills
- дублировать состояние кэша вне единственного пути к файлу кэша
- открывать FAISS-индексы из кода Skills напрямую
- создавать альтернативный vector storage backend рядом с FAISS без явного OpenSpec change

## Consumers

- Skills (через `CacheProvider`) — SQL-запросы и vector search.
- Владелец индексов capability `vectors` (`VectorIndexOwner`,
  `mcp-platform/libs/vectors/owner.py`) — через `search_vector`; агентский
  `VectorIndexService` **снят**.
- Операции capability `data` платформы — чтение через операции платформы; агентский
  `ApplicationContext` в этой роли **не участвует** (refresh/preload уехали на
  платформу).
- Сборщик capability — наполнение векторного хранилища и сборка FAISS-индексов
  (`mcp-platform/servers/enterprise/build_index.py`,
  `mcp-platform/libs/vectors/builder.py`); агентский `tools/build_vectors.py`
  **снят**.
- Тесты (`mcp-platform/tests/test_snapshot_store.py`,
  `mcp-platform/tests/test_snapshot_load_service.py`); агентские
  `tests/test_duckdb_cache_store.py` и `tests/test_pg_duckdb_sync_service.py`
  **сняты** вместе с предметом.

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
