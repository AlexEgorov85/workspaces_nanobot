# Work brief `02-services-cache`

Product files: **10**, LOC: **4056**

## `lib/services/duckdb_cache_store.py` — 1455 LOC (code 1182)
- module: `lib.services.duckdb_cache_store`
- docstring: DuckDbCacheStore — локальное хранилище данных аудита (DuckDB + FAISS). Отвечает за ДАННЫЕ, а не за их источник: данные приходят извне методом ``upsert_records(table, records)`` (обычно — из PgDuckDbSyncService через call
- static importers (7): `lib/services/cache_provider.py`, `tests/integration/test_vector_build_e2e.py`, `tests/test_cache_no_file_hold.py`, `tests/test_cache_provider_mode.py`, `tests/test_cache_provider_open_failure.py`, `tests/test_duckdb_cache_store.py`, `tests/test_single_cache_interface.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 8

### class `UnsupportedFilesystemError` — lines 137-146 (10 LOC), 0 methods
- bases: RuntimeError
- decorators: —
- docstring: Concrete cache storage MUST reject unsupported network/shared filesystem. D11 / design D12: ``DuckDbCacheStore.open(path, mode)`` (и будущие SQLite/SQL-реализации) MUST проверить, что ``path`` лежит на локальной FS (ext4
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `DuckDbCacheStore` — lines 339-1455 (1117 LOC), 33 methods
- bases: CacheStore
- decorators: —
- docstring: Единственная concrete-реализация ``CacheProvider``: один DuckDB-файл + FAISS в памяти. Generic infrastructure component: получает записи через :meth:`upsert_records` (от любого синхронизатора), отвечает на SQL-запросы и 
- name referenced in 8 file(s); tests: 6

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 356-405 | `(self, *, cache_path: str='', schema: str='main', tables: list[str] | None=None, vector_db` | 7 | 0 | 6 | — |
| `connect` | 411-439 | `(self) -> bool` | 5 | 0 | 23 | Открыть DuckDB connection через ``_open_locked``. Возвращает ``False`` только для ошибок, не связанных с занят |
| `_read_conn` | 442-474 | `(self)` | 7 | 8 | 1 | Соединение на время операции чтения. Режим ``READ_WRITE`` (стадия загрузки) держит соединение постоянно — его  |
| `_close_locked` | 476-488 | `(self) -> None` | 3 | 2 | 2 | Закрыть соединение, если оно открыто по себе. Включается после операции чтения и перед передачей егё владелецу |
| `_open_locked` | 490-522 | `(self) -> None` | 7 | 5 | 2 | — |
| `_classify_open_error` | 525-548 | `(exc: Exception, path: str) -> Exception` | 5 | 1 | 1 | Разобрать ошибку открытия файла кэша. Конфликт блокировки превращается в ``CacheBusyError`` — процесс держит ф |
| `configure` | 550-582 | `(self, *, schema: str='main', tables: list[str] | None=None, vector_db_table: str='', embe` | 7 | 0 | 11 | Настроить экземпляр перед первым ``connect()``. Существует, чтобы composition root **не писал приватные поля** |
| `open` | 585-611 | `(cls, path: str, mode: CacheAccessMode) -> DuckDbCacheStore` | 1 | 0 | 7 | Concrete factory — создать ``DuckDbCacheStore`` с заданным access mode. Является единственным путём для открыт |
| `is_ready` | 613-614 | `(self) -> bool` | 1 | 0 | 5 | — |
| `close` | 616-626 | `(self) -> None` | 4 | 1 | 30 | — |
| `__enter__` | 628-629 | `(self) -> DuckDbCacheStore` | 1 | 0 | 4 | — |
| `__exit__` | 631-632 | `(self, *args) -> None` | 1 | 0 | 1 | — |
| `upsert_records` | 638-684 | `(self, table: str, records: list[dict[str, Any]], *, key_column: str | None=None) -> bool` | 4 | 0 | 1 | Добавить/обновить строки таблицы в локальный кэш. Батч заменяет существующие записи с тем же ключом (upsert),  |
| `ensure_schema` | 686-713 | `(self, table: str, columns: list[dict[str, Any]]) -> bool` | 4 | 0 | 2 | Создать таблицу по описанию колонок из источника (типы, NOT NULL, комментарии). Используется вместо вывода стр |
| `_ensure_schema_locked` | 715-751 | `(self, table: str, columns: list[dict[str, Any]]) -> None` | 13 | 2 | 1 | — |
| `replace_records` | 753-775 | `(self, table: str, records: list[dict[str, Any]]) -> bool` | 3 | 0 | 2 | Полностью пересоздать содержимое таблицы из полного батча. Используется при полной пересинхронизации (сверка у |
| `_replace_locked` | 777-834 | `(self, table: str, records: list[dict[str, Any]]) -> None` | 14 | 1 | 1 | — |
| `_ensure_meta_table` | 838-845 | `(self) -> None` | 1 | 2 | 1 | — |
| `_save_schema_meta` | 847-868 | `(self, schema: str, table: str, columns: list[dict[str, Any]]) -> None` | 11 | 1 | 1 | — |
| `_load_schema_meta` | 870-886 | `(self, schema: str) -> dict[tuple, tuple]` | 4 | 0 | 1 | Метаданные схемы: {(table, col\|None) -> (comment, pg_type)}. |
| `_upsert_locked` | 888-989 | `(self, table: str, records: list[dict[str, Any]], key_column: str | None=None) -> None` | 30 | 2 | 1 | — |
| `_ingest_arrow` | 991-1046 | `(self, table: str, records: list[dict[str, Any]], cols: list[str], create_table: bool) -> ` | 10 | 3 | 1 | Залить записи в DuckDB через pyarrow + conn.register. create_table=True → CREATE OR REPLACE TABLE create_table |
| `_mark_vector_sources_dirty` | 1048-1064 | `(self, table: str, records: list[dict[str, Any]]) -> None` | 6 | 2 | 1 | Пометить vector-источники как dirty, чтобы FAISS пересобрался. Lookup через ``table_registry.vector_resources( |
| `get_schema` | 1070-1082 | `(self, schema_name: str | None=None, table_names: list[str] | None=None) -> dict[str, Any]` | 5 | 0 | 3 | — |
| `query_sql` | 1084-1095 | `(self, sql: str, params: list[Any] | None=None) -> dict[str, Any]` | 3 | 0 | 9 | — |
| `_assert_query_sql_allowed_locked` | 1097-1124 | `(self, sql: str) -> None` | 5 | 1 | 1 | Второй уровень защиты (assertion guard) для ``query_sql``. Первый уровень — DuckDB connection opened с ``read_ |
| `explain` | 1126-1132 | `(self, sql: str) -> dict[str, Any]` | 3 | 0 | 3 | — |
| `execute_readonly` | 1134-1166 | `(self, sql: str, params: dict[str, Any] | list[Any] | None=None, max_rows: int=1000) -> di` | 9 | 0 | 1 | Выполнить read-only SQL к настроенному DuckDB-кэшу. Используется ``CacheProvider.execute_readonly`` (generic C |
| `preload_indexes` | 1172-1263 | `(self) -> list[dict[str, Any]]` | 11 | 0 | 5 | Прогреть FAISS-индексы всех источников из DuckDB-кэша в память. Returns: Список построенных индексов [{"index_ |
| `preload_errors` | 1265-1272 | `(self) -> list[dict[str, Any]]` | 1 | 0 | 1 | Последние ошибки ``preload_indexes`` (сброс при каждом вызове). Каждая ошибка — dict с ключами ``index_name``  |
| `_load_source_index` | 1274-1320 | `(self, source: str, metric: str | None=None) -> tuple[Any, dict | None]` | 17 | 2 | 2 | Прочитать векторы source из DuckDB и построить FAISS-индекс. ``metric`` передаётся в ``build_faiss_index`` (но |
| `search_vector` | 1322-1402 | `(self, query: str, index_name: str='default_index', index_path: str | None=None, top_k: in` | 22 | 0 | 6 | Семантический поиск по локальному FAISS-индексу. Возвращает список ``SearchResult`` (lib.services.cache_provid |
| `get_stats` | 1408-1455 | `(self) -> dict[str, Any]` | 11 | 0 | 7 | Снимок состояния хранилища для мониторинга. |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_split_table` | 60-65 | `(table: str) -> tuple[str, str]` | 2 | 2 | Разбить ``schema.table`` (значение ``vector_db_table`` / ``storage_table``) на (schema, table). |
| `_infer_duckdb_type` | 68-81 | `(values) -> str` | 16 | 1 | Вывести тип DuckDB для колонки по её значениям (для ALTER ADD COLUMN). |
| `_records_to_arrow` | 84-125 | `(records: list[dict[str, Any]])` | 13 | 1 | Сериализовать list[dict] в pyarrow.Table (без pandas). Сохраняет вложенные типы: - list[number] → DOUBLE[] (Du |
| `_safe_str` | 128-134 | `(v: Any) -> str | None` | 3 | 1 | Строковое представление для гетерогенных/нестандартных значений. |
| `_reject_unsupported_filesystem` | 149-196 | `(path: str) -> None` | 13 | 2 | Поднять ``UnsupportedFilesystemError``, если ``path`` на network FS. Работает через ``/proc/mounts`` (только L |
| `_classify_sql` | 216-252 | `(sql: str) -> str` | 10 | 1 | Классифицировать SQL statement type для ``query_sql`` валидации. Returns: Один из ``"SELECT" / "DML" / "DDL" / |
| `_extract_lock_holder` | 295-304 | `(message: str) -> str | None` | 4 | 2 | Извлечь «кто держит файл» из текста ошибки DuckDB (``None`` — не нашли). |
| `_map_pg_type` | 307-336 | `(pg_type: str) -> str` | 22 | 2 | Смаппить PG-тип колонки в DuckDB-тип. Возвращает тип, пригодный для ``CREATE TABLE`` / ``ALTER ADD COLUMN`` в  |

## `lib/services/cache_provider_impl.py` — 476 LOC (code 388)
- module: `lib.services.cache_provider_impl`
- docstring: Вспомогательные функции слоя кэша, общие для реализации ``CacheProvider``. Модуль **не** является реализацией интерфейса: она живёт в ``lib/services/duckdb_cache_store.py``. Здесь осталось то, что нужно и реализации, и п
- static importers (17): `lib/core/skill_config.py`, `lib/services/cache_provider.py`, `lib/services/duckdb_cache_store.py`, `lib/services/preload_service.py`, `lib/services/vector_index_service.py`, `tests/integration/test_vector_build_e2e.py`, `tests/test_auto_register_skills.py`, `tests/test_cache_provider_meta.py`, `tests/test_check_indexes.py`, `tests/test_duckdb_cache_store.py`, `tests/test_get_embedding_auth.py`, `tests/test_preload_service.py`, `tests/test_single_cache_interface.py`, `tests/test_skill_config_api.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 9

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `compute_index_signature` | 77-95 | `(cfg: dict[str, Any]) -> str` | 8 | 5 | SHA256-хеш канонической конфигурации индекса. Вход: dict, где ключи — поля из ``_INDEX_SIGNATURE_FIELDS`` (неп |
| `verify_index_signature` | 98-128 | `(stored_meta: dict[str, Any] | None, current_cfg: dict[str, Any]) -> Literal['CURRENT', 'S` | 7 | 2 | Сравнить signature в сохранённом metadata с текущим конфигом. После change ``remove-vector-index-store`` persi |
| `list_runtime_vector_indexes` | 131-228 | `(store_table: str | None=None, *, provider: Any | None=None) -> list[dict[str, Any]]` | 29 | 4 | Прочитать runtime-артефакты vector-индексов из DuckDB-снапшота. После change ``remove-vector-index-store`` per |
| `_is_missing_relation` | 231-244 | `(error: str) -> bool` | 4 | 1 | Отсутствует ли в сообщении признак «таблицы/представления нет». Отличается от «кэш недоступен»: отсутствие таб |
| `get_embedding` | 265-314 | `(text: str) -> list[float] | None` | 10 | 4 | Единая точка получения эмбеддинга текста через Ollama /api/embed. Параметры подключения (``base_url`` / ``mode |
| `read_embedding_config` | 317-333 | `() -> dict[str, Any]` | 3 | 3 | Параметры эмбеддера — из захардкоженных констант. Секция ``gateway.vector.embedding`` удалена; параметры подкл |
| `read_embedding_defaults` | 336-347 | `() -> dict[str, Any]` | 1 | 1 | Дефолтные chunk-параметры сборки (``_DEFAULT_CHUNK_*``). Единая точка чтения для build- и verify-сторон: ``too |
| `read_vector_index_config` | 350-388 | `() -> dict[str, Any]` | 24 | 6 | Конфиг векторных индексов из ``project.json::gateway.vector.index.indexes``. Единственный источник декларации  |
| `_capture_schema_meta` | 392-476 | `(conn: Any, pg_conn: Any, schema_pairs: list[tuple]) -> None` | 12 | 1 | Сохранить комментарии таблиц/колонок и исходные PG-типы в DuckDB-кэш. ``schema_pairs`` — список ``(schema, [ta |

## `lib/services/cache_load_service.py` — 472 LOC (code 405)
- module: `lib.services.cache_load_service`
- docstring: Загрузка локального кэша навыков из PostgreSQL — разовая синхронная операция. Change ``drop-local-cache-read-from-pg``. Заменяет ``PgDuckDbSyncService``. Назначение модуля ----------------- Кэш существует ради **экономии
- static importers (4): `lib/core/application_context.py`, `tests/test_application_context_cache_lifecycle.py`, `tests/test_cache_load_service.py`, `tests/test_unified_event_logging_contract.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 3, module functions: 0

### class `CacheLoadError` — lines 46-51 (6 LOC), 0 methods
- bases: RuntimeError
- decorators: —
- docstring: Загрузка кэша невозможна (PostgreSQL недоступен или соединение оборвано). Вызывающий MUST NOT выставлять READY: кэш остаётся пустым, и потребитель получает явную ошибку отсутствия данных вместо устаревших значений.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `CacheLoadResult` — lines 55-80 (26 LOC), 1 methods
- bases: object
- decorators: dataclass
- docstring: Итог разовой загрузки кэша. Attributes: loaded_tables: число таблиц, загруженных успешно. total_tables: число запрошенных таблиц. errors: число таблиц, где загрузка упала не по отсутствию таблицы. missing_tables: таблицы
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `loaded_at` | 78-80 | `(self) -> datetime.datetime | None` | 1 | 0 | 2 | Время, на которое актуален снимок. |

### class `CacheLoadService` — lines 83-472 (390 LOC), 14 methods
- bases: object
- decorators: —
- docstring: Разовая загрузка кэша навыков из PostgreSQL. Единственный writer кэша: держит роль ``CacheStore`` и вызывает ``ensure_schema`` / ``replace_records``. Никаких колбэков и вторых путей записи. Аргументы: dsn: DSN PostgreSQL
- name referenced in 4 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 104-123 | `(self, *, dsn: str, store: 'CacheStore', schema: str='main', tables: list[str] | None=None` | 4 | 0 | 6 | — |
| `load` | 129-212 | `(self) -> CacheLoadResult` | 11 | 0 | 10 | Загрузить кэш целиком и вернуть результат. Блокирует до завершения. Не порождает фоновых потоков и не оставляе |
| `get_stats` | 214-225 | `(self) -> dict[str, Any]` | 7 | 0 | 7 | Сведения о последней загрузке (для health-summary и диагностики). |
| `_effective_workers` | 231-232 | `(self) -> int` | 4 | 4 | 2 | — |
| `_load_one` | 234-300 | `(self, table: str, result: CacheLoadResult) -> int` | 5 | 1 | 1 | Загрузить одну таблицу целиком. Возвращает число строк. |
| `_ensure_table_schema` | 302-307 | `(self, table: str) -> None` | 2 | 1 | 1 | Создать/привести схему таблицы кэша к схеме PG (information_schema). |
| `_log_sync_event` | 309-339 | `(self, event_type: str, summary: str, payload: dict[str, Any] | None=None, *, level: str='` | 2 | 6 | 2 | Записать sync-событие в ``agent_gateway_logs`` (единый конвейер). Единственный writer — ``DbLoggingService`` ч |
| `_track_column_for` | 341-368 | `(self, table: str) -> str` | 6 | 1 | 2 | Вернуть колонку для инкрементального отслеживания изменений. Источник истины в ``lib.services.table_registry`` |
| `_fetch_schema` | 370-431 | `(self, table: str) -> list[dict]` | 14 | 1 | 1 | Описание колонок таблицы из PG: типы, NOT NULL, комментарии. |
| `_split_table` | 433-438 | `(self, table: str) -> tuple[str, str]` | 2 | 1 | 1 | Р Р°Р·Р±РёС‚СЊ 'oarb.audits' РЅР° (schema, table). |
| `_fq_table` | 440-444 | `(self, table: str) -> str` | 2 | 1 | 3 | Полное имя таблицы ``schema.table`` (без точки — схема из конфига). |
| `_db_run` | 446-452 | `(self, fn)` | 2 | 2 | 2 | Выполнить ``fn(conn)`` на свободном соединении общего пула ``utils.db``. |
| `_fetch_all` | 454-467 | `(self, table: str) -> tuple[list[dict], Any]` | 2 | 1 | 1 | — |
| `_max_track` | 470-472 | `(rows: list[dict], track_col: str) -> Any` | 6 | 1 | 1 | — |

## `lib/services/cache_provider.py` — 440 LOC (code 342)
- module: `lib.services.cache_provider`
- docstring: Универсальный интерфейс провайдера кэша данных (СУБД + векторные индексы). Слой абстрагирует два источника данных, типичных для RAG/аналитики: * **SQL-кэш** — локальная аналитическая БД (по умолчанию DuckDB-файл), котору
- static importers (17): `lib/core/application_context.py`, `lib/core/skill_config.py`, `lib/services/cache_load_service.py`, `lib/services/cache_provider_impl.py`, `lib/services/duckdb_cache_store.py`, `lib/services/vector_index_service.py`, `tests/test_application_context_cache_lifecycle.py`, `tests/test_cache_no_file_hold.py`, `tests/test_cache_provider_mode.py`, `tests/test_cache_provider_open_failure.py`, `tests/test_single_cache_interface.py`, `tests/test_skill_tool_integration.py`, `tools/build_vectors.py`, `tools/check_indexes.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 10, module functions: 1

### class `CacheAccessMode` — lines 29-44 (16 LOC), 0 methods
- bases: Enum
- decorators: —
- docstring: Режим доступа к cache storage. Описывает **свойство доступа**, а не результат координации владения между процессами: распределённый ownership удалён (см. change ``drop-local-cache-read-from-pg``), и режим определяется ст
- name referenced in 11 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SearchResult` — lines 48-65 (18 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Один группированный результат векторного поиска по СУБД. Отражает запись в индексном источнике: исходный текст (content), метрику схожести (score) и атрибуты исходной строки (source/table/pk, полный row — исходная запись
- name referenced in 5 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `IndexIntegrityError` — lines 68-82 (15 LOC), 1 methods
- bases: Exception
- decorators: —
- docstring: Векторный индекс не прошёл проверку signature (STALE/INVALID). Поднимается провайдером (``search_vector``), когда сохранённая сигнатура индекса не совпадает с текущей конфигурацией (модель эмбеддингов, размерность, колон
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 78-82 | `(self, index_name: str, status: str, reason: str='') -> None` | 1 | 0 | 6 | — |

### class `UnsupportedSqlError` — lines 85-98 (14 LOC), 1 methods
- bases: Exception
- decorators: —
- docstring: SQL statement type не поддерживается CacheProvider. Поднимается ``query_sql()`` при попытке выполнить DDL (``CREATE/ALTER/DROP/TRUNCATE``) в любом mode. Отдельный exception class — чтобы tool мог показать пользователю ст
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 95-98 | `(self, sql: str, reason: str='') -> None` | 2 | 0 | 6 | — |

### class `ReadOnlyAssertionError` — lines 101-116 (16 LOC), 1 methods
- bases: Exception
- decorators: —
- docstring: Попытка мутации через ``query_sql()`` при ``mode=READ_ONLY``. Первый уровень защиты — DuckDB connection с ``read_only=True`` — физически блокирует ``INSERT/UPDATE/DELETE``. Это второй уровень (assertion guard в ``CachePr
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 111-116 | `(self, sql: str='') -> None` | 1 | 0 | 6 | — |

### class `CacheBusyError` — lines 119-146 (28 LOC), 1 methods
- bases: Exception
- decorators: —
- docstring: Файл кэша уже держит другой процесс. Файл кэша — **process-exclusive** ресурс: в любой момент у него ровно один активный владелец. Попытка открыть его вторым процессом невозможна по определению. **Проверкой занятости слу
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 136-146 | `(self, path: str='', holder: str='', cause: Exception | None=None) -> None` | 2 | 0 | 6 | — |

### class `CacheOpenError` — lines 149-173 (25 LOC), 1 methods
- bases: ConfigurationError
- decorators: —
- docstring: Файл кэша не удалось открыть (кроме случая «занят другим процессом»). Занятость разбирается отдельно и поднимается как :class:`CacheBusyError`. Всё остальное — битый/повреждённый файл, неподдерживаемая FS, read-only к не
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 165-173 | `(self, path: str='', cause: Exception | None=None) -> None` | 2 | 0 | 6 | — |

### class `CacheProvider` — lines 176-273 (98 LOC), 7 methods
- bases: ABC
- decorators: —
- docstring: Роль **чтения**: что получают потребители (runtime, skills, tools). Единственная точка доступа к файлу кэша для всех, кто читает. Конкретная реализация (DuckDB, другая СУБД, файл в памяти) — деталь этого модуля: называть
- name referenced in 7 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `is_ready` | 210-212 | `(self) -> bool` | 1 | 0 | 5 | Готов ли кэш к запросам (файл открыт и в памяти). |
| `preload_indexes` | 215-221 | `(self) -> list[dict[str, Any]]` | 1 | 0 | 5 | Прогреть векторные индексы из БД/файлов в память. Returns: Список загруженных индексов [{"index_name", "vector |
| `search_vector` | 226-239 | `(self, query: str, index_name: str='default_index', index_path: str | None=None, top_k: in` | 1 | 0 | 6 | Семантический поиск по векторному индексу. Возвращает пустой список, если ничего не найдено или поиск невозмож |
| `query_sql` | 242-248 | `(self, sql: str, params: list | None=None) -> dict[str, Any]` | 1 | 0 | 9 | Выполнить SELECT-запрос к SQL-кэшу (агрегации/отчёты). Returns: dict: {status, row_count, columns, rows} (+ er |
| `explain` | 251-257 | `(self, sql: str) -> dict[str, Any]` | 1 | 0 | 3 | EXPLAIN на SQL-кэше — синтаксическая проверка без выполнения. Returns: dict: {"valid": True, "plan": [...]} ил |
| `get_schema` | 260-266 | `(self, schema_name: str | None=None, table_names: list[str] | None=None) -> dict[str, Any]` | 1 | 0 | 3 | Получить структуру таблиц кэша (information_schema). |
| `close` | 271-273 | `(self) -> None` | 1 | 0 | 30 | Закрыть открытые ресурсы (соединение кэша и т.п.). |

### class `CacheIngestion` — lines 276-334 (59 LOC), 3 methods
- bases: ABC
- decorators: —
- docstring: Роль **записи**: её получает только sync-слой. Sync-сервис — единственный владелец содержимого кэша, и он умеет три разные вещи, которые нельзя смешивать: * ``upsert_records`` — долить/обновить дельту; * ``replace_record
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `upsert_records` | 294-311 | `(self, table: str, records: list[dict[str, Any]], *, key_column: str | None=None) -> bool` | 1 | 0 | 1 | Добавить/обновить строки таблицы в кэше. Args: table: ``schema.table`` (или ``table`` в схеме хранилища). reco |
| `replace_records` | 314-324 | `(self, table: str, records: list[dict[str, Any]]) -> bool` | 1 | 0 | 2 | Полностью пересоздать содержимое таблицы (full resync). Деструктивная операция: несвязанные строки удаляются.  |
| `ensure_schema` | 327-334 | `(self, table: str, records: list[dict[str, Any]], schema_meta: dict[tuple[str, str], tuple` | 1 | 0 | 2 | Привести схему таблицы в соответствие с батчем (DDL при нехватке). |

### class `CacheStore` — lines 337-346 (10 LOC), 0 methods
- bases: CacheProvider, CacheIngestion
- decorators: —
- docstring: Полный контракт единственного хранилища кэша: чтение + ingestion. Это то, что возвращает :func:`open_cache_provider` и что лежит в ``ApplicationContext.cache_provider``. Роли разделены, но реализация по-прежнему одна, а 
- name referenced in 5 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `open_cache_provider` | 349-440 | `(*, mode: CacheAccessMode, db_logging_service: Any | None=None) -> CacheStore` | 22 | 6 | **Единственная точка создания** ``CacheProvider`` в рантайме. Её зовут одинаково: runtime (composition root),  |

## `lib/services/table_registry.py` — 347 LOC (code 274)
- module: `lib.services.table_registry`
- docstring: Pluggable реестр ресурсов runtime'а для синхронизации PostgreSQL → DuckDB. Единая точка регистрации для двух видов ресурсов: * skill-ресурсы (``SkillRegistration``): доменные таблицы и вектора, декларируются навыком в ``
- static importers (17): `lib/core/application_context.py`, `lib/core/infra_registration.py`, `lib/core/skill_config.py`, `lib/core/skill_registration.py`, `lib/services/cache_load_service.py`, `lib/services/cache_provider.py`, `lib/services/duckdb_cache_store.py`, `tests/test_application_context.py`, `tests/test_application_context_cache_lifecycle.py`, `tests/test_auto_register_skills.py`, `tests/test_cache_load_service.py`, `tests/test_duckdb_cache_store.py`, `tests/test_infra_registration.py`, `tests/test_project_settings.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 4, module functions: 0

### class `TableResource` — lines 37-63 (27 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Декларативное описание одной PG-таблицы для DuckDB-кэша. DTO: только имя, опциональная tracking-колонка, опциональная ``label``. Не открывает соединения, не выполняет SQL, не управляет кэшем — синхронизация выполняется и
- name referenced in 8 file(s); tests: 6

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__post_init__` | 58-63 | `(self) -> None` | 3 | 0 | 0 | — |

### class `VectorResource` — lines 67-88 (22 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Декларативное описание одной PG-таблицы сырых эмбеддингов. Vector-таблица попадает в два независимых pipeline'а: обычный table-sync (PG → DuckDB) для чтения эмбеддингов через ``vector_search``, и vector-индексация (FAISS
- name referenced in 9 file(s); tests: 6

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__post_init__` | 83-88 | `(self) -> None` | 3 | 0 | 0 | — |

### class `SkillRegistration` — lines 97-135 (39 LOC), 4 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Описание ресурсов одного skill'а. Attributes: name: уникальное имя skill'а. resources: единый набор ресурсов (TableResource/VectorResource). enabled: ``False`` — ресурсы пропускаются при sync.
- name referenced in 9 file(s); tests: 7

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__post_init__` | 110-112 | `(self) -> None` | 2 | 0 | 0 | — |
| `table_resources` | 114-116 | `(self) -> tuple[TableResource, ...]` | 2 | 2 | 6 | Все ``TableResource`` этого skill'а. |
| `vector_resources` | 118-120 | `(self) -> tuple[VectorResource, ...]` | 2 | 2 | 7 | Все ``VectorResource`` этого skill'а. |
| `tracking_column_for` | 122-135 | `(self, table: str) -> str` | 6 | 0 | 4 | Track-колонка для таблицы в этом skill'е. ``VectorResource`` без явного ``tracking_column`` → ``"id"`` (append |

### class `TableRegistry` — lines 139-333 (195 LOC), 22 methods
- bases: object
- decorators: dataclass
- docstring: Singleton-реестр ресурсов runtime'а. Два независимых namespace'а: * ``_registrations`` — skill-ресурсы (доменные таблицы + вектора), ключ — ``SkillRegistration.name``. Регистрируются через ``register(SkillRegistration(..
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `register` | 166-172 | `(self, registration: SkillRegistration) -> None` | 2 | 0 | 12 | Зарегистрировать skill. |
| `register_infra` | 174-193 | `(self, key: str, resources: tuple[Resource, ...]) -> None` | 6 | 0 | 2 | Зарегистрировать инфраструктурные ресурсы. ``key`` — логический идентификатор namespace'а. Не пересекается с и |
| `unregister_infra` | 195-197 | `(self, key: str) -> None` | 1 | 0 | 2 | Удалить инфраструктурную регистрацию. |
| `unregister` | 199-201 | `(self, name: str) -> None` | 1 | 0 | 3 | Удалить регистрацию skill'а. |
| `get` | 203-204 | `(self, name: str) -> SkillRegistration | None` | 2 | 0 | 149 | — |
| `get_infra` | 206-208 | `(self, key: str) -> tuple[Resource, ...]` | 2 | 0 | 3 | Инфраструктурные ресурсы по ``key`` (пустой tuple, если нет). |
| `infra_keys` | 210-211 | `(self) -> tuple[str, ...]` | 1 | 0 | 1 | — |
| `names` | 213-214 | `(self) -> tuple[str, ...]` | 1 | 0 | 14 | — |
| `enabled_names` | 216-220 | `(self) -> tuple[str, ...]` | 2 | 0 | 1 | Имена enabled-registrations. |
| `skill_for_table` | 222-232 | `(self, table: str) -> SkillRegistration | None` | 6 | 0 | 3 | Найти регистрацию, владеющую таблицей. |
| `resources_by_label` | 234-248 | `(self, label: str) -> tuple[TableResource, ...]` | 6 | 0 | 3 | Все ``TableResource`` skill'ов с заданным ``label``. Ищет только в skill-регистрациях (label — доменная метка, |
| `_infra_table_resources` | 250-251 | `(self) -> tuple[TableResource, ...]` | 2 | 1 | 1 | — |
| `_infra_vector_resources` | 253-254 | `(self) -> tuple[VectorResource, ...]` | 2 | 1 | 1 | — |
| `_iter_infra` | 256-260 | `(self) -> tuple[Resource, ...]` | 2 | 3 | 1 | — |
| `table_resources` | 262-273 | `(self) -> tuple[TableResource, ...]` | 3 | 2 | 6 | Все ``TableResource`` (skills + infra). Порядок: skill-ресурсы, затем инфра-ресурсы. |
| `vector_resources` | 275-286 | `(self) -> tuple[VectorResource, ...]` | 3 | 2 | 7 | Все ``VectorResource`` (skills + infra). Порядок: skill-ресурсы, затем инфра-ресурсы. |
| `resources` | 288-290 | `(self) -> tuple[Resource, ...]` | 1 | 0 | 5 | Все ресурсы (skills + infra, таблицы + векторы). |
| `table_names` | 292-294 | `(self) -> tuple[str, ...]` | 2 | 1 | 5 | Имена всех таблиц в порядке регистрации. |
| `vector_names` | 296-298 | `(self) -> tuple[str, ...]` | 2 | 1 | 6 | Имена всех vector-таблиц в порядке регистрации. |
| `tracking_column_for` | 300-314 | `(self, table: str) -> str` | 9 | 0 | 4 | Track-колонка для таблицы (skills + infra). |
| `clear` | 316-325 | `(self) -> None` | 1 | 0 | 12 | Очистить реестр (skill + infra + embedding). Полная очистка состояния singleton'а. Безопасна для повторного вы |
| `snapshot_path` | 327-333 | `(self, workspace_path: Path, filename: str='cache.duckdb') -> Path` | 1 | 0 | 1 | Путь к runtime-снапшоту DuckDB. По умолчанию: ``<workspace>/data_store/duckdb/cache.duckdb`` — единый файл для |

## `lib/core/skill_config.py` — 325 LOC (code 247)
- module: `lib.core.skill_config`
- docstring: Runtime API для skill'ов: конфигурация, таблицы, FAISS. Параметризован по ``skill_name``. Каждый skill вызывает функции со своим именем (например, ``get_db_tables("audit_analyzer")``). Это единая точка для всех skill'ов 
- static importers (0): — NONE —
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 0, module functions: 21

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_skills` | 27-31 | `() -> dict[str, Any]` | 3 | 1 | Секция ``skills.*`` из project.json. |
| `_skill_cfg` | 34-38 | `(skill_name: str) -> dict[str, Any]` | 3 | 1 | — |
| `_tables_list` | 41-44 | `(skill_name: str) -> list[dict]` | 5 | 1 | — |
| `_vector_indexes_list` | 47-50 | `(skill_name: str) -> list[dict]` | 4 | 1 | — |
| `get_db_tables` | 53-66 | `(skill_name: str) -> list[str]` | 6 | 4 | Доменные таблицы skill'а для LLM-схемы. Возвращает имена таблиц из ``tables[]`` без ``label`` — это доменные т |
| `get_db_schema` | 69-82 | `(skill_name: str) -> str` | 4 | 4 | Схема skill'а (по первой таблице в ``tables[]``). |
| `get_predefined_scripts_table` | 85-101 | `(skill_name: str) -> str` | 2 | 4 | Имя таблицы реестра предопределённых SQL-скриптов (``label='scripts_registry'``). Lookup идёт через ``TableReg |
| `load_db_config` | 104-105 | `(skill_name: str) -> dict[str, Any]` | 1 | 0 | — |
| `get_llm_config` | 108-111 | `(skill_name: str) -> dict[str, Any]` | 1 | 4 | — |
| `get_tool_config` | 114-115 | `(skill_name: str) -> dict[str, Any]` | 1 | 0 | — |
| `get_cli_config` | 118-126 | `(skill_name: str) -> dict[str, Any]` | 7 | 7 | — |
| `get_max_retries` | 129-132 | `(skill_name: str) -> int` | 4 | 3 | — |
| `get_chunking_config` | 135-167 | `(skill_name: str) -> dict[str, Any]` | 10 | 5 | Параметры map-reduce чанкинга из ``skills.<name>.chunking.*``. Дефолты согласованы с прежней реализацией навык |
| `get_brief_context_config` | 170-202 | `(skill_name: str) -> dict[str, Any]` | 10 | 2 | Параметры BriefContextBuilder (``skills.<name>.brief_context.*``). Новый секционный ключ, введённый в brief-re |
| `get_in_memory_cache_path` | 205-231 | `(skill_root: Path | str) -> str` | 6 | 0 | Путь к файлу runtime-кэша (``cache.duckdb``). Файл общий для всех skill'ов. v2.5.2+ путь вычисляется через :fu |
| `get_vector_index_path` | 234-251 | `(skill_name: str, skill_root: Path | str) -> str` | 12 | 0 | Путь к FAISS-индексу: ``<default_root>/<index_name>``. Берёт первый индекс из ``vector_indexes[]``. Путь относ |
| `get_vector_db_table` | 254-271 | `(skill_name: str) -> str` | 14 | 1 | Имя таблицы-хранилища векторов. Источник — ``gateway.vector.index.storage_table``. Fallback — ``tables[type="v |
| `build_cache_provider` | 274-300 | `(skill_name: str, skill_root: Path | str) -> CacheProvider` | 1 | 2 | Провайдера кэша для skill'а — через ту же точку создания, что и у runtime. Тонкий делегат в :func:`lib.service |
| `get_vector_indexes` | 303-309 | `(skill_name: str) -> dict[str, Any]` | 1 | 0 | Метаданные индексов из ``gateway.vector.index.indexes`` (см. ``VectorIndexSettings.indexes`` и ``cache_provide |
| `get_embedding_config` | 312-321 | `() -> dict[str, Any]` | 1 | 1 | Embedding-конфиг из захардкоженных констант. Источник — ``cache_provider_impl.read_embedding_config()`` (``_EM |
| `get_embedding_model` | 324-325 | `() -> str` | 2 | 0 | — |

## `lib/services/preload_service.py` — 318 LOC (code 268)
- module: `lib.services.preload_service`
- docstring: PreloadService — прогрев FAISS-индексов при старте агента. Тонкий сервис: только runtime-метод ``preload_vector_indexes(store)``, который gateway вызывает после initial sync, чтобы FAISS-индексы были готовы к первому зап
- static importers (4): `lib/core/application_context.py`, `tests/test_preload_service.py`, `tests/test_remove_vector_index_store_guards.py`, `tests/test_unified_event_logging_contract.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 3

### class `PreloadService` — lines 175-318 (144 LOC), 3 methods
- bases: object
- decorators: —
- docstring: Runtime-сервис: прогрев FAISS-индексов в память. Этот сервис НЕ знает о цикле запуска — он предоставляет async-метод, который точка входа (``gateway.py``) запускает после ``PgDuckDbSyncService`` initial_load. Имя и API с
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 184-190 | `(self, settings: Any=None, db_logging_service: Any | None=None) -> None` | 1 | 0 | 6 | — |
| `preload_vector_indexes` | 192-235 | `(self, store: Any) -> list | None` | 5 | 0 | 1 | Прогреть FAISS-индексы из DuckDB-кэша в память (gateway). ``store.preload_indexes()`` — тяжёлая синхронная опе |
| `_emit_health_summary` | 237-318 | `(self, loaded: list | None, store: Any=None) -> None` | 9 | 1 | 1 | Печать в stderr + запись в ``agent_gateway_logs``. ``declared`` берём из JSON (read_vector_index_config). ``ru |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_emit_health_event` | 42-76 | `(summary: str, payload: dict[str, Any], *, level: str, service: Any | None) -> None` | 1 | 2 | DEPRECATED: используется только тестами как testable unit контракта. Production call-site в ``preload_vector_i |
| `_format_lines` | 79-109 | `(declared_names: list[str], loaded_items: list[dict[str, Any]], missing: list[str], orphan` | 15 | 2 | Human-readable multi-line для терминала. Цвет/жирность не навешиваем (нет ANSI на Windows-cmd). Только текст. |
| `compute_index_health` | 112-172 | `(declared: dict[str, Any], loaded: list[dict[str, Any]] | None, runtime_rows: list[dict[st` | 22 | 3 | Pure-функция: посчитать declared/loaded/missing/orphan/stale. Args: declared: результат ``read_vector_index_co |

## `lib/core/skill_registration.py` — 98 LOC (code 78)
- module: `lib.core.skill_registration`
- docstring: Утилиты для регистрации skill'ов в ``table_registry``. Используется в ``ApplicationContext._auto_register_skills`` (runtime старт gateway) и в standalone-утилитах (``tools/build_vectors.py``). Контракт декларации skill'а
- static importers (4): `lib/core/application_context.py`, `tests/test_auto_register_skills.py`, `workspace/skills/audit_analyzer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/cli.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_resources_for_skill` | 31-60 | `(skill_cfg: dict) -> list` | 17 | 1 | Построить список ресурсов для одного skill'а из его секции ``project.json``. Дедупликация: если ``name`` встре |
| `register_skill_from_config` | 63-98 | `(skill_name: str, cfg: dict, registry=None) -> SkillRegistration | None` | 8 | 4 | Зарегистрировать skill в ``table_registry`` из его ``project.json``-секции. ``enabled=False`` → skill пропуска |

## `lib/services/vector_index_service.py` — 71 LOC (code 49)
- module: `lib.services.vector_index_service`
- docstring: Единый сервисный слой работы с векторными индексами. Собирает в одном месте операции build-слоя: * создание эмбеддинга (Ollama /api/embed) — ``get_embedding`` (re-export из ``lib/services/cache_provider_impl`` — единая ф
- static importers (1): `tools/build_vectors.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `VectorIndexBuildService` — lines 41-71 (31 LOC), 2 methods
- bases: object
- decorators: —
- docstring: Build-слой над общим провайдером кэша. Держит ОДИН экземпляр ``CacheProvider`` (не создаёт новый на каждый вызов), поэтому кэш индексов переиспользуется между операциями. FAISS собирается провайдером (``preload_indexes``
- name referenced in 0 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 60-66 | `(self, cfg: dict[str, Any] | None=None, base_dir: str='') -> None` | 2 | 0 | 6 | — |
| `provider` | 69-71 | `(self) -> Any` | 1 | 0 | 4 | Общий ``CacheProvider`` — для чтения/поиска. |

## `lib/core/infra_registration.py` — 54 LOC (code 39)
- module: `lib.core.infra_registration`
- docstring: Регистрация инфраструктурных ресурсов в ``TableRegistry``. Единая точка для runtime (``ApplicationContext``) и standalone-утилит (``tools/build_vectors.py``). Читает конфиг из ``gateway.vector.index.*`` и регистрирует ин
- static importers (6): `lib/core/application_context.py`, `tests/test_infra_registration.py`, `tests/test_project_settings.py`, `tools/build_vectors.py`, `workspace/skills/audit_analyzer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/cli.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_settings` | 21-24 | `() -> dict[str, Any]` | 1 | 12 | — |
| `register_vector_storage` | 27-54 | `() -> bool` | 14 | 6 | Зарегистрировать ``vector.storage`` (PG-таблица-хранилище эмбеддингов). Источник — ``project.json::gateway.vec |

