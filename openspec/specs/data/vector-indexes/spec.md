# Vector Indexes (Векторные индексы)

## Purpose

Определение логической модели для векторных индексов: источник конфигурации, lifecycle индекса, доступ Skills/сервисов и поведение при ошибке. Векторные индексы управляются централизованно и предоставляются capability `vectors` платформы (`mcp-platform/libs/vectors/owner.py`). Прежняя формулировка называла `CacheProvider.search_vector` — метода агентского `CacheProvider` в дереве нет, класс снят вместе с локальным снимком.

## Scope

`platform` — сборка и владение FAISS-индексами уехали в capability `vectors`; агент индексы не строит и не хранит
Реализация: `mcp-platform/libs/vectors/`, объявления в `platform.json → vectors.indexes`

## Requirements

> **Блок ниже — не текущая норма: он описывает снятую агентскую подсистему и будет
> заменён при архивации change'ов. Читать его как живое предписание нельзя.**
> Владение индексами объявлено в `mcp-platform/platform.json → vectors.indexes`,
> реализация — в `mcp-platform/libs/vectors/`, и именно это описывают остальные
> разделы спеки (`## Scope`, `## Boundary`, `## Inputs`, `## Outputs`,
> `## Configuration`, `## Lifecycle`, `## Data Ownership`, `## Invariants`).
> Перечисленные ниже адреса **в дереве отсутствуют**, и вводить их не следует:
> `tools/build_vectors.py`, `lib/services/vector_index_service.py`,
> `lib/core/infra_registration.py`, `lib/services/preload_service.py`,
> `lib/services/cache_provider.py` (`CacheProvider.search_vector`),
> `provider.preload_indexes(...)` из уже удалённого `vector_index_service.py`,
> сигнал `READY` от `runtime-health` агента, а также секция
> `gateway.vector.index.*` — такой секции в `config.json` нет вовсе.
>
> Замена объявлена в
> `openspec/changes/2026-10-05-vector-indexes-canon-gap` (4 `REMOVED`,
> 3 `MODIFIED`, 1 `ADDED`) и
> `openspec/changes/2026-10-05-vector-preload-canon` (`MODIFIED` требования
> «Прогрев индексов при старте»). Все 8 требований этого блока покрыты ими
> ровно по одному разу и по дословному имени, то есть пересечений нет.
> Каноническая спека правится **после** архивации change'а
> (`openspec/specs/COMPONENTS.md:9-14`: workflow change → archive → canonical spec
> → entry в реестре), поэтому переписывание тел здесь было бы третьей редакцией
> и сломало бы сопоставление требований по имени при архивации.

### Requirement: Hydrated payload берётся из DuckDB-снапшота

Система SHALL подтягивать `content` / `search_text` / `row_data` для каждого
FAISS-hit'а из таблицы-источника по ключу `(source, pk_value, chunk_index)`; она
SHALL NOT читать эти поля из метаданных, сериализованных в индекс.

Прежняя редакция привязывала источник к `gateway.vector.index.storage_table`.
Ключ удалён; источник теперь определяется объявлением
`platform.json → vectors.indexes`.

> Заголовок требования оставлен дословно как в каноне
> (`openspec/specs/data/vector-indexes/spec.md:116`): OpenSpec сопоставляет
> требования по имени, и переименование заголовка превратило бы `MODIFIED` в
> новое требование — канонное осталось бы жить и требовало бы читать
> `gateway.vector.index.storage_table`. Меняется тело, не имя.

#### Scenario: Подтягивание payload

- **WHEN** поиск по индексу выдаёт результат с `pk_value=P` и `chunk_index=K`
- **THEN** система SHALL получить `(content, search_text, row_data)` из
  таблицы-источника по `source = <index_name> AND pk_value = P AND chunk_index = K`
- **AND** SHALL NOT читать их из сериализованного в индексе

#### Scenario: Индекс без payload

- **WHEN** FAISS-hit найден, а строка источника отсутствует
- **THEN** отказ SHALL быть явным, без silent fallback на данные из индекса

### Requirement: Агент не объявляет состав индексов

Секции `gateway.vector.index` и `skills.<name>.vector_indexes` MUST
отсутствовать в `config.json`, а модели конфигурации — исключены из
`lib/core/project_settings.py`. Пока секция объявлена, но не читается, она выглядит
владельцем: её значения печатаются как ожидаемые в `tests/test_config_keys.py`, а
`VectorIndexConfig` называет себя единственным источником деталей построения.

Агент, не объявляющий состав индексов, не строит их и не прогревает: постройка
принадлежит capability `vectors` и выполняется ею на старте сервера, до event loop
(`_prepare_capabilities`). Бюджет первичной сборки переезжает сюда из требования о
прогреве, а не исчезает вместе с ним, — но платит его старт платформы, а не первый
пользовательский запрос.

#### Scenario: Секция удалена, тест это утверждает

- **WHEN** `gateway.vector.index` или `skills.<name>.vector_indexes` удалены из
  `config.json`, из моделей и из ассертов `tests/test_config_keys.py`
