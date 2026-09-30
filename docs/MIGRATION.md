# Migration Notes

Сводка ключевых изменений между релизами для тех, кто апгрейдится с предыдущей
версии. **Источник истины** — [CHANGELOG.md](../CHANGELOG.md); здесь — только
краткая выжимка с фокусом на **breaking changes и ручные действия**.

---

## Незарелизованное (`master`, CHANGELOG → [Unreleased](../CHANGELOG.md)) — векторные индексы, изоляция `history_search`, снятие протокола аренды задач

⚠️ **Breaking change** в подсистеме векторных индексов: persisted FAISS-кеш
удалён, таблица-сигнатура и настройка `signature_table` больше не существуют.

⚠️ **Breaking change** в канале PostgreSQL: протокол аренды задач (мульти-машинный
пул воркеров) удалён целиком. Таблица `public.agent_worker_claims` и настройки
`channels.postgres.{claims_table, claim_strategy, lease_interval}` больше не
существуют. Активный режим всегда был `single`, поэтому поведение канала на
рабочей инсталляции не меняется. **Потеряно:** отказоустойчивость уровня HA —
упавший инстанс gateway не отдаёт задачу, пока её не вернёт `_unstick_loop`
соседнего. `SkillSettings` объявлен с `extra="forbid"`: оставленные в
`project.json` ключи из удалённых остановят старт gateway с `ConfigurationError`.

**Ручные действия:**

0. **Удалить таблицу аренды.** `sql/migrations/V006__drop_agent_worker_claims.sql`
   (`python tools/migrate.py --apply`) содержит `DROP TABLE IF EXISTS
   public.agent_worker_claims;` — runner выполняет SQL как есть, подстановок
   нет, так что скрипт применяется штатно. Номер 006, а не 005: `005` уже
   использовался ранее (`V005__create_agent_cache_ownership.sql`, удалён), и
   базы, где он применился, хранят его в `public.schema_migrations`.

**Автоматические изменения**:

- FAISS-индексы собираются **в памяти** при старте gateway
  (`PreloadService.preload_vector_indexes`) из DuckDB-снапшота
  `gateway.vector.index.storage_table`; persisted-артефактов в PG больше нет.
- `tools/build_vectors.py` пишет векторы в `storage_table` и пересобирает
  FAISS в памяти; настройки `gateway.vector.index.signature_table` и
  `config_table` удалены из `VectorIndexSettings` (их наличие в `project.json`
  — fail-fast на старте).
- `--list-indexes` (CLI `audit_analyzer`) и `tools/check_indexes.py` читают
  runtime-состояние из того же снапшота, а не из PG-таблицы.
- `history_search(session_scope="all")` изолирован по `user_id` (security):
  колонка `agent_gateway_logs.user_id` + индекс `(user_id, "timestamp" DESC)`.
  Семантика `scope="all"` — «все сессии текущего пользователя», а не глобальная
  выборка; при отсутствии identity-store возвращается `missing_user_identity` /
  `missing_session_identity` **без** обращения к БД.

**Ручные действия**:

1. **Удалить legacy-таблицу FAISS-кэша.** Миграция `V003__drop_vector_index_store.sql`
   содержит **шаблон**: `DROP TABLE IF EXISTS "<signature_table>";` — runner
   (`tools/migrate.py`) выполняет SQL как есть, без подстановок, поэтому при
   `--apply` это no-op. Оператор подставляет реальное имя (в существующих
   инстансах — `public.agent_vector_index_store`) и выполняет DROP вручную:

   ```sql
   DROP TABLE IF EXISTS public.agent_vector_index_store;
   ```

   `V004__agent_gateway_logs_user_id.sql` (колонка `user_id` + backfill из
   `agent_question_runs.user_id` + индекс) — обычная, применяется через
   `python tools/migrate.py --apply`.

   Не редактируйте уже применённые миграции: изменение содержимого ломает
   checksum (`--verify` → DRIFT). Для отката — `DROP TABLE IF EXISTS ...`
   вручную и `--force` при повторном применении.

