# 🗃 База данных, пул соединений и SQL-скрипты

> Навигационный индекс каталога `docs/` — в [`README.md`](README.md). Этот документ —
> самодостаточное описание подсистемы.

## 🔌 Универсальный слой данных lib/services

**Интерфейс** — `lib/services/cache_provider.py`:

- `CacheProvider` (ABC): `is_ready()`, `refresh()`, `check_stale()`,
  `preload_indexes()`, `search_vector()`, `query_sql()`, `explain()`,
  `get_schema()`, `close()`.
- `SearchResult` (dataclass): `content`, `score`, `source`, `table`, `pk_value`,
  `chunk`, `matched_chunks`, `row`.

**Реализация** — `lib/services/cache_provider_impl.py`:

- `PostgresDuckDbProvider` — единый провайдер: DuckDB-кеш + векторные индексы.
  Конструктор принимает только конфигурацию (схему, таблицы, пути, модель
  эмбеддинга) — без привязки к «аудиту».
- Модульные функции: `get_embedding()` (Ollama `/api/embed`),
  `load_cache_from_postgres(cache_path, db_config)`, `check_cache_stale(...)`,
  `read_vector_index_config(cfg)` (конфиг индексов — только из БД,
  `agent_vector_index_config`), `read_embedding_config(cfg)` (через
  `audit_vector_settings()`), `build_cache_provider(cfg, base_dir)` (фабрика
  провайдера из конфиг-секции навыка).
- Тяжёлые зависимости (`duckdb`, `psycopg2`, `faiss`, `numpy`, `pyarrow`, `httpx`)
  импортируются **лениво** внутри методов — импорт модуля остаётся лёгким,
  и gateway может управлять жизненным циклом без побочных эффектов.
- Если передан `dsn` — провайдер сам вызывает `utils.db.configure(dsn)`
  (идемпотентно), поэтому пригоден для использования автономно.

**Единый интерфейс бэкенда запросов.** `Database` (прямой PG) и
`PostgresDuckDbProvider` (кеш) реализуют одинаковые методы
`get_schema / query_sql / explain` (протокол `QueryBackend` в
`scripts/database.py`), поэтому режимы `predefined`/`sql` работают с любым
бэкендом без ветвлений.

**Фабрика провайдера** — универсальная `lib.services.cache_provider_impl.build_cache_provider(cfg, base_dir)`
собирает провайдера из конфиг-секции навыка (DuckDB-кеш, индексы, эмбеддинг).
Навык делегирует ей через `scripts/skill_config.build_cache_provider()`, тот же
набор настроек читает `gateway.py::_build_audit_services()` и индексатор
`tools/build_vectors.py`.

## 🔌 Единый пул соединений PostgreSQL (`workspace/utils/db.py`)

Чтобы на сервере никогда не было десятков параллельных подключений к PG
(проблема «too many connections» решается на уровне архитектуры, а не ретраями),
все подсистемы пользуются **одним общим пулом** соединений:

- **Одна job-очередь + пул воркеров** (по умолчанию `min_conn=1`, `max_conn=4`).
  Каждый воркер — поток, владеющий ровно одним psycopg2-соединением; он берёт
  задачи из общей очереди и выполняет их последовательно.
- **Публичный API** сохраняется: `execute/fetch/fetchone/fetchval`, async-варианты
  через `asyncio.to_thread`, `transaction()/async_transaction()`,
  `configure/resolve_dsn/run/set_pool_config/get_stats/start/shutdown`.
  Дополнительно: `probe_connections(count=None, timeout=None)` — «прогрев» пула:
  отправляет `count` (по умолчанию `min_conn`) лёгких задач, чтобы воркеры
  реально попытались подключиться к БД (ленивые воркеры иначе видны как
  0 живых соединений до первой задачи). Ошибки подключений наружу не бросаются —
  их состояние читается через `get_stats()`.
- **`get_stats()`** дополнительно отдаёт живые счётчики состояния воркеров:
  `connected_workers` (воркеры с живым соединением в этот момент) и
  `failed_workers` (воркеры, чья последняя попытка подключения завершилась
  ошибкой — «запустились с ошибкой»).