- **THEN** тест SHALL утверждать отсутствие этих секций, а не их значения

#### Scenario: Секция вернулась

- **WHEN** кто-то добавит `gateway.vector.index` в `config.json` снова
- **THEN** `tests/test_config_keys.py` SHALL упасть, а не принять значение
  молча

#### Scenario: Правка канона вместо кода

- **WHEN** нормативный текст упоминает `gateway.vector.index.indexes` как
  источник состава индексов
- **THEN** такое упоминание SHALL считаться расхождением, и это расхождение
  SHALL выявляться grep-проверкой из `tasks.md` п. 4.6 либо правкой спеки
- **AND** автоматического guard'а, который бы ловил такое упоминание, спека не
  обещает: `tools/validate_component_specs.py` (`:277-318`) проверяет только
  наличие `### Requirement:`, наличие `#### Scenario:` и маркер WHEN/THEN
  (`:106-110`), по содержимому требований не смотрит

#### Scenario: Имена индексов приходят от платформы, а не из навыка

Состав индексов объявляет `platform.json → vectors.indexes`, и этот факт должен
быть виден модели, иначе объявление остаётся вещью для чтения людьми: она не
сможет ни спросить, что есть, ни позвать поиск по имени, которого не знает.

- **WHEN** модели нужен `index_name` для `vector_search`
- **THEN** имя ДОЛЖНО приходить из операции `list_indexes`, объявленной модели в
  `config.json → tools.mcpServers.enterprise.enabled_tools`
- **AND** навык ДОЛЖЕН NOT перечислять имена индексов: перечисление — копия
  объявления, и оно расходится с платформой молча, пока кто-то не отредактирует
  навык руками
- **AND** `index_stats` ДОЛЖНА остаться не объявленной модели: состояние индекса
  доступно через `list_indexes` и через `index_state` в ответе `vector_search`,
  поэтому отдельная операция модели ничего не добавляет

#### Scenario: Смена состава индексов не требует правки агента

- **WHEN** в `mcp-platform/platform.json → vectors.indexes` добавлен индекс
- **THEN** модель ДОЛЖНА увидеть его в ответе `list_indexes` без изменения
  `config.json`, кода навыка и кода спецификации
- **AND** ДОЛЖЕН NOT требовать пересборки агента: перечисление имён в навыке
  запрещено предыдущим сценарием, поэтому копировать нечего

#### Scenario: Стоимость первичной постройки

Перенесено дословно из снятого требования
«FAISS собирается в памяти из DuckDB-снапшота», сценарий «Стоимость холодного
старта» (`openspec/specs/data/vector-indexes/spec.md:106-109`): ленивая постройка
не отменяет его, а меняет только то, кто её выполняет.

- **WHEN** первый `vector_search` для `index_name` вызван до того, как индекс
  этого `index_name` собран
- **THEN** первичная постройка SHALL завершаться за ≤ 5 секунд на эталонной
  рабочей станции для индексов до 20 000 векторов × 1024
- **AND** отказ от прогрева ДОЛЖЕН NOT поднимать этот порог до отведённого
  пользовательского таймаута: холодный поиск — штатный путь, и его стоимость
  обязана быть видна в измеримом пороге, а не в снятом требовании

### Requirement: Конфигурация индексов читается только из platform.json

Система MUST читать состав и параметры векторных индексов **только** из
`mcp-platform/platform.json → vectors.indexes`, объявленного capability `vectors`.
Агент MUST NOT держать собственный список индексов, ни в `config.json`, ни в
`gateway.*`, ни в `skills.*`.

Прежняя редакция требовала читать `gateway.vector.index.indexes.*` из
`config.json`. Секция объявлена и провалидируется, но не читается: grep по `lib/`
даёт только объявления моделей и docstring-комментарии
(`lib/core/project_settings.py:186,481`). Её собственный докстринг называет секцию
«единственным источником деталей построения индекса» (`:484`), то есть нормативный
текст и код описывали источник истины, которого нет.

#### Scenario: Состав индексов в одном месте

- **WHEN** векторный индекс добавлен или изменён
- **THEN** его декларация ДОЛЖНА находиться в
  `mcp-platform/platform.json → vectors.indexes`
- **AND** агент ДОЛЖЕН NOT объявлять его ни в `gateway.vector.index.indexes.*`,
  ни в `skills.<name>.vector_indexes`

#### Scenario: Смена состава индексов без релиза

- **WHEN** оператор меняет состав или параметры объявления в
  `mcp-platform/platform.json → vectors.indexes`
- **THEN** система SHALL использовать новое объявление без изменений в коде
  спецификации или runtime-коде, требующих релизов
- **AND** ДОЛЖЕН NOT требовать пересборки агента: смена состава — операция
  контура платформы, а не агента

#### Scenario: Агентский список индексов отвергается

Две половины проверяются разными механизмами, и различать их обязательно: обе
возникают **только после реализации этого change**, но путём разным.

- **WHEN** в `config.json` присутствует `skills.<name>.vector_indexes`
  (или `skills.<name>.tables`)