2. **Пересобрать векторные индексы**: `python tools/build_vectors.py --full-rebuild`.
   До пересборки поиск в `--mode vector` вернёт пустую выдачу — runtime
   получает векторы из снапшота `storage_table`, который наполняется этой
   командой.

3. **Проверить согласованность декларации и runtime**:
   `python tools/check_indexes.py` (exit 0 — согласовано, 1 — divergence,
   2 — инфраструктурная ошибка).

4. **Аудит вызовов `history_search`**: агент, полагавшийся на глобальную выдачу
   по `session_scope="all"`, теперь получает события только своего пользователя
   либо `missing_user_identity`, если identity-store не заполнен.

---

## v3.x.x — Storage hybridization (upstream SessionManager + cold PG mirror + LLMUsageStore)

⚠️ **Breaking change** в архитектуре хранения сессий и LLM usage:

- Hot-path запись/чтение сессий — теперь через upstream
  `nanobot.session.manager.SessionManager` (JSONL),
  `PGSessionManager` стал compatibility layer (см.
  [docs/architecture/storage-layers.md](architecture/storage-layers.md)).
- PostgreSQL остаётся как **cold-storage mirror** через
  фоновый `SessionColdSyncService` (см.
  [docs/architecture/storage-layers.md](architecture/storage-layers.md)).
- LLM usage теперь идёт в upstream `LLMUsageStore` (SQLite WAL)
  через observer-pipeline (см.
  [docs/architecture/usage-tracking.md](architecture/usage-tracking.md));
  `DbLoggingService` больше НЕ пишет `event_type="llm_usage"`.

**Автоматические изменения** (ничего делать не нужно для greenfield):

- Добавлен `SessionColdSyncService` в
  `lib/services/session_cold_sync_service.py` — фоновый daemon-поток
  с per-transaction advisory lock, батчами по 50 сессий.
- Добавлен `lib/services/llm_usage_store_factory.py` — фабрика
  `LLMUsageStore` с дефолтом
  `<get_runtime_subdir("usage")>/usage.db`.
- Добавлен `lib/services/llm_observer.py` —
  `wrap_provider_snapshot_loader` подключает observer-pipeline.
- `PGSessionManager` теперь — тонкий compatibility layer
  (hot-path → `super()`); никаких прямых `INSERT/UPDATE` в
  `agent_session_meta` / `agent_session_messages`.
- Добавлены секции `gateway.usage_store.*` и
  `gateway.session_cold_sync.*` в `project.json`.
- Новые contract tests: `tests/contract/test_session_manager_api.py`,
  `tests/contract/test_usage_store_api.py`,
  `tests/contract/test_llm_observer_api.py`.
- Архитектурный гард `tests/test_storage_hybridization.py` ловит
  прямой SQL в `agent_session_*` и создание новых psycopg2-пулов.

**Ручные действия** (ОБЯЗАТЕЛЬНО для проектов с историческими сессиями):

1. **Миграция исторических PG-сессий в JSONL (до deploy).**
   Если в вашем проекте в `agent_session_meta` /
   `agent_session_messages` есть исторические сессии, написанные
   старой версией `PGSessionManager` — перенесите их в upstream
   JSONL отдельным скриптом **до** deploy
   `storage-hybridization`. Без этого:

   - история пользователей будет потеряна (sync удалит PG-строки
     без upstream-двойника первым же циклом);
   - UI покажет пустую историю для этих сессий.

   Миграционный скрипт вне scope этого change; см. обсуждение в
   архивированном proposal
   [openspec/changes/archive/2026-09-27-storage-hybridization/proposal.md](../openspec/changes/archive/2026-09-27-storage-hybridization/proposal.md).

2. **Передача DSN в pool.** Убедитесь, что `channels.postgres.dsn`
   настроен — иначе `SessionColdSyncService` не запустится (см.
   `gateway.session_cold_sync.enabled=true` дефолт).