- **Активность пула в терминал** — ключ конфига пула `print_activity`
  (флаг подключается из `gateway.print_db_activity` через
  `ApplicationContext` → `_configure_db_pool`). При включении каждый db-воркер
  печатает `[db-worker] ... взял job / ... закончил job (Nms)` и текущий размер
  очереди `[очередь-БД] N` — по образцу активности воркеров задач канала
  `[task-worker]` / `[очередь-задач]` (флаг `gateway.print_worker_activity`).
  Приоритет вывода — rich-консоль, при сбое — обычный `print`. Названия строк
  писан латиницей (`db-worker`), очереди — по-русски; юникодные стрелки
  `←/→` заменяются на ASCII `<-/->` (cp1251-консоль Windows их не кодирует),
  а квадратные скобки меток выводятся через `markup=False` чтобы rich не
  трактовал их как стили.
- **Кто ходит в БД** — каждая задача несёт метку вызывающего (`Job.tag`):
  публичные функции `db.execute/fetch/fetchone/fetchval` (sync/async),
  `db.run`, транзакции begin/end и `probe_connections` помечают себя
  `файл:строка` через `_caller_tag(frames_back=2)`, внутренние
  прокси транзакций — цепочку `файл:строка <- файл:строка …` (4-й кадр + chain
  до 8). Метка печатается в лог активности (`взял job [...путь...] N`) и в
  loguru-строке воркера. По ней видно, какой модуль генерирует запрос (например,
  постоянный поток `nanobot/agent/loop.py` → `list_sessions` — см. §
  «Управление сжатием контекста»). Метка `[unknown]` остаётся только при
  сбое разрешения кадров стека (патологический случай) — в штатном режиме
  каждая задача несёт тег вызывающей стороны.
- **Транзакции — эксклюзивная аренда соединения** (`lease_id`): пока `with
  transaction()` жив, воркер первого вызова выполняет только задачи этой
  транзакции, а свободные воркеры продолжают обслуживать обычные задачи.
  Транзакция работает через прокси-объекты `_ConnectionProxy/_CursorProxy`
  (поддерживают справочные атрибуты psycopg2 — `autocommit`, `closed`,
  `commit()/rollback()/cursor()` и т.п.).
- **Транзакции честно ждут в очереди.** Если все воркеры заняты лизами,
  транзакция НЕ падает с ошибкой через `pool_timeout`, а ждёт освобождения
  воркера (как обычные job-ы). `pool_timeout` — порог диагностического
  warning («no free worker for lease …»), а не лимит ожидания. Если воркер
  был зализован, а `begin`-задача упала (обрыв соединения, полная очередь),
  lease гарантированно возвращается в пул (в `_acquire_lease` — `try/except`).
- **Реконнект с backoff** — внутри воркера: при обрыве соединение закрывается
  и пересоздаётся с экспоненциальной паузой (`reconnect_backoff_sec` →
  `reconnect_backoff_max_sec`); retry-able задачи переподнимаются до
  `job_max_retries`. После `connect_max_retries` неудачных подключений воркер
  сдаётся с ошибкой — сервисы, ждущие `run()`, не блокируются навсегда.
- **Неподключённые воркеры уступают очередь подключённым.** Если часть
  воркеров не смогла установить соединение (например, исчерпан лимит
  `CONNECTION LIMIT` роли или БД недоступна), они не отнимают задачи у живых:
  в `_take_job` такой воркер берёт обычную задачу, только когда в пуле нет ни
  одного воркера с живым соединением. Пока хотя бы один воркер подключён —
  вся очередь обслуживается им, неподключённые не тратят время на
  retry-connect. При полной недоступности БД задачи быстро падают с ошибкой
  подключения, а не висят в очереди вечно. Транзакции (`lease_id != 0`) на
  это правило не влияют — их воркер забирает безусловно.
- **Настройка** — `project.json → channels.postgres.pool`:
  `min_conn`, `max_conn`, `pool_timeout`, `queue_maxsize`,
  `reconnect_backoff_sec`, `reconnect_backoff_max_sec`, `connect_max_retries`,
  `idle_timeout_sec`, `job_max_retries`. `ApplicationContext.create()` читает эту
  секцию и применяет через `set_pool_config()`; `ctx.start()/stop()` вызывают
  `utils.db.start()/shutdown()`.