- **THEN** валидация конфигурации ДОЛЖНА отвергнуть секцию как неизвестный
  ключ
- **AND** отказ ДОЛЖЕН быть вызван `extra="forbid"` модели `SkillSettings`
  (`lib/core/project_settings.py:663`), но сработать он сможет **только после
  удаления полей** `SkillSettings.tables` (`:666`) и
  `SkillSettings.vector_indexes` (`:667`): сегодня это **объявленные** поля
  модели, а `extra="forbid"` типизирует объявленное поле, а не отвергает его, —
  обе секции валидируются (`config.json:817,835`). Удаление объявлено в
  `tasks.md` п. 3.3

- **WHEN** в `config.json` присутствует `gateway.vector.index`
- **THEN** валидация конфигурации ДОЛЖНА отвергнуть секцию явно, а не принять
  её молча как лишний ключ
- **AND** отказ ДОЛЖЕН быть наблюдаемым: `ConfigurationError` на старте, а не
  «всё стартануло, но объявление нигде не читается»

> **Маршрут реализации — решение этого change, а не уже работающий механизм.**
> В ветке `gateway.*` отвержения нет: `_StrictOptional` объявлен как
> `extra="allow"` (`lib/core/project_settings.py:55-58`), `GatewaySettings`
> (`:237`) своего `model_config` не имеет, а `ProjectSettings` (`:759`) —
> тоже. Единственный действующий отвергатель в этой ветке —
> валидатор `_reject_legacy_renamed_sections` (`:253-261`) поверх списка
> `_LEGACY_GATEWAY_KEYS` (`:782-784`), и он знает ровно одну секцию —
> `gateway.vector_index` **без точки** (legacy-путь, а не текущий
> `gateway.vector.index`). Поэтому требование отвержения `gateway.vector.index`
> невыполнимо в текущем виде: без явного решения (добавить путь в
> legacy-guard как fail-fast либо запретить секцию иначе) ключ будет принят
> как лишний. Реализация зафиксирована в `tasks.md` п. 3.9 (выбор механизма),
> 3.3 (удаление моделей) и 3.6 (тест, утверждающий отказ).

#### Scenario: Расхождение двух списков невозможно выразить

- **WHEN** оператор правит состав индексов
- **THEN** система ДОЛЖНА находить ровно одно объявление, потому что второго
  места для него в агенте не осталось
- **AND** система ДОЛЖЕН NOT применять правило приоритета между двумя
  источниками: выбирать «более новый» из двух нечего, если объявление одно

### Requirement: Vector search доступен только через операцию vectors

Система MUST предоставлять vector search модели исключительно через операцию
capability `vectors` (`mcp_enterprise_vector_search`), а коду платформы — через
владельца индексов `mcp-platform/libs/vectors/`.

Прежняя редакция требовала `CacheProvider.search_vector` и запрещала Skill-коду
обращаться к FAISS напрямую. `CacheProvider` удалён вместе с локальным кэшем;
Skill, которому был нужен vector search, тоже уехал в платформу
(`skills/legal-summarizer-query` закрыт change `2026-10-03-mcp-native-tools`, п. D6).
Запрет «обходить владельца индексами» сохраняется, но адресат у него изменился.

#### Scenario: Модель ищет по вектору

- **WHEN** модели нужен vector similarity query
- **THEN** она ДОЛЖНА вызвать операцию `vector_search` capability `vectors`
- **AND** поиск ДОЛЖЕН пройти через владельца индексов, а не через прямой
  доступ к FAISS

#### Scenario: Обход владельца

Маршрут поиска, объявленный соседним каноном
(`openspec/specs/data/cache-provider/spec.md`, дельта
`2026-10-04-close-cache-provider-canon-gap/specs/data/cache-provider/spec.md:504-506`),
таков и здесь: навык → операция `vector_search` → `search_vector` хранилища
(`mcp-platform/libs/enterprise_data/snapshot/store.py`) → `mcp-platform/libs/vectors/`.
Обход — любой выход за `libs/vectors/` на этом маршруте.

- **WHEN** код capability или библиотеки обращается к FAISS в обход
  `mcp-platform/libs/vectors/`
- **THEN** такой доступ ДОЛЖЕН считаться нарушением контракта владения, даже
  если технически работает
- **AND** на стороне платформы поиск MUST идти через `search_vector` хранилища,
  делегирующий владельцу индексов, а не через прямой `import faiss` в хранилище

### Requirement: Отказ прогрева индекса не роняет старт платформы

Платформа SHALL собирать векторный индекс каждого **объявленного и включённого**
источника (`platform.json → vectors.indexes`, поле `enabled` не равно `false`)
**до начала обслуживания запросов**: `ensure_index` вызывается из
`servers/enterprise/server.py::_prepare_capabilities` до старта event loop.
Индекс с `enabled=false` SHALL NOT собираться, и его отсутствие в памяти SHALL
NOT считаться дефектом. Источник, присутствующий в снимке, но отсутствующий в
объявлении, SHALL NOT собираться — у него не объявлены ни метрика, ни
размерность, ни подпись — и SHALL NOT считаться неготовым индексом.