3. **Для multi-instance deploy**: `pg_try_advisory_xact_lock`
   автоматически делает leader-election; никакой внешней
   координации не требуется. Если хотите отключить sync на
   конкретной реплике (по политике) — установите
   `gateway.session_cold_sync.enabled=false`.

**Rollback**: `git revert <commit-hash>` откатывает все изменения;
PG-таблицы `agent_session_meta` / `agent_session_messages`
остаются нетронутыми (sync-сервис только зеркалирует upstream,
не удаляет PG-таблицу). SQLite-файлы
`<get_runtime_subdir("usage")>/usage.db` также остаются
(additive, не destructive).

Если что-то пошло совсем не так (например, observer-pipeline ломает
LLM-вызовы в production):

1. `git revert <commit-hash>`;
2. перезапустить gateway;
3. LLM-вызовы восстановятся, `LLMUsageStore` отключится
   (без observer нет записей);
4. сессии останутся в JSONL (upstream hot path не трогали).

---

## v2.5.2 → v2.5.3 — Профили конфигурации (prod / test)

⚠️ **Breaking change** в порядке запуска: `python gateway.py` без флагов
больше **не стартует** — падает с `ConfigurationError` и `exit 2`. Профиль
обязателен и передаётся только CLI-флагом `--profile` (whitelist: `prod` /
`test`). Env-передача профиля не читается runtime-кодом: **ни одна**
переменная окружения не участвует в выборе профиля.

**Автоматические изменения** (ничего делать не нужно):

- Добавлен `ConfigurationResolver` в `config.py` (см. [docs/PROFILES.md](PROFILES.md)).
- Добавлен файл `profiles/test.jsonc` (в репозитории) — оверлей для test-режима.
- Добавлен `--profile` CLI-флаг в `gateway.py`.
- Баннер теперь содержит `profile=<mode>` (`profile=test` или `profile=prod`).
- Удалён параметр `session_manager_json` из `SessionStorageService.__init__()` — теперь override из `session_manager.json` применяется централизованно в `ConfigurationResolver`.

**Ручные действия** (ОБЯЗАТЕЛЬНО для prod-деплоев):

1. **Явно указать профиль в проде.** Передайте `--profile` в точке входа:

   ```bash
   command: python gateway.py --profile=prod
   ```

   Передача профиля через env runtime-кодом **не читается** —
   деплой с ней завершится с `exit 2`.

2. **Проверить баннер.** При старте в терминале должно быть:
   `Starting nanobot gateway · project v… · profile=prod...`
   Если видите `profile=test` в проде — это ошибка деплоя, алертите.

3. **Проверить наличие `profiles/test.jsonc`.** Должен быть в репозитории.
   Без него `python gateway.py --profile=test` выбросит `ConfigurationError`.

**Что НЕ изменилось:**

- DSN, read-only данные (домен скилла, vector-storage, реестры) намеренно общие между prod и test.
- Архитектура: ниже `ConfigurationResolver` runtime-код не знает о режиме (`if profile == "test"` в бизнес-логике — баг, а не фича).

**Подробности**: [docs/PROFILES.md](PROFILES.md).

---

## v2.3.1 → v2.4.0

**Автоматические изменения** (ничего делать не нужно):

- v2.4.0 — MINOR поверх v2.3.1, обратно совместим.
- `agent_worker_claims` — новая таблица (создаётся автоматически миграцией схемы).
- `metadata.context_window` — новое поле в финальном outbound; UI рисует прогресс-бар.
- `gateway.print_llm_calls`, `gateway.print_worker_activity`, `gateway.print_db_activity` —
  новые опциональные ключи `project.json` (`false` по умолчанию).

**Новые ключи `project.json`** (опциональны, дефолты в коде):

