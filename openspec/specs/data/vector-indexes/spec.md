# Vector Indexes (Векторные индексы)

## Purpose

Определение логической модели для векторных индексов: источник конфигурации, lifecycle индекса, доступ Skills/сервисов и поведение при ошибке. Векторные индексы управляются централизованно и предоставляются через `CacheProvider.search_vector`.

## Responsibility

Vector Indexes отвечают за:
- определение источника конфигурации векторных индексов
- управление lifecycle векторных индексов (создание, сборка, загрузка)
- предоставление единого API для vector search через CacheProvider
- обеспечение FAISS-backed хранения

## Boundary

### Owns
- конфигурацией векторных индексов в project.json
- сборкой и хранением FAISS индексов
- предоставлением vector search API через CacheProvider.search_vector

### Does Not Own
- прямым доступом к FAISS файлам извне CacheProvider
- бизнес-логикой Skills
- альтернативными vector storage backends

### May Depend On
- project.json (конфигурация индексов)
- FAISS library
- PostgreSQL (хранение embeddings)
- CacheProvider (доступ к индексу)

### Must Not Depend On
- конкретной реализации Skills
- legacy vector_index_config таблиц
- других vector storage implementations

## Public Contract

VectorIndexBuildService предоставляет:
- загрузку конфигурации индексов из project.json
- сборку FAISS индексов через build_vectors.py
- поиск по векторному сходству через CacheProvider.search_vector

## Requirements

### Requirement: Единый источник конфигурации

Система ДОЛЖНА читать конфигурацию векторного индекса только из `gateway.vector.index.indexes.*` в `project.json`.

#### Scenario: Конфигурация индекса

- **КОГДА** векторный индекс добавлен или изменён
- **ТОГДА** его декларация ДОЛЖНА находиться под `gateway.vector.index.indexes.<name>` в `project.json`

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

- **WHEN** оператор меняет значение `gateway.vector.index.storage_table` или `gateway.vector.index.signature_table` в `project.json`
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
  ```text
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

- читать конфигурацию векторного индекса из legacy таблицы `public.agent_vector_index_config` (сохранена только для исторической справки; не authoritative)
- создавать второй vector storage backend рядом с FAISS без явного OpenSpec change
- молча fallback на non-FAISS backend при ошибках FAISS
- bypass `CacheProvider.search_vector` из Skill кода
- читать legacy ключ конфигурации `gateway.vector_index.*` (удалён; runtime-mute если присутствует)

## Dependencies

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `lib/services/vector_index_service.py:VectorIndexBuildService` — реализация
- `tools/build_vectors.py` — сборка индексов
- FAISS library — vector index engine
- `lib/services/cache_provider.py:CacheProvider` — доступ к поиску

## Configuration

```json
{
  "gateway": {
    "vector": {
      "index": {
        "default_root": "/path/to/faiss/indexes",
        "storage_table": "vector_embeddings",
        "indexes": {
          "audit_patterns": {
            "dimension": 768,
            "metric": "cosine"
          },
          "legal_docs": {
            "dimension": 768,
            "metric": "cosine"
          }
        }
      }
    }
  }
}
```

## Lifecycle

1. **Конфигурация**: индексы определяются в project.json
2. **Сборка**: build_vectors.py строит FAISS индексы из данных
3. **Публикация**: индексы сохраняются в default_root
4. **Загрузка**: индексы загружаются по demand при query
5. **Обновление**: periodic rebuild по расписанию или событию

## State

VectorIndexBuildService хранит:
- конфигурацию индексов
- пути к FAISS файлам
- статус последней сборки

## Invariants

- Конфигурация читается только из project.json
- Все индексы FAISS-backed
- Единый access path через CacheProvider.search_vector
- Нет legacy table reads

## Error Behavior

- FAISS build error → явная ошибка, нет silent fallback
- Index not found → ошибка возвращается потребителю
- Config missing → fail fast при старте

## Consumers

- AuditAnalyzer — vector search для паттернов
- LegalSummarizer — поиск юридических документов
- Skills — general vector queries

## Implementation

Основная реализация:
- `lib/services/vector_index_service.py:VectorIndexBuildService`

Связанные компоненты:
- `tools/build_vectors.py` — сборка индексов
- `lib/services/cache_provider.py:CacheProvider` — search_vector API
- `lib/core/infra_registration.py:register_vector_storage` — регистрация таблицы

## Verification

Валидация включает:
1. Проверка отсутствия прямого доступа к FAISS из Skills (code review)
2. Проверка конфигурации только из project.json (тесты)
3. Проверка отсутствия legacy table reads (grep, тесты)
