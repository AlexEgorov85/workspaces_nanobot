## ADDED Requirements

### Requirement: Векторные индексы собираются из PostgreSQL без локального снимка

Система SHALL собирать FAISS-индексы напрямую из PostgreSQL, без
промежуточного локального файла. Таблица-источник эмбеддиндов задаётся
конфигурацией (`vector-mcp.storage_table`, ранее
`gateway.vector.index.storage_table`) и SHALL NOT быть зашита в код.

Система SHALL NOT создавать и SHALL NOT читать локальный кэш-файл DuckDB.
Путь `gateway.cache.local_path` SHALL быть удалён из конфигурации: кэшировать
на диске нечего.

#### Scenario: Сборка индекса при старте процесса

- **WHEN** процесс `vector-mcp` поднимается
- **THEN** он SHALL выбрать строки эмбеддингов из PostgreSQL по `source = ?`
  для каждого объявленного индекса
- **AND** SHALL собрать FAISS в памяти вызовом, эквивалентным
  `build_faiss_index(records, metric)`
- **AND** SHALL NOT создавать файлов на диске и SHALL NOT писать строки в
  таблицу подписи индекса

#### Scenario: Поиск по вектору

- **WHEN** вызван инструмент `vector_search` с `index_name` и `query_vector`
- **THEN** FAISS-индекс SHALL быть уже собран в памяти либо собран по требованию
  из той же таблицы PostgreSQL
- **AND** payload (`content` / `search_text` / `row`) для каждого hit'а SHALL
  подтягиваться запросом к PostgreSQL по `(source, pk_value, chunk_index)`

#### Scenario: Отсутствие модели эмбеддингов

- **WHEN** `vector-mcp` собирает или обслуживает индекс
- **THEN** он SHALL NOT загружать модель эмбеддингов и SHALL NOT выполнять
  сетевых вызовов к LLM-провайдеру
- **AND** эмбеддинги SHALL считаться отдельной задачей, записывающей строки в
  таблицу-источник

#### Scenario: Стоимость холодного старта

- **WHEN** первый `vector_search` для индекса вызван после старта процесса
- **THEN** сборка индекса SHALL завершаться не дольше, чем текущая сборка из
  локального снапшота, для эталонной рабочей станции и тех же объёмов

---

## REMOVED Requirements

### Requirement: Локальный файл кэша как источник для чтения

Удаляется требование «FAISS собирается в памяти из DuckDB-снапшота таблицы-источника».
Посредник между PostgreSQL и FAISS упраздняется.

**Удаляемые модули** (реальная стоимость смерти — ~2 600 строк; остальное
переезжает в `vector-mcp`):

| Модуль | Строк |
|---|---:|
| `lib/services/duckdb_cache_store.py` | 1455 |
| `lib/services/cache_load_service.py` | 472 |
| `lib/services/cache_provider.py` | 440 |
| `lib/utils/duckdb_query.py` (DuckDB-часть) | ~200 |
| `lib/services/cache_provider_impl.py` (`_capture_schema_meta`) | ~30 |
| `lib/core/skill_registration.py` | 98 |

**Удаляемые артефакты:** `sql/vectors/create_vector_index_config.sql` (помечен
LEGACY в шапке, кодом не читается), `sql/vectors/create_vector_index_store.sql`
(таблица уже удалена миграцией `V003`), `gateway.cache.local_path`,
`gateway.vector.index.default_root` (уже помечен DEPRECATED),
`skills.*.tables`, `skills.*.vector_indexes`.

**Удаляемые тесты (10 модулей):** `test_duckdb_cache_store.py`,
`test_cache_provider_meta.py`, `test_single_cache_interface.py`,
`test_cache_no_file_hold.py`, `test_cache_provider_open_failure.py`,
`test_cache_provider_mode.py`, `test_cache_load_service.py`,
`test_table_registry.py`, `test_skill_cache_boundary.py`,
`test_shared_cache_path_across_profiles.py`.

#### Scenario: Смешанный модуль извлекается до удаления

- **WHEN** `lib/utils/duckdb_query.py` удаляется
- **THEN** `build_faiss_index`, `group_vector_hits` и `build_raw_items` SHALL
  уже находиться в `vector-mcp`
- **AND** группировка чанков и косинусная нормализация SHALL NOT выводиться
  заново

#### Scenario: Реестр ресурсов не удаляется целиком

- **WHEN** удаляется агрегация имён в список загрузки снапшота
- **THEN** `resources_by_label("scripts_registry")` SHALL продолжать резолвить
  имя таблицы PostgreSQL для `audit_analyzer`
- **AND** `register_infra("vector.storage")` SHALL продолжать регистрировать
  `oarb.audit_vectors`
- **AND** `tracking_column_for` SHALL продолжать описывать колонку-маркер в PG

### Requirement: DuckDB как зависимость

- **WHEN** состав `requirements.txt` проверяется
- **THEN** `duckdb` и `pyarrow` SHALL отсутствовать
- **AND** `grep` по `lib/` и `workspace/` SHALL не находить упоминаний DuckDB
  в живом коде
