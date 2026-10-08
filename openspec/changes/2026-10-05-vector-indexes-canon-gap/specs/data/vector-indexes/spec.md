# data/vector-indexes Specification — дельта

## Purpose

Закрыть противоречие внутри одной спеки. `## Scope` уже описывает переезд владения в
capability `vectors` («агент индексы не строит и не хранит»), а `## Requirement`
требует читать `gateway.vector.index.indexes.*` из `config.json`. Оба утверждения
стоят в одном файле, и нормативным считается второе — то есть канон требует то, чего
в коде нет и что уже объявлено ненормативным.

Переезд при этом зафиксирован только в стороннем документе
(`openspec/specs/OWNERSHIP.md:28`: преемник — `mcp-platform/libs/vectors/`,
объявления в `platform.json → vectors.indexes`). Тело спеки за переездом не пошло,
потому что переезд объявляли в другом месте.

Дельта состоит из трёх разных вещей, и смешивать их нельзя:

- **прошлый мир** (`build_vectors.py`, `vector_index_service.py`,
  `register_vector_storage`, `CacheProvider.search_vector`, `preload_indexes` до
  `READY`, `audit_cache.duckdb`) — кода нет, требования снимаются;
- **намерение, которое остаётся** (единственный источник состава индексов, единый
  путь доступа, payload из источника, а не из сериализованного индекса) —
  переписывается на нового владельца;
- **молчание о настоящем** (кто объявляет состав индексов и почему агент своего
  списка не держит) — `ADDED`.

## Scope

`platform` — владение векторными индексами принадлежит capability `vectors`:
`mcp-platform/libs/vectors/`, объявление состава — `mcp-platform/platform.json →
vectors.indexes`.

Операции, доступные **модели**, ровно две — `vector_search` и `list_indexes`:
обе объявлены в `config.json → tools.mcpServers.enterprise.enabled_tools` вместе с
`list_scripts`, `run_script`, `generate_sql`, `query_operation`, `history_search`,
`read_result`, и обе упомянуты в `workspace/skills/audit_analyzer/SKILL.md`.
`list_indexes` выдана модели затем, что **имена индексов нужны ей для
`vector_search`, а навык их не перечисляет**: перечень в навыке был копией
объявления `platform.json → vectors.indexes`, и новый индекс появлялся в
модельной поверхности только если кто-то отредактирует `SKILL.md` — то есть
состав объявления тиражировался вручную. `index_stats`
(`mcp-platform/servers/enterprise/capabilities/vectors/tools/`) остаётся
**диагностической операцией capability**: файлы операций есть, но модели она не
объявлена, и описывать её как операцию модели нельзя — состояние индекса
модель получает в `list_indexes` (весь каталог) и в `vector_search`
(`index_state` конкретного поиска).

В агенте индексами не владеет никто: `VectorInfrastructureSettings`
(`lib/core/project_settings.py:186`) и `VectorIndexConfig` (`:481`) объявлены, но в
`lib/` не читаются ни разу; `tools/build_vectors.py` в дереве отсутствует (есть
только в `.worktrees/fork-…`); секции `gateway.vector.index` и
`skills.audit_analyzer.vector_indexes` в `config.json` не имеют потребителей,
кроме двух тестовых файлов: `tests/test_config_keys.py:90-101, :146-159` и
`tests/test_project_settings.py` (импорт моделей на уровне модуля `:9-13`,
`TestGatewayVectorIndexConfig` `:446` с чтением
`result.gateway.vector.index.indexes["audits_index"]` на `:479`, секция
`skills.*.vector_indexes` на `:260,289`).

Владение снимком DuckDB — capability `data`, и оно описано в
`openspec/specs/data/cache-provider/spec.md`, который закрывает активный change
`2026-10-04-close-cache-provider-canon-gap`. Здесь та часть не дублируется; из
соседней дельты берётся только уже зафиксированный факт: постройка индекса
выполняется на старте сервера, до event loop.