Система SHALL NOT собирать индекс лениво, на пользовательском запросе:
холодный поиск SHALL NOT быть штатным путём, и ошибка «индекс не прогрет» SHALL
NOT подменять собой отказ прогрева.

Отказ сборки SHALL быть назван в логе процесса с именем индекса и SHALL быть
виден в `list_indexes` через `state` (`missing` / `error`) и `error`. Снимок по
контракту необязателен (`OPTIONAL`), поэтому отказ SHALL NOT ронять старт
процесса: подняться без снимка — законное состояние, а не дефект. Проверка
готовности на стороне агента SHALL судить **объявленные и включённые** индексы;
незаявленный источник из снимка SHALL быть назван отдельно, как расхождение
объявления и данных, а не как неготовность.

Прежняя редакция требовала `provider.preload_indexes(db_table)` до сигнала
`READY`, запрещала ленивую постройку и срывала старт при отказе. Это не
соответствует ни коду (`ensure_index` в `_prepare_capabilities`, `:901-927`),
ни `CHANGELOG.md:121-122`, ни контракту `OPTIONAL` снимка. Имя требования
сохранено дословно: OpenSpec сопоставляет требования по имени, и переименование
превратило бы `MODIFIED` в новое требование, а старое осталось бы жить.

#### Scenario: Индексы готовы до первого запроса

- **WHEN** процесс платформы стартует с подключённым снимком
- **THEN** к моменту обслуживания запросов FAISS-индекс каждого объявленного и
  включённого источника SHALL находиться в памяти
- **AND** `list_indexes` SHALL отдавать по ним `state="ready"`
- **AND** первый же `vector_search` SHALL NOT платить за сборку индекса

#### Scenario: Отключённый объявлением индекс не собирается

- **WHEN** источник объявлен в `platform.json → vectors.indexes` с
  `enabled=false`
- **THEN** его сборка SHALL NOT выполняться на старте
- **AND** его `state="missing"` SHALL NOT считаться неготовностью: отсутствие
  индекса в памяти — исполнение объявления, а не отказ
- **AND** проверка готовности агента SHALL исключать его из знаменателя и
  называть его отключённым

#### Scenario: Незаявленный источник из снимка

- **WHEN** в снимке есть векторный источник, которого нет в
  `platform.json → vectors.indexes`
- **THEN** он SHALL NOT собираться на старте
- **AND** он SHALL NOT делать проверку готовности нездоровой: отсутствие
  объявления — расхождение объявления и данных, а не поломка поиска
- **AND** он SHALL быть назван в сводке агента своим именем

#### Scenario: Снимок не настроен

- **WHEN** оператор не задал путь к снимку
- **THEN** процесс SHALL подняться
- **AND** capability `vectors` SHALL отвечать «снимок не подключён», а не
  «индексов нет»: различать «не работает» и «не настроено» обязательно

#### Scenario: Отказ сборки назван, а не проглочен

- **WHEN** сборка индекса падает при подключённом снимке
- **THEN** процесс SHALL подняться, потому что снимок необязателен
- **AND** отказ SHALL быть назван в логе с именем индекса
- **AND** `list_indexes` SHALL отдавать по этому индексу `state="error"` с
  текстом отказа, а не `state="ready"`

#### Scenario: Стоимость первичной сборки платит старт, а не поиск

- **WHEN** оценивается стоимость сборки индекса
- **THEN** она SHALL входить в бюджет старта платформы, а не в ответ на
  пользовательский запрос
- **AND** для индекса 20 000 векторов размерности 1024 ориентир SHALL
  оставаться прежним — не более 5 секунд на сборку: величина не изменилась,
  изменился момент её уплаты



## Responsibility

Vector Indexes отвечают за:
- объявление состава и параметров векторных индексов — единственный источник
  `mcp-platform/platform.json → vectors.indexes`
- владение FAISS-индексами в памяти процесса платформы: сборка на старте
  сервера, состояние индекса, подпись индекса и её сверка с текущей
  конфигурацией процесса
- предоставление vector search модели через операцию `vectors.vector_search`
- подтягивание `content` / `search_text` / `row_data` найденных чанков из
  таблицы-источника, а не из метаданных индекса

## Boundary

### Owns
- объявлением состава и параметров векторных индексов: `mcp-platform/platform.json → vectors.indexes`
- сборкой и хранением FAISS-индексов в памяти процесса платформы; владелец —
  `mcp-platform/libs/vectors/`
- подписью индекса и её сверкой с текущей конфигурацией процесса
  (`mcp-platform/libs/vectors/signature.py`)
- предоставлением vector search модели через операцию `vectors.vector_search`

### Does Not Own
- владением файлом снимка DuckDB: снимок принадлежит capability `data`
  (`mcp-platform/libs/enterprise_data/snapshot/`)
- прямым доступом к FAISS из кода вне `mcp-platform/libs/vectors/`
- бизнес-логикой Skills
- альтернативными vector storage backends