**Кто ходит в БД через пул:** `DbLoggingService`, `PgDuckDbSyncService`,
`PGSessionManager`, `PostgresChannel`, `session_storage`, `streamlit_app.py`,
инструменты и `cache_provider_impl` (bulk-load снимает соединение пула на всё
время копирования). Ни один сервис-поток не держит собственного psycopg2-
соединения — соединение выдаёт пул на время запроса/транзакции.

**Санитизация NUL-байта.** PostgreSQL не принимает NUL (0x00) в text-литералах,
а psycopg2 не любит литеральные escape `\u0000`..`\u0003` — такой контент
(бинарь из `exec`/`read_file` или LLM-вывод) валит запись сессии
(`A string literal cannot contain NUL (0x00) characters.`). Вычистка идёт на
двух уровнях:
  1. **на источнике** — патч `RuntimePatcher.patch_session_content_cleanup`
     оборачивает `Session.add_message` и чистит контент через канонический
     `workspace/utils/clean_text.py`;
  2. **страховка на границе БД** — `utils.db._sanitize_param` (все параметры
     `execute`/`mogrify`, включая `execute_values`) тоже прогоняет значение через
     `clean_text`, чтобы обойдённая живым патчем строка не упала на записи.

---

## ⚙️ Конфигурация навыка

Секция `skills.audit_analyzer` в `project.json`:

| Ключ | Назначение | Значение / по умолчанию |
|------|-----------|-------------|
| `llm.provider` / `llm.model` / `llm.api_base` | LLM для генерации SQL | `minimax` / `MiniMax-M3` / `https://api.minimax.io/v1` (единая с агентом; любой OpenAI-compatible: Mistral, OpenAI, MiniMax, Ollama, vLLM) |
| `llm.max_tokens` / `llm.temperature` | Параметры генерации | `8192` / `0.1` |
| `db.schema` | Схема по умолчанию для голых имён таблиц | `oarb` |
| `db.tables` | Таблицы, доступные агенту (`str` ИЛИ `{name, label?, tracking_column?}`) | `audit_reports, audits, report_items, violations` (значение project.json; код по умолч. — пустой список) |
| `db.additional_tables` | Таблицы других схем (`[schema, table]` ИЛИ `"schema.table"` ИЛИ `{schema, table}`) | `[["public", "agent_predefined_scripts"]]` |
| `db.predefined_scripts_table` | Таблица реестра SQL-скриптов (audit_analyzer-специфика) → label `"scripts_registry"` | `public.agent_predefined_scripts` |
| `gateway.vector.index.storage_table` | Vector-таблица сырых эмбеддингов (`oarb.audit_vectors`); регистрируется через `lib.core.infra_registration.register_vector_storage` → `TableRegistry.register_infra("vector.storage", ...)` | `oarb.audit_vectors` |
| `sync.poll_interval_sec` | Интервал инкрементального полла PG | `14400` |
| `sync.full_resync_every` | Каждые N поллов — полная пересинхронизация | `10` |
| `embedding.base_url` | URL Ollama `/api/embed` | `http://localhost:11434/api/embed` |
| `embedding.model` | Модель эмбеддингов | `mxbai-embed-large:latest` |
| `embedding.dimension` | Размерность векторов | `1024` |
| `vector_index.config_table` | PG-таблица реестра индексов | `public.agent_vector_index_config` |
| `vector_index.store_table` | PG-таблица сериализованных FAISS-блобов | `public.agent_vector_index_store` |
| `vector_index.default_path` | Путь к FAISS-индексу | `data_store/vectors/audits_index` |
| `cache.enabled` | Включить DuckDB-кэш | `true` |
| `cache.cache_path` | Путь к кэшу (теперь общий для всех skills) | `cache/audit_cache.duckdb` |
| `cli.default_mode` | Режим CLI навыка (резерв; CLI требует `--mode`) | `predefined` |
| `cli.max_retries` | Ретраи HTTP-запросов LLM-клиента (`llm.py`) | `3` |
| `cli.timeout_sec` | Таймаут запроса к LLM (`llm.py`) | `60` |

Декларация — единый источник истины. `ApplicationContext._auto_register_skills` (см. `lib/core/application_context.py`) читает эту секцию при старте и автоматически создаёт `TableResource`/`VectorResource` в `table_registry`. Никакого `register.py` не требуется. Для добавления нового skill достаточно добавить секцию `skills.<name>` в `project.json`. DoD-проверка — `tests/test_resource_universality.py`.

