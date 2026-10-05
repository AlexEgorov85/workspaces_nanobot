# Vector Indexes (Векторные индексы)

## Purpose

Определение логической модели для векторных индексов: источник конфигурации, lifecycle индекса, доступ Skills/сервисов и поведение при ошибке. Векторные индексы управляются централизованно и предоставляются через `CacheProvider.search_vector`.

## Scope

`platform` — сборка и владение FAISS-индексами уехали в capability `vectors`; агент индексы не строит и не хранит
Реализация: `mcp-platform/libs/vectors/`, объявления в `platform.json → vectors.indexes`

## Responsibility

Vector Indexes отвечают за:
- объявление состава и параметров векторных индексов — единственный источник
  `mcp-platform/platform.json → vectors.indexes`
- владение FAISS-индексами в памяти процесса платформы: ленивая сборка, состояние
  индекса, подпись индекса и её сверка с текущей конфигурацией процесса
- предоставление vector search модели через операцию `vector_search` capability
  `vectors`
- подтягивание `content` / `search_text` / `row_data` найденных чанков из
  таблицы-источника, а не из метаданных индекса

## Boundary

### Owns
- объявлением состава и параметров векторных индексов: `mcp-platform/platform.json → vectors.indexes`
- сборкой и хранением FAISS-индексов в памяти процесса платформы; владелец —
  `mcp-platform/libs/vectors/`
- подписью индекса и её сверкой с текущей конфигурацией процесса
  (`mcp-platform/libs/vectors/signature.py`)
- предоставлением vector search модели через операцию `vector_search` capability `vectors`

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

Векторный поиск модели предоставляет операция `vector_search` capability `vectors`
(`mcp-platform/servers/enterprise/capabilities/vectors/tools/vector_search.py`):
- параметры: `query`, `index_name`, `top_k`, `threshold`
- ответ: `index_name`, `index_state`, `found`, `results[]` с `content`, `score`,
  `source`, `table`, `pk_value`, `chunk`, `matched_chunks`, `row`;
  `index_state` — состояние индекса на момент поиска (`missing` / `building` /
  `ready` / `error`)

Диагностические операции capability — `list_indexes` и `index_stats`
(`mcp-platform/servers/enterprise/capabilities/vectors/tools/`). Они не поднимают
индекс и модели не объявлены.

Маршрут поиска: навык → операция `vector_search` → `search_vector` хранилища
(`mcp-platform/libs/enterprise_data/snapshot/store.py`) → `mcp-platform/libs/vectors/`.
Выход за `mcp-platform/libs/vectors/` на этом маршруте — обход владельца индексов.

## Requirements

### Requirement: Единый источник конфигурации

Система ДОЛЖНА читать конфигурацию векторного индекса только из `gateway.vector.index.indexes.*` в `config.json`.

#### Scenario: Конфигурация индекса

- **КОГДА** векторный индекс добавлен или изменён
- **ТОГДА** его декларация ДОЛЖНА находиться под `gateway.vector.index.indexes.<name>` в `config.json`

### Requirement: Storage table зарегистрирован через infra API

Система ДОЛЖНА сохранять векторные embeddings в таблице, зарегистрированной через `lib.core.infra_registration.register_vector_storage`.

#### Scenario: Таблица векторного хранилища

- **КОГДА** `gateway.vector.index.storage_table` установлен
- **ТОГДА** эта таблица ДОЛЖНА быть зарегистрирована через `register_vector_storage`, чтобы `TableRegistry` знал о ней для синхронизации

### Requirement: FAISS-backed

Система ДОЛЖНА строить векторные индексы используя FAISS, вызываемый через `tools/build_vectors.py` и `lib/services/vector_index_service.py`.

#### Scenario: Сборка индекса

- **КОГДА** векторный индекс строится
- **ТОГДА** FAISS индекс ДОЛЖЕН быть сохранён под `<gateway.vector.index.default_root>/<index_name>` и ДОЛЖЕН загружаться по требованию при query time

