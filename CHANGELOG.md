# Changelog

Все значимые изменения в проекте **nanobot — Personal AI Agent** будут задокументированы в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/), проект придерживается [Semantic Versioning](https://semver.org/lang/ru/).

Релизные ветки именуются как `release/vX.Y`, теги патч-релизов — `vX.Y.Z`.

## [Unreleased]

## [2.0.1] — 2026-08-14

> **Patch-релиз:** регрессии и баги, обнаруженные сразу после выхода v2.0.0.
> Совместимость с `nanobot 0.3.0`, восстановленный снапшот DuckDB-кеша,
> починенный `build_vectors.py`, GP-6.5-only SQL.

### Fixed

- **Gateway: регресс публикации DuckDB-снапшота.** После `af37488`
  (ApplicationContext-рефакторинг) callback `on_sync_callback` только
  ставил `asyncio.Event` для FAISS-preload, но не вызывал
  `memory_store.publish()` — данные копились в in-memory DuckDB
  `AuditMemoryStore`, а файл
  `workspace/skills/audit_analyzer/cache/audit_cache.duckdb` не создавался.
  На Linux это было особенно заметно: CLI/skill читают снимок строго
  с диска. Фикс: добавлен `memory_store.publish()` в обёртку
  `on_sync_callback` (после установки Event) + финальный `publish()`
  перед `ctx.stop()`. `gateway.py:73-83, 95-101`.
- **Gateway: `connect_timeout=10` для PostgreSQL.** В `AuditSyncService`
  выставлен таймаут 10с (раньше дефолтный ~2 минуты маскировал
  DNS/файрвол-проблемы за счёт длинных задержек). Дополнительно
  добавлено логирование и инкремент `stats` при неудачных PG-коннектах.
  `lib/services/audit_sync_service.py:568`.
- **`tools/build_vectors.py`: NameError на первом чанке.** После
  переименования `_get_embeddings` → `_get_embedding` в `ba7bb58`
  цикл `build_index` остался со старым именем — падал с `NameError`
  при первом чанке. Дополнительно `_get_embedding` валидирует тип
  первого вектора (`list[float]`) и логирует WARN, если сервер
  эмбеддингов вернул >1 вектора на 1 текст. `tools/build_vectors.py:74-110, 380`.
- **`PostgresChannel`: совместимость outbound-сигнатур с `nanobot 0.3.0`.**
  До фикса `ChannelManager._send_reasoning_end` / `_send_delta` /
  `_send_stream_event` бросали `TypeError: ... unexpected keyword
  argument 'stream_id'`. Изменены сигнатуры:
  - `send_reasoning_delta(chat_id, delta, metadata=None, *, stream_id=None)`
  - `send_reasoning_end(chat_id, metadata=None, *, stream_id=None)`
  - `send_delta(chat_id, delta, metadata=None, *, stream_id=None,
    stream_end=False, resuming=False)`
  Внутри `stream_id` принимается, но для рассуждений канал
  ключуется по `assistant_msg_id`; `del stream_id` для совместимости.
  Буфер `_stream_buffers` теперь ключуется по `stream_id`
  (fallback: `meta["_stream_id"]` → `chat_id`).
  `lib/channels/postgres_channel.py:618-830`.

### Changed

- **`build_vectors.py`: одиночные вызовы `/api/embed`.** Цикл
  эмбеддинга переписан с батчей на одиночные запросы (`input=text`),
  добавлен CLI-аргумент `--pause-sec` (default 5.0 или
  `build_pause_sec` из `project.json`). Это даёт более ровную нагрузку
  на Ollama при больших индексах.
- **`sql/`: один файл = одна таблица, GP 6.5 only.** Объединённые
  `create_*_tables.sql` разделены на отдельные скрипты по одной таблице
  (`create_<schema>_<table>.sql`); удалены дубли PG13+ вариантов
  (оставлен только GP 6.5). `COMMENT ON TABLE/COLUMN` перенесён
  внутрь файлов создания. Все скрипты: `DISTRIBUTED BY`, `pgcrypto`,
  `BIGINT IDENTITY`, без FK; `CREATE INDEX IF NOT EXISTS` заменён на
  `DO`-блок с проверкой `pg_indexes`.
- **Удалены устаревшие SQL-каталоги:** `sql/comments/`,
  `sql/snapshot/`, `sql/migrations/` (DONE-миграции не нужны в
  e2e-инсталляциях). Новый `sql/auto_migrate_1.4_2.0/` (см. ниже)
  переехал с inline-SQL на Python-генераторы. Скрипты «обновлены»:
  `sql/README.md`.

### Added

- **`sql/auto_migrate_1.4_2.0/` — генераторы миграции v1.4 → v2.0.**
  - `generate_vector_indexes_migration.py` — перенос конфигов
    vector-индексов из JSON v1.4 в `public.agent_vector_index_config`
    (upsert через `DO`-блок, GP 6.5-совместимо).
  - `generate_predefined_scripts_migration.py` — перенос реестра
    SQL-скриптов из `scripts_registry.py` v1.4 в
    `public.agent_predefined_scripts` (DELETE+INSERT, GP 6.5 совместимо).
  - `created_tables.sql` — готовый DDL всех таблиц v2.0 для Greenplum 6.5
    (UUID, `DISTRIBUTED BY`, комментарии). Применяется как отдельный
    шаг перед миграцией данных.
  Требование: `CREATE EXTENSION IF NOT EXISTS "uuid-ossp";`

### Migration notes

- **С v2.0.0 → v2.0.1**: код-совместимо. Требуется перезапуск gateway,
  если запущен на `nanobot >= 0.3.0` (signatures `send_*` изменились).
- **Пользователям v1.4 → v2.0**: используйте генераторы из
  `sql/auto_migrate_1.4_2.0/` (см. `sql/auto_migrate_1.4_2.0/README.md`).
  Полный сценарий — три шага: создание таблиц → миграция
  vector_indexes → миграция predefined_scripts.
- **Обновите `tools/build_vectors.py`** — без фикса `81fbc28`
  первая же попытка индексации упадёт с `NameError`.

---

## [2.0.0] — 2026-08-13

> **Главный релиз:** выделен сервисный слой и единый bootstrap-контекст
> (`ApplicationContext`). `gateway.py` и `cli_agent.py` стали тонкими
> оркестраторами. Аудит-инфраструктура (`audit_analyzer`) переехала в
> универсальный слой `lib/services`, gateway — единственный владелец
> DuckDB-кеша навыка. Расширен `agent_`-префикс на все таблицы агента,
> вынесены magic-числа в `project.json`, нейтрализован LLM-провайдер.

### Added

**Конфигурация: единый стиль settings-чтения.** Добавлен хелпер
`config.get_setting(*keys, default=...)` для безопасного доступа к вложенным
ключам `SETTINGS` с fallback.

**Тест-каркас `tests/test_config_keys.py`:** проверяет наличие и дефолты
всех обязательных ключей в `project.json` через JSONC-парсер из `config.py`.
При добавлении новой настройки — добавляйте запись в `REQUIRED_KEYS`.

**Новые ключи `project.json` (из рефакторинга hardcoded-значений):**
- `channels.postgres.{max_stuck_retries, msg_ctx_max_size, media_cache_dir}` и под-секция `pool.{min_conn, max_conn, pool_timeout}`;
- `channels.redis.{error_backoff_sec, reply_to_max_size, reply_to_trim_to}`;
- `skills.audit_analyzer.{sync_max_queue_size, reconnect_backoff_sec, reconnect_backoff_max_sec, cache_max_age_sec, cache_refresh_interval_sec, embedding_http_timeout_sec, mode_vector_store_table, vector_index_default_path, cli_default_format, text_chunk_size, text_chunk_overlap, build_batch_pause_sec}`;
- `cli.repl_idle_timeout_sec`;
- `streamlit.{files_dir, failed_window_sec}`;
- `gateway.{restart_initial_delay_sec, restart_max_delay_sec, streamlit_port, streamlit_log_filename, subprocess_shutdown_timeout_sec}`;
- `logging.db.{dialect, fallback_path, connect_backoff_sec, connect_backoff_max_sec, summary_max_chars}`.

**`agent_question_runs` хранит полный текст вопроса и ответа (без обрезки):**
добавлены колонки `question` (полный текст сообщения пользователя),
`response` (полный ответ агента) и `media` (JSON-список приложенных файлов).
`question`/`media` заполняются в `register_request` (inbound), `response` —
в `finish_request` (after_run/подагент). В `summary` остаётся краткая
версия (обрезанная) для быстрого просмотра.

**Логирование вложенных файлов (media):** `log_inbound`/`log_outbound`
теперь кладут `media` (список `media` из `InboundMessage`/`OutboundMessage`)
в `payload["media"]` события `agent_gateway_logs`. Файлы от пользователя
(inbound) и вложения агента (outbound) больше не теряются в логах.
В `register_request` media также сохраняется в `agent_question_runs.media`
(вопросные вложения не затираются при финализации через `COALESCE`).

**Колонки `question`/`response`/`media` покрыты миграциями** в
`create_logs_table.sql(+_gp)` и `migrate_logs_v1.sql(+_gp)` — идемпотентное
`ADD COLUMN` для существующих установок.

**Сервисный слой v2.0.0 (`lib/`)**

- **`lib/core/application_context.py:ApplicationContext`** — единый bootstrap
  всех общих сервисов. Поля: `bus`, `agent`, `tool_audit_hook`, `hooks`,
  `session_manager`, `db_logging_service`, `audit_sync_service`,
  `audit_memory_store`, `config_service`, `runtime_patcher`,
  `transcription_service`, `subprocess_manager`, `preload_service`. Метод
  `start()` использует `ShutdownCoordinator`, `stop()` — LIFO graceful
  shutdown. Graceful degradation: при недоступности БД сервис остаётся `None`.
- **`lib/core/agent_factory.py:AgentFactory`** — `create(...)` возвращает
  `(agent, hooks)`; создаёт `AgentLoop` с `ToolAuditHook` + `DatabaseLoggingHook`.
- **`lib/core/bus_factory.py:BusFactory`** — оборачивает `publish_inbound` /
  `publish_outbound` async-логгерами `DbLoggingService` без monkey-patch'ей.
- **`lib/services/config_service.py`** — единый SETTINGS-аксессор,
  `_load_runtime_config`, pre-resolve `${VAR}` из `.secrets.env`.
- **`lib/services/session_storage.py`** — выбор `PGSessionManager` /
  `SessionManager` (auto / postgres / file).
- **`lib/services/runtime_patcher.py`** — оба monkey-patch'а в одном классе
  (`ContextGovernor.normalize_tool_result` + `agent._assemble_outbound`) с
  fallback при изменении API nanobot.
- **`lib/services/channel_factory.py`** — `ChannelManager` + Redis + Postgres
  каналы + транскрипция.
- **`lib/services/transcription_service.py`** — openai/groq key/URL/language.
- **`lib/services/subprocess_manager.py`** — Streamlit spawn + terminate/kill.
- **`lib/services/preload_service.py`** — FAISS preload (gateway) +
  audit_cache refresh (cli).
- **`lib/services/db_logging_service.py`**  — структурированный журнал
  агента в `gateway_logs`: worker-поток, единственное psycopg2-соединение,
  неблокирующая очередь, batch INSERT через `execute_batch`, JSONL-fallback
  при недоступности БД, `get_stats()`. Методы `log_inbound`, `log_outbound`,
  `log_tool_call`, `log_tool_result` (с `latency_ms`), `log_error`.
- **`lib/services/db_logging_bus.py`**  — обёртки `publish_inbound` /
  `publish_outbound` для `DbLoggingService`.
- **`sql/logs/create_logs_table.sql`** — DDL для `gateway_logs`
  (UUID, JSONB, индексы по `timestamp` / `session_id` / `event_type` / `level`).
- **`lib/lifecycle/gateway_runner.py`** — `run_forever` с exponential backoff
  (1с → 30с) при падении.
- **`lib/lifecycle/shutdown_coordinator.py`** — LIFO graceful shutdown.
- **`lib/cli/console_loop.py`** — REPL + typewriter + `consume_outbound`
  (вынесено из `cli_agent.py`).
- **`lib/cli/display_config.py`** — `DisplayConfig`.
- **`lib/cli/hook_loader.py`** — сканирование `workspace/hooks/*.py`.
- **`workspace/hooks/database_logging_hook.py`** — `AgentHook` для tool-событий
  + `after_run` summary в БД.

**`audit_analyzer` — универсальный слой данных (`lib/services`)**

- **`lib/services/cache_provider.py`** — интерфейс `CacheProvider`
  (`is_ready` / `refresh` / `check_stale` / `preload_indexes` / `search_vector` /
  `query_sql` / `explain` / `get_schema` / `close`) + dataclass `SearchResult`.
- **`lib/services/cache_provider_impl.py`** — `PostgresDuckDbProvider`
  (DuckDB-кеш + FAISS-индексы). Модульные функции: `get_embedding`
  (Ollama `/api/embed`), `load_cache_from_postgres`, `check_cache_stale`,
  `read_vector_index_config`, `read_embedding_config`, `build_cache_provider`.
  Тяжёлые зависимости импортируются лениво внутри методов.
- **`lib/services/text_splitter.py`** — чанкование текстов для индексаторов
  (вынесено из навыка).
- **`lib/services/audit_memory_store.py`** — in-memory DuckDB-зеркало +
  FAISS-индексы + атомарный `publish()` (ATTACH temp + `os.replace`).
  `ensure_schema()` создаёт таблицы с типами из PG, сохраняет
  `pg_type` + комментарии в `__nanobot_meta.__schema_meta`. Снапшот
  публикуется в файл `in_memory_cache_path` навыка; `publish()` no-op,
  если `_dirty=False` или `publish_path` пуст.
- **`lib/services/audit_sync_service.py`** — фоновый worker-поток, единственный
  psycopg2-коннекшн. `_fetch_schema` собирает структуру из PG
  (`information_schema` + `pg_description`). Callbacks: `on_new_records`,
  `on_replace_records`, `on_schema`, `on_sync`. Полная пересинхронизация
  каждые `full_resync_every` циклов.

**Инфраструктура `audit_analyzer`**

- **`tools/build_vectors.py`** — индексатор вынесен в корень проекта
  (вне навыка). Флаги: `--full-rebuild`, `--check`, `--status`,
  `--dry-run`, `--index`, `--batch-size`, `--chunk-size`, `--chunk-overlap`,
  `--db-table`. Чанкование через `lib/services/text_splitter.py`.
- **`sql/audit_analyzer/create_audit_source_tables_gp.sql`** — REFERENCE DDL домена
  (`oarb.audits`, `oarb.violations`, `oarb.audit_reports`, `oarb.report_items`).
- **`sql/audit_analyzer/create_audit_vectors_table_gp.sql`** — `oarb.audit_vectors` +
  `oarb.vector_index_store` + индексы.
- **`sql/audit_analyzer/create_vector_index_config_gp.sql`** — `oarb.vector_index_config`.

**Конфигурация и секреты**

- **`project.json`** (JSONC с `//` и `/* */` комментариями) — новый формат
  проектных настроек. Порядок мержа: `project.json → config.json →
  .secrets.env` (поздний перекрывает ранний).
- Новые секции `project.json`: `channels.*` (postgres/redis), `skills.*`,
  `cli`, `benchmark`, `streamlit`, `gateway`, `logging.db`.
- Механизм подстановки секретов `${VAR}` из `.secrets.env` /
  `os.environ` при чтении конфигурации.
- **`.secrets.env.example`** — шаблон переменных окружения.
- **`workspace/utils/db.py:resolve_dsn()`** — единое разрешение DSN
  (`configure()` → `channels.postgres.dsn` → `DATABASE_URL`/`PG_DSN`),
  идемпотентная настройка глобального коннектора.

**Документация**

- `DEVELOPMENT.md` — техническая документация: архитектура сервисного
  слоя v2.0.0, полная таблица связей между файлами (`lib/core/`,
  `lib/services/`, `lib/cli/`, `lib/lifecycle/`), жизненный цикл кеша,
  раздел «Управление синхронизацией» (callbacks, ключи конфига,
  мониторинг, требования к таблицам источника).
- `README.md` — обновлён под v2.0.0: mermaid-диаграммы, 11 компонентов,
  таблица БД, запуск.

### Changed

- **Точки входа → тонкие оркестраторы:**
  `gateway.py` сократился с 696 до 132 строк, `cli_agent.py` — с 865 до 165.
- **`audit_analyzer` — тонкий CLI поверх `lib/services`.** Удалены
  `InMemoryDatabase`, `vector_mode.py`, `check_status.py`,
  `cache/query_audit.py`. Навык работает с `PostgresDuckDbProvider`
  через `build_cache_provider()`; `Database` (прямой PG) и провайдер
  кеша реализуют единый протокол `QueryBackend` (`get_schema` /
  `query_sql` / `explain`).
- **Gateway — единственный владелец файла кеша навыка.** `AuditSyncService`
  инкрементально синхронизирует таблицы в `AuditMemoryStore`, после
  каждого цикла `store.publish()` атомарно записывает снимок
  (`temp + os.replace`) в файл кеша навыка. CLI открывает снимок только
  на чтение.
- **Pre-resolve `${VAR}` от `.secrets.env`**: gateway больше НЕ требует
  `export LLM_API_KEY=...` в shell — `ConfigService._pre_resolve_env_refs`
  кладёт ключи в `os.environ` ДО `_load_runtime_config`.
- **`--force` в `audit_analyzer` удалён.**
  `cli.py` завершается с `FileNotFoundError`, указывающим на gateway.
- **`audit_analyzer.SCRIPTS_REGISTRY`** вынесен из `scripts_registry.py`
  в отдельный `predefined_scripts.py` (позднее — реестр перенесён в БД,
  см. ниже).
- **`requirements.txt`** — убраны неиспользуемые пакеты (`requests`,
  `sentence-transformers`, `anthropic`, `openai`), версии — точные
  `=X.Y.Z` для полной воспроизводимости.
- **`config.py`** — удалена загрузка `.env` (защитный fallback) и
  константа `_ENV_FILE` (больше не используется); merge-order
  комментарий обновлён.
- **Документация:** ASCII-арт заменён на mermaid-диаграммы,
  `REFACTORING_PLAN.md` удалён (план завершён).
- **Все ранее хардкоженные magic-числа и пути вынесены в `project.json`**:
  timeout'ы, интервалы, retry-лимиты, пороги, размеры пулов и очередей,
  пути к кешу/индексам, chunk-параметры — теперь управляются через настройки.
- `cli_agent.py` (`--mode`): снят `required=True`. Значение по умолчанию
  берётся из `skills.audit_analyzer.cli_default_mode` в `project.json`.
- `channels.postgres.processing_timeout`: дефолт унифицирован на `120`
  (раньше расходился между `project.json=120` и `postgres_channel.py=600`).
- `skills.audit_analyzer.vector_index_default_path`: дефолт сменён с
  `~/.nanobot/vectors/audits_index` на `workspace/data_store/vectors/audits_index`.
- `DbLoggingService`: удалены мёртвые параметры `min_conn`/`max_conn`
  (не подключались к реальному пулу).
- **`DbLoggingService._render_sql` исправлена для PostgreSQL:** наивная
  `replace("agent_gateway_logs", ...)` ломала `RENAME TO` в миграциях
  (`RENAME TO "public"."agent_gateway_logs"`). Теперь цель `RENAME TO`
  и schema-qualified `public.agent_gateway_logs` не квалифицируются повторно.
- `lib/channels/postgres_channel.py`: `_session_media_dir` стал инстанс-методом
  (раньше — static с глобальной `_DATA_STORE_DIR`); путь управляется ключом
  `channels.postgres.media_cache_dir`.
- `tools/build_vectors.py`: default'ы `--chunk-size` / `--chunk-overlap` /
  `--batch-pause` читаются из `skills.audit_analyzer.*`.
- **Единый `agent_`-префикс для оставшихся таблиц агента.**
  `public.predefined_scripts` → `public.agent_predefined_scripts`;
  `conversation_messages` → `public.agent_conversation_messages` (канал Web/Streamlit);
  векторные таблицы перенесены из `oarb` в `public`:
  `oarb.vector_index_config` → `public.agent_vector_index_config`,
  `oarb.vector_index_store` → `public.agent_vector_index_store`.
  Данные переносятся миграцией `sql/migrations/migrate_agent_table_names_v1.sql`
  (идемпотентно — сохраняет строки). Обновлены конфиг-ключи
  `channels.postgres.table_name`, `skills.audit_analyzer.{predefined_scripts_table,
  mode_vector_index_config_table, mode_vector_store_table}`, DDL, seed'ы,
  комментарии, `generate_predefined_scripts_sql.py` и тесты. Доменные таблицы
  навыка (`oarb.audits/violations/audit_reports/report_items/audit_vectors`)
  и `audit_interactions` не затронуты.
- **Навык `audit_analyzer` теперь читает ВСЕ данные из DuckDB-кэша.**
  Прямой psycopg2 в read-only потоке навыка не используется.
  Раньше `db_loader` ходил в PG через `utils.db.fetch` — теперь через
  `cache_provider.query_sql(...)` (тот же DuckDB-файл, что и для аудит-данных).
- `lib/services/cache_provider_impl.py`: копирование PG → DuckDB переведено
  с `pd.DataFrame(records)` на `COPY ... TO STDOUT` + `read_csv_auto`
  (без `pandas`, без pyarrow-IPC). Сохраняются типы JSONB → JSON,
  TIMESTAMPTZ → TIMESTAMPTZ, NUMERIC → DECIMAL, UUID → UUID.
- `lib/services/audit_memory_store.py`: `pd.DataFrame(records)` →
  `pyarrow.Table.from_pylist` + `conn.register`. Сохраняет `list[float]`
  как `DOUBLE[]` (раньше через `pd` → `DOUBLE[]`, через CSV → `VARCHAR`).
- Удалён `workspace/skills/audit_analyzer/scripts/predefined_scripts.py`
  (267 строк) — реестр перенесён в БД.
- `cache_provider_impl.py` теперь поддерживает `additional_tables`
  (минимальное расширение для копирования таблиц из произвольных схем).

### Removed

- Навыки **`data-analyzer`** и **`html_presentation_generator`** —
  вычищены зависимости из `requirements.txt`, блоки из
  `project.json`/`config.json`. Все артефакты аудита этих навыков
  удалены (`webui/`, `ws/`, `media/`, `workspace/data_store/cache/*.html`,
  `count_numbers.py`).
- **`pg_agent_worker.py`** и `tests/test_pg_agent_worker.py` — старый
  standalone Postgres-воркер, не использовался в v2.0.0.
- Мёртвые ключи конфига: `schema_cache`, `cli_default_format`,
  `_ENV_FILE` (не читались кодом).
- Мусорные артефакты: `lib/channels/workspace/` (баг путей v1.4.0),
  `__pycache__/` старых хуков.
- `webui/` (старый SPA-dist из v1.3.0), `ws/`, `media/`.
- `REFACTORING_PLAN.md` (план завершён).
- Legacy `workspace/skills/audit_analyzer/DEVELOPMENT/*` —
  перенесены в корень (`DEVELOPMENT.md`) и `tools/build_vectors.py`.
- Legacy мигратор `migrate_vectors_to_db.py`.
- Legacy-таблицы **`public.agent_questions`** и **`public.agent_responses`**.
  Они не использовались ни одним Python-модулем; их роль полностью покрыта
  `agent_gateway_logs` (вопрос → `inbound`, ответ → `outbound_final` в
  `payload.content`) + `agent_question_runs` (контекст).
- `pandas` из `requirements.txt` (заменён на `pyarrow`).

### Fixed

- **Race-condition FAISS preload**: callbacks на `AuditSyncService`
  устанавливаются **ДО** `ctx.start()`, иначе worker-тред при первом
  `_do_initial_load` скипает записи → DuckDB остаётся пустым →
  `preload_vector_indexes` видит «нет данных». Теперь в `gateway.py:main()`
  callbacks идут раньше `start()`.
- **Совместимость с nanobot 0.2.2**: инъекция провайдерских API-ключей
  из `.secrets.env` в конфиг на старте (провайдер-скоупинг формат
  `.secrets.env` не попадал в `os.environ` как `LLM_API_KEY`).
- **`PostgresChannel.send`** — runtime-progress события больше не
  затирают `media` сообщений-инструментов.
- **Streamlit**: `_get_extension_from_mime` корректно выводит расширение
  через `mimetypes` (с fallback `.bin`); ожидание ответа агента больше
  не имеет таймаута — на статусе `failed` re-check 5 минут, далее
  бесконечное ожидание возврата в `processing`.
- **Аттачменты в `PostgresChannel`**: `_decode_media_from_db` корректно
  обрабатывает dict-entries `{filename, data}` и сохраняет оригинальные
  имена файлов в session cache. `_poll_once` добавляет
  `[Attachment: name (saved at path)]` к пользовательскому контенту.
  Исправлено разрешение workspace dir и санитизация session key для
  Windows-путей.
- **`database.py`** (навык): упрощён на 78 строк без изменения поведения
  (удалён мёртвый Schema cache).
- **`lib/session/pg_session_manager.py`**: удалён нерабочий `sys.path`
  hack, указывавший на несуществующий путь.
- **README/DEVELOPMENT**: устранены неточности — убраны упоминания
  удалённых навыков, исправлена заметка о GP-схеме, счётчик seed-записей,
  ссылки на бенчмарки, ссылка nanobot (была `opencode.ai`).
- **Тесты:** `test_exception` использует объект с `__getattr__`,
  поднимающим исключение (т.к. `_get` использует `getattr`, не `.get()`).
  Pre-resolve использует `patch.dict` для nanobot.
- `audit_memory_store._replace_locked` / `_upsert_locked`: добавлены транзакции
  `BEGIN/COMMIT/ROLLBACK` для DELETE + INSERT (ранее при ошибке INSERT
  таблица оставалась пустой). DDL (ALTER/DROP) остаётся вне транзакции
  (DuckDB не откатывает DDL).
- `db_loader.get_provider()` теперь fail-fast без инжекции — ранее молча
  создавал второй CacheProvider, что приводило к Windows-блокировкам файла.
- `db_loader._parse_parameters()` корректно обрабатывает `None` и пустые
  строки (раньше бросал `TypeError`).

### Renamed

- **Контракт LLM-провайдера нейтрализован.** Имя env-переменной для ключа
  провайдера сменено: `MISTRAL_API_KEY` → `LLM_API_KEY`. Больше нет привязки
  к конкретному вендору в имени переменной. Изменения:
  - `config.py`: ключ из любой непустой `SETTINGS.providers.*.api_key`
    всегда экспортируется как `LLM_API_KEY`.
  - `config.json`: `${MISTRAL_API_KEY}` → `${LLM_API_KEY}` в
    `providers.minimax.apiKey` и `providers.mistral.apiKey`.
  - `lib/services/config_service.py:_pre_resolve_env_refs`:
    `LLM_API_KEY` берётся из первой непустой `SETTINGS.providers.*.api_key`.
  - Дефолты `project.json` (`llm_provider`, `llm_model`, `llm_api_base`)
    нейтральны (`openai-compatible` / `gpt-4o-mini` / OpenAI URL); выбрать
    Mistral/MiniMax/etc. — задать `llm_api_base` явно.
  - `.secrets.env.example`: секция `# providers: mistral` →
    `# providers: llm` (любое имя секции допустимо).

  Действия при миграции:
  - В `.secrets.env` переименуйте секцию `# providers: mistral` в
    `# providers: llm` (необязательно, но рекомендуется для ясности).
  - Если вы где-то задавали `MISTRAL_API_KEY` в shell через `export` —
    переименуйте в `LLM_API_KEY`.
  - В `project.json` при необходимости укажите свой `llm_api_base`
    (например, `https://api.minimax.io/v1`).

### Security

- **`.gitignore` (новый, корневой):** защита `.secrets.env`
  (КРИТИЧНО — API-ключи и DSN), Python (`__pycache__/`, `*.pyc`),
  pytest/coverage, артефакты удалённых навыков, runtime
  (`workspace/data_store/`, `workspace/sessions/`), DuckDB, IDE.
- API-ключи вынесены из кода и конфигурации в `.secrets.env`.

### Tests

- **701 → 683 unit-теста** (`-18` после удаления `test_pg_agent_worker.py`).
- `+107` новых тестов в v2.0.0: `test_application_context.py`,
  `test_agent_factory.py`, `test_bus_factory.py`, `test_config_service.py`,
  `test_session_storage.py`, `test_runtime_patcher.py`,
  `test_transcription_service.py`, `test_channel_factory.py`,
  `test_subprocess_manager.py`, `test_preload_service.py`,
  `test_db_logging_service.py`, `test_hooks_database_logging.py`,
  `test_gateway_runner.py`, `test_shutdown_coordinator.py`,
  `test_console_loop.py`.
- Покрытие `audit_analyzer`: `TestSchema`, `TestReplace`,
  `TestSchemaAndResync`, `test_map_pg_type`, `publish()`,
  `on_sync_callback`.

### Migration notes

- Если у вас уже есть `session_manager.json` с плоскими `min_conn`/`max_conn`/
  `pool_timeout` — продолжает работать (legacy-fallback в `session_storage.py`).
  Рекомендуется перенести в `channels.postgres.pool.*` в `project.json`.
- Если вы переопределяли `~/.nanobot/vectors/audits_index` через свой скрипт —
  теперь значение по умолчанию другое (`workspace/data_store/vectors/audits_index`).
  Чтобы сохранить старое — задайте `vector_index_default_path` явно.
- **`agent_question_runs`: новые колонки `question`/`response`/`media`.**
  Для существующих установок выполните `create_logs_table.sql` (и
  `migrate_logs_v1.sql`) — они содержат идемпотентное `ADD COLUMN`; или
  вручную: `ALTER TABLE agent_question_runs ADD COLUMN IF NOT EXISTS
  question TEXT, ADD COLUMN IF NOT EXISTS response TEXT,
  ADD COLUMN IF NOT EXISTS media TEXT;`
- **Миграция `agent_`-префикса:** `sql/migrations/migrate_agent_table_names_v1.sql`
  переименовывает/переносит `public.predefined_scripts`,
  `public.conversation_messages`, `oarb.vector_index_config`,
  `oarb.vector_index_store` под новые имена с сохранением данных.
  Доменные таблицы навыка (`oarb.audits/violations/...`) не затрагиваются.

---

## [1.5.0] — 2026-07-22

### Added
- Векторные поисковые индексы перенесены из файлов в PostgreSQL/Greenplum:
  - `oarb.audit_vectors` — сырые эмбеддинги `REAL[]` с метаданными (строит `build_vectors.py`);
  - `oarb.vector_index_store` — сериализованный FAISS-индекс `BYTEA` (ищет `vector_mode.py`);
  - `oarb.vector_index_config` — конфигурация индексов (таблицы/колонки), чанкование, автосинхронизация.
  - Параметры `--top-k` / `--threshold` задаются аргументами CLI (`--index-name`), а не конфигом.
- DuckDB-кеш для `audit_analyzer` с фоновым обновлением (`in_memory_enabled`, `cache/audit_cache.duckdb`); `init`-режим загрузки кеша из PostgreSQL.
- Передача файлов между агентами через БД как base64 `data URL` вместо файловых ссылок.
- 75 unit-тестов по runner/gateway/streamlit (`tests/`), исправлены найденные баги.
- `requirements.txt` со всеми зависимостями.
- Инъекция провайдерских API-ключей из `.secrets.env` в конфиг на старте (совместимость с nanobot 0.2.2).
- Инструкция разработчика по векторным индексам (docs).

### Changed
- Конфигурация мигрирована из кода в `.env` (+ исправлена коллизия имени `scripts/config.py`); из `.env` в `.secrets.env` вынесены API-ключи.
- Реорганизована структура: модули `lib/channels` и `lib/session`, добавлены README для них; итоговое расположение `sql/session/`, `sql/channels/`, `scripts/`, `logs/`.
- README исправлен (неточности), добавлены README для `lib/channels` и `lib/session`.

### Fixed
- Совместимость с Greenplum 6.25: ручной UPSERT вместо `ON CONFLICT` в `PGSessionManager`.
- Хранение файлов сессии в `data_store/cache/sessions/{session_key}`.
- Убран `ThreadedConnectionPool` — вызывал double free на Windows с asyncio.
- DSN берётся из `gateway_settings.py`; retry LLM при 429; вывод реальной ошибки БД в fallback-сообщениях.
- gssencmode=disable для GP 6.25 / PG 9.4 (и URI, и key=value DSN; через kwargs `connect()`, а не модификацией строки).
- Отдельные счётчики retry в `_connect()`: 50 попыток для «too many connections», 15 для остальных.
- Исправлен индекс `parents` (3 вместо 2 — работает на всех версиях Python); при retry удаляется assistant-placeholder вместо установки `status='failed'`.

### Security
- API-ключи вынесены из кода и конфигурации в `.secrets.env` (файл в `.gitignore`).

---

## [1.4.0] — 2026-06-16

### Added
- Русские docstrings во всех `.py`.
- Хук `_run_sync` fallback для случая, когда нет event loop (Streamlit, CLI) — использует временный пул.

### Changed
- Стек БД переведён с `asyncpg` → `psycopg2`, API переведён с async на sync.
- Убран общий пул: каждый запрос создаёт и закрывает собственное подключение; удалён модуль `db_api`; импорты переведены на функции модульного уровня.
- `DB_RETRYABLE_ERRORS` экспортирован из `db.py` (убрана дубликация в `pg_session_manager`).
- Навык `db_analyzer` переименован в `audit_analyzer`; `config.json` грузится из папки `gateway.py`.

### Fixed
- Совместимость с PG 9.4 и GP 6.25: `DISTRIBUTED BY`, pgcrypto, schema-introspection; удалены все DDL (`ensure_tables`) — таблицы должны существовать заранее.
- Раздельные счётчики retry: `TooManyConnectionsError` — 50×, остальные ошибки — 10×.
- Таймаут 30с на `pool.acquire()` (канал больше не зависает); предотвращена утечка соединения в `_get_conn` при ошибке `_init_jsonb`.
- `ON CONFLICT` → `UPDATE+INSERT` для GP6; `IF NOT EXISTS` → проверки через `pg_catalog`; убраны касты `::jsonb` из DML; синтаксис `session ON CONFLICT` исправлен, `msg_timestamp` дедуплицирован.
- `pool_max_conn` снижен до 1 против «too many connections» на Greenplum.
- Streamlit ожидает ответ агента без `st.rerun` (обход лимита `maxReruns`).

### Removed
- Пул соединений (включая шаринг одного пула между async/sync через `run_coroutine_threadsafe`) — перевыделение ресурсов на каждый запрос.
- Все DDL и `::jsonb`-касты.

---

## [1.3.0] — 2026-06-10

### Added
- Единый слой БД `SharedDB` (один psycopg2-коннекшн с блокировкой) + конфигурируемый асинхронный пул (`min_size`/`max_size`); sync-методы используют отдельные подключения.
- HTTP **DB API Server** — доступ к PostgreSQL из любых процессов; автоочистка БД; поддержка DSN для subprocess-процессов.
- **Self-review** система: `ReviewAgentLoop`, `RepeatGuardHook`, навык response-verification; метаданные `_review` (quality, attempts, issues, tool_repeat_stopped).
  - Ревьюер разбит на 8 независимых проверок с русскими промптами; fast-path по приветствию; фиксы multi-turn контекста.
  - **Fresh Data Rule** — агент обязан делать свежие tool-вызовы, а не переиспользовать историю.
  - Check 1 (Tool Usage) — детект обхода инструментов и ответа «из памяти»; Check 3 (Error Honesty) — детект «нет данных» вместо реальных ошибок инструментов.
  - `on max_iterations` — подстановка ответа «could not get data» вместо галлюцинированного контента.
- `ToolAuditHook` — запись всех tool-вызовов (статус/ошибки/аргументы) в `metadata._tool_audit`; `ToolParamsHook` влит в него.
- **Benchmark-фреймворк**: русские YAML-элементы, хук-фиксы, поддержка `qwen3-coder`; `fix bechmark` на точке реза ветки.
- Нативный инструмент `db_analyzer` для gateway (с валидацией параметров predefined-скриптов и защитой от необработанных исключений; позже откатан в ветку).
- Streamlit запускается как subprocess вместе со всеми каналами; тонкий клиент через `conversation_messages` + единый `AgentLoop` в gateway.
- UI: file-based история по умолчанию (`--storage` для DB), сворачиваемое reasoning, отображение tool events, загрузка хуков.
- Redis-канал `redis_channel.py`; блок `session_manager` в конфиге (читается из сырого JSON в обход валидации Pydantic) + совместимость с PG 9.4.20.

### Changed
- `psycopg2`/`asyncpg` → единый `asyncpg SharedDB` для каналов, сессий, навыка и CLI (`:param` → `%s`).
- Единый DSN в `gateway_settings.py` (убраны дубликаты из навыка); унифицирована конфигурация gateway.
- `conversation_id` → `chat_id` для блокировок по чатам; per-chat locking.
- Убран `INDEX.json` — каждый результат сохраняется отдельным файлом; ограничение `MAX_OUTPUT` у ExecTool до 10M; `processing_timeout` 600 → 120 с.
- `_tool_events` → `_tool_audit` без дублирования; слияние `reasoning` и `_reasoning` в ключ `metadata.reasoning`.

### Fixed
- Двойное кодирование JSONB в postgres_channel (хелпер `_decode_jsonb`, backward compat для старых записей); JSONB-декодер в SharedDB (asyncpg возвращает `dict`).
- Путь workspace в data-analyzer и захардкоженный путь в e2e-тесте.
- Обработка переполнения диска в `_normalize_with_persist`; gateway обёрнут в автоперезапуск при краше; limit роста INDEX.json (preview убран).
- Транзакционный `_mark_failed`; гонка UPSERT в `PGSessionManager` (`ON CONFLICT`); соответствие `seed_messages.sql` DDL; исправлена двойная JSONB-кодировка в `pg_agent_worker`.
- `%s`-плейсхолдеры для asyncpg; `to` (/quote) очистка в навыке; не переконфигурировать SharedDB.
- postgres channel: поллинг, `allow_from`, `timezone.UTC`, создание каталогов.

### Removed
- WebSocket-канал (конфиг + примеры `gateway_settings`), `webui-dist/` (SPA) и код `_patch_webui_dist`, `patches/` (reviewer, review_agent_loop) — мёртвый код из benchmark-dev.
- Мёртвые файлы: `temp_loop.py`, `create_table.sql`, `test_file_*.py`, регенерированные артефакты workspace, `_tmp_checks.py`, `fibonacci.py`; `connection_string` из docstring.
- `ResponseReviewHook`, `INDEX.json`, `DbAnalyzerTool` (revert).

---

## [1.2.0] — 2026-05-29

### Added
- **Streamlit-чат** с live-отображением рассуждений агента (`streamlit_app.py`).
- CLI: стриминг reasoning и ответа в реальном времени (вывод tool-выводов скрыт).

### Changed
- PostgresChannel переведён на **единотабличную** архитектуру (`conversation_messages`) с батчингом reasoning и контролем конкурентности (макс. параллельных сообщений).
- Вывод CLI переписан: typewriter-эффект, хуки, константы конфигурации.

---

## [1.1.0] — 2026-05-27

### Changed
- Модель конфигурации обновлена до `gpt-oss:20b-cloud`; исправлены стрелочные символы в presentation-инструменте.
- `db_analyzer`: класс `Database`, кеш схемы, фильтр таблиц, прямой DSN; улучшенный формат схемы для LLM (`NOT NULL`, `varchar(N)`, `schema.table`).
- `cli_agent`: добавлены константы `_CONFIG_PATH` и `_WORKSPACE_DIR`; скан `workspace/skills/` на предмет `tool.py`.

### Fixed
- Отображение рассуждений в `cli_agent` — по-дельтам, без накопления, с Rich markup; устранено дублирование ответа; откат пере-скана навыков (два дублирующих коммита).
- Показ результатов tool-вызовов (`show_tool_results`).
- Исправлен остаток merge-конфликта в `config.json`; трекинг `config.json` (секреты санитизированы).

---

## [1.0.0] — 2026-05-27

### Added
- Навыки `db_analyzer` и `html_presentation_generator` (полный код, E2E-тесты, исправленный `.gitignore`); разрешение `vector_source`, JSON-safe вывод.
- CLI-режим vector: параметры `--top-k` и `--threshold` (примеры для Linux в SKILL.md).

### Changed
- CLI: `--params` поддерживает формат `key=value` (фикс кавычек для Windows); примеры в SKILL.md.


---