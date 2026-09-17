## Context

См. `proposal.md` для мотивации. Техническое состояние, на которое
отвечает этот дизайн:

- `_save_index_to_store` (`lib/services/cache_provider_impl.py:980`) пишет
  единственное значение `JSONB metadata`, содержащее метаданные всех
  FAISS-чанков, дублируя поля, уже присутствующие в таблице-источнике
  (имя берётся из `gateway.vector.index.storage_table`).
- `build_faiss_index` (`lib/utils/duckdb_query.py:213`) — источник этого
  дублированного payload.
- В `_load_index` (`:1142`) уже есть рабочий путь, который собирает FAISS из
  DuckDB-снапшота — `_load_index_from_cache` (`:1080`) — используется сегодня
  как третий fallback после `_load_index_from_store` и `_load_vectors_from_db`.
- `_load_vectors_from_db` читает FAISS-данные **из PostgreSQL** (прямо из
  таблицы-источника); `_load_index_from_cache` читает из
  **DuckDB-снапшота** той же таблицы. Только последний согласован с
  контрактом `gateway.cache.local_path` и `PgDuckDbSyncService`.
- Per-process LRU-кэш `self._index_cache` (`:830`) уже существует и
  заполняется `_load_index`.

Спецификация и этот дизайн говорят об именах таблиц в терминах
настроек (`gateway.vector.index.storage_table`,
`gateway.vector.index.signature_table`), а не фиксированных имён —
оператор может менять их без изменения кода.

## Lifecycle path (после change)

Описывает путь векторов и индекса по всем точкам системы после
применения Stage B (см. § Migration Plan). Все имена таблиц —
через `gateway.vector.index.storage_table` и
`gateway.vector.index.signature_table`; нигде не зашиты.

### 0. Конфигурация (статическая, в `project.json`)

```
gateway.vector.index.indexes.*     → список индексов (table, pk,
                                     content_columns, embedding_columns,
                                     chunk_size, chunk_overlap, metric,
                                     track_column, enabled)
gateway.vector.index.storage_table → <storage_table>: сырые эмбеддинги
                                     (REAL[] + content/search_text/row)
gateway.vector.index.signature_table → <signature_table>: DEPRECATED,
                                     runtime игнорирует (после Stage A)
gateway.vector.index.default_root  → DEPRECATED, не используется
                                     (был для .faiss файлов)
```

`register_vector_storage` (`lib.core.infra_registration`) регистрирует
`<storage_table>` в `TableRegistry` → попадает в DuckDB-снапшот.

### 1. Сборка сырых векторов (внешний источник → `<storage_table>`)