### May Depend On
- `mcp-platform/libs/vectors/` — владелец индексов
- `mcp-platform/libs/enterprise_data/snapshot/store.py` — владелец снимка: `search_vector`,
  чтение векторов и payload чанка
- FAISS library — движок индекса
- эмбеддера capability `llm` (`platform.json → llm.embed_*`) — вектор запроса
- PostgreSQL — источник строк для наполнения векторного хранилища

### Must Not Depend On
- конфигурации агента (`config.json`): состав и параметры индексов там не объявляются
- конкретной реализации Skills
- других vector storage implementations

## Public Contract

Векторный поиск модели предоставляет операция `vectors.vector_search`
(`mcp-platform/servers/enterprise/capabilities/vectors/tools/vector_search.py`):
- параметры: `query`, `index_name`, `top_k`, `threshold`
- ответ: `index_name`, `index_state`, `found`, `results[]` с `content`, `score`,
  `source`, `table`, `pk_value`, `chunk`, `matched_chunks`, `row`;
  `index_state` — состояние индекса на момент поиска (`missing` / `building` /
  `ready` / `error`)

Каталог индексов модели отдаёт операция `vectors.list_indexes`
(`mcp-platform/servers/enterprise/capabilities/vectors/tools/list_indexes.py`),
объявленная модели в `config.json → tools.mcpServers.enterprise.enabled_tools`:
имена, состояние, число векторов и размерность — без поднятия FAISS. Навык
(`workspace/skills/audit_analyzer/SKILL.md`) имена индексов **не перечисляет**:
он берёт их из `vectors.list_indexes`, поэтому копии состава объявления в агенте нет и
новый индекс в `platform.json` не требует правки агентных файлов.

`vectors.index_stats` остаётся диагностической операцией capability: файлы есть,
модели она не объявлена, а состояние индекса доступно через `vectors.list_indexes`
(весь каталог) и через `index_state` в ответе `vectors.vector_search`.

Маршрут поиска: навык → операция `vectors.vector_search` → `search_vector` хранилища
(`mcp-platform/libs/enterprise_data/snapshot/store.py`) → `mcp-platform/libs/vectors/`.
Выход за `mcp-platform/libs/vectors/` на этом маршруте — обход владельца индексов.

## Inputs

Capability `vectors` читает четыре источника, и ни один из них она не добывает сама:

- **Объявление индексов и имя таблицы хранения** — из конфигурации платформы,
  приведённой к pythonic-виду: `read_vector_index_config` разворачивает
  `gateway.vector.index.indexes` в `{имя: {table, pk, source_table, content_columns,
  embedding_columns, track_column, chunk_size, chunk_overlap, metric, enabled}}`, а
  `read_vector_storage_table` отдаёт `schema.table` хранилища. Незаданное значение
  остаётся `None`, а не подставляется дефолтом: подпись индекса обязана отражать
  конфигурацию процесса
  (`mcp-platform/libs/vectors/config.py:69-103`, `:106-110`)
- **Параметры эмбеддинга, входящие в подпись** — модель, размерность и таймаут
  эмбеддера; адрес и ключ провайдера здесь не читаются принципиально, это зона
  `libs/llm` (`mcp-platform/libs/vectors/config.py:36-54`, `:15-21`)
- **Строки векторного хранилища из снимка** — по протоколу `SnapshotReader`
  (`vector_source_stats`, `fetch_source_vectors`, `fetch_chunk_payload`). Это
  структурный тип: снимок принадлежит capability `data`, владелец индексов получает
  её оттуда и **не открывает файл снимка сам**, поэтому `duckdb` в
  `mcp-platform/libs/vectors/` не импортируется ни одним модулем
  (`mcp-platform/libs/vectors/owner.py:57-77`; единственная функция, читающая
  хранилище напрямую, делает это через обязательный `fetch_fn` —
  `mcp-platform/libs/vectors/runtime.py:28-32`)
- **Эмбеддер запроса** — подставляется сверху (`VectorIndexOwner(embed=...)`);
  у владельца индексов нет ни адреса провайдера, ни HTTP-клиента, поэтому
  отсутствие эмбеддера — `InfrastructureError` на сборке, а не «поиск без
  эмбеддинга» (`mcp-platform/libs/vectors/owner.py:103-129`)

Неприменимо: прямого чтения конфигурационных **файлов** (в отличие от процесса
сервера) внутри capability нет — конфигурация передаётся параметром, иначе
`platform.json` стал бы вторым источником наряду с сервером
(`mcp-platform/libs/vectors/config.py:7-13`).

## Outputs

- **`vectors.list_indexes` / `vectors.index_stats`** — каталог и метрики индекса
  (`index_name`, `state`, `declared`, `enabled`, `vector_count`, `dimension`,
  `metric`, `error`, `error_code`; для статистики ещё `last_built_at`, `queries`,
  `builds`). Обе операции отвечают **не поднимая FAISS**: размерность и количество
  векторов берутся из строк снимка, поэтому работают и для ещё не построенного
  индекса, у которого `last_built_at` равен `None`
  (`mcp-platform/libs/vectors/owner.py:158-216`)