| Ключ | Дефолт | Смысл |
|---|---|---|
| `channels.postgres.claim_strategy` | `"single"` | `"single"` (как v2.3.1) или `"worker_pool"` (мульти-машинный пул) |
| `channels.postgres.unstick_interval` | `max(60, processing_timeout/5)` | Интервал фонового unstick в single-режиме |
| `gateway.compact.enabled` | `true` | Ручное/авто-сжатие контекста |
| `gateway.compact.notify_in_history` | `true` | Писать `.compact-notice` в `agent_conversation_messages` |
| `gateway.compact.print_to_terminal` | `false` | Печатать отчёт о сжатии в терминал gateway |
| `gateway.print_llm_calls` | `false` | Токены LLM в терминал gateway (CLI — всегда вкл.) |
| `gateway.print_worker_activity` | `false` | Активность воркеров в терминал |
| `gateway.print_db_activity` | `false` | Активность db-job'ов в терминал |
| `gateway.vector.index.storage_table` | `oarb.audit_vectors` | Единая PG-таблица-хранилище сырых эмбеддингов. Регистрируется через `TableRegistry.register_infra("vector.storage", ...)` |
| `gateway.vector.index.indexes.*` | `{}` | Конфиг vector-индексов (`VectorIndexConfig` per name; см. CHANGELOG → Resource Model Refactoring). PG-реестр `public.agent_vector_index_config` больше не читается кодом. |
| `cli.show_context_window` | `true` | Метка занятости контекстного окна в CLI |
| `streamlit.enabled` | не задано (`None`) — отключено по умолчанию | Гейт запуска Streamlit-UI на :8501 (явное `true` включает) |

**Удалённые ключи**:

- `streamlit.failed_window_sec` → `streamlit.error_window_sec`
  (теперь окно повтора `error`-задач, а не `failed`).
- `gateway.vector_index.cache_tables` — удалён (был мёртв: никем не читался).
- `gateway.vector_index.storage_tables` (list) → `gateway.vector.index.storage_table` (str)
  — единая общая storage-таблица для runtime'а. Если у вас был список с одной
  таблицей (`["oarb.audit_vectors"]`), замените на строку (`"oarb.audit_vectors"`).
- `gateway.vector_index.*` (legacy) → `gateway.vector.index.*`
  — секция переименована. Обратной совместимости нет (fail-fast через
  runtime-проверку в `register_vector_storage`).
- `gateway.vector.embedding` — удалена целиком. Параметры эмбеддера
  (`base_url`, `model`, `dimension`, `http_timeout_sec`, `retries`)
  захардкожены модульными константами `_EMBED_*` в
  `lib/services/cache_provider_impl.py`. Bearer-токен — из переменной
  окружения `EMBED_TOKEN` (env, не `project.json`).
- `skills.<name>.embedding` — удалена; embedding больше не параметризован по skill'у.
- `skills.<name>.vector_indexes[].source` — поле `source` больше не нужно.
  Source-таблица (`table`/`pk`/`content_columns`/`embedding_columns`/`track_column`/
  `chunk_size`/`chunk_overlap`/`metric`/`enabled`) для каждого индекса — в
  `gateway.vector.index.indexes.<name>` (`VectorIndexConfig`, `extra="forbid"`).
  PG-реестр `public.agent_vector_index_config` остаётся как legacy-артефакт
  (SQL-артефакты в `sql/vectors/create_vector_index_config.sql`,
  `sql/migrations/V002__vector_chunk_params.sql`,
  `sql/audit_analyzer/seed_default_indexes.sql`); код их не читает.

**Изменённые пути**:

- DuckDB-снапшот: `workspace/skills/audit_analyzer/cache/audit_cache.duckdb`
  → `workspace/data_store/duckdb/cache.duckdb` (публикуется gateway'ом,
  историческое поведение до v2.5.2).
  → **`~/.cache/nanobot/duckdb/cache.duckdb`** (v2.5.2+, default —
  безопасная локальная ФС, см. `gateway.cache.local_path` для override).
  Старое поле `project.json:in_memory_cache_path` больше не читается.

**Изменённое поведение**:

- `patch_save_turn` теперь **сохраняет большие результаты tool'ов полным файлом**
  в `data_store` (через `SessionFileStore`), а не режет до 16K символов в истории.
