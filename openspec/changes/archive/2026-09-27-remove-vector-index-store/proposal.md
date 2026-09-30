## Why

`tools/build_vectors.py` падает на индексе из 19 770 × 1024 с
`psycopg2.errors.ProgramLimitExceeded: total size of jsonb object elements exceeds the maximum of 268435455 bytes`
(конкретные значения имени таблицы и индекса берутся из
`gateway.vector.index.storage_table` / `gateway.vector.index.indexes.*`).
Корень — `_save_index_to_store` сериализует в JSONB-metadata
таблицы-сигнатуры (`gateway.vector.index.signature_table`) полный
payload каждого чанка (`content` + `search_text` + `row`), который уже
есть в таблице-источнике. Это симптом архитектурной ошибки: у нас
**два** хранилища одних и тех же данных. Любой рост `content_cols`
снова упрётся в `jsonb`-лимит (`2³¹ − 1 = 256 МБ`). Решаем задачу
единственным способом — оставить один источник истины
(таблицу, заданную `gateway.vector.index.storage_table`) и убрать
второй (таблицу, заданную `gateway.vector.index.signature_table`).
Платим ~5 сек cold-start на индекс; warm-search — без регрессии.

## What Changes

- **Удалить** persisted FAISS-кеш в таблице, заданной
  `gateway.vector.index.signature_table` (методы `_save_index_to_store`,
  `_load_index_from_store`, `rebuild_and_store_index`,
  `_compute_index_signature_from_config` в
  `lib/services/cache_provider_impl.py`; `_rebuild_faiss` в
  `tools/build_vectors.py`; `VectorIndexBuildService.rebuild_and_store`
  в `lib/services/vector_index_service.py`).
- **Удалить** DDL `sql/vectors/create_vector_index_store.sql`,
  добавить миграцию `DROP TABLE IF EXISTS <signature_table>` (имя
  резолвится из настроек).
- **Изменить** `_load_index` в `lib/services/cache_provider_impl.py`:
  единственный путь — `_load_index_from_cache` (DuckDB-снапшот
  таблицы-источника), плюс fallback на `.faiss`-файлы.
- **Изменить** `build_faiss_index` (`lib/utils/duckdb_query.py:213`):
  больше не дублирует `content`/`search_text`/`row` — возвращает
  минимальный `meta = {"metric": ...}`.
- **Изменить** `build_raw_items` (`lib/utils/duckdb_query.py:145`):
  подтягивает `content`/`search_text`/`row` из DuckDB-снапшота
  таблицы-источника по `(source, pk_value, chunk_index)`.
- **Изменить** `preload_indexes` (`cache_provider_impl.py:1267`):
  итерируется по `gateway.vector.index.indexes.*`, не по
  `SELECT DISTINCT source FROM <signature_table>`.
- **Изменить** `search_vector`: сообщение об ошибке при отсутствии
  индекса ссылается только на DuckDB-кэш (не на таблицу-сигнатуру).
- **Изменить** конфиг: `gateway.vector.index.signature_table` —
  помечается DEPRECATED в `lib/core/project_settings.py` и `project.json`.
- **Документация**: `docs/VECTOR_INDEXES.md`, `docs/DATABASE.md`,
  `docs/ARCHITECTURE.md` § Vector, `AGENTS.md` § Configuration —
  убрать упоминания таблицы-сигнатуры;
  `CHANGELOG.md` → блок `## [Unreleased]` секции `Removed` + `Changed`;
  `docs/skill-tool-inventory.md` — пометить удалённые модули.
- **Тесты**: `tests/integration/test_vector_build_e2e.py`
  переписать под сценарий «INSERT в таблицу-источник → `preload_indexes`
  → `search_vector`»; добавить тест cold-start ≤ 5 с.

**BREAKING**: удаление публичной таблицы, имя которой задаётся
`gateway.vector.index.signature_table` (по умолчанию в существующих
проектах — `public.agent_vector_index_store`). Требует **MAJOR** по
SemVer (см. `AGENTS.md` § «Release Process» → MAJOR = «несовместимые
изменения API, удаление публичных модулей»).

## Capabilities

### New Capabilities
Нет.

### Modified Capabilities
- `data/vector-indexes`: требование «FAISS-backed» (персистенция в
  `<gateway.vector.index.default_root>/<index_name>` и/или в таблице,
  заданной `gateway.vector.index.signature_table`) заменяется
  требованием «FAISS собирается в памяти из DuckDB-снапшота
  таблицы-источника». Отрицательные требования дополняются запретом
  читать таблицу-сигнатуру / `gateway.vector.index.signature_table`.

## Impact

- **Код:**
  - `lib/services/cache_provider_impl.py` — удалить `_save_index_to_store`,
    `_load_index_from_store`, `rebuild_and_store_index`,
    `_compute_index_signature_from_config`; переписать `_load_index`,
    `preload_indexes`, `search_vector`.
  - `lib/services/vector_index_service.py` — удалить
    `rebuild_and_store`.
  - `lib/utils/duckdb_query.py` — `build_faiss_index` без тяжёлого
    payload; `build_raw_items` подтягивает payload из DuckDB.
  - `lib/core/project_settings.py` — `VectorIndexSettings.signature_table`
    помечается `deprecated=True` (значение по-прежнему читается,
    но не используется; следует удалить в следующем мажоре).
  - `tools/build_vectors.py` — `_rebuild_faiss` → no-op +
    `preload_indexes`.
- **SQL:** миграция
  `sql/migrations/<timestamp>_drop_signature_table.sql` (имя таблицы
  резолвится из `gateway.vector.index.signature_table`); устаревший
  `sql/vectors/create_vector_index_store.sql` помечается DEPRECATED.
- **Конфигурация:** `project.json` — комментарий-маркер DEPRECATED для
  `gateway.vector.index.signature_table`.
- **Документация:** `docs/VECTOR_INDEXES.md` (полный rewrite секции
  «Where vector data lives»), `docs/DATABASE.md` (§ vector-store),
  `docs/ARCHITECTURE.md` (§ Vector), `AGENTS.md` (§ Configuration
  Vector), `CHANGELOG.md`, `docs/skill-tool-inventory.md`,
  `docs/README.md` (навигация).
- **Тесты:** `tests/integration/test_vector_build_e2e.py`,
  контрактные тесты `_save_index_to_store` /
  `_load_index_from_store` / `rebuild_and_store_index`.
- **Совместимость:** существующие gateway-инстансы, которые ещё держат
  blob в таблице-сигнатуре, после деплоя будут игнорировать его;
  cold-start +5 с × N индексов при первом запросе; митигация —
  `preload_indexes` при старте gateway (`lib/services/preload_service.py`).