**Кто**: `tools/build_vectors.py` (вызывается оператором вручную или
из cron'а).

**Шаги**:

1. Для каждого `index_name` из `gateway.vector.index.indexes.*` —
   читает исходную PG-таблицу, чанкует текст по `embedding_columns`,
   считает эмбеддинг через Ollama (`mxbai-embed-large`, 1024-dim).
2. `INSERT` строк в `<storage_table>` (одна строка = один чанк):
   `id, source, content, search_text, table, pk_value,
    chunk_index, chunk_count, row_data, embedding, content_hash,
    max_src_track, synced_at, created_at`.
3. `PgDuckDbSyncService` (фоновый тред, `lib/services/pg_duckdb_sync_service.py`)
   подхватывает изменения строк через инкрементальный поллинг по
   `track_column` (`id` для `<storage_table>`) и переписывает
   соответствующие строки в локальный DuckDB-снапшот
   `workspace/data_store/duckdb/cache.duckdb`.

**Что пишется**: только строки в `<storage_table>`. **Никаких
blob'ов в `<signature_table>` нет** (Stage B).

### 2. Прогрев индекса при старте gateway (обязательный этап)

**Кто**: `lib/services/preload_service.py` → `provider.preload_indexes(db_table)`
(`cache_provider_impl.py:1267`), вызывается как часть startup-flow
`ApplicationContext.start()` (см. `lib/lifecycle/gateway_runner.py`)
**ДО** того, как runtime-health сигнализирует `READY`.

**Шаги для каждого `index_name` из `gateway.vector.index.indexes.*` (и
`enabled != false`)**:

1. `_load_index(index_path="", index_name, db_table)`
   → `self._index_cache` пуст для этого `index_name`.
2. `_load_index_from_cache(index_name, metric)` (единственный путь
   после Stage B):
   - `self._conn.execute("SELECT id, source, content, search_text,
      table, pk_value, chunk_index, chunk_count, row_data, embedding
      FROM <storage_table> WHERE source = ? ORDER BY id",
      [index_name])` — читает из DuckDB-снапшота.
   - `build_faiss_index(records, metric)` → собирает `IndexFlatIP`
     + `meta = {"metric": <metric>}`.
3. `self._index_cache[index_name] = (idx, meta)` — кладёт в
   per-process LRU.
4. `_check_index_signature(index_name, meta)` — вычисляет сигнатуру
   из текущей index-конфигурации (in-memory, не из персистенции)
   и помечает `meta["_signature_status"] = CURRENT/STALE/INVALID`.

**Стоимость**: ~2-5 с × N индексов при холодном старте. Прогрев
выполняется **синхронно** как часть startup; runtime-health не
сигнализирует `READY`, пока все индексы не в кэше. Пользовательские
запросы не принимаются до завершения прогрева.

**Запрет ленивой сборки на пользовательском запросе**: см. § 3 —
первый `search_vector` для `index_name` после старта процесса SHALL
NOT выполнять сборку FAISS; если индекс отсутствует в `_index_cache`,
это `fatal startup error` (см. спеку «Preload indexes at startup»).

**Health-summary после прогрева**: после `preload_indexes` gateway
вызывает `PreloadService._emit_health_summary(loaded)` (см.
`lib/services/preload_service.py:229`). Этот механизм сохраняется
без изменений по контракту, но меняются **источники** для двух полей:

- `orphan`: был `runtime_rows = SELECT DISTINCT source FROM
  <signature_table>`; после change — `runtime_rows = SELECT DISTINCT
  source FROM <storage_table>` (DuckDB-снапшот). Семантика та же —
  «есть `source` в данных, но не объявлен в `gateway.vector.index.indexes.*`».
- `stale`: был `verify_index_signature(stored_meta, declared_cfg)`,
  читавший `metadata.signature` из `<signature_table>`; после change —
  статус берётся inline из `meta["_signature_status"]`, который
  `_check_index_signature` положил в `loaded_items` при прогреве.
  Внешний контракт (`stale = ["name:STALE", ...]`) сохраняется.

Остальные поля (`declared`, `loaded`, `missing`) — без изменений.
Механизм dual-sink (stderr + `agent_gateway_logs`
`event_type="vector_index_preload_health"`) — без изменений.

### 3. Запрос `search_vector(query, index_name, top_k, threshold)`

**Кто**: Skill-скрипт или tool через `CacheProvider.search_vector`
(`cache_provider_impl.py:1317`).

**Шаги**:

1. `_load_index("", index_name, db_table)`:
   - **hit** в `self._index_cache` → возврат готового `(idx, meta)`
     (нормальный путь после успешного startup).
   - **miss** (индекс не прогрет при старте) → `RuntimeError` /
     `_search_error` с понятным сообщением; пользовательский запрос
     завершается ошибкой, никакой ленивой сборки FAISS не
     выполняется. Это нарушение контракта
     (см. спеку «Preload indexes at startup»), startup-flow должен
     был прогреть все индексы синхронно до сигнала `READY`.
2. `get_embedding(query)` → Ollama → 1024-dim float32.
3. Если `meta["metric"] == "cosine"` — `faiss.normalize_L2(query_vec)`.
4. `idx.search(query_vec, k)` → `scores`, `ids`.
5. `build_raw_items(meta_items, scores, ids, index_name, threshold, conn, db_table)`
   (`lib/utils/duckdb_query.py:145`):
   - Для каждого hit'а — DuckDB `SELECT content, search_text, row_data
     FROM <storage_table> WHERE source = ? AND pk_value = ? AND chunk_index = ?`
     → заполняет `SearchResult.content` и `SearchResult.row`.
6. `group_vector_hits(raw, top_k, threshold)` — группирует чанки в
   один документ (по `(source, table, pk_value)`), оставляет `top_k`.
7. Возврат `list[SearchResult]` в Skill.

**Стоимость**: warm — `< 200 мс` (FAISS search + до 10 точечных
DuckDB-lookup'ов). Cold-start целиком перенесён в startup-flow и
скрыт от пользователя.

### 4. Инвалидация кэша

**Кто**: `provider.invalidate_cache(source)` (или `_index_cache.clear()`).

**Когда**:
- `tools/build_vectors.py` после INSERT в `<storage_table>` — pop
  только этого `index_name` из `_index_cache` (чтобы при следующем
  `search_vector` в этом процессе пересобрать актуальный FAISS).
- `PgDuckDbSyncService` при изменении строк `<storage_table>`
  (настраиваемый hook в runtime_patcher; сейчас есть, оставляем
  как есть).

### 5. Что НЕ существует после Stage B

- Нет `<signature_table>` (`public.agent_vector_index_store`).
- Нет файлов `<default_root>/<index_name>.faiss`.
- Нет методов `_save_index_to_store`, `_load_index_from_store`,
  `rebuild_and_store_index`, `_compute_index_signature_from_config`,
  `VectorIndexBuildService.rebuild_and_store`.
- Нет ветки `_load_index_from_db → _save_index_to_store` (PG-read
  на лету + side-effect persist).

### 6. Диаграмма

```
                    ┌─────────────────────────────────────────┐
                    │  PostgreSQL                              │
                    │  ┌────────────────────────────────────┐  │
                    │  │ <storage_table> (REAL[] + meta)    │  │
                    │  └────────────────────────────────────┘  │
                    └────────────┬────────────────────────────┘
                                 │ PgDuckDbSyncService (инкремент
                                 │  по track_column)
                                 ▼
┌──────────────────┐    ┌─────────────────────────────────────┐
│ tools/build_     │    │ DuckDB-снапшот                       │
│ vectors.py       │───▶│ workspace/data_store/duckdb/         │
│ (operator/cron)  │    │   cache.duckdb                       │
└──────────────────┘    │   <storage_table> mirror             │
                        └────────────┬────────────────────────┘
                                     │ SELECT source=?, pk=?, chunk=?
                                     │   (per hit)
                                     ▼
┌──────────────────┐    ┌─────────────────────────────────────┐
│ Skill / Tool     │    │ CacheProvider                       │
│ (search_vector)  │───▶│   _index_cache (per-process LRU)     │
└──────────────────┘    │     (IndexFlatIP, {"metric": ...})   │
                        └─────────────────────────────────────┘
                                     ▲
                                     │ build_faiss_index (cold miss)
                                     │
                        preload_indexes() при старте gateway
                        (lib/services/preload_service.py)
```

Что исчезает после change: вертикальная стрелка из
`build_vectors.py → <signature_table>` и весь блок
`<signature_table>`.

## Goals / Non-Goals

**Goals:**

- Один persistence-слой для векторных данных: таблица, заданная
  `gateway.vector.index.storage_table`. FAISS — это производное in-memory
  представление этого слоя.
- Публичный API `CacheProvider.search_vector` сохраняется
  (сигнатура, тип возврата, семантика ошибок).
- `tools/build_vectors.py` остаётся точкой входа для добавления/обновления
  векторов; после записи строк в таблицу-источник он прогревает FAISS-кэш
  вместо того, чтобы писать персистнутый blob.
- Стоимость cold-start ограничена: ≤ 5 с на 20 000 × 1024; достигается
  через `preload_indexes()` при старте gateway (уже реализован).
- Путь deprecation'а разделен на этапы, чтобы существующие развёрнутые
  инстансы, всё ещё пишущие в таблицу-сигнатуру, не падали и
  существующие строки не терялись молча.
- Никакого хардкода имён таблиц в runtime-коде — имена берутся из
  `gateway.vector.index.*` (это уже сложилось в существующем коде
  через `read_vector_store_table()` / `register_vector_storage`).

**Non-Goals:**

- Миграция строк из СТАРОЙ таблицы-сигнатуры в новый persistence-слой
  (они устаревают — DROP TABLE делает зачистку).
- Разбиение крупных индексов на IVF/HNSW шарды. Это отдельная
  оптимизация производительности, может последовать после
  пере-измерения cold-start по результатам change.
- Изменение embedding-модели, размерности или chunking-политики.
- Изменение схемы `gateway.vector.index.indexes.*`.
- Изменение границы Skill / Tool (`docs/skill-tool-architecture.md`).

## Decisions

### D1. Полностью удалить таблицу-сигнатуру

**Decision**: `DROP TABLE IF EXISTS <signature_table>` (имя
резолвится из `gateway.vector.index.signature_table`) через новую
миграцию в `sql/migrations/`.

**Rationale**: Таблица — первопричина `ProgramLimitExceeded`. In-memory
`self._index_cache` плюс снапшот уже дают эквивалентное read-поведение.
Хранить отдельный персистнутый FAISS-blob для бэкапа или
производительности не оправдано: cold-start ограничен, других
вызывающих таблицы нет.

**Alternatives considered**:

- *Подрезать metadata JSONB на месте.* Оставляет дизайн с двумя
  слоями и по-прежнему требует сопровождения при росте
  `content_cols`. Отклонено.
- *Перенести FAISS-blob в чистую `BYTEA`-колонку (бросить JSONB
  payload целиком).* Убирает непосредственный краш, но оставляет
  indirection (metadata чанков всё равно придётся пересобирать из
  таблицы-источника в момент загрузки — тот же кодовый путь, что
  и in-memory сборка). Отклонено как лишняя сложность без выигрыша
  в производительности.

### D2. Холодная сборка FAISS из DuckDB-снапшота, не прямой PG-read

**Decision**: `_load_index` использует только `_load_index_from_cache`
(DuckDB). Удаляются путь `_load_vectors_from_db → _save_index_to_store`
и путь `_load_index_from_store`.

**Rationale**: DuckDB-снапшот уже сопровождается `PgDuckDbSyncService`
(см. `docs/DATABASE.md` § Postgres→DuckDB sync), локален, и это
единственный путь, переживающий медленный или недоступный PG.
Чтение напрямую из PG на каждый запрос умножит latency и переносит
сложность на query-сторону.

**Alternatives considered**:

- *Читать напрямую из PG в `_load_index_from_db`.* Тот же класс
  падений возвращается под нагрузкой (таблица-источник может расти
  для горячих индексов). Отклонено.
- *Оставить `_load_index_from_cache` как fallback после попытки
  PG-чтения.* Два кодовых пути без выигрыша в производительности —
  Gateway всё равно читает из снапшота. Отклонено.

### D3. Payload (content / search_text / row) подтягивается per hit через DuckDB SELECT (решено — вариант A)

**Решение пользователя** (см. Open Questions Q1): выбран вариант A —
точечные DuckDB-запросы по `(source, pk_value, chunk_index)` для каждого
FAISS-hit'а. DuckDB — in-process OLAP, round-trip ≈ десятки микросекунд,
точечный PK-lookup ≈ 0.1 мс; при `top_k ≤ 10` суммарная стоимость
незаметна. Батч `IN (...)` и материализация lookup в `_index_cache`
**не делаются** — оверкилл для текущих объёмов.

### D3. Payload (content / search_text / row) подтягивается per hit через DuckDB SELECT

**Decision**: `build_raw_items` (`lib/utils/duckdb_query.py:145`)
принимает DuckDB-коннекшен + имя таблицы (берётся из
`gateway.vector.index.storage_table` через runtime) и для каждого
FAISS-hit'а делает lookup `(content, search_text, row_data)` по
`(source, pk_value, chunk_index)`.

**Rationale**: Это совпадает с реальным источником истины и убирает
реплицированный payload из метаданных индекса. Для типичных
`top_k ≤ 10` запросов это не более 10 точечных lookup'ов в DuckDB —
пренебрежимо.

**Alternatives considered**:

- *Материализовать весь lookup `dict[i] → (content, row)` один раз
  в `_load_index` и хранить его в `_index_cache` рядом с `idx` и
  `meta`.* Избегает per-hit SQL, но дублирует строки в RAM. При
  строках таблицы-источника ~5 КБ и индексах ~20k векторов это
  до 100 МБ дополнительной RAM на индекс. Отклонено для пути
  по умолчанию; оставлено как ОПЦИОНАЛЬНЫЙ переключатель через
  существующую форму `_index_cache[(idx, meta, lookup)]` (решение
  отдаётся на code-review реализации; см. Open Question Q1).

### D4. Пометить `gateway.vector.index.signature_table` DEPRECATED (не удалять)

**Decision**: Поле `VectorIndexSettings.signature_table`
(`lib/core/project_settings.py:113`) помечается `deprecated=True`.
Поле по-прежнему принимается Pydantic, но игнорируется в runtime.
Никакой ошибки для существующих `project.json`; одноразовое
предупреждение логируется на старте.

**Rationale**: Возможны downstream-вызывающие (Streamlit, ops-скрипты),
которые всё ещё обращаются к этому полю. Удаление Pydantic-записи
в этом change сломало бы их вызовы `ProjectSettings(**)`. Хранение
`deprecated=True` ещё на одно релизное окно позволяет вызывающим
мигрировать.

### D5. Сигнатура/CURRENT-детекция переезжает из `metadata.signature` на in-memory сравнение

**Decision**: `_check_index_signature` продолжает работать, но
вычисляет ожидаемую сигнатуру из текущей index-конфигурации
(`_read_current_index_config`), а не читает сохранённое значение из
индекса. Per-index кэширование `meta["_signature_status"]` остаётся
как было.

**Rationale**: Сигнатура — всегда функция от
`(embedding_model, dimension, chunk_size, chunk_overlap, metric,
src_table, pk_column, content_cols, embedding_cols, track_column)`.
Всё это живёт в `gateway.vector.index.indexes.*` (и embedding-конфиге),
поэтому может быть заново получено в момент загрузки без персистенции.

**Alternatives considered**:

- *Вернуть отдельную компактную meta-таблицу
  `<signature_table>` или новую `agent_vector_index_meta`.* Добавляет
  новый артефакт ради единственного integer-хэша. Избыточно. Отклонено.

### D6. Имена таблиц в runtime-коде берутся только из настроек

**Decision**: Любые ссылки на конкретные таблицы в runtime-коде
должны проходить через `read_vector_storage_table()` /
`read_vector_store_table()` / `gateway.vector.index.storage_table` /
`gateway.vector.index.signature_table`. Прямой хардкод имён в коде
спецификации, дизайна, runtime или миграциях запрещён.

**Rationale**: В проекте уже сложилась практика настраиваемых имён
(`register_vector_storage` через `lib.core.infra_registration`,
`VectorIndexSettings` в `lib/core/project_settings.py:113`). Любая
новая привязка к конкретному имени ломает оператора, который
захочет вынести таблицы в отдельный schema/namespace.

**Alternatives considered**:

- *Прямое использование `oarb.audit_vectors` /
  `public.agent_vector_index_store` как «известных имён».* Отклонено:
  уже были инциденты с переименованием схем, и спецификация должна
  переживать их без релизов.

### D7. Никаких compatibility shim'ов (решено — вариант «сразу чисто»)

**Decision**: Stage A из первоначального Migration Plan удаляется.
Этот релиз сразу:

- удаляет `_save_index_to_store`, `_load_index_from_store`,
  `rebuild_and_store_index`, `_compute_index_signature_from_config`,
  `VectorIndexBuildService.rebuild_and_store`;
- переключает `_load_index` на единственный путь
  `_load_index_from_cache`;
- удаляет `VectorIndexSettings.signature_table` поле целиком (не
  `deprecated=True`);
- поставляет миграцию `DROP TABLE IF EXISTS <signature_table>` и
  применяет её в этом же релизе (Stage C из первоначального плана).

Никакого флага `gateway.vector.index.allow_legacy_store` нет.

**Rationale**: Shim создаёт два пути выполнения, которые нужно
поддерживать, тестировать, документировать и потом удалять — это
удваивает работу и плодит regression-риск. Лучше один механизм,
покрытый хорошими тестами (см. `tasks.md` § 5 — не меньше, чем
раньше покрывал shim + основной путь).

**Alternatives considered**:

- *Оставить Stage A на одно релизное окно для плавного rollout'а.*
  Отклонено: пользователь выбрал «лучше тестирование, чем двойной
  код». Тесты будут шире (см. `tasks.md`), а сценарии отката
  покрываются через `python tools/migrate.py --downgrade` /
  downgrade релиза, не через runtime-флаг.

## Risks / Trade-offs

| # | Риск | Митигация |
|---|------|-----------|
| R1 | Cold-start +5 с × N индексов при первом запросе | `preload_indexes()` при старте gateway уже реализован — убедиться, что он подключён к lifecycle (`lib/lifecycle/gateway_runner.py`). Добавить явный runtime-health сигнал, отличающий cold от warm кэша. |
| R2 | Per-hit DuckDB SELECT увеличивает warm-search latency | Точечные lookup'ы DuckDB по PK — порядка долей миллисекунды. При `top_k=10` дополнительная latency ≤ 5 мс. Если телеметрия скажет иначе (метрики R1/R2) — откат на материализованный lookup-вариант из D3, огороженный config-флагом. |
| R3 | Старые развёрнутые инстансы по-прежнему пишут в таблицу-сигнатуру; ничего уже её не читает, но таблица растёт | `DROP TABLE` миграция в этом же релизе (`sql/migrations/<timestamp>_drop_signature_table.sql`), применяется как часть деплоя. В CHANGELOG как REMOVED entity. |
| R4 | Существующие `project.json` ссылаются на удалённый `gateway.vector.index.signature_table` | Поле `VectorIndexSettings.signature_table` удаляется целиком (не `deprecated=True`). `ProjectSettings(**)` будет падать на старых `project.json`. Допустимо: оператор накатывает релиз → `python tools/migrate.py --apply` → удаляет ключ из `project.json` перед следующим стартом. Если нужна мягкая миграция — отдельный PR с `deprecated=True` окном (см. Open Question Q4). |
| R5 | У `_save_index_to_store` есть вызывающие помимо `tools/build_vectors.py` (например, прямой `provider.rebuild_and_store_index` из скриптов) | Удаление метода + unit-тест `grep -rn '_save_index_to_store\\|rebuild_and_store_index' lib tools workspace` возвращает пусто (см. `tasks.md` § 5). |
| R6 | Хардкод имён таблиц в runtime-коде, миграциях или документации | Code-review + `tools/architecture_guard.py`-подобный чек (или явный grep в CI) на конкретные имена вне `gateway.vector.index.*`. |

## Migration Plan

Этот релиз — **один цельный шаг**, без staging'а (см. D7).

1. **Код (см. `tasks.md` § 1-§ 3):**
   - Удалить `_save_index_to_store`, `_load_index_from_store`,
     `rebuild_and_store_index`,
     `_compute_index_signature_from_config`,
     `VectorIndexBuildService.rebuild_and_store`,
     `_load_vectors_from_db` (он шёл только к `_save_index_to_store`).
   - Переписать `_load_index` — единственный путь
     `_load_index_from_cache`.
   - Убрать `_vector_store_table` и `_default_root` из провайдера,
     `_load_index_from_files` тоже (нет `.faiss` файлов).
   - В `tools/build_vectors.py::_rebuild_faiss` оставить только
     `provider.preload_indexes(db_table)`.
   - В `preload_indexes` источник имён — `gateway.vector.index.indexes.*`,
     без `SELECT DISTINCT source FROM <signature_table>`.
   - `VectorIndexSettings.signature_table` — удалить поле.
   - В `compute_index_health`: `orphan` берётся из DuckDB-снапшота
     `<storage_table>` (DISTINCT source), `stale` — из
     `loaded_items[i]["signature_status"]`.

2. **SQL:**
   - Новая миграция `sql/migrations/<timestamp>_drop_signature_table.sql`:
     ```sql
     -- Имя таблицы резолвится из gateway.vector.index.signature_table
     -- (по умолчанию в существующих развёрнутых инстансах —
     --  public.agent_vector_index_store).
     DROP TABLE IF EXISTS "<signature_table>";
     ```
   - `sql/vectors/create_vector_index_store.sql` — пометить как
     DEPRECATED в шапке (комментарий-маркер).
   - Миграция применяется как часть деплоя
     (`python tools/migrate.py --apply`).

3. **Документация и конфигурация:**
   - `docs/VECTOR_INDEXES.md`, `docs/DATABASE.md`,
     `docs/ARCHITECTURE.md` § Vector, `AGENTS.md` § Configuration
     Vector — убрать упоминания таблицы-сигнатуры.
   - `CHANGELOG.md` → блок `## [Unreleased]` секции `Removed`
     (`public.agent_vector_index_store`) и `Changed` (FAISS-индекс
     собирается на лету из DuckDB-снапшота).
   - `docs/skill-tool-inventory.md` — пометить удалённые модули.
   - `project.json::gateway.vector.index.signature_table` — убрать
     (Pydantic `ProjectSettings(**)` теперь не принимает это поле;
     оператор чистит вручную или через отдельный мигратор).

4. **Откат**: `python tools/migrate.py --downgrade` восстанавливает
   таблицу, `git revert` PR восстанавливает код. Никаких
   runtime-toggle'ов — один путь, один механизм.

## Open Questions

- **Q1 (решён).** Подтягивание payload — точечные DuckDB-запросы
  по `(source, pk_value, chunk_index)` на каждый FAISS-hit.
  Реализация в `tasks.md` § 2.

- **Q2 (открыт, не блокирует).** Есть ли операторский инструментарий
  или Streamlit-панель, читающая таблицу-сигнатуру напрямую?
  Провести `grep -rn 'agent_vector_index_store' workspace tools
  benchmarks docs` **ДО** merge — если найдутся вызывающие, перевести
  их на `_index_cache` / `search_vector` в этом же PR.

- **Q4 (открыт, не блокирует).** Делать ли «мягкую миграцию» для
  `signature_table`-поля в `ProjectSettings` (принимать его как
  no-op с warning, удалять только в следующем MAJOR) или
  «жёсткую» (Pydantic отвергает сразу)? Текущий план — **жёсткая**
  (см. R4). Если на ревью решат смягчить — добавить задачу в
  `tasks.md` § 4 как опциональную.