> Примечание: ретраи *генерации* SQL в режиме `sql` захардкожены в
> `sql_mode.py` (`MAX_RETRIES = 2` → до 3 попыток) и от `cli_max_retries`
> не зависят.

DSN подключается только через `channels.postgres.dsn` в `project.json`
(обычно `"${DATABASE_URL}"` из `.secrets.env`) через `utils.db.resolve_dsn()`.
Подключение возможно только через полный DSN (`channels.postgres.dsn`
в `project.json`, обычно `"${DATABASE_URL}"` из `.secrets.env` через
`utils.db.resolve_dsn()`). Частичные ключи `host`/`port`/`dbname`/`user`
не поддерживаются. Навык собственного DSN не хранит.

---

## 🔄 Жизненный цикл кеша

**Владелец файла кеша навыка — `gateway.py`.** Навык (CLI) про создание и
обновление кеша больше не знает: `--force` удалён.

Пара сервисов строится в `gateway.py::_build_audit_services()` (возвращает
`(None, None)`, если `in_memory_enabled` выключен, нет DSN или таблиц):

- **`PgDuckDbSyncService`** — единственный владелец подключения к PostgreSQL
  (worker-поток). При старте выполняет полную загрузку таблиц, далее каждые
  `poll_interval_sec` (по умолч. 60 с) инкрементально опрашивает таблицы по
  track-колонке (`updated_at`; для `audit_vectors` — `id`). Новые/изменённые
  строки передаёт в callback `on_new_records` → `DuckDbCacheStore.upsert_records`.
  Структуру таблиц собирает из PG `information_schema` (+ `pg_description`):
  колонки, типы, NOT NULL, комментарии → callback `on_schema` →
  `DuckDbCacheStore.ensure_schema` (создание пустых таблиц, типы из PG).
  Каждые `full_resync_every` циклов (по умолч. 10) делает полную перезагрузку
  таблицы через `on_replace_records` → `DuckDbCacheStore.replace_records`
  (сверка удалённых строк; курсор поллинга не откатывается).
- **`DuckDbCacheStore`** — живое зеркало в чисто in-memory DuckDB
  (`cache_path=""`) + FAISS-индексы. `ensure_schema()` создаёт таблицы с типами
  из PG и сохраняет комментарии + исходные PG-типы в мета-таблицу
  `__nanobot_meta.__schema_meta` (входит в снимок). `get_schema()` возвращает
  исходные PG-типы и комментарии (без них — DuckDB-тип из information_schema).
  `publish()` атомарно записывает снимок таблиц (ATTACH во временный файл →
  `os.replace`) в `publish_path`, который вычисляется
  `table_registry.snapshot_path(workspace_path)` →
  `workspace/data_store/duckdb/cache.duckdb`. Поле `in_memory_cache_path`
  в `project.json` оставлено для истории, но игнорируется. Без изменений
  (`_dirty` = False) файл не перезаписывается; если снимок занят читателем
  (CLI) — публикация откладывается до следующего цикла, ошибка не теряет данные.

Схема в `gateway.py::run()`:

```
_build_audit_services() ──► (store, sync_service)
sync_service.set_on_new_records_callback(store.upsert_records)
sync_service.set_on_replace_records_callback(store.replace_records)
sync_service.set_on_schema_callback(store.ensure_schema)
sync_service.set_on_sync_callback(store.publish)     # снимок после каждого цикла
sync_service.start(initial_load=True)                # полная загрузка + поллинг
_preload_vector_indexes(store)                       # прогрев FAISS в память
...
(finally) store.publish() → store.close()            # финальный снимок при выходе
```

**Правило одного писателя.** DuckDB допускает только один процесс-писатель на
файл, поэтому gateway никогда не открывает целевой файл на запись: он пишет во
временный файл и атомарно подменяет его `os.replace()`. Навык (CLI) открывает
опубликованный снимок только на чтение и видит целостные данные в любой момент.

**cli_agent.py** по-прежнему содержит унаследованную фоновую загрузку/свежесть
кеша (`load_cache_from_postgres` / `check_cache_stale`) как резерв; основной
владелец и источник снимка — `gateway.py`.

### 🔧 Управление синхронизацией

