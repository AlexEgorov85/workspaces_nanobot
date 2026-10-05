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
vectors.indexes`, операции модели — `vector_search`, `list_indexes`, `index_stats`
(`mcp-platform/servers/enterprise/capabilities/vectors/tools/`).

В агенте индексами не владеет никто: `VectorInfrastructureSettings`
(`lib/core/project_settings.py:186`) и `VectorIndexConfig` (`:481`) объявлены, но в
`lib/` не читаются ни разу; `tools/build_vectors.py` в дереве отсутствует (есть
только в `.worktrees/fork-…`); секции `gateway.vector.index` и
`skills.audit_analyzer.vector_indexes` в `config.json` не имеют потребителей, кроме
`tests/test_config_keys.py:88-97`.

Владение снимком DuckDB — capability `data`, и оно описано в
`openspec/specs/data/cache-provider/spec.md`, который закрывает активный change
`2026-10-04-close-cache-provider-canon-gap`. Здесь та часть не дублируется; из
соседней дельты берётся только уже зафиксированный факт: постройка индекса ленивая,
по первому поиску.

Не в объёме этого change: **правка кода платформы** и публикация сводки здоровья
индексов. Расчёт `compute_index_health` / `format_index_health_lines` остаётся в
`mcp-platform/libs/vectors/preload.py` и не удаляется вместе с требованием;
возвращать его публикацию — отдельное решение, у которого должны быть владелец и
адресат.

## REMOVED Requirements

### Requirement: Storage table зарегистрирован через infra API

**Reason**: `lib/core/infra_registration.py` удалён вместе с `TableRegistry` (change
`drop-local-cache-read-from-pg`), регистрировать нечем. Хранилище снимка
принадлежит capability `data`; состав индексов объявляет capability `vectors`
(`platform.json → vectors.indexes`), и отдельного «storage table», который кто-то
регистрирует, больше нет.

### Requirement: FAISS-backed

**Reason**: Требование указывало на `tools/build_vectors.py` и
`lib/services/vector_index_service.py`. Обоих файлов нет. Сборка индекса
выполняется в платформе, лениво, по первому обращению, владельцем индексов
(`mcp-platform/libs/vectors/`), а не отдельной командой агента.

### Requirement: FAISS собирается в памяти из DuckDB-снапшота

**Reason**: Требование указывало на `tools/build_vectors.py`,
`lib/services/vector_index_service.py`, `gateway.vector.index.signature_table`,
`gateway.vector.index.storage_table` и `gateway.vector.index.default_root`, а также
допускало оффлайн-источник в локальном кэше `audit_cache.duckdb` — путь на NFS,
известный как неработоспособный. Ключи конфигурации не читаются, файлов нет,
локального кэша в дереве агента нет.

Сохранённое намерение (ленивая постройка из источника) переезжает в
`MODIFIED Requirement: Единый источник конфигурации` и
`ADDED Requirement: Агент не объявляет состав индексов`.

### Requirement: Прогрев индексов при старте

**Reason**: Требовало синхронного `provider.preload_indexes(db_table)` до сигнала
`READY` и запрещало ленивую постройку. Это прямо противоречит коду платформы, где
постройка ленивая по первому поиску
(`mcp-platform/libs/vectors/owner.py:8-11` — «сборка на старте процесса запрещена»,
`:305-311` — «На старте процесса **не вызывается**»), и докстрингу владельца
индексов. Требование «запрос без прогрева — ошибка `_search_error`» отменяется
вместе с требованием: холодный поиск — штатный путь, а не нарушение контракта старта.
Тот же факт ленивой постройки уже зафиксирован в дельте capability `data`
(change `2026-10-04-close-cache-provider-canon-gap`), поэтому противоречие между
канонами снимается, а не переносится.

### Requirement: Preload health-summary виден оператору и логируется

**Reason**: Расчёт есть
(`mcp-platform/libs/vectors/preload.py:22,57`, экспорт — `libs/vectors/__init__.py:47,66`),
но **производственного вызова нет ни одного** — только
`mcp-platform/tests/test_vectors_index_health.py`. Публикация в `stderr` и событие
`vector_index_preload_health` в `agent_gateway_logs` не происходят ни разу, а порог
«более 30% устаревших» не проверяет никто. Требование описывало `PreloadService`
(`lib/services/preload_service.py:229`) — файла нет.

## MODIFIED Requirements

### Requirement: Единый источник конфигурации

Система ДОЛЖНА читать состав и параметры векторных индексов **только** из
`mcp-platform/platform.json → vectors.indexes`, объявленного capability `vectors`.
Агент ДОЛЖЕН NOT держать собственный список индексов, ни в `config.json`, ни в
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

#### Scenario: Агентский список индексов отвергается

- **WHEN** в `config.json` присутствует `gateway.vector.index` или
  `skills.<name>.vector_indexes`
- **THEN** валидация конфигурации ДОЛЖНА отвергнуть секцию как неизвестный ключ
  (`extra="forbid"`), а не молча проигнорировать её

#### Scenario: Расхождение двух списков невозможно выразить

- **WHEN** оператор правит состав индексов
- **THEN** система ДОЛЖНА находить ровно одно объявление, потому что второго
  места для него в агенте не осталось
- **AND** система ДОЛЖЕН NOT применять правило приоритета между двумя
  источниками: выбирать «более новый» из двух нечего, если объявление одно

### Requirement: Единый путь доступа

Система ДОЛЖНА предоставлять vector search модели исключительно через операцию
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

- **WHEN** код capability или библиотеки обращается к FAISS в обход
  `mcp-platform/libs/vectors/`
- **THEN** такой доступ ДОЛЖЕН считаться нарушением контракта владения, даже
  если технически работает

### Requirement: Hydrated payload берётся из источника, а не из индекса

Система SHALL подтягивать `content` / `search_text` / `row_data` для каждого
FAISS-hit'а из таблицы-источника по ключу `(source, pk_value, chunk_index)`; она
SHALL NOT читать эти поля из метаданных, сериализованных в индекс.

Прежняя редакция привязывала источник к `gateway.vector.index.storage_table`.
Ключ удалён; источник теперь определяется объявлением
`platform.json → vectors.indexes`.

#### Scenario: Подтягивание payload

- **WHEN** поиск по индексу выдаёт результат с `pk_value=P` и `chunk_index=K`
- **THEN** система SHALL получить `(content, search_text, row_data)` из
  таблицы-источника по `source = <index_name> AND pk_value = P AND chunk_index = K`
- **AND** SHALL NOT читать их из сериализованного в индексе

#### Scenario: Индекс без payload

- **WHEN** FAISS-hit найден, а строка источника отсутствует
- **THEN** отказ SHALL быть явным, без silent fallback на данные из индекса

## ADDED Requirements

### Requirement: Агент не объявляет состав индексов

Секции `gateway.vector.index` и `skills.<name>.vector_indexes` ДОЛЖНЫ быть
отсутствовать в `config.json`, а модели конфигурации — исключены из
`lib/core/project_settings.py`. Пока секция объявлена, но не читается, она выглядит
владельцем: её значения печатаются как ожидаемые в `tests/test_config_keys.py`, а
`VectorIndexConfig` называет себя единственным источником деталей построения.

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
- **THEN** такое упоминание SHALL считаться расхождением и проверяться
  `tools/validate_component_specs.py` либо правкой спеки