Не в объёме этого change: **правка кода платформы** и публикация сводки здоровья
индексов. Расчёт `compute_index_health` / `format_index_health_lines` остаётся в
`mcp-platform/libs/vectors/preload.py` и не удаляется вместе с требованием;
возвращать его публикацию — отдельное решение, у которого должны быть владелец и
адресат.

Операции дельт, нацеленные на требования, уже лежащие в каноне, приведены к
тексту канона: тело `MODIFIED`-требования `Hydrated payload берётся из
DuckDB-снапшота` заменено текстом из `openspec/specs/data/vector-indexes/spec.md` —
архив при таком состоянии не может ни добавить второй заголовок с тем же
именем, ни переписать живое требование. Пять требований, объявленных `REMOVED`,
из дельты сняты целиком: `FAISS-backed`, `FAISS собирается в памяти из
DuckDB-снапшота`, `Preload health-summary виден оператору и логируется`,
`Единый источник конфигурации`, `Единый путь доступа` — они живут в каноне, а
работа change'а не сделана, и архив удалил бы их. Требования, которых в каноне
нет, дельта не трогает: это работа change'а. Причина — расхождение дельты с
каноном; незавершённое остаётся в `tasks.md`.


## REMOVED Requirements

### Requirement: Storage table зарегистрирован через infra API

**Reason**: `lib/core/infra_registration.py` удалён вместе с `TableRegistry` (change
`drop-local-cache-read-from-pg`), регистрировать нечем. Хранилище снимка
принадлежит capability `data`; состав индексов объявляет capability `vectors`
(`platform.json → vectors.indexes`), и отдельного «storage table», который кто-то
регистрирует, больше нет.

### Requirement: Preload health-summary виден оператору и логируется

**Reason**: публиковать сводку нечем. Требование предписывало multi-line вывод
в `stderr` и ровно один `LogEvent` с
`event_type="vector_index_preload_health"` от `PreloadService`, а агентский
`lib/services/preload_service.py` **удалён** — файла нет ни на диске, ни в
git-индексе. Удалён и сам класс: FAISS-прогрев уехал на платформу, а цикла и
журнала у процесса платформы нет.

**Расчёт остаётся.** `compute_index_health`
(`mcp-platform/libs/vectors/preload.py:65`) и `format_index_health_lines`
(`mcp-platform/libs/vectors/preload.py:30`) экспортированы
(`mcp-platform/libs/vectors/__init__.py:50`; в `__all__` —
`mcp-platform/libs/vectors/__init__.py:69` и
`mcp-platform/libs/vectors/__init__.py:71`) и остаются посчитанными. Снимается
публикация, а не расчёт. Производственных вызовов нет: единственный вызов —
`mcp-platform/tests/test_vectors_index_health.py:35`, и это признаёт сам модуль
(`mcp-platform/libs/vectors/preload.py:20-22`).

**Второй канон это событие уже не требует.** Живой
`openspec/specs/observability/logging-db/spec.md` не содержит ни строки
`vector_index_preload_health`, ни требования, которое её предписывало: «Sync-события
через DbLoggingService» снято архивным change
`2026-10-05-logging-db-dead-producers`
(`openspec/changes/archive/2026-10-05-logging-db-dead-producers/specs/observability/logging-db/spec.md:132`).
Поэтому дельта снимает одно требование в одном каноне: удалять из `logging-db`
нечего, а `REMOVED` на несуществующем требовании архив отверг бы.

**Решение владельца**: публикация не возвращается. Если она понадобится, это
отдельный change с новым адресатом — оператор либо capability `data`; он обязан
вернуть требование здесь и в `logging-db` разом.

## MODIFIED Requirements

### Requirement: Hydrated payload берётся из DuckDB-снапшота
Система SHALL подтягивать `content` / `search_text` / `row_data` для каждого FAISS-hit'а через SELECT строки из DuckDB-снапшота таблицы-источника, заданной `gateway.vector.index.storage_table`, по ключу `(source, pk_value, chunk_index)`; она SHALL NOT читать эти поля из in-memory metadata, сериализованной в индекс.