#### Полный цикл обновления данных (что происходит по шагам)

1. **Старт gateway** (`gateway.py::run()`): `_build_audit_services()` читает
   секцию `skills.audit_analyzer` из `project.json`. Сервисы создаются, только
   если `in_memory_enabled: true`, задан DSN и есть таблицы; иначе — `(None, None)`
   и синхронизация не запускается.
2. **Initial load**: `PgDuckDbSyncService._do_initial_load()` для каждой таблицы из
   `db_tables` (+ таблица векторов) делает:
   - `_fetch_schema()` — запрос структуры из PG `information_schema.columns`
     + `pg_description` (колонки, типы, NOT NULL, комментарии таблиц/колонок);
   - `_ensure_table_schema()` → колбека `on_schema` → `store.ensure_schema()` —
     создаёт таблицу в in-memory DuckDB **с типами из PG** (включая пустые);
   - `_fetch_all()` → `SELECT *` → колбека `on_new_records` → `store.upsert_records()`.
3. **Поллинг**: worker-поток каждые `poll_interval_sec` вызывает
   `_poll_table()`: `SELECT * WHERE "<track>" > <последняя_метка>`. Track-колонка:
   `updated_at` (доменные таблицы) или `id` (`audit_vectors`). Новые/изменённые
   строки → `upsert_records()` (upsert по `id`: DELETE + INSERT).
4. **Публикация снимка**: после initial load и каждого цикла поллинга
   `_fire_sync_callback()` → `store.publish()` — атомарный снимок (ATTACH во
   временный файл → `os.replace`) в файл кеша навыка. `publish()` — no-op, если
   данных не менялось (`_dirty = False`) или `publish_path` пуст.
5. **Полная пересинхронизация** (сверка удалений): каждые `full_resync_every`
   циклов поллинга `_poll_table()` вместо инкрементального запроса делает
   `_fetch_all()` + `_dispatch_replace()` → `store.replace_records()` — таблица
   перезаписывается целиком (структура и типы сохраняются), удалённые в PG
   строки исчезают. Курсор поллинга не откатывается (новое значение только если
   больше текущего).
6. **Завершение**: при остановке gateway `sync_service.stop()` дописывает
   очередь, затем в `finally` — финальный `store.publish()` и `store.close()`.

#### Как связаны компоненты (callbacks)

Колбеки подключаются в `gateway.py::run()` (строки ~608-613) — это единственная
точка связывания `PgDuckDbSyncService` и `DuckDbCacheStore`:

| Событие в PgDuckDbSyncService | Колбека | Метод store | Что делает |
|---------------------------|---------|-------------|-----------|
| новые/изменённые строки | `set_on_new_records_callback` | `upsert_records` | инкрементальный апдейт |
| полная перезагрузка таблицы | `set_on_replace_records_callback` | `replace_records` | сверка удалений |
| структура из PG | `set_on_schema_callback` | `ensure_schema` | типы, NOT NULL, комментарии |
| завершение цикла | `set_on_sync_callback` | `publish` | публикация снимка для CLI |

Чтобы изменить поведение (например, публиковать снимок реже или дополнительно
инвалидировать индексы) — правишь именно эту секцию `gateway.py`.

#### Управляющие ключи (`skills.audit_analyzer` в `project.json`)

| Ключ | Где читается | По умолч. | Эффект |
|------|-------------|-----------|--------|
| `in_memory_enabled` | `gateway.py:511` | `false` | Выключает весь кеш и синхронизацию: CLI ходит напрямую в PG. `true` — включает |
| `in_memory_cache_path` | `application_context.py:491` | `workspace/data_store/duckdb/cache.duckdb` | Куда публикуется снимок (через `table_registry.snapshot_path()`). Поле `in_memory_cache_path` в `project.json` оставлено для обратной совместимости, но игнорируется новым путём |
| `db_schema` / `db_tables` | `gateway.py:516-518` | `oarb` / 4 таблицы | Какие таблицы синхронизировать. Список — массив строк; добавил таблицу → она появится в кеше после следующего цикла |
| `poll_interval_sec` | `gateway.py:549` | `60` | Частота инкрементального поллинга, сек. Меньше → свежее кеш, больше запросов к PG |
| `full_resync_every` | `gateway.py:552` | `10` | Полная перезагрузка таблиц каждые N циклов поллинга. `0` — отключить (удалённые строки останутся в кеше) |
| `mode_vector_db_table` | `gateway.py:517` | `oarb.audit_vectors` | Таблица векторов, включается в синхронизацию и прогревается в FAISS |