### Requirement: Единый путь доступа

Система ДОЛЖНА предоставлять vector search исключительно через `CacheProvider.search_vector`.

#### Scenario: Skill выполняет vector search

- **КОГДА** Skill нуждается в vector similarity query
- **ТОГДА** он ДОЛЖЕН вызвать `CacheProvider.search_vector` и НЕ ДОЛЖЕН загружать FAISS индексы напрямую

### Requirement: FAISS собирается в памяти из DuckDB-снапшота

Система SHALL build FAISS-индексы через `tools/build_vectors.py` и `lib/services/vector_index_service.py`. Система SHALL NOT персистить FAISS-блобы (ни файлами под `gateway.vector.index.default_root`, ни строками в таблице, заданной `gateway.vector.index.signature_table`); вместо этого FAISS-индекс SHALL собираться в памяти по требованию из DuckDB-снапшота таблицы-источника, заданной `gateway.vector.index.storage_table` (либо — для оффлайн/предопубликованных снапшотов — из той же таблицы в локальном кэше навыка `audit_cache.duckdb`).

Имена таблиц задаются конфигурацией (`gateway.vector.index.storage_table` и `gateway.vector.index.signature_table`), а не зашиты в код спецификации.

#### Scenario: Сборка индекса через build_vectors.py

- **WHEN** `tools/build_vectors.py` завершил запись строк в таблицу-источник (`gateway.vector.index.storage_table`)
- **THEN** он SHALL вызвать `provider.preload_indexes(db_table)`, чтобы прогреть per-process FAISS-кэш.
- **AND** он SHALL NOT делать INSERT/UPDATE в таблицу-сигнатуру (`gateway.vector.index.signature_table`) и SHALL NOT писать файлы `<default_root>/<index_name>.faiss`.

#### Scenario: Загрузка индекса при поиске

- **WHEN** `CacheProvider.search_vector` вызывается с `index_name`
- **THEN** FAISS-индекс SHALL собираться (или читаться из `self._index_cache`) через SELECT строк таблицы-источника (`gateway.vector.index.storage_table`) по `source = ?` из DuckDB-снапшота и вызов `build_faiss_index(records, metric)`.
- **AND** payload (`content` / `search_text` / `row`) SHALL подтягиваться для каждого FAISS-hit'а через SELECT той же строки из DuckDB-снапшота по `(source, pk_value, chunk_index)`.

#### Scenario: Стоимость холодного старта

- **WHEN** первый `search_vector` для `index_name` вызван после старта процесса
- **THEN** сборка индекса SHALL завершаться за ≤ 5 секунд на эталонной рабочей станции для индексов до 20 000 векторов × 1024.

#### Scenario: Смена таблицы через настройки

- **WHEN** оператор меняет значение `gateway.vector.index.storage_table` или `gateway.vector.index.signature_table` в `config.json`
- **THEN** система SHALL использовать новые имена без изменений в коде спецификации или runtime-коде, требующих релизов.

### Requirement: Hydrated payload берётся из DuckDB-снапшота

Система SHALL подтягивать `content` / `search_text` / `row_data` для каждого FAISS-hit'а через SELECT строки из DuckDB-снапшота таблицы-источника, заданной `gateway.vector.index.storage_table`, по ключу `(source, pk_value, chunk_index)`; она SHALL NOT читать эти поля из in-memory metadata, сериализованной в индекс.

#### Scenario: Подтягивание payload

- **WHEN** `group_vector_hits` выдаёт результат с `pk_value=P` и `chunk_index=K`
- **THEN** система SHALL сделать SELECT `(content, search_text, row_data)` из снапшотной таблицы-источника `WHERE source = <index_name> AND pk_value = P AND chunk_index = K`, чтобы заполнить `SearchResult.content` и `SearchResult.row`.

### Requirement: Прогрев индексов при старте

