## RENAMED Requirements

### Requirement: FAISS-backed → FAISS собирается в памяти из DuckDB-снапшота

**FROM**: `### Requirement: FAISS-backed`

**TO**: `### Requirement: FAISS собирается в памяти из DuckDB-снапшота`

Система SHALL build FAISS-индексы через
`tools/build_vectors.py` и `lib/services/vector_index_service.py`.
Система SHALL NOT персистить FAISS-блобы (ни файлами под
`gateway.vector.index.default_root`, ни строками в таблице,
заданной `gateway.vector.index.signature_table`); вместо этого
FAISS-индекс SHALL собираться в памяти по требованию из DuckDB-снапшота
таблицы-источника, заданной `gateway.vector.index.storage_table`
(либо — для оффлайн/предопубликованных снапшотов — из той же таблицы
в локальном кэше навыка `audit_cache.duckdb`).

Имена таблиц задаются конфигурацией
(`gateway.vector.index.storage_table` и `gateway.vector.index.signature_table`),
а не зашиты в код спецификации.

#### Scenario: Сборка индекса через build_vectors.py

- **WHEN** `tools/build_vectors.py` завершил запись строк в таблицу-источник
  (`gateway.vector.index.storage_table`)
- **THEN** он SHALL вызвать `provider.preload_indexes(db_table)`,
  чтобы прогреть per-process FAISS-кэш.
- **AND** он SHALL NOT делать INSERT/UPDATE в таблицу-сигнатуру
  (`gateway.vector.index.signature_table`) и SHALL NOT писать файлы
  `<default_root>/<index_name>.faiss`.

#### Scenario: Загрузка индекса при поиске

- **WHEN** `CacheProvider.search_vector` вызывается с `index_name`
- **THEN** FAISS-индекс SHALL собираться (или читаться из
  `self._index_cache`) через SELECT строк таблицы-источника
  (`gateway.vector.index.storage_table`) по `source = ?` из
  DuckDB-снапшота и вызов `build_faiss_index(records, metric)`.
- **AND** payload (`content` / `search_text` / `row`) SHALL
  подтягиваться для каждого FAISS-hit'а через SELECT той же строки
  из DuckDB-снапшота по `(source, pk_value, chunk_index)`.

#### Scenario: Стоимость холодного старта

- **WHEN** первый `search_vector` для `index_name` вызван после старта процесса
- **THEN** сборка индекса SHALL завершаться за ≤ 5 секунд на эталонной
  рабочей станции для индексов до 20 000 векторов × 1024.

#### Scenario: Смена таблицы через настройки

- **WHEN** оператор меняет значение `gateway.vector.index.storage_table`
  или `gateway.vector.index.signature_table` в `project.json`
- **THEN** система SHALL использовать новые имена без изменений
  в коде спецификации или runtime-коде, требующих релизов.

### Requirement: Hydrated payload берётся из DuckDB-снапшота

Система SHALL подтягивать `content` / `search_text` / `row_data` для
каждого FAISS-hit'а через SELECT строки из DuckDB-снапшота
таблицы-источника, заданной `gateway.vector.index.storage_table`,
по ключу `(source, pk_value, chunk_index)`; она SHALL NOT читать эти
поля из in-memory metadata, сериализованной в индекс.

#### Scenario: Подтягивание payload

- **WHEN** `group_vector_hits` выдаёт результат с
  `pk_value=P` и `chunk_index=K`
- **THEN** система SHALL сделать SELECT
  `(content, search_text, row_data)` из снапшотной таблицы-источника
  `WHERE source = <index_name> AND pk_value = P AND chunk_index = K`,
  чтобы заполнить `SearchResult.content` и `SearchResult.row`.

### Requirement: Прогрев индексов при старте

Система SHALL вызывать `provider.preload_indexes(db_table)` при старте
gateway как часть startup-flow, синхронно, **ДО** того как
runtime-health сигнализирует `READY`. Система SHALL NOT выполнять
сборку FAISS лениво на пользовательском запросе — все индексы,
объявленные в `gateway.vector.index.indexes.*` и не помеченные
`enabled=false`, MUST быть прогреты до начала приёма пользовательских
запросов.

#### Scenario: Тёплый старт

- **WHEN** процесс gateway запускается
- **THEN** для каждого `index_name`, объявленного в
  `gateway.vector.index.indexes.*` и не помеченного `enabled=false`,
  FAISS-индекс SHALL присутствовать в `self._index_cache` к моменту,
  когда runtime-health сигнализирует `READY`.

#### Scenario: Прогрев блокирует READY

