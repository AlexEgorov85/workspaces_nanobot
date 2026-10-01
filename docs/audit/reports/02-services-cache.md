# Аудит: 02 — services/cache (подсистема «Кэш»)

## Сводка группы

Файлов: 10 · LOC: 3761 (включая 325 строк `skill_config.py`, из которых в scope ~120) · классов: 16 · методов: 101 · функций уровня модуля: 39 (+14 вложенных).

Подсистема: один файл кэша DuckDB, один интерфейс (`CacheProvider`/`CacheIngestion`/`CacheStore`), одна реализация (`DuckDbCacheStore`), единственный писатель (`CacheLoadService`), прогрев FAISS в память (`PreloadService`), декларация ресурсов (`TableRegistry` + два регистратора), build-слой (`vector_index_service.py`).

**Ключевые находки**

- `lib/services/duckdb_cache_store.py:1326` — `search_vector(index_path=...)` принимает параметр, который **нигде не используется** в теле метода. Параметр присутствует и в ABC (`cache_provider.py:226`), и во всех вызывающих (`cli.py:487`, `tools/build_vectors.py`, `preload_service.py`). Остаток модели persisted-FAISS-файлов на диске (удалённая `agent_vector_index_store`). **Вердикт: Упростить** — убрать из ABC + реализации + 3 вызова.
- `lib/services/duckdb_cache_store.py:137-196, 608` — `UnsupportedFilesystemError` + `_reject_unsupported_filesystem()` живут в concrete-модуле, но объявлены в докстринге `open_cache_provider` (`cache_provider.py:390`, `duckdb_cache_store.py:605`) как часть контракта фабрики. Фабрика их не ловит и не реэкспортирует; ни один production-вызывающий не обрабатывает этот тип — падение уходит наружу сырым `RuntimeError` мимо `CacheOpenError`. **Вердикт: Упростить** (реэкспортировать в `cache_provider` как часть контракта ИЛИ убрать из `Raises`-докстрингов фабрики).
- `lib/services/cache_provider.py:326-334` vs `duckdb_cache_store.py:686` — **сигнатуры `ensure_schema` не совпадают**: ABC объявляет `(table, records, schema_meta=None)`, реализация — `(table, columns)`. Единственный вызывающий (`cache_load_service.py:307`) передаёт `columns` — то есть работает по неявной позиционной подстановке, а третий параметр `schema_meta` не принимается вообще. Скрытый контракт: любая реализация, написанная по ABC, упадёт. **Вердикт: Упростить** — привести ABC к `(table, columns)`; `schema_meta` удалён (см. §findings о `_capture_schema_meta`).
- `lib/services/duckdb_cache_store.py:1134-1166` — `execute_readonly()` не входит в `CacheProvider`/`CacheIngestion`, production-вызывающих нет (только 4 теста), а его докстринг (`:1141`) утверждает «Используется `CacheProvider.execute_readonly`» — метода в ABC не существует. Строка лжёт. **Вердикт: Удалить** (или добавить в ABC, если он действительно нужен skill'у — но skill его не зовёт, `cli.py` использует `query_sql`).
- `lib/services/vector_index_service.py:41-71` — `VectorIndexBuildService` в списке мёртвых символов. Проверено вручную: production-вызывающих **ноль** (ни gateway, ни skills, ни benchmarks), `tools/build_vectors.py:78` импортирует только re-export `get_embedding` (строка 78), а не класс. Класс — обёртка без потребителя. **Вердикт: Удалить класс, оставить модуль как re-export-шим** (`get_embedding`), который действительно используется standalone-утилитой.
- `lib/services/duckdb_cache_store.py:1391-1401` — `SearchResult` создаётся **без** `signature_status`/`signature_reason`, хотя поля объявлены в `cache_provider.py:64-65`, а `cli.py:525-529` читает их через `getattr` и строит по ним warning STALE/INVALID. Ветка недостижима: `search_vector` никогда их не заполняет. **Вердикт: Упростить** — удалить поля из `SearchResult` + мёртвый warning-блок из `cli.py` (кросс-подсистемная находка), либо достроить вычисление статуса.
- `lib/services/preload_service.py:151-158` — ветка `stale` в `compute_index_health` читает `it.get("signature_status")` из `loaded_items`, но `duckdb_cache_store.py:1262` кладёт в `loaded` только `{"index_name", "vectors"}`. Значит `stale` **всегда пуст** в рантайме; `divergence` никогда не поднимается по этой причине. Докстринг `:122-125` обещает `signature_status` в `loaded`. **Вердикт: Упростить** — либо заполнять статус в `preload_indexes`, либо убрать `stale` из summary.
- `lib/services/cache_provider.py:68-82` + `duckdb_cache_store.py` — `IndexIntegrityError` **никогда не поднимается** в prod-коде (0 `raise`), но `cli.py:493` ловит его, а тест `test_cache_provider_mode.py:434-435` проверяет только `is not None`. Мёртвый тип исключения с живым обработчиком. **Вердикт: Упростить** — удалить класс + except-блок + тест-«проверку непустоты».
- `lib/services/cache_provider_impl.py:392-476` — `_capture_schema_meta()`: production-вызывающих **ноль**, 11 упоминаний только в `tests/test_cache_provider_meta.py`. Дублирует `DuckDbCacheStore._ensure_meta_table`/`_save_schema_meta` (те же константы `_META_SCHEMA="__nanobot_meta"`, `_META_TABLE="__schema_meta"` продублированы в обоих файлах: `cache_provider_impl.py:256-257` и `duckdb_cache_store.py:256-258`). **Вердикт: Удалить** (вместе с дублем констант и тремя тестами, переносимыми на `DuckDbCacheStore._save_schema_meta`).
- `lib/services/table_registry.py:327-333` — `snapshot_path()` возвращает legacy `<workspace>/data_store/duckdb/cache.duckdb`, который `resolve_cache_path` (`application_context.py:1259-1262`) объявляет **неподдерживаемым**. Production-вызывающих нет (упоминания — только в комментарии `application_context.py:1272`, в `tools/release_v252.py:46` как историческая справка и в тестах). Метод actively вредный: возвращает путь, на котором кэш работать не будет. **Вердикт: Удалить.**
- `lib/services/table_registry.py:300-314` — `TableRegistry.tracking_column_for()` содержит логический баг: цикл по регистрациям возвращает `tc` от **первой** enabled-регистрации, для которой `table in self.table_names() or table in self.vector_names()` — то есть track-колонка чужой таблицы может быть отдана для несвязанной регистрации. Production-вызывающих нет (загрузчик идёт через `skill_for_table()` → `SkillRegistration.tracking_column_for`). **Вердикт: Удалить** (агрегатор, который никто не зовёт и который неверен).
- `lib/core/skill_config.py:205-271, 303-325` — пять функций мертвы в prod: `get_in_memory_cache_path` (0 вызовов), `get_vector_index_path` (0), `get_vector_indexes` (0), `get_embedding_config` (только docstring), `get_embedding_model` (только вызывает мёртвую `get_embedding_config`). `get_vector_db_table` — 3 вызова, все в тестах. `load_db_config` (`:104`), `get_tool_config` (`:114`) — по 1 упоминанию (само объявление). Итого 8 из 21 функции модуля не имеют prod-потребителя. **Вердикт: Удалить** (см. таблицу).
- `lib/core/application_context.py:1615` — `_INFRA_KEY_VECTOR_STORAGE = "vector_index.storage"` — мёртвая константа **с неверным значением**: реальный ключ — `"vector.storage"` (`infra_registration.py:18`). Если её «оживить», `get_infra()` вернёт пустой tuple. Единственное упоминание — сама строка. **Вердикт: Удалить.**
- `project.json` — секции `gateway.cache` **нет** (проверено: `git grep '"cache"'` даёт только `media_cache_dir` и комментарии). Значит `resolve_cache_path` всегда идёт по default `~/.cache/nanobot/duckdb/`, а документированный в `AGENTS.md`/docstring'ах override `gateway.cache.local_path` не имеет ни одной живой настройки — код читает несуществующий ключ. Мёртвых настроек в конфиге нет, но и живого override тоже. **Вердикт: Оставить код, отметить рассинхрон документации.**
- Инвариант «`CacheLoadService` — единственный писатель» **подтверждён**: `replace_records`/`ensure_schema` в prod зовутся только из `cache_load_service.py:239,307`; `upsert_records` в prod не зовётся ниоткуда (только тесты + `benchmarks/README.md`). К PostgreSQL в подсистеме ходит только `CacheLoadService` (`_db_run`/`_fetch_all`/`_fetch_schema`) и мёртвая `_capture_schema_meta`. **Подтверждено, замечаний нет.**
- Read-путь **дисциплинирован**: единственная точка открытия файла — `_open_locked` (`duckdb_cache_store.py:490-522`), вызывается из `connect()` (READ_ONLY — открыть и сразу закрыть, `:429`) и из `_read_conn` (`:467`). Второго `duckdb.connect` в prod-коде нет (проверено по всему репозиторию). **Подтверждено.**
- `CacheAccessMode` определён **ровно в одном месте** (`cache_provider.py:29-44`). Дублирующего определения нет ни в `duckdb_cache_store.py`, ни где-либо ещё. Проверено отдельно, т.к. риск молча сломать READ_ONLY-защиту реален. **Подтверждено.**
- Остатки старой модели синхронизации в докстрингах (change `drop-local-cache-read-from-pg` удалил `cache_ownership.py` и `pg_duckdb_sync_service.py`): `duckdb_cache_store.py:5, 378, 1141`; `cache_provider_impl.py:142, 155`; `preload_service.py:179, 201-203`; `cache_provider.py:202`. **Вердикт: Упростить** — переписать на «`CacheLoadService`».

**Вердикты (по таблицам символов):** Оставить 83 · Упростить 17 · Удалить 23 · Слить 1 · Перенести 0. Символов без вердикта: 0, `НЕ РАЗОБРАНО`: 0.

---

## `lib/services/cache_provider.py` — 440 LOC

**Назначение.** Слой интерфейса кэша: ABC двух ролей (чтение/запись), DTO результата, типы исключений и единственная фабрика `open_cache_provider`.
**Что делает.** Объявляет 7 абстрактных read-методов и 3 абстрактных write-метода; фабрика читает `gateway.cache`/`gateway.vector.index.*`/`SETTINGS`, зовёт `resolve_cache_path`, `DuckDbCacheStore.open(...).configure(...).connect()` и бросает `CacheOpenError` при неудаче. Соединение с БД не держит.
**Зачем нужен.** Единственная точка, через которую весь runtime (composition root, gateway, skills, standalone-утилиты) получает доступ к файлу кэша; именно этот модуль запрещает называть конкретный класс хранилища.
**Вердикт.** Оставить.
**Обоснование.** Модель «один интерфейс — одна реализация» выдержана: фабрика инстанцирует concrete-класс, но наружу отдаёт `CacheStore`. Проблемы — не в структуре, а в трёх деталях контракта (`ensure_schema`, `get_stats`/`preload_errors` вне ABC, `index_path`), которые разобраны ниже по символам.
**Доказательства.** Импортируют: `application_context.py`, `skill_config.py`, `duckdb_cache_store.py`, `cache_provider_impl.py`, `preload_service.py`, `tools/build_vectors.py`, `workspace/skills/audit_analyzer/scripts/cli.py` + 9 тестов. Guard: `tests/test_single_cache_interface.py` (16 кейсов), `tests/test_cache_provider_mode.py` (41).

#### class `CacheAccessMode` (29–44, Enum)
Значения `READ_WRITE`/`READ_ONLY` — единственное определение режима доступа во всём репозитории. **Оставить.** Проверено: дубликата нет; `duckdb_cache_store.py:390, 422, 498-499` импортирует, не переопределяет.

#### class `SearchResult` (48–65, dataclass)
DTO одного группированного результата поиска. **Упростить** — поля `signature_status` (64) и `signature_reason` (65) не заполняются ни одним продюсером: `duckdb_cache_store.py:1391-1400` их не передаёт, а `signature-механика` удалена (`remove-vector-index-store`). При удалении нужно поправить `cli.py:525-529` (кросс-подсистемно) и `openspec/specs/data/cache-provider/spec.md:64`.

#### class `IndexIntegrityError` (68–82, Exception)
**Удалить.** 0 `raise` во всём репозитории; единственные потребители — `except` в `cli.py:493` и тест `test_cache_provider_mode.py:434-435`, который проверяет лишь `is not None`. Удаление: снять класс, `except`-блок `cli.py:493-502`, тест-ассерт; поправить докстринг `duckdb_cache_store.py:1141`.

#### class `UnsupportedSqlError` (85–98, Exception)
Поднимается `_classify_sql` при DDL; ловится в `query_sql`. **Оставить.** 6 файлов-ссылок, покрыт `test_duckdb_cache_store.py`, `test_single_cache_interface.py`.

#### class `ReadOnlyAssertionError` (101–116, Exception)
READ_ONLY-guard: DML при `mode=READ_ONLY`. Ядро защиты, заявленной в `AGENTS.md`. **Оставить.**

#### class `CacheBusyError` (119–146, Exception)
Файл кэша держит другой процесс. Строится в `_classify_open_error` (`duckdb_cache_store.py:543`), ловится в `connect()` (`:432`) — то есть **не подавляется**. **Оставить.**

#### class `CacheOpenError` (149–173, ConfigurationError)
Провал открытия без busy-причины; поднимается в `open_cache_provider` (`:436`). **Оставить.**

#### class `CacheProvider` (176–273, ABC, 7 методов)
Роль чтения. **Оставить.** 8 структурно идентичных абстрактных заглушек (`0f36065bca909ed1` в `duplicates.md`) — норма для ABC, ни одна не несёт логики, удалять нечего.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `is_ready` | 210–212 | флаг готовности | гейт «не отдавать наружу неготовый провайдер» | `preload_service.py:217`, `_init_cache_runtime` | Оставить |
| `preload_indexes` | 215–221 | прогрев FAISS в память | стартовый прогрев, чтобы первый запрос не платил сборку | `preload_service.py:220` | Оставить |
| `search_vector` | 226–239 | семантический поиск | единственный вход skill'а в векторный поиск | `cli.py:487` | **Упростить** — убрать `index_path` (не используется в `duckdb_cache_store.py:1322-1402`) |
| `query_sql` | 242–248 | SELECT к кэшу | predefined/generated_sql режимы skill'а | `db_loader.py:116,143`, `generated_sql_mode.py:158` | Оставить |
| `explain` | 251–257 | EXPLAIN без выполнения | валидация SQL до исполнения | `generated_sql_mode.py:245` | Оставить |
| `get_schema` | 260–266 | структура таблиц кэша | описание схемы для LLM | `generated_sql_mode.py:158` | Оставить |
| `close` | 271–273 | освобождение ресурсов | Windows держит файл залоченным без явного close | `application_context.py:669` | Оставить |

#### class `CacheIngestion` (276–334, ABC, 3 метода)
Роль записи, получает только загрузчик. **Оставить.**

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `upsert_records` | 294–311 | долить/обновить дельту | в рантайме не используется (см. `test_cache_load_service.py:221-226`), остаётся примитивом для фикстур | только тесты | **Упростить** — оставить метод (задокументирован в `AGENTS.md` как примитив), но убрать упоминание `PgDuckDbSyncService` из докстринга `:283` |
| `replace_records` | 314–324 | полная перезапись таблицы | единственная операция записи в рантайме | `cache_load_service.py:239` | Оставить |
| `ensure_schema` | 327–334 | DDL под набор колонок | создаёт таблицы с PG-типами, включая пустые | `cache_load_service.py:307` | **Упростить** — сигнатура `(table, records, schema_meta=None)` не совпадает с реализацией `(table, columns)`; привести ABC к фактической |

#### class `CacheStore` (337–346, `CacheProvider`+`CacheIngestion`)
`class CacheStore(CacheProvider, CacheIngestion):` с одним докстрингом, без тела. **Оставить** — именно этот тип стоит в `ApplicationContext.cache_provider` и в аннотациях `ctx.cache_store`.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `open_cache_provider` | 349–440 | единственная фабрика | единая точка разрешения пути + режима + проверки открытия | `application_context.py:1534, 1558`, `skill_config.py:300`, `vector_index_service.py:66`, `tools/build_vectors.py:443` | **Упростить** — `:390` объявляет `UnsupportedFilesystemError` в `Raises`, но фабрика его не ловит; `:435` читает `provider.get_stats()`, которого нет в ABC |

---

## `lib/services/cache_provider_impl.py` — 476 LOC

**Назначение.** Общие помощники слоя кэша (не реализация): чтение конфигов, эмбеддинги, чтение runtime-состояния векторных индексов.
**Что делает.** Побочные эффекты: (1) `sys.path.insert` на импорте модуля (`:247-250`) — мутация глобального состояния; (2) импорт `httpx` и сетевой вызов к Ollama в `get_embedding`; (3) `retry_on_exception` с `base_delay=1.0, max_delay=16.0`.
**Зачем нужен.** Выносит конфиг-чтение и эмбеддинги из concrete-класса, чтобы `duckdb_cache_store.py` не зависел от `SETTINGS`/`httpx` напрямую.
**Вердикт.** Упростить.
**Обоснование.** Три из девяти функций не имеют prod-потребителя, две из них тянут за собой дубль констант мета-таблицы и устаревший `sys.path`-хак. Остальное — живой код, покрытый тестами.
**Доказательства.** Импортируют: `duckdb_cache_store.py`, `cache_provider.py`, `preload_service.py`, `vector_index_service.py`, `tools/build_vectors.py`, `tools/check_indexes.py`, `skill_config.py`, `skill_registration.py`. Тесты: `test_cache_provider_meta.py` (11), `test_get_embedding_auth.py` (5), `test_check_indexes.py` (3), `test_cache_provider_mode.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `compute_index_signature` | 77–95 | подпись конфига индекса | сверка build/verify в `tools/check_indexes.py` | `verify_index_signature:127`, `check_indexes.py:122` | Оставить (prod-вызова нет, но используется dev-утилитой) |
| `verify_index_signature` | 98–128 | сверка сохранённой подписи с текущей | тот же declared-vs-runtime diff | `tools/check_indexes.py:114` | Оставить с оговоркой: прод-путь проверки подписи не существует (см. `IndexIntegrityError`) |
| `list_runtime_vector_indexes` | 131–228 | `SELECT source, COUNT(*)` из storage-таблицы через чужой `provider.query_sql` | orphan-детекция в health-summary; файл кэша сам не открывает | `preload_service.py:261` | Оставить; `:220-226` кладёт 4永远-None поля (`dimension`, `updated_at`, `metric`, `signature`) — **Упростить**, оставить `source`+`vector_count` |
| `_is_missing_relation` | 231–244 | распознавание «таблицы нет» по тексту ошибки | отличает «индексов нет» от «кэш недоступен» | `list_runtime_vector_indexes:200` | Оставить |
| `get_embedding` | 265–314 | HTTP-вызов к Ollama `/api/embed` с retry | единственный источник векторов | `duckdb_cache_store.py:1356`, `tools/build_vectors.py:78`, `vector_index_service.py:32` (re-export) | Оставить |
| `read_embedding_config` | 317–333 | параметры эмбеддера из констант | единственная точка чтения для build/verify | `cache_provider.py:419`, `skill_config.py:319` | Оставить; докстринг `:320-325` упоминает удалённые `_read_current_index_config` / `DuckDbCacheStore._check_index_integrity` — **переписать** |
| `read_embedding_defaults` | 336–347 | дефолты чанкинга | build-сторона `tools/build_vectors.py:958` | `tools/build_vectors.py:958` | Оставить (standalone-потребитель) |
| `read_vector_index_config` | 350–388 | разбор `gateway.vector.index.indexes` | единственный источник декларации индексов | `preload_service.py:255`, `cli.py:361`, `tools/build_vectors.py` | Оставить |
| `_capture_schema_meta` | 392–476 | снятие комментариев/PG-типов из PG в `__nanobot_meta.__schema_meta` | заменён `DuckDbCacheStore._save_schema_meta` | **только тесты** (`test_cache_provider_meta.py`, 11 упоминаний) | **Удалить** |

Также к удалению: `sys.path`-хак `:247-250` (после удаления `_capture_schema_meta` модуль не делает `from utils.db import ...` нигде — проверено), неиспользуемый импорт `CacheProvider, SearchResult` (`:252` — оба имени не встречаются в теле модуля), дубль констант `_META_SCHEMA`/`_META_TABLE` (`:256-257` против `duckdb_cache_store.py:256-258`).

---

## `lib/services/duckdb_cache_store.py` — 1455 LOC

**Назначение.** Единственная concrete-реализация обоих ABC: DuckDB-файл кэша + FAISS-индексы в памяти.
**Что делает.** Держит `_conn` под `RLock`; в `READ_WRITE` соединение персистентно (владеет загрузчик), в `READ_ONLY` открывается на операцию и закрывается сразу. Пишет метаданные схемы в `__nanobot_meta.__schema_meta`, строит FAISS из `vector_db_table`, кэширует индексы в `_index_cache`, сбрасывает их через `_dirty_sources` после записи в storage-таблицу. Пишет в stderr через `print(...)` (`:712`, `:448` в impl) и события в `agent_gateway_logs` через `try_log_event`.
**Зачем нужен.** Собственно кэш. Без него `CacheProvider` нечем реализовать.
**Вердикт.** Оставить.
**Обоснование.** Модель «файл не удерживается между операциями» выдержана и покрыта guard-тестом `test_cache_no_file_hold.py`. Замечания — точечные (5 символов), не архитектурные.
**Доказательства.** Импортируют: `cache_provider.py:397` (фабрика), `application_context.py` (через фабрику). Тесты: `test_duckdb_cache_store.py` (12), `test_cache_provider_mode.py` (41), `test_single_cache_interface.py` (16), `test_cache_no_file_hold.py`, `test_cache_provider_open_failure.py` (9), `test_remove_vector_index_store_guards.py`, `test_vector_search_silent_failure.py`.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_split_table` | 60–65 | `"schema.table"` → кортеж | единый разбор имени во всех запросах | `:1188, 1283, 1428` | Оставить; дубль — `cache_load_service.py:433-438` (**Слить** с одним из них) |
| `_infer_duckdb_type` | 68–81 | вывод DuckDB-типа по значениям | fallback, когда PG-схемы нет | `_ingest_arrow` | Оставить |
| `_records_to_arrow` | 84–125 | dict → Arrow-таблица | быстрый ingest через pandas/Arrow | `_ingest_arrow` | Оставить |
| `_safe_str` | 128–134 | приведение к строке без `None` | безопасная сериализация в Arrow | `_records_to_arrow` | Оставить |
| `_reject_unsupported_filesystem` | 149–196 | fail-fast на NFS/SMB/CIFS через `/proc/mounts` | эмпирически подтверждённый «PID 0» на NFS | `open():608` | **Упростить** — Linux-only; на Windows/macOS путь не проверяется, а тип исключения не входит в контракт `cache_provider` (декларируется в `:390`) |
| `_classify_sql` | 216–252 | head-ключевой классификатор SELECT/DML/DDL | 1-й уровень защиты перед `conn.execute` | `_assert_query_sql_allowed_locked` | Оставить; дублирует по смыслу `lib/utils/sql_safety.py` (sqlglot AST-политика) — **Упростить** после проверки покрытия |
| `_extract_lock_holder` | 295–304 | PID держателя из текста ошибки DuckDB | диагностика `CacheBusyError` | `_classify_open_error:545` | Оставить |
| `_map_pg_type` | 307–336 | PG-тип → DuckDB-тип | честные типы в снимке вместо вывода из CSV | `_ensure_schema_locked`, `_ingest_arrow` | Оставить |

#### class `UnsupportedFilesystemError` (137–146, RuntimeError)
**Упростить.** Объявлен в concrete-модуле, но описан в `Raises` фабрики (`cache_provider.py:390`) и `open()` (`:605`). Либо реэкспортировать в `cache_provider` рядом с остальными исключениями, либо убрать из докстрингов фабрики — сейчас статус «часть контракта» декларируется, но не обеспечен.

#### class `DuckDbCacheStore` (339–1455, `CacheStore`, 29 методов)
**Оставить.** Реализует оба ABC; write-путь доступен только через `CacheIngestion`-типизацию на стороне composition root.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 356–405 | инициализация состояния | конструктор | `open():609` | **Упростить** — legacy-путь (`_mode=None` → RW по умолчанию, `:386-389`, `:496-501`) сохраняется «для back-compat», но ни один prod-вызывающий не конструирует класс напрямую (проверено: 0 в `lib/`, `workspace/`, `gateway.py`) |
| `connect` | 411–439 | открыть + в `READ_ONLY` сразу закрыть | проверка доступности файла при старте | `open_cache_provider:431` | Оставить; докстринг `:423-428` содержит **искажённый текст** («между операциями ми есть некому, кому подхватно делать… но самфайл») — переписать |
| `_read_conn` | 442–474 | contextmanager: открыть на операцию, закрыть сразу | инвариант «файл не удерживается» | все read-методы | Оставить |
| `_close_locked` | 476–488 | закрыть соединение, если открыто по себе | парный к `_read_conn` | `_read_conn:474`, `connect:429`, `close:620` | Оставить; докстринг `:479-480` бессмысленен («Включается после операции чтения и перед передачей егё владельцу») — переписать |
| `_open_locked` | 490–522 | единственный `duckdb.connect` во всём prod-коде | централизация открытия файла | `connect:421`, `_read_conn:467`, write-методы | Оставить |
| `_classify_open_error` | 525–548 | текст ошибки → `CacheBusyError` | busy подаётся, остальное — как есть | `_open_locked:513` | Оставить |
| `configure` | 550–582 | настройка до первого `connect` | composition root не пишет приватные поля | `open_cache_provider:422` | Оставить; `:571-575` кидает `RuntimeError` (не доменное исключение) |
| `open` | 585–611 | classmethod-фабрика | единственный путь создания с режимом | `cache_provider.py:421` | Оставить |
| `is_ready` | 613–614 | флаг готовности | гейт `_read_conn` | `preload_service.py:217` | Оставить |
| `close` | 616–626 | закрыть, сбросить индексы и флаги | Windows: без close файл остаётся залоченным | `application_context.py:669, 1552` | Оставить |
| `__enter__` | 628–629 | context-manager вход | — | **0 вызовов в prod** (только `subprocess_manager`, `workspace/utils/db.py` имеют свои) | **Удалить** вместе с `__exit__` |
| `__exit__` | 631–632 | context-manager выход | — | 0 вызовов | **Удалить** |
| `upsert_records` | 638–684 | upsert по `key_column` | примитив хранилища | только тесты | **Упростить** — в рантайме не вызывается; докстринг `:645` упоминает `PgDuckDbSyncService.key_column_for` (модуль удалён) |
| `ensure_schema` | 686–713 | DDL по описаниям колонок | создаёт таблицы с PG-типами, включая пустые | `cache_load_service.py:307` | Оставить; реализация правильная — расходится только ABC |
| `_ensure_schema_locked` | 715–751 | CREATE/ALTER под локом | внутреннее | `ensure_schema:707`, `_replace_locked:927` | Оставить |
| `replace_records` | 753–775 | полная перезапись | единственная запись в рантайме | `cache_load_service.py:239` | Оставить |
| `_replace_locked` | 777–834 | DROP + пересоздание | реализация `replace_records` | `replace_records:769` | Оставить; `:827` `conn.unregister(...)` — duckdb-API, не покрыт guard'ом на upgrade |
| `_ensure_meta_table` | 838–845 | CREATE мета-таблицы | подвал `_save_schema_meta` | `_save_schema_meta:849`, `_load_schema_meta:876` | Оставить |
| `_save_schema_meta` | 847–868 | запись комментариев/типов | LLM-схема видит комментарии PG | `_ensure_schema_locked:751` | Оставить; **покрывает функциональность** мёртвой `_capture_schema_meta` |
| `_load_schema_meta` | 870–886 | чтение мета-таблицы | подмешивается в `get_schema` | `get_schema:1082` | Оставить |
| `_upsert_locked` | 888–989 | upsert/merge под локом | ядро write-пути | `upsert_records:675`, `_replace_locked:794` | Оставить |
| `_ingest_arrow` | 991–1046 | Arrow-insert | быстрый путь записи | `_upsert_locked:933, 969, 982` | Оставить |
| `_mark_vector_sources_dirty` | 1048–1064 | сброс FAISS-кэша для затронутых source | корректность поиска после перезаписи storage | `upsert_records:678`, `replace_records:769` | Оставить |
| `get_schema` | 1070–1082 | описание схемы кэша | LLM-схема для skill'а | `generated_sql_mode.py:158` | Оставить |
| `query_sql` | 1084–1095 | SELECT с DDL/DML-валидацией | predefined и generated_sql режимы | `db_loader.py:116,143`, `list_runtime_vector_indexes:196` | Оставить |
| `_assert_query_sql_allowed_locked` | 1097–1124 | 2-й уровень защиты | DDL → `UnsupportedSqlError`; DML при RO → `ReadOnlyAssertionError` | `query_sql` | Оставить |
| `explain` | 1126–1132 | EXPLAIN | валидация SQL | `generated_sql_mode.py:245` | Оставить |
| `execute_readonly` | 1134–1166 | произвольный read-only SQL | — | **только тесты** (4 вызова) | **Удалить**; докстринг `:1141` лжёт про `CacheProvider.execute_readonly` |
| `preload_indexes` | 1172–1263 | построить FAISS по всем source | стартовый прогрев | `preload_service.py:220` | **Упростить** — `:1262` не кладёт `signature_status`, из-за чего ветка `stale` в `preload_service.py:151-158` мертва; либо добавить статус, либо убрать ветку |
| `preload_errors` | 1265–1272 | ошибки последнего прогона | вывод в консоль gateway | `gateway.py:232` | **Упростить** — метод **не входит ни в один ABC**, а `gateway.py` типизирует `cache_store` как `CacheStore`; guard `test_single_cache_interface.py` его не ловит, потому что `gateway.py` не в `_WIRING_SITES`. Либо добавить в `CacheProvider`, либо печатать из `PreloadService` |
| `_load_source_index` | 1274–1320 | собрать FAISS по одному source | ленивая сборка индекса | `preload_indexes:1229`, `search_vector:1344` | Оставить; обращается к `self._conn` напрямую (`:1287`) — работает только внутри `with _read_conn()` (вызывающий в `search_vector` уже вышел из блока к `:1356`, где `self._conn == None` → **скрытый контракт**, хрупко) |
| `search_vector` | 1322–1402 | эмбеддинг запроса + FAISS + гидратация строк | единственный вход skill'а в поиск | `cli.py:487` | **Упростить** — убрать неиспользуемый `index_path`; не заполнять `signature_status`/`signature_reason` |
| `get_stats` | 1408–1455 | снимок состояния (таблицы, индексы, счётчики) | диагностика на старте | `gateway.py:212`, `cache_provider.py:435` | **Упростить** — **не входит ни в один ABC**, оба вызова идут через `CacheStore`-типизацию; тот же разрыв контракта, что у `preload_errors` |

---

## `lib/services/cache_load_service.py` — 472 LOC

**Назначение.** Разовая синхронная загрузка снимка PG → DuckDB; единственный писатель кэша и единственный, кто ходит в PostgreSQL в этой подсистеме.
**Что делает.** `ThreadPoolExecutor` с числом слотов = `channels.postgres.pool.max_conn`; каждая задача берёт соединение из общего пула `utils.db.run` и отпускает его. Пишет события `cache_load_start` / `cache_load_done` / `cache_load_table` в `agent_gateway_logs`. Не порождает фоновых потоков и не оставляет обращений к PG после возврата.
**Зачем нужен.** Единственный путь, который наполняет кэш данными; без него `DuckDbCacheStore` остаётся пустым.
**Вердикт.** Оставить.
**Обоснование.** Заявленный инвариант «единственный писатель» **подтверждён**: `replace_records`/`ensure_schema` в prod зовутся отсюда и больше нигде. Единственная реальная проблема — локальный дубль `_split_table`.
**Доказательства.** Импортирует: `application_context.py:1532`. Тесты: `test_cache_load_service.py` (9), `test_cache_no_file_hold.py`, `test_application_context_cache_lifecycle.py` (13).

#### class `CacheLoadError` (46–51, RuntimeError)
Ошибка соединения с PG → fail loudly. **Оставить.**

#### class `CacheLoadResult` (55–80, dataclass)
Снимок результата загрузки; `loaded_at` (property, 78–80) — время актуальности. **Оставить.**

#### class `CacheLoadService` (83–472, 12 методов)
**Оставить.**

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 104–123 | состояние загрузчика | DI из `_init_cache_runtime:1538` | `application_context.py:1538` | Оставить |
| `load` | 129–212 | оркестрация: пул потоков, прогресс, события | точка входа загрузки | `application_context.py:1550` | Оставить |
| `get_stats` | 214–225 | метрики последней загрузки | — | **только тесты** (`test_cache_load_service.py:252-253`) | **Упростить** — удалить метод и `_result` (`:123, 141, 216`), либо оставить как наблюдаемость; prod-потребителя нет |
| `_effective_workers` | 231–232 | `min(len(tables), max_conn)` | не дать потокам драться за слоты пула | `load:176` | Оставить |
| `_load_one` | 234–300 | загрузка одной таблицы | рабочая единица пула | `load:172, 178` | Оставить |
| `_ensure_table_schema` | 302–307 | `ensure_schema` по описаниям колонок | типы до загрузки | `_load_one` | Оставить |
| `_log_sync_event` | 309–339 | событие в `agent_gateway_logs` | наблюдаемость загрузки | `load`, `_load_one` | Оставить |
| `_track_column_for` | 341–368 | track-колонка из реестра, с кэшем | прогресс/актуальность снимка | `_fetch_all:465` | Оставить; `try/except Exception: pass` (`:364-365`) глушит ошибки реестра без лога |
| `_fetch_schema` | 370–431 | описание колонок из `information_schema` + `pgd.description` | комментарии и типы в снимке | `_ensure_table_schema:303` | Оставить |
| `_split_table` | 433–438 | разбор `"schema.table"` | квалификация в SQL | `_fetch_schema:372`, `_fetch_all:459`, `_fq_table:441` | **Слить с `duckdb_cache_store._split_table`** (тот же алгоритм в двух файлах) |
| `_fq_table` | 440–444 | `"schema"."name"` | безопасная квалификация | `_fetch_all:458` | Оставить |
| `_db_run` | 446–452 | `utils.db.run(fn)` — слот пула | единый пул PG | `_fetch_all:464` | Оставить |
| `_fetch_all` | 454–467 | `SELECT *` + max track-значение | данные + метка актуальности | `_load_one` | Оставить |
| `_max_track` | 470–472 | максимум по track-колонке | `loaded_at`/статистика | `_fetch_all:466` | Оставить |

---

## `lib/services/table_registry.py` — 347 LOC

**Назначение.** Плагин-реестр ресурсов runtime'а: два namespace'а (skill-ресурсы и инфраструктурные), один singleton `table_registry`.
**Что делает.** Только чтение/запись в памяти, без побочных эффектов. `SkillRegistration`/`TableResource`/`VectorResource` — frozen dataclass'ы с валидацией в `__post_init__`.
**Зачем нужен.** Единственный источник правды «какие таблицы грузить и по какой колонке отслеживать изменения» — для `CacheLoadService`, фабрики провайдера и skill-конфига.
**Вердикт.** Оставить.
**Обоснование.** Двухпространственная модель работает и действительно используется (`resources_by_label` → `skill_config.get_predefined_scripts_table`, `skill_for_table` → `_track_column_for`, `vector_resources` → `_mark_vector_sources_dirty`). Мёртвых методов 7 из 26, один из них (`snapshot_path`) actively вредный.
**Доказательства.** Импортируют 7 prod-файлов: `application_context.py`, `infra_registration.py`, `skill_config.py`, `skill_registration.py`, `cache_load_service.py`, `cache_provider.py`, `duckdb_cache_store.py`. Тесты: `test_table_registry.py` (51), `test_resource_universality.py` (5), `test_infra_registration.py` (20).

#### class `TableResource` (37–63, frozen dataclass)
DTO PG-таблицы: имя, `tracking_column`, `label`. `__post_init__` (58–63) валидирует непустое имя. **Оставить.**

#### class `VectorResource` (67–88, frozen dataclass)
DTO PG-таблицы эмбеддингов; `__post_init__` (83–88) валидирует имя и **квантует `tracking_column` в `"id"`**. **Оставить.**

#### class `SkillRegistration` (97–135, frozen dataclass)
**Оставить.**

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__post_init__` | 110–112 | валидация имени | fail-fast | `register()` | Оставить |
| `table_resources` | 114–116 | DTO-таблицы skill'а | агрегаторы реестра | `table_registry.py:227, 245, 271` | Оставить |
| `vector_resources` | 118–120 | DTO-векторы skill'а | агрегаторы реестра | `table_registry.py:228, 284` | Оставить |
| `tracking_column_for` | 122–135 | track-колонка таблицы/вектора skill'а | дефолт `updated_at` / `id` | `cache_load_service.py:361`, `table_registry.py:305` | Оставить |

#### class `TableRegistry` (139–333, 20 методов)
**Оставить.**

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `register` | 166–172 | регистрация skill'а | декларативный вход | `skill_registration.py:93` | Оставить |
| `register_infra` | 174–193 | регистрация инфра-ресурса | storage-таблица векторов | `infra_registration.py:50` | Оставить |
| `unregister_infra` | 195–197 | удаление по ключу | — | **0 prod-вызовов** (6 в тестах) | **Удалить** |
| `unregister` | 199–201 | удаление skill'а | — | **0 prod-вызовов** | **Удалить** |
| `get` | 203–204 | регистрация по имени | — | **0 prod-вызовов** (только тесты) | **Удалить** |
| `get_infra` | 206–208 | инфра-ресурсы по ключу | идемпотентность регистрации | `infra_registration.py:46` | Оставить |
| `infra_keys` | 210–211 | список infra-ключей | — | **0 prod-вызовов** (3 в тестах) | **Удалить** |
| `names` | 213–214 | имена skill-регистраций | — | **0 prod-вызовов** (6 в тестах) | **Удалить** |
| `enabled_names` | 216–220 | имена включённых skill'ов | — | **0 prod-вызовов** (1 в тесте) | **Удалить** |
| `skill_for_table` | 222–232 | skill-владелец таблицы | track-колонка без core-правок | `cache_load_service.py:359` | Оставить |
| `resources_by_label` | 234–248 | skill-DTO по `label` | поиск реестра скриптов | `skill_config.py:93` | Оставить — **реально используется** |
| `_infra_table_resources` | 250–251 | инфра-DTO таблиц | `table_resources` | внутренний | Оставить |
| `_infra_vector_resources` | 253–254 | инфра-DTO векторов | `vector_resources` | внутренний | Оставить |
| `_iter_infra` | 256–260 | плоский список инфра-ресурсов | 4 вызывающих | внутренний | Оставить |
| `table_resources` | 262–273 | все table-DTO (skills + infra) | загрузка кэша, фабрика | `application_context.py:1478`, `cache_provider.py:413` | Оставить |
| `vector_resources` | 275–286 | все vector-DTO (skills + infra) | загрузка кэша, dirty-source | `application_context.py:1478`, `duckdb_cache_store.py:1057` | Оставить |
| `resources` | 288–290 | всё (4 типа) | gate «реестр пуст» | `application_context.py:1388` | Оставить |
| `table_names` | 292–294 | имена таблиц | список загрузки | `application_context.py:1448`, `cache_provider.py:409` | Оставить |
| `vector_names` | 296–298 | имена vector-таблиц | `vector_db_table` | `application_context.py:1449`, `cache_provider.py:410` | Оставить |
| `tracking_column_for` | 300–314 | агрегатор track-колонок по обоим namespace'ам | документирован в `:152-155` как объединяющий оба | **0 prod-вызовов**; логически неверен (`:302-307` возвращает колонку первой подходящей регистрации для произвольной таблицы) | **Удалить** |
| `clear` | 316–325 | полный сброс singleton'а | повторный `create()` в одном процессе | `application_context.py:269` | Оставить |
| `snapshot_path` | 327–333 | legacy `<workspace>/data_store/duckdb/` | — | **0 prod-вызовов**; путь объявлен неподдерживаемым в `resolve_cache_path` | **Удалить** |

Одно-строчники `_infra_table_resources`/`_infra_vector_resources`/`_iter_infra` между собой не дублируют: это разные фильтры/развёртка одного списка. Дублирующие однострочники — `table_names`/`vector_names` (разные namespace'а) и `resources` (конкатенация двух) — функционально различны, оставить.

---

## `lib/services/vector_index_service.py` — 71 LOC

**Назначение.** Заявленный «единый сервисный слой работы с векторными индексами» (build-слой).
**Что делает.** Re-export `get_embedding` из `cache_provider_impl` (`:32`) + класс-обёртка `VectorIndexBuildService`, который в `__init__` открывает `READ_ONLY`-провайдера. Побочный эффект: `sys.path.insert` на импорте (`:36-38`).
**Зачем нужен.** Исторически — чтобы `tools/build_vectors.py` не заводил свою реализацию эмбеддинга.
**Вердикт.** Упростить.
**Обоснование.** Ключевой вопрос брифа закрыт: **build-слой FAISS в рантайме не вызывается вообще.** `VectorIndexBuildService` — 0 production-вызывающих; `tools/build_vectors.py:78` импортирует из этого модуля **только** `get_embedding` и FAISS сам не строит (индексы в памяти собирает `DuckDbCacheStore.preload_indexes`). Класс — обёртка без потребителя, которую никто не удалил после переноса сборки в провайдер. Оговорка про standalone: даже standalone-утилита его не использует, поэтому «оставить ради tools» неприменимо.
**Доказательства.** `git grep VectorIndexBuildService` → 3 prod-вхождения, все внутри самого файла (`:6` в докстринге, `:41` определение, `:56` в doctest-примере) + 2 в тестах. Импортируется как модуль: только `tools/build_vectors.py:78` (`get_embedding`) и `tests/test_core_infrastructure_independence.py:24` (список запрещённых зависимостей). Покрыт `test_gaps.md` как модуль без тестов.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `VectorIndexBuildService` | 41–71 | обёртка «один провайдер на сервис» | не осталось: сборка переехала в `DuckDbCacheStore` | 0 | **Удалить класс** (вместе с `__init__` 60–66 и `provider` 68–71) |
| `__init__` | 60–66 | открыть `READ_ONLY`-провайдера | — | только из `__init__` класса | **Удалить** |
| `provider` (property) | 68–71 | доступ к провайдеру | — | 0 | **Удалить** |
| `get_embedding` (re-export, `:32`) | — | единая функция эмбеддинга | импорт из `tools/build_vectors.py:78` | `tools/build_vectors.py:78` | **Оставить** — но лучше переключить импорт на `cache_provider_impl` и удалить модуль целиком; как минимум убрать `sys.path`-хак (`:34-38`) |
| `_ROOT`/`_WORKSPACE`/цикл `sys.path` | 34–38 | «чтобы `from utils.db import ...` работал» | модуль не делает ни одного такого импорта | — | **Удалить** (двойник `cache_provider_impl.py:247-250`) |

---

## `lib/services/preload_service.py` — 318 LOC

**Назначение.** Прогрев FAISS-индексов в память при старте + health-summary в stderr и `agent_gateway_logs`.
**Что делает.** `asyncio.to_thread(store.preload_indexes)` (не блокирует loop), затем **всегда** считает health-summary — даже при `loaded is None`; пишет в `sys.stderr` и через `try_log_event` в БД. Все исключения проглатываются с `logger.warning` — по замыслу автора summary не должен валить startup.
**Зачем нужен.** Первый пользовательский vector-запрос не должен платить за сборку всех индексов; оператор получает картину declared-vs-runtime на старте.
**Вердикт.** Оставить.
**Обоснование.** Живой и правильно встроен в gateway. Замечания: недостижимая ветка `stale`, неиспользуемый `settings`, deprecated-helper, и докстринги, описывающие удалённый `PgDuckDbSyncService`.
**Доказательства.** Вызывает `gateway.py:228`, `benchmarks/runner.py:622`. Тесты: `test_preload_service.py` (18), `test_remove_vector_index_store_guards.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_emit_health_event` | 42–76 | сборка `LogEvent` + `try_log_event` | — | **DEPRECATED по собственному докстрингу `:49`**, используется только `tests/test_preload_service.py` (10 упоминаний); production-путь использует инлайн-вызов `:303-318` | **Удалить** вместе с тест-классом (инлайн-блок `:303-318` остаётся) |
| `_format_lines` | 79–109 | текстовый summary | диагностика на старте | `_emit_health_summary:272` | Оставить |
| `compute_index_health` | 112–172 | declared/loaded/missing/orphan/stale | расчёт divergence | `_emit_health_summary:265` | **Упростить** — ветка `stale` (`:151-158`) **недостижима в рантайме**: `preload_indexes` не возвращает `signature_status` (`duckdb_cache_store.py:1262`). Либо заполнять статус, либо убрать `stale` из summary и `divergence` |

#### class `PreloadService` (175–318, 3 метода)
**Оставить.**

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 184–190 | DI (`settings`, `db_logging_service`) | хранение зависимостей | `application_context.py:507` | **Упростить** — `self._settings` (`:189`) **нигде не читается** во всём модуле (проверено); убрать параметр `settings` и поле |
| `preload_vector_indexes` | 192–235 | `to_thread(preload_indexes)` + summary | прогрев | `gateway.py:228`, `benchmarks/runner.py:622` | **Упростить** — докстринг `:201-203` описывает удалённый `PgDuckDbSyncService`, `asyncio.Event` и таймаут 30 с, которых больше нет; `:179-181` — то же в докстринге класса |
| `_emit_health_summary` | 237–318 | stderr + событие в БД | наблюдаемость | `preload_vector_indexes:231` | Оставить; `:303` — локальный импорт `lib.services.db_logging_service` (совпадает с дублирующейся практикой `_emit_health_event`, который удаляется) |

---

## `lib/core/skill_config.py` — 325 LOC (только функции кэша/векторов)

**Назначение.** Runtime-API, параметризованный `skill_name`, для чтения конфига и разрешения ресурсов skill'а.
**Зачем нужен.** Единая точка вместо копипасты `skill_config.py` в каждом skill'е; skill-обёртки (`audit_analyzer/scripts/skill_config.py`, `legal_summarizer/scripts/llm/config.py`) — тонкие делегаты с фиксированным `_SKILL_NAME`.
**Вердикт.** Упростить.
**Обоснование.** Из 21 функции модуля 8 не имеют ни одного prod-потребителя — почти все именно в cache/vector-сегменте, который составлял исходную raison d'être модуля. Оставшиеся (`build_cache_provider`, `get_db_tables`, `get_db_schema`, `get_predefined_scripts_table`, `get_vector_db_table` в тестах) живы.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_vector_indexes_list` | 47–50 | `skills.<n>.vector_indexes[]` | только для `get_vector_index_path` | `get_vector_index_path:242` | **Удалить** вместе с ней |
| `get_in_memory_cache_path` | 205–231 | путь к `cache.duckdb` через `resolve_cache_path` | документированный override `gateway.cache.local_path` | **0 prod-вызовов** (только `tools/release_v252.py:46` как историческая справка) | **Удалить** — путь и так вычисляет `open_cache_provider`; skill получает провайдера через `build_cache_provider` |
| `get_vector_index_path` | 234–251 | `<default_root>/<index_name>` | путь к FAISS-файлу | **0 prod-вызовов**; persisted-FAISS удалён, индекс в памяти | **Удалить** (вместе с `_vector_indexes_list`) |
| `get_vector_db_table` | 254–271 | `storage_table` из конфига | — | **0 prod-вызовов**, 3 в тестах (`test_skill_config_api.py:99,111,118`) | **Упростить** — оставить (де-факто публичный read-only хелпер с тестовым контрактом) либо удалить вместе с тестами; `_tables_list`-fallback мёртв (в `project.json` `storage_table` всегда задан) |
| `build_cache_provider` | 274–300 | делегат в `open_cache_provider` | единая точка создания для skill'а | `audit_analyzer/scripts/skill_config.py:82` → `cli.py:253` | **Оставить** — живой публичный контракт; параметры `skill_name`/`skill_root` игнорируются осознанно (докстринг `:283-286`) |
| `get_vector_indexes` | 303–309 | `read_vector_index_config()` | — | **0 вызовов вообще** | **Удалить** |
| `get_embedding_config` | 312–321 | `read_embedding_config()` | — | **0 вызовов** (только докстринг `:10`) | **Удалить** |
| `get_embedding_model` | 324–325 | `get_embedding_config()["model"]` | — | вызывает только мёртвую `get_embedding_config` | **Удалить** |

Смежный мёртвый код вне cache/vector-сегмента (зафиксировано для полноты, в границы подсистемы не входит): `load_db_config` (104–105) и `get_tool_config` (114–115) — по 1 упоминанию (само объявление), prod-вызывающих нет.

---

## `lib/core/infra_registration.py` — 54 LOC

**Назначение.** Регистрация инфраструктурных ресурсов (единая storage-таблица эмбеддингов) в `TableRegistry` — общая точка для runtime и standalone-утилит.
**Что делает.** Читает `SETTINGS.gateway.vector.index.storage_table`; если задано и содержит `.` и ещё не зарегистрировано — `register_infra("vector.storage", (VectorResource(tracking_column="id"),))`. Идемпотентна.
**Зачем нужен.** Единственный писатель infra-namespace'а реестра; гарантирует, что storage-таблица векторов попадёт в кэш и в `vector_names()`.
**Вердикт.** Оставить.
**Обоснование.** 12 prod-ссылок, покрыт `test_infra_registration.py` (20). Замечание — только к форме: 4-строчный `_settings()` существует ради одной строки.
**Доказательства.** Импортируют: `application_context.py:1631`, `tools/build_vectors.py`, 2 skill-CLI. Тесты: `test_infra_registration.py`, `test_project_settings.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_settings` | 21–24 | `from config import SETTINGS; return SETTINGS` | обёртка ради одного вызова | `register_vector_storage:37` | **Упростить** — инлайнить (одно-строчная обёртка, помечена клоном в `duplicates.md`) |
| `register_vector_storage` | 27–54 | регистрация `VectorResource` для storage-таблицы | идемпотентная регистрация infra-ресурса | `application_context.py:1633`, `tools/build_vectors.py` | **Оставить** |

---

## `lib/core/skill_registration.py` — 98 LOC

**Назначение.** Декларативная регистрация skill'а из `project.json::skills.<name>` в `TableRegistry`.
**Что делает.** `build_resources_for_skill` конструирует `TableResource`/`VectorResource` из `tables[]` (валидация через `TableRegistry` и `project_settings`-схему); `register_skill_from_config` собирает `SkillRegistration` и регистрирует. `SkillRegistration.enabled` уважается всеми агрегаторами реестра.
**Зачем нужен.** Единственное место, где декларация skill'а превращается в runtime-ресурсы; используется и `ApplicationContext._auto_register_skills`, и standalone-утилитами.
**Вердикт.** Оставить.
**Обоснование.** Живой код с 9 prod-ссылками, покрыт `test_auto_register_skills.py` (31) и `test_table_registry.py`.
**Доказательства.** Импортируют: `application_context.py`, `tools/build_vectors.py`. Тесты: `test_auto_register_skills.py` (31), `test_table_registry.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `build_resources_for_skill` | 31–60 | DTO-ресурсы из конфига skill'а | чистая функция, отделена от singleton'а | `register_skill_from_config:87` | Оставить |
| `register_skill_from_config` | 63–98 | собрать `SkillRegistration` + `registry.register` | точка регистрации skill'а | `application_context.py:1612`, `tools/build_vectors.py` | Оставить; докстринг `:11` упоминает `get_vector_index_path()` (функция удаляется) — переписать |

---

## Кросс-подсистемные находки

1. **`gateway.py:212, 232` вызывает у провайдера `get_stats()` и `preload_errors()`, которых нет ни в `CacheProvider`, ни в `CacheIngestion`.** `gateway.py:210` типизирует `cache_store` как `CacheStore`, то есть статически это ошибка, которую никто не ловит. Guard `tests/test_single_cache_interface.py::TestRuntimeUsesDeclaredContract` (строки 363–369) перечисляет wiring-sites `{application_context.py, preload_service.py}` и `_WIRING_SITES_ATTRS {cache_load_service.py: _store}` — **`gateway.py` в списке нет**, поэтому дыра не обнаруживается. Либо расширить guard, либо вынести оба метода в контракт.
2. **`SearchResult.signature_status` / `signature_reason` (`cache_provider.py:64-65`) — поля-призраки.** Продюсер (`duckdb_cache_store.py:1391`) их не заполняет, потребитель (`workspace/skills/audit_analyzer/scripts/cli.py:520-529`) читает через `getattr` и строит warning по пустым значениям. Ветка STALE/INVALID в skill-CLI недостижима. То же для `IndexIntegrityError`: `except` в `cli.py:493` ловит исключение, которое никто не бросает.
3. **`compute_index_health` (`preload_service.py:151-158`) — недостижимая ветка `stale`**, потому что `duckdb_cache_store.py:1262` возвращает `{"index_name", "vectors"}` без `signature_status`. Health-summary на старте **никогда** не показывает расхождение по подписи, хотя `openspec/specs/data/vector-indexes/spec.md:177` и `docs/VECTOR_INDEXES.md:336` это обещают.
4. **Дубль `_split_table`**: `duckdb_cache_store.py:60-65` и `cache_load_service.py:433-438` — один и тот же алгоритм в двух файлах. Кандидат на слияние в `lib/utils/table_utils.py` (там уже живёт `normalize_table_names`).
5. **Дубль `sys.path`-хака**: `cache_provider_impl.py:247-250` и `vector_index_service.py:34-38` делают одно и то же; ни один из модулей не делает `from utils.db import ...` (проверено). Побочный эффект — мутация глобального `sys.path` на импорте.
6. **Дубль констант мета-таблицы**: `_META_SCHEMA`/`_META_TABLE` определены и в `cache_provider_impl.py:256-257`, и в `duckdb_cache_store.py:256-258`, и в `lib/utils/node_access.py`. Три источника одного имени схемы.
7. **`_INFRA_KEY_VECTOR_STORAGE = "vector_index.storage"` (`application_context.py:1615`) — мёртвая константа с неверным значением.** Реальный ключ — `"vector.storage"` (`infra_registration.py:18`). Единственное упоминание — сама строка; при «оживлении» `get_infra()` вернул бы пустой tuple.
8. **Строки, описывающие удалённое поведение** (change `drop-local-cache-read-from-pg`): `duckdb_cache_store.py:5` («из `PgDuckDbSyncService`»), `:378`, `:645`; `cache_provider_impl.py:142`, `:155`; `cache_provider.py:202`; `preload_service.py:179`, `:201-203`; `cache_load_service.py:87` (в порядке «нет, здесь корректно»). Плюс искажённый текст докстрингов `duckdb_cache_store.py:423-428` и `:479-480`.
9. **Кросс-подсистемный дубль `enable`-флага:** `VectorIndexSettings.enable` (`project_settings.py:163`) и per-index `enabled` (`cache_provider_impl.py:386`) объявлены, но в подсистеме кэша **не читаются нигде** — `preload_indexes` строит индексы по всем `source` из storage-таблицы, игнорируя `enabled`. То же для `default_root` (`:164`) — читается только в мёртвой `get_vector_index_path`.
10. **Таблица `agent_gateway_logs` пишется из трёх мест подсистемы кэша** (`cache_load_service._log_sync_event`, `preload_service._emit_health_summary`, `duckdb_cache_store` через `try_log_event`) — единый sink, но каждый собирает `LogEvent` вручную; удаление `_emit_health_event` оставляет два из трёх. Кандидат на общий хелпер.