Система SHALL вызывать `provider.preload_indexes(db_table)` при старте gateway как часть startup-flow, синхронно, **ДО** того как runtime-health сигнализирует `READY`. Система SHALL NOT выполнять сборку FAISS лениво на пользовательском запросе — все индексы, объявленные в `gateway.vector.index.indexes.*` и не помеченные `enabled=false`, MUST быть прогреты до начала приёма пользовательских запросов.

#### Scenario: Тёплый старт

- **WHEN** процесс gateway запускается
- **THEN** для каждого `index_name`, объявленного в `gateway.vector.index.indexes.*` и не помеченного `enabled=false`, FAISS-индекс SHALL присутствовать в `self._index_cache` к моменту, когда runtime-health сигнализирует `READY`.

#### Scenario: Прогрев блокирует READY

- **WHEN** `preload_indexes` для какого-либо `index_name` падает (например, источник недоступен, DuckDB-снапшот не синк'нут)
- **THEN** startup-flow SHALL NOT сигнализировать `READY` и SHALL завершиться ошибкой с понятным сообщением, какой именно индекс не удалось прогреть.

#### Scenario: Запрос без прогрева — ошибка

- **WHEN** `CacheProvider.search_vector` вызван с `index_name`, которого нет в `self._index_cache` (cold miss)
- **THEN** система SHALL NOT собирать FAISS на лету и SHALL вернуть ошибку `_search_error` с указанием, что startup-flow не прогрел индекс — это нарушение контракта startup'а, не пользовательский retry.

### Requirement: Preload health-summary виден оператору и логируется

После прогона `preload_indexes` система SHALL опубликовать health-summary (declared / loaded / missing / orphan / stale) в **stderr** (multi-line, human-readable) и одним событием в `agent_gateway_logs` (`event_type="vector_index_preload_health"`). Это поведение существующего `PreloadService._emit_health_summary` (`lib/services/preload_service.py:229`), которое должно быть сохранено при удалении persisted-кеша.

#### Scenario: Health-summary после preload

- **WHEN** `preload_indexes` завершился (успешно или с ошибкой)
- **THEN** система SHALL вывести в stderr строки вида:
  ```
  [vector] сводка состояния индексов после прогрева:
    объявлено (N): ...
    загружено (N): name1(12345), name2(19770), ...
    не найдено (N): name3, ...
    сироты    (N): ...
    устаревшие (N): name4:STALE, ...
  ```
  и SHALL записать одно событие в `agent_gateway_logs` с payload, содержащим эти же поля.

  Имена ключей в payload (`declared`, `loaded`, `missing`, `orphan`, `stale`) и `event_type="vector_index_preload_health"` остаются на латинице — это API-контракт `agent_gateway_logs`, его изменение требует отдельного OpenSpec-change. Переводится ТОЛЬКО human-readable вывод в stderr (заголовок и подписи секций).

#### Scenario: Что загружено

- **WHEN** `preload_indexes` успешно прогрел индексы
- **THEN** для каждого успешно загруженного `index_name` оператор SHALL видеть `name(N)` где `N` — количество векторов в `idx.ntotal` (читается из DuckDB-снапшота).

#### Scenario: Что НЕ загрузилось

- **WHEN** часть индексов не прогрелась (preload упал или индекс отсутствует в DuckDB-снапшоте)
- **THEN** оператор SHALL видеть их в `missing` секции summary и SHALL увидеть `level=WARN` (вместо `INFO`), чтобы grep/CI могли алёртить. Источник `declared` — `gateway.vector.index.indexes.*`, источник `loaded` — то, что вернул `preload_indexes`.

#### Scenario: Orphan-индексы

- **WHEN** в DuckDB-снапшоте `<storage_table>` есть строки с `source` (index_name), которого нет в `gateway.vector.index.indexes.*`
- **THEN** этот `source` SHALL попасть в `orphan` секцию summary. (После удаления `<signature_table>` источником `orphan` становится DuckDB-снапшот `<storage_table>`, а не persisted store.)

#### Scenario: Stale-индексы

- **WHEN** `_check_index_signature` пометил прогретый индекс как `STALE` или `INVALID`
- **THEN** этот `index_name` SHALL попасть в `stale` секцию summary с указанием статуса (`name:STALE` / `name:INVALID`). Статус берётся из `loaded_items[i]["signature_status"]`, вычисленного inline при прогреве (без чтения persisted metadata).

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

## Dependencies

- `mcp-platform/platform.json → vectors` — объявление состава, выключатель capability,
  инфраструктурная таблица хранения векторов
- `mcp-platform/libs/vectors/` — владелец индексов: сборка, подпись, группировка
  чанков, состояние
- `mcp-platform/libs/enterprise_data/snapshot/store.py` — владелец снимка: `search_vector`,
  чтение векторов и payload чанка
- `mcp-platform/servers/enterprise/capabilities/vectors/tools/` — операции `vector_search`,
  `list_indexes`, `index_stats`
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
chunk_size, chunk_overlap, metric, enabled}}`) — `servers/enterprise/server.py::_vectors_config`
собирает его из настроек capability, `libs/vectors/config.py::read_vector_index_config`
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
3. **Сборка**: FAISS-индекс собирается в памяти лениво, по первому обращению к
   `index_name`. Сборки на старте процесса нет: единственный триггер — векторный
   запрос. Параллельные обращения к одному индексу дают ровно одну сборку.
   Первичная сборка укладывается в ≤ 5 секунд на эталонной рабочей станции для
   индексов до 20 000 векторов × 1024: ленивость меняет исполнителя, а не
   пользовательский контракт первого поиска
4. **Поиск**: `vector_search` эмбедит запрос, сверяет подпись индекса с текущей
   конфигурацией процесса и ищет по FAISS; payload найденных чанков
   (`content` / `search_text` / `row_data`) подтягивается из таблицы-источника по
   ключу `(source, pk_value, chunk_index)`
5. **Обновление**: векторы добавляет сборщик capability. Индекс, собранный в
   процессе, живёт до конца его жизни; новое объявление или новые векторы учтутся
   в следующем процессе платформы, а расхождение подписи с текущим объявлением
   отражается отказом, а не молчаливой выдачей

## State

Состояние индекса принадлежит capability `vectors` и наблюдаемо в ответе операции
как `index_state`: `missing` → `building` → `ready`, либо `error` с кодом и
текстом. На каждый `index_name` владелец индексов держит состояние, сам
FAISS-индекс, метаданные, время сборки и счётчики `queries` / `builds`.

Персиста у индекса нет — ни файла индекса, ни таблицы подписи: индекс существует
только в памяти процесса, и его нельзя прочитать из другого процесса.

## Invariants

- Состав и параметры индексов читаются только из
  `mcp-platform/platform.json → vectors`; второго источника нет, и правило
  приоритета между двумя списками не применяется
- Векторными индексами не владеет ни один компонент агента: агент их не строит,
  не хранит и не прогревает
- Все индексы FAISS-backed, в памяти, без персиста
- Сборка индекса ленивая и ровно одна на процесс
- Payload (`content` / `search_text` / `row_data`) читается из таблицы-источника,
  а не из метаданных, сериализованных в индекс
- Поиск идёт единственным маршрутом: операция `vector_search` → `search_vector`
  хранилища → `mcp-platform/libs/vectors/`
- Расхождение подписи индекса с текущей конфигурацией — отказ, а не молчаливая
  деградация и не пустая выдача

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

## Consumers

- Модель (навык) — операция `vector_search` capability `vectors`; в дереве агента
  её называет `workspace/skills/audit_analyzer/SKILL.md`
- Владелец снимка — `search_vector`
  (`mcp-platform/libs/enterprise_data/snapshot/store.py`), делегирующий владельцу
  индексов
- Оператор — диагностические операции `list_indexes` и `index_stats`, которые
  отвечают, не поднимая индекс

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