`config.py` мержит `project.json` в `SETTINGS`; после правки `project.json`
перезапуск gateway обязателен.

#### Требования к таблицам источника

- Колонка `id` (для upsert `DELETE + INSERT` по ключу; если её нет — таблица
  пересоздаётся из батча целиком).
- Колонка `updated_at` (инкрементальный поллинг). Для `audit_vectors` — `id`.
  Тип track-колонки должен быть сравнимым (`>`) — `timestamp`/`bigint`.
- Безопасность поллинга: строки, изменённые **и** удалённые между циклами,
  подхватятся полной пересинхронизацией (`full_resync_every`).

#### Практические сценарии

- **Свежее кеш, чаще опрос**: `poll_interval_sec: 15`.
- **Меньше нагрузки на PG**: `poll_interval_sec: 300`, `full_resync_every: 5`
  (реже опрос, но регулярная сверка удалений).
- **Не нужна сверка удалений / большие таблицы**: `full_resync_every: 0`.
- **Выключить кеш совсем**: `in_memory_enabled: false` (CLI → прямой PG).
- **Добавить таблицу в анализ**: вставить её имя в `db_tables` и перезапустить
  gateway.

#### Мониторинг

- `DuckDbCacheStore.get_stats()`: `tables` (кол-во строк), `dirty`,
  `upserts`, `publishes`, `publish_errors`, `last_upsert_at`, `last_publish_at`,
  `last_error`, `indexes_in_memory`, `vector_sources`.
- `PgDuckDbSyncService.get_stats()`: `polls`, `full_resyncs`, `reconnects`,
  `errors`, `queue_size`, `last_sync` (метка на таблицу), `connected`.
- Внешние признаки работы: mtime файла кеша
  (`workspace/data_store/duckdb/cache.duckdb`, путь из
  `table_registry.snapshot_path()`) обновляется после каждого publish; лог
  gateway: `audit_analyzer sync started (publish -> <path>)`.

---

## 🗃 SQL-скрипты: создание таблиц

Все DDL собраны в корневом каталоге [`../sql/`](../sql/). Каталог, порядок применения, совместимость — в [`../sql/README.md`](../sql/README.md). Здесь только краткая сводка по новой структуре v2.

### Совместимость с Greenplum 6.5+

Таблицы `oarb.audit_vectors` и `oarb.vector_index_*` разработаны для полной совместимости с **Greenplum 6.5** (PostgreSQL 9.4 ядро): `BIGINT GENERATED BY DEFAULT AS IDENTITY` для PK (нет переполнения), `TEXT` для `pk_value` (UUID/BIGINT), `DISTRIBUTED BY (source)` / `REPLICATED` для управляемой сегментации.

**Использование:**

| СУБД | Файлы |
|------|-------|
| Все (PG/GP) | `sql/audit_analyzer/create_<schema>_<table>.sql` — один файл на таблицу, Greenplum 6.5 (`DISTRIBUTED BY`) |

**Миграция со старой версии:** скрипты миграции векторов удалены (см. `CHANGELOG.md`).
Примените актуальные DDL из `sql/audit_analyzer/` и пересоберите индексы:
`python tools/build_vectors.py --full-rebuild`.

⚠️ Миграция удаляет данные в `audit_vectors` и `agent_vector_index_store`. После ОБЯЗАТЕЛЬНО:

```bash
python tools/build_vectors.py --full-rebuild
```

### Структура (DDL)

```sql
-- PG 13+ вариант
public.agent_vector_index_config (
    index_name      TEXT PRIMARY KEY,         -- + 8 полей
    embedding_cols  JSONB NOT NULL,
    ...
);

public.agent_vector_index_store (
    source       TEXT PRIMARY KEY,
    index_binary BYTEA NOT NULL,
    ...
);

oarb.audit_vectors (
    id             BIGINT IDENTITY PRIMARY KEY,
    pk_value       TEXT,                     -- было INTEGER
    embedding      REAL[] NOT NULL,
    source         TEXT NOT NULL,
    ... + 3 индекса
);
```