- **`vectors.vector_search`** — список `SearchResult` с `content`, `score`,
  `source`, `table`, `pk_value`, `chunk`, `matched_chunks`, `row`. FAISS даёт
  только id и скор, а payload каждого хита дочитывается из снимка по
  `(source, pk_value, chunk_index)` и хиты группируются по исходной строке
  (`mcp-platform/libs/vectors/owner.py:346-412`;
  `mcp-platform/libs/vectors/grouping.py:24-76` — дочитывание payload по ключу,
  `:92-122` — группировка хитов)
- **Состояние индекса** — `missing` / `building` / `ready` / `error`, наблюдаемое
  в каждом ответе и в выдаче прогрева: «индекс не поднят» видно, а не прячется за
  пустой выдачей (`mcp-platform/libs/vectors/owner.py:50-54`, `:318-340`)
- **Отказы вместо пустой выдачи** — `NotFoundError`, `IndexIntegrityError`
  (`stale_index` / `invalid_index`), `InfrastructureError` с кодом
  (`mcp-platform/libs/vectors/owner.py:356-374`, `:362-366`)

Неприменимо: артефактов на диске у capability нет — FAISS-индекс не персистится ни
файлом, ни строкой, поэтому «выход» наружу в файловую систему невозможен
(`mcp-platform/libs/vectors/indexing.py:1-10`).

## State

Состояние индекса принадлежит capability `vectors` и наблюдаемо в ответе операции
как `index_state`: `missing` → `building` → `ready`, либо `error` с кодом и
текстом. На каждый `index_name` владелец индексов держит состояние, сам
FAISS-индекс, метаданные, время сборки и счётчики `queries` / `builds`.

Персиста у индекса нет — ни файла индекса, ни таблицы подписи: индекс существует
только в памяти процесса, и его нельзя прочитать из другого процесса.

## Dependencies

- `mcp-platform/platform.json → vectors` — объявление состава, выключатель capability,
  инфраструктурная таблица хранения векторов
- `mcp-platform/libs/vectors/` — владелец индексов: сборка, подпись, группировка
  чанков, состояние
- `mcp-platform/libs/enterprise_data/snapshot/store.py` — владелец снимка: `search_vector`,
  чтение векторов и payload чанка
- `mcp-platform/servers/enterprise/capabilities/vectors/tools/` — операции
  `vectors.vector_search`, `vectors.list_indexes`, `vectors.index_stats`
- `mcp-platform/servers/enterprise/build_index.py` — наполнение векторного хранилища
- `mcp-platform/docs/MCP-CONTRACTS.md` — контракт операций capability
- `mcp-platform/docs/TARGET-ARCHITECTURE.md` — архитектурные принципы платформы
- FAISS library — vector index engine
- `docs/ARCHITECTURE.md` — раздел о владении данными: агент данных не держит,
  состав векторной инфраструктуры объявляет платформа

## Configuration

Состав и параметры векторных индексов объявляет capability `vectors` в
`mcp-platform/platform.json → vectors`. Это единственный источник: агент своего
списка индексов не держит, ни в `config.json`, ни в `gateway.*`, ни в `skills.*`.

```json
{
  "vectors": {
    "enable": true,
    "storage_table": "oarb.audit_vectors",
    "indexes": {
      "violations_index": {
        "table": "oarb.violations",
        "pk": "id",
        "source_table": "violations",
        "content_columns": [
          "description",
          "recommendation",
          "violation_code",
          "severity"
        ],
        "embedding_columns": [
          {
            "column": "description",
            "chunk": true,
            "chunk_size": 500,
            "chunk_overlap": 80
          },
          "violation_code"
        ],
        "track_column": "updated_at",
        "chunk_size": 500,
        "chunk_overlap": 80,
        "metric": "cosine",
        "enabled": true
      }
    }
  }
}
```

`storage_table` — инфраструктурная таблица хранения векторов внутри снимка; она же
таблица эмбеддингов, поэтому второй настройки для того же факта не заводится.
`table`, `pk` и `source_table` задают, по каким строкам и под каким именем индекс
живёт в снимке; `track_column` — по какой колонке отслеживается изменение строки;
`metric` — мера близости.

`embedding_columns` — что эмбедится: значения этих колонок объединяются в один
текст `search_text` с подписями колонок, и уже он эмбедится, поэтому набор
колонок больше одного даёт один вектор на чанк. Колонка может быть задана строкой
либо объектом со своими chunk-параметрами — второй формой задают разбиение.
Незаданный `embedding_columns` означает `content_columns`. `content_columns` — что
возвращается в `content`, то есть текст, который увидит человек.

`enabled: false` исключает индекс из сборки. Объявление без `table` или `pk`
пропускается как неполное, а не роняет сборку.