- `patch_exec_limits` поднял дефолтные потолки `exec`/`shell`.
- `patch_tool_limits` поднял потолки `read_file`/`grep`/`list_dir`.
- Новые кастомные tool'ы из `workspace/tools/*.py` (патч `patch_project_tools`):
  `compact_context`. Дополнительные `audit_run_predefined_script` и
  `audit_search_vector` появились и были удалены в этом же релизе
  (см. [docs/skill-tool-inventory.md](skill-tool-inventory.md)).
- `ApplicationContext.create()` теперь автоматически подключает
  `SessionFileRedirectHook` и фреймворковые хуки из `lib/hooks/`.
- `lib/services/table_registry.py::TableRegistry.set_embedding_config` /
  `embedding_config()` удалены; `lib/core/skill_registration.py::register_embedding_config`
  удалён. Регистрация embedding-конфига больше не нужна.

**Миграции**:

- `V002__vector_chunk_params.sql` добавляет в
  `public.agent_vector_index_config` колонки `chunk_size`/`chunk_overlap`/`metric`
  (дефолты `500`/`80`/`cosine`). DDL применяется через
  `python tools/migrate.py --apply`.
- **⚠️ После этого релиза код `public.agent_vector_index_config` НЕ читает.**
  Таблица остаётся в репозитории как legacy-артефакт (для старых миграций
  и исторических ссылок), но новый конфиг — в `project.json`.
  При первоначальной настройке проекта перенесите seed-данные из
  `sql/audit_analyzer/seed_default_indexes.sql` в секцию
  `gateway.vector.index.indexes` (формат см. `VectorIndexConfig`).

## v2.3.0 → v2.3.1

- PATCH-релиз. Полностью обратно совместим.
- Хуки переехали: фреймворковые — в `lib/hooks/`, плагины — в `workspace/hooks/`.
- `office_files` skill — чтение docx/xlsx/xls/pdf/pptx/csv/txt.

## v2.0.0 → v2.3.0

**breaking changes v2.0.0**:

- Конфигурация векторных индексов переехала из файлов `.faiss` и `project.json`
  в таблицу `public.agent_vector_index_config` (управление через SQL).
- DSN задаётся единым `channels.postgres.dsn` (обычно `"${DATABASE_URL}"` из `.secrets.env`, резолвится через `utils.db.resolve_dsn()`). Частичные ключи `host`/`port`/`user`/`dbname` не поддерживаются.
- Все таблицы логов и сессий получили префикс `agent_` (`agent_gateway_logs`,
  `agent_conversation_messages`, `agent_worker_claims`).
- Имя LLM-провайдера — каноническое `LLM_API_KEY` (вместо `MISTRAL_API_KEY`).

---

## До v1.5.0

Legacy-мигратор файлов `.faiss` удалён. Если у вас остались артефакты v1.x —
обращайтесь к [../CHANGELOG.md](../CHANGELOG.md) → соответствующая версия.

---

## Ручные действия миграции 1.5.0 → 2.0.0

Краткий таймлайн релизов — в [../CHANGELOG.md](../CHANGELOG.md). Этот раздел — только то, что **требует ручных действий при миграции**.

### Миграция 1.5.0 → 2.0.0

**Конфигурация:**

| Изменение | Действие |
|-----------|----------|
| `.env` → `project.json` + `.secrets.env` | Скопировать секции `channels.*`, `skills.*`, `cli`, `benchmark`, `streamlit`, `gateway` в `project.json` (JSONC). Секреты — в `.secrets.env` с провайдер-скоупинг форматом |
| Провайдерские ключи больше не через `export` | Секция `# providers: llm` с `api_key=...` в `.secrets.env`. `ConfigService._pre_resolve_env_refs` подставит в `os.environ` автоматически (env-переменная — каноническая `LLM_API_KEY`) |
| `vector_indexes` / `mode_vector_index_path` в `config.json` | Удалить; теперь в `public.agent_vector_index_config` (см. [docs/VECTOR_INDEXES.md](VECTOR_INDEXES.md)) |
| DuckDB-кеш audit_analyzer | CLI запускал загрузку | gateway-only — CLI читает готовый снимок |
| `data-analyzer`, `html_presentation_generator` | Удалены. Убрать из импортов и `config.json` |
| `pg_agent_worker.py` | Удалён. Использовать `streamlit_app.py` + `PostgresChannel` |