- **WHEN** `preload_indexes` для какого-либо `index_name` падает
  (например, источник недоступен, DuckDB-снапшот не синк'нут)
- **THEN** startup-flow SHALL NOT сигнализировать `READY` и SHALL
  завершиться ошибкой с понятным сообщением, какой именно индекс
  не удалось прогреть.

#### Scenario: Запрос без прогрева — ошибка

- **WHEN** `CacheProvider.search_vector` вызван с `index_name`,
  которого нет в `self._index_cache` (cold miss)
- **THEN** система SHALL NOT собирать FAISS на лету и SHALL вернуть
  ошибку `_search_error` с указанием, что startup-flow не прогрел
  индекс — это нарушение контракта startup'а, не пользовательский
  retry.

### Requirement: Preload health-summary виден оператору и логируется

После прогона `preload_indexes` система SHALL опубликовать
health-summary (declared / loaded / missing / orphan / stale) в
**stderr** (multi-line, human-readable) и одним событием в
`agent_gateway_logs` (`event_type="vector_index_preload_health"`).
Это поведение существующего `PreloadService._emit_health_summary`
(`lib/services/preload_service.py:229`), которое должно быть
сохранено при удалении persisted-кеша.

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
  и SHALL записать одно событие в `agent_gateway_logs` с payload,
  содержащим эти же поля.

  Имена ключей в payload (`declared`, `loaded`, `missing`, `orphan`,
  `stale`) и `event_type="vector_index_preload_health"` остаются на
  латинице — это API-контракт `agent_gateway_logs`, его изменение
  требует отдельного OpenSpec-change. Переводится ТОЛЬКО
  human-readable вывод в stderr (заголовок и подписи секций).

#### Scenario: Что загружено

- **WHEN** `preload_indexes` успешно прогрел индексы
- **THEN** для каждого успешно загруженного `index_name` оператор
  SHALL видеть `name(N)` где `N` — количество векторов в
  `idx.ntotal` (читается из DuckDB-снапшота).

#### Scenario: Что НЕ загрузилось

- **WHEN** часть индексов не прогрелась (preload упал или индекс
  отсутствует в DuckDB-снапшоте)
- **THEN** оператор SHALL видеть их в `missing` секции summary и
  SHALL увидеть `level=WARN` (вместо `INFO`), чтобы grep/CI могли
  алёртить. Источник `declared` — `gateway.vector.index.indexes.*`,
  источник `loaded` — то, что вернул `preload_indexes`.

#### Scenario: Orphan-индексы

- **WHEN** в DuckDB-снапшоте `<storage_table>` есть строки
  с `source` (index_name), которого нет в
  `gateway.vector.index.indexes.*`
- **THEN** этот `source` SHALL попасть в `orphan` секцию summary.
  (После удаления `<signature_table>` источником `orphan` становится
  DuckDB-снапшот `<storage_table>`, а не persisted store.)

#### Scenario: Stale-индексы

- **WHEN** `_check_index_signature` пометил прогретый индекс как
  `STALE` или `INVALID`
- **THEN** этот `index_name` SHALL попасть в `stale` секцию summary
  с указанием статуса (`name:STALE` / `name:INVALID`). Статус
  берётся из `loaded_items[i]["signature_status"]`, вычисленного
  inline при прогреве (без чтения persisted metadata).

## REMOVED Requirements

### Requirement: FAISS персистится на диск и в PG-store

**Reason**: Хранение FAISS-блобов рядом с таблицей-источником дублирует
embedding-payload вторым persistence-слоем (колонка JSONB metadata
была неограниченной и превысила PostgreSQL `jsonb`-лимит
`2³¹ − 1` байт для индекса из 19 770 векторов в таблице
`oarb.audit_vectors`). Единственный источник истины теперь —
таблица-источник (`gateway.vector.index.storage_table`); FAISS-индекс
собирается из него заново на стороне запроса.

**Migration**: Миграция данных не требуется. Существующие строки в
таблице-сигнатуре (`gateway.vector.index.signature_table`) удаляются
миграцией этого change. Downstream-вызывающие должны полагаться на
`CacheProvider.search_vector`, который теперь собирает FAISS по
требованию из DuckDB-снапшота. Настройка `gateway.vector.index.signature_table`
в `project.json` становится no-op; запись может быть удалена при
следующем cleanup'е конфигурации.

## ADDED Negative Requirements

Система SHALL NOT:

- INSERT/UPDATE строки в таблицу-сигнатуру
  (`gateway.vector.index.signature_table`).
- читать `gateway.vector.index.signature_table` для материализации
  FAISS-блобов (настройка сохранена как deprecated-поле для обратной
  совместимости, но игнорируется в runtime).
- читать FAISS-блобы из `<gateway.vector.index.default_root>/<index_name>.faiss`
  на основном пути загрузки (fallback на файл остаётся только для
  pre-snapshot оффлайн-пути на протяжении одного транзитного релиза).
- вводить второй persistence-слой для FAISS-индексов рядом с
  таблицей-источником.
- хардкодить имена таблиц `oarb.audit_vectors` /
  `public.agent_vector_index_store` (или любых других) в коде
  спецификации или в runtime-коде; имена SHALL приходить из
  `gateway.vector.index.*` и могут меняться оператором без релиза.