Объявление читается как есть и разворачивается в pythonic-вид
(`{имя: {table, pk, source_table, content_columns, embedding_columns, track_column,
chunk_size, chunk_overlap, metric, enabled}}`) — `mcp-platform/servers/enterprise/server.py:507::_vectors_config`
собирает его из настроек capability, `mcp-platform/libs/vectors/config.py:69::read_vector_index_config`
приводит форму. Размерность эмбеддинга и таймаут эмбеддера объявлены отдельно
(`platform.json → llm.embed_dimension`, `llm.embed_timeout`) и входят в подпись
индекса, но объявлением индекса они не являются.

Смена состава или параметров — операция контура платформы: она применяется без
изменений в коде и без релиза, и пересборки агента не требует.

Пустой `vectors.indexes` — законное состояние «индексов не объявлено», а не ошибка.
Отсутствующий ключ в файле — ошибка конфигурации: у настроек платформенного контура
значений по умолчанию в коде нет, и сервер на старте называет недостающий ключ.

## Lifecycle

1. **Объявление**: состав, параметры и таблица хранения объявляет capability `vectors`
   в `mcp-platform/platform.json → vectors`
2. **Наполнение**: векторы пишет в инфраструктурную таблицу хранения сборщик
   capability (`mcp-platform/servers/enterprise/build_index.py`); агент в этом шаге
   не участвует
3. **Сборка**: FAISS-индекс собирается в памяти на старте сервера, до event
   loop, — `mcp-platform/servers/enterprise/server.py:893::_prepare_capabilities` зовёт
   `ensure_index` (`mcp-platform/libs/vectors/owner.py:244`) для каждого объявленного и включённого `index_name`. Холодного
   поиска не остаётся: первый запрос уже работает с готовым индексом. Параллельные
   обращения к одному индексу дают ровно одну сборку, в том числе при прогреве на
   старте. Первичная сборка укладывается в ≤ 5 секунд на эталонной рабочей станции
   для индексов до 20 000 векторов × 1024; величина не изменилась, изменился момент
   её уплаты — старт платформы вместо первого пользовательского запроса
4. **Поиск**: `vectors.vector_search` эмбедит запрос, сверяет подпись индекса с текущей
   конфигурацией процесса и ищет по FAISS; payload найденных чанков
   (`content` / `search_text` / `row_data`) подтягивается из таблицы-источника по
   ключу `(source, pk_value, chunk_index)`
5. **Обновление**: векторы добавляет сборщик capability. Индекс, собранный в
   процессе, живёт до конца его жизни; новое объявление или новые векторы учтутся
   в следующем процессе платформы, а расхождение подписи с текущим объявлением
   отражается отказом, а не молчаливой выдачей

## Data Ownership

Владеет ровно одним: **FAISS-индексами в памяти одного процесса платформы**.
Экземпляр `VectorIndexOwner` создаётся один раз в конструкторе capability, и от
этого зависит «одна сборка на процесс»; конструктор ничего не читает и не строит
(`mcp-platform/libs/vectors/owner.py:96-101`, `:99-100`).

- **В памяти, без персиста.** Индекс живёт до конца жизни процесса; на диске и в
  строках FAISS-блобов не хранится, поэтому переживать перезапуск нечему и
  «устаревшая копия на диске» невозможна
  (`mcp-platform/libs/vectors/indexing.py:1-10`;
  `mcp-platform/libs/vectors/runtime.py:35-38` — runtime-состояние это набор
  `source` в таблице эмбеддингов, а не сохранённый индекс)
- **Состояние и счётчики** — по слоту на `index_name`: `state`, `index`, `meta`,
  `built_at`, `queries`, `builds`, `error`, `error_code`; чтение без локера,
  запись под ним (`mcp-platform/libs/vectors/owner.py:80-93`, `:397-398`)
- **Единственный владелец тяжёлого ресурса** — `faiss` импортируется только здесь и
  только в модулях сборки и поиска; хранилище снимка FAISS не импортирует, оно
  отдаёт строки (`mcp-platform/libs/vectors/indexing.py:6-9`, `:92`)

Не владеет (и не владеет ничем из перечисленного агентом):

- **файлом снимка** — он принадлежит capability `data`; владелец индексов получает
  доступ структурным протоколом `SnapshotReader` и не разрешает путь к файлу
  (`mcp-platform/libs/vectors/owner.py:57-63`;
  `mcp-platform/libs/vectors/runtime.py:6-11` — собственный `duckdb.connect` и
  self-resolve пути здесь уже убраны)
- **эмбеддером** — вызов эмбеддера принадлежит capability `llm`, владелец индексов
  получает готовый объект сверху (`mcp-platform/libs/vectors/owner.py:118-129`)
- **строками эмбеддингов и payload** — их пишет сборщик capability
  (`mcp-platform/libs/vectors/builder.py:308-310`, `:576-605`), а владелец индексов
  читает и возвращает их содержимое, не становясь их хозяином
  (`mcp-platform/libs/vectors/owner.py:308-315`)

## Error Behavior

- Неизвестный индекс (нет ни в объявлении, ни в снимке) → отказ `not_found`, а не
  пустая выдача