**Код (если вы форкали):**

| Что | Изменение |
|-----|-----------|
| `gateway.py` | Было 696 строк, стало 132. Вся инициализация — в `lib/core/ApplicationContext`. Свой код инициализации → переносить в `ApplicationContext.create()` или в новый сервис в `lib/services/` |
| `cli_agent.py` | Было 865 строк, стало 165. То же самое |
| `RuntimePatcher` | Оба monkey-patch'а (`ContextGovernor.normalize_tool_result`, `agent._assemble_outbound`) теперь в `lib/services/runtime_patcher.py` с fallback при изменении API nanobot |
| `DbLoggingService` | Новый. Если раньше логировали вызовы иначе — мигрировать на `lib/services/db_logging_service.py` + `lib/hooks/database_logging_hook.py` |
| Хуки | `lib/hooks/database_logging_hook.py` встроен в `AgentLoop` через `AgentFactory`: общий инстанс заменён на per-turn фабрику `hook_factories=` (см. `database_logging_hook.py:make_db_logging_hook_factory`) |

**Данные:**

> Имена таблиц ниже — значения текущей инсталляции, настраиваемые в `project.json`
> (`channels.postgres.table_name`/`messages_table`/`meta_table`/`claims_table`,
> `logging.db.table_name`/`question_runs_table`, `benchmark.runs_table`/`results_table`,
> `gateway.vector.index.storage_table`/`config_table`/`signature_table`). В других
> развёртываниях они могут отличаться.

- **Сессии** (`public.agent_session_meta`, `public.agent_session_messages`) —
  схема та же. DDL: `sql/session/create_public_agent_session_meta.sql`,
  `sql/session/create_public_agent_session_messages.sql`.
- **Канал** (`public.agent_conversation_messages`) — без миграции (имя уже актуально).
  DDL: `sql/channels/create_public_agent_conversation_messages.sql`.
- **DuckDB-снимок** — gateway пересоздаст автоматически (in-memory → новый snapshot); путь вычисляется через единый `resolve_publish_path()` (`lib/core/application_context.py`) — default `~/.cache/nanobot/duckdb/cache.duckdb`, override через `gateway.cache.local_path`. Legacy `<workspace>/data_store/duckdb/cache.duckdb` больше не выбирается ни через какой knob (escape hatch `use_workspace_path` удалён в v2.5.2).
- **Векторные индексы** (`oarb.audit_vectors`, `public.agent_vector_index_store`,
  `public.agent_vector_index_config`) — без миграции (1.5.0 уже хранил их в БД);
  DDL в `sql/audit_analyzer/`.
- **Бенчмарки** (`public.agent_benchmark_runs`, `public.agent_benchmark_results`) — без миграции;
  DDL в `sql/benchmarks/`.
- **`agent_gateway_logs` / `agent_question_runs`** — новые таблицы:
  `sql/logs/create_public_agent_gateway_logs.sql`,
  `sql/logs/create_public_agent_question_runs.sql`.

**Что НЕ изменилось:**

- API точек входа: `python gateway.py --profile=<prod|test>`
  (флаг `--profile` обязателен) и `python cli_agent.py -P`
  (CLI — фиксированный профиль `test`, флаг не принимается).
- Имена таблиц БД.
- `benchmarks/items/*.yaml` — формат совместим.
- `audit_analyzer` режимы `predefined` / `sql` / `vector`.
- Параметры CLI `audit_analyzer` (`--top-k`, `--threshold`, `--index-name`).

Краткий таймлайн релизов — в [../CHANGELOG.md](../CHANGELOG.md).

---