```sql
-- GP 6.5 вариант — добавляется распределение:
DISTRIBUTED BY (index_name);     -- agent_vector_index_config
DISTRIBUTED REPLICATED;          -- agent_vector_index_store (полная копия на каждом сегменте)
DISTRIBUTED BY (source);         -- audit_vectors
```

### Ограничения GP 6.5

- **BYTEA**: до ~1GB на сегмент. Для индексов >1M векторов × 1024 dim = ~400MB OK.
  Для >2.5M × 1024 dim — нужен партиционирование (выходит за рамки).
- **REAL[]**: до ~1GB на массив. Для 1024-dim = ~1M векторов на сегмент OK.
- **`bigserial`**: не поддерживается, используйте `BIGINT IDENTITY`.

### Известные несовместимости с PG

- `DISTRIBUTED BY` — только GP. PG падает с `syntax error at or near "DISTRIBUTED"`.
- `DISTRIBUTED REPLICATED` — только GP. PG не поддерживает.
- `GENERATED BY DEFAULT AS IDENTITY` — PG 10+. Старые PG 9.4-9.6 нуждаются в `SERIAL`.

---

## 🛡 Инфраструктурные границы P0 (stabilization)

Четыре инфраструктурные границы, не зависящие от домена навыков
(TARGET_ARCHITECTURE §16/§20/§28/§29).

### SQL Security Guard — `lib/utils/sql_safety.py`

AST-политика read-only SQL на `sqlglot` (dialect postgres). Контракт
`validate_sql(sql) -> None|str` сохранён (None = безопасен); внутри:

- разрешены SELECT / WITH...SELECT / UNION (и EXPLAIN от них);
- запрещены DML/DDL по первому слову (быстрый путь) и структурно:
  `SELECT INTO`, опасные функции (`pg_read_file`, `pg_sleep`,
  `dblink`, `nextval/setval`...), системные каталоги
  (`pg_catalog`/`information_schema`; флаг `SqlPolicy.allow_catalog_access`);
- multi-statement запрещён; EXPLAIN валидирует внутренний statement рекурсивно;
- `validate_sql_report()` возвращает `ValidationReport` (allowed/reason/
  issues + `normalize_sql()`/`query_hash()`) для audit trail вызывающей стороны;
- при недоступном sqlglot — graceful degradation на regex-проверки.

Потребители: `workspace/tools/duckdb_query_tool.py`,
skill `audit_analyzer` (`sql_mode`, `database`). Тесты: `tests/test_sql_safety.py`.

### Contract tests nanobot API — `tests/contract/`

Фиксируют поверхность `nanobot-ai==0.3.0`, от которой зависит адаптер:
импорт-пути, сигнатуры `AgentLoop.from_config/_assemble_outbound/_save_turn`,
hook-протокол, MessageBus, SessionManager, BaseChannel ABC, CommandRouter,
AutoCompact/Consolidator, ключи консолидации конфига, ToolContext,
`_SubagentHook`, `prompt_templates._environment`. Падение набора при
обновлении nanobot = сигнал к ревизии RuntimePatcher. Запуск в CI:
job `upgrade-readiness`.

### Валидация проектных настроек — `lib/core/project_settings.py`

Pydantic-модель `ProjectSettings` поверх merged SETTINGS; вызывается из
`ApplicationContext.create()` сразу после загрузки конфига. Все ключи
опциональны с дефолтами (отсутствие — не ошибка), но неверный ТИП или
значение поднимает `ConfigurationError` со списком всех проблем сразу.
Неизвестные ключи разрешены (extra=allow). Тесты: `tests/test_project_settings.py`.

### Миграции схемы — `sql/migrations/` + `tools/migrate.py`

Версионные миграции `V<N>__<name>.sql` с tracking-таблицей
`public.schema_migrations` (version PK, SHA256-checksum, applied_at).
Runner: `python tools/migrate.py --status|--dry-run|--apply|--verify|--baseline`
(DSN: `DATABASE_URL` или `channels.postgres.dsn`). Drift применённого
файла блокирует apply без `--force`. Существующая БД штампуется через
`--baseline` (V001 не содержит DDL). Подробности: [../sql/README.md](../sql/README.md).
Тесты: `tests/test_migrations.py`.