- Пустой `query` → `invalid_request`
- Расхождение подписи индекса с текущей конфигурацией процесса →
  `IndexIntegrityError` с кодом `stale_index` / `invalid_index`
- Снимок недоступен → отказ инфраструктуры (`infrastructure_error`): снимок
  принадлежит capability `data`, и его недоступность — не «пустое нашлось»
- Эмбеддер не вернул вектор запроса → отказ инфраструктуры, а не пустой результат
- Ошибка сборки индекса → состояние `error` с кодом и текстом и явный отказ;
  следующий поиск в том же процессе повторяет сборку
- Отсутствующее объявление в `platform.json` → отказ конфигурации на старте сервера
  с именем недостающего ключа

Коды операции перечислены в `mcp-platform/docs/MCP-CONTRACTS.md` §5.1; молчаливого
перехода на другой backend при любом из этих отказов нет.

## Invariants

- Состав и параметры индексов читаются только из
  `mcp-platform/platform.json → vectors`; второго источника нет, и правило
  приоритета между двумя списками не применяется
- Векторными индексами не владеет ни один компонент агента: агент их не строит,
  не хранит и не прогревает
- Все индексы FAISS-backed, в памяти, без персиста
- Сборка индекса идёт на старте сервера, до event loop, и ровно одна на процесс
- Payload (`content` / `search_text` / `row_data`) читается из таблицы-источника,
  а не из метаданных, сериализованных в индекс
- Поиск идёт единственным маршрутом: операция `vectors.vector_search` → `search_vector`
  хранилища → `mcp-platform/libs/vectors/`
- Расхождение подписи индекса с текущей конфигурацией — отказ, а не молчаливая
  деградация и не пустая выдача

## Forbidden Behavior

Система НЕ ДОЛЖНА:

- читать состав и параметры векторных индексов откуда-либо, кроме
  `mcp-platform/platform.json → vectors.indexes` — ни из конфигурации агента, ни из
  SQL-реестра
- создавать второй vector storage backend рядом с FAISS без отдельного OpenSpec change
- молча переходить на non-FAISS backend при ошибках FAISS
- обращаться к FAISS в обход `mcp-platform/libs/vectors/`
- возвращать пустой результат вместо отказа, когда индекс не найден, его подпись
  разошлась с объявлением, снимок недоступен или эмбеддер не ответил
- заводить в агенте standalone-сборку индексов: наполнением векторного хранилища
  занимается сборщик capability (`mcp-platform/servers/enterprise/build_index.py`)

## Consumers

- Модель (навык) — операция `vectors.vector_search`; в дереве агента
  её называет `workspace/skills/audit_analyzer/SKILL.md`
- Владелец снимка — `search_vector`
  (`mcp-platform/libs/enterprise_data/snapshot/store.py`), делегирующий владельцу
  индексов
- Оператор — диагностические операции `vectors.list_indexes`
  и `vectors.index_stats`, которые отвечают, не поднимая индекс

## Implementation

Владелец индексов — capability `vectors`:
- `mcp-platform/libs/vectors/owner.py:VectorIndexOwner` — ленивая сборка,
  single-flight, состояние индекса, поиск
- `mcp-platform/libs/vectors/indexing.py` — `build_faiss_index`, `as_vector`,
  `vector_dimension`
- `mcp-platform/libs/vectors/signature.py` — `compute_index_signature`,
  `verify_index_signature`
- `mcp-platform/libs/vectors/grouping.py` — `build_raw_items`, `group_vector_hits`:
  чанки сворачиваются в документы
- `mcp-platform/libs/vectors/config.py` — чтение объявления индексов и параметров
  эмбеддинга
- `mcp-platform/libs/vectors/preload.py` — `compute_index_health`,
  `format_index_health_lines`

Связанные компоненты:
- `mcp-platform/servers/enterprise/capabilities/vectors/tools/vector_search.py` —
  операция для модели
- `mcp-platform/libs/enterprise_data/snapshot/store.py` — `search_vector`, чтение
  векторов и payload чанка
- `mcp-platform/servers/enterprise/build_index.py` — наполнение векторного
  хранилища

## Verification

Валидация включает:
1. Проверка единственного источника объявления: состав и параметры индексов в
   `mcp-platform/platform.json → vectors.indexes` и отсутствие объявления
   состава индексов в конфигурации агента
2. Проверка единственного пути доступа: FAISS читается только из
   `mcp-platform/libs/vectors/` (code review, grep по `mcp-platform/`)
3. Проверка ленивой сборки: состояние `missing` до первого поиска и ровно одна
   сборка на процесс при параллельных обращениях (счётчик `builds`)
4. Проверка payload из источника: `content` и `row` соответствуют строке
   таблицы-источника по `(source, pk_value, chunk_index)`, а не метаданным индекса
5. Проверка отказов: `not_found` на неизвестный индекс, `stale_index` /
   `invalid_index` на расхождение подписи
6. Сверка контракта операции с `mcp-platform/docs/MCP-CONTRACTS.md` §5.1