#### Scenario: Подтягивание payload

- **WHEN** `group_vector_hits` выдаёт результат с `pk_value=P` и `chunk_index=K`
- **THEN** система SHALL сделать SELECT `(content, search_text, row_data)` из снапшотной таблицы-источника `WHERE source = <index_name> AND pk_value = P AND chunk_index = K`, чтобы заполнить `SearchResult.content` и `SearchResult.row`.

### Requirement: Индексы собираются на старте платформы; сбой одного не роняет старт

Состав индексов объявляет capability `vectors`
(`mcp-platform/platform.json → vectors.indexes`), а не агент. Процесс платформы
SHALL, ДО начала обслуживания запросов, перечислить объявленные индексы и для
каждого синхронно вызвать `vectors.owner.ensure_index(name)`
(`mcp-platform/servers/enterprise/server.py:940-965`). Подготовка выполняется
синхронно до запуска event loop
(`mcp-platform/servers/enterprise/server.py:1099` — `_prepare_capabilities` до
`anyio.run`): на старте все операции доступны быстро, ленивых загрузок на
горячем пути нет.

Сбой MUST NOT ронять старт. Отказ по одному индексу SHALL быть записан в журнал с
именем индекса, после чего подготовка продолжится следующим
(`server.py:956-958`). Отказ перечисления объявленных индексов — то же самое:
ошибка пишется и подготовка завершается без индексов
(`server.py:948-950`). Процесс, который не смог собрать один из трёх индексов,
MUST подниматься и обслуживать остальные.

Поиск по несобранному индексу MUST собирать его по требованию, а не отказывать:
`search` идёт через `_ensure_known` → `ensure_index`
(`mcp-platform/libs/vectors/owner.py:414-420`, `:244-249`), то есть ровно одна
сборка на индекс независимо от числа параллельных запросов. Отказ MUST приходить
тогда и только тогда, когда имя индекса не объявлено и не найдено в снимке —
`NotFoundError` (`owner.py:416-419`). Расхождение подписи индекса
(`STALE` / `INVALID`) — отдельная ошибка `IndexIntegrityError` с отдельным кодом
(`owner.py:362-366`), а не «ничего не найдено».

Прежний текст требования противоречил коду в трёх местах, и все три были
существенными: называл несуществующий `provider.preload_indexes(db_table)` и
`self._index_cache`, объявлял устаревшее место объявления
`gateway.vector.index.indexes.*`, требовал ронять старт на сбое индекса и
запрещал сборку по требованию — тогда как код именно её и делает
(`owner.py:414-420`).

#### Scenario: Прогрев на старте платформы

- **WHEN** процесс платформы стартует
- **THEN** для каждого индекса, объявленного в `platform.json → vectors.indexes`
  и не помеченного `enabled=false`, `ensure_index` MUST быть вызван ДО начала
  обслуживания, а состояние индекса — записано в журнал поимённо

#### Scenario: Сбой одного индекса не роняет старт

- **WHEN** `ensure_index` падает для одного из объявленных индексов
- **THEN** ошибка MUST быть записана с именем этого индекса, подготовка MUST
  продолжиться по остальным, а процесс MUST подняться

#### Scenario: Отказ перечисления индексов не роняет старт

- **WHEN** перечисление объявленных индексов падает
- **THEN** ошибка MUST быть записана и подготовка MUST завершиться без индексов,
  а не поднимать исключение

#### Scenario: Холодный поиск собирает индекс, а не отказывает

- **WHEN** выполняется поиск по объявленному индексу, который ещё не собран
- **THEN** система MUST собрать его через `ensure_index` и вернуть результат;
  отказ `_search_error` и требование «не собирать FAISS на лету» к живому коду
  отношения не имели

#### Scenario: Неизвестное имя индекса

- **WHEN** запрошен индекс, не объявленный и не найденный в снимке
- **THEN** MUST быть вызван `NotFoundError` с именем индекса

## ADDED Requirements

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
