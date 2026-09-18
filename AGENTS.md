# Agent Instructions

Этот файл — инструкции для opencode-ассистента, работающего с кодовой базой проекта.
Для инструкций **нано-агенту** (запускаемому через `nanobot gateway` / `cli_agent.py`) см. `workspace/AGENTS.md`.

## Project Layout

- `lib/` — кастомные сервисы поверх библиотеки `nanobot 0.3.0` (core/cli/lifecycle/services/session/channels/utils + hooks).
  - `lib/hooks/` — фреймворковые хуки (общий каркас `base_tool_tracking`, `tool_audit`, `database_logging` + `terminal_tool_print`), провязываются явно через `AgentFactory`/`ApplicationContext`.
  - `lib/core/agent_factory.py` — фабрика AgentLoop с хуками; `lib/core/bus_factory.py` — создание MessageBus.
  - `lib/core/application_context.py` — точка сборки сервисов; `ctx.start()` / `ctx.stop()` — lifecycle.
  - `lib/cli/console_loop.py` — REPL/typewriter для CLI-агента.
  - `lib/services/channel_factory.py` — фабрика каналов (Postgres/Redis).
  - `lib/services/duckdb_cache_store.py` — DuckDB-кеш + FAISS для всех skills (публикуется в `workspace/data_store/duckdb/cache.duckdb`); был `audit_memory_store.py`, переименован в Фазе 6 Resource Model Refactoring.
  - `lib/services/pg_duckdb_sync_service.py` — фоновый worker-тред инкрементальной синхронизации PG → DuckDB (был `audit_sync_service.py`, переименован в Фазе 6).
  - `lib/services/table_registry.py` — pluggable-реестр ресурсов с двумя namespace'ами: skill-ресурсы (`register(SkillRegistration)` для `TableResource`/`VectorResource`) и инфра-ресурсы (`register_infra(key, resources)` для runtime-storage общего назначения, например `oarb.audit_vectors`). Агрегаторы (`table_names`, `vector_names`, `resources`, `tracking_column_for`) объединяют оба namespace'а; `resources_by_label` смотрит только skills. Per-resource `label` — opaque marker для Skill-логики; runtime-sync его игнорирует. Track-колонки: per-resource `TableResource.tracking_column`, дефолты `updated_at`/`id`. Подробности в `docs/table-registry.md`.
  - `lib/core/skill_registration.py` — утилита декларативной регистрации skill'ов из `project.json::skills.<name>` (делегирует в `TableRegistry.register`). Используется `ApplicationContext._auto_register_skills` и standalone-утилитами (`tools/build_vectors.py`).
  - `lib/core/skill_config.py` — параметризованный runtime API для skill'ов (`get_db_tables(skill_name)`, `get_llm_config(skill_name)`, `get_in_memory_cache_path(skill_root)`, `get_vector_*` и т.д.). `get_embedding_config()` / `get_embedding_model()` — без `skill_name` (embedding — общий runtime). Единая точка для всех skill'ов — никакой копипасты полноценного `skill_config.py` в каждом skill'е. У `audit_analyzer` есть `scripts/skill_config.py` — тонкая обёртка (только реально используемые функции, делегирует в `lib.core.skill_config` с фиксированным `_SKILL_NAME`).
  - `lib/core/infra_registration.py` — регистрация инфраструктурных ресурсов runtime'а (`register_vector_storage` читает `gateway.vector.index.storage_table` → `TableRegistry.register_infra("vector.storage", ...)`). Делегируется из `ApplicationContext._register_infra_resources` и из standalone-режима `tools/build_vectors.py`.
  - `lib/services/context_compaction.py` — `ContextCompactionService`: единая точка записи факта сжатия контекста. Входы: настоящая slash-команда `/compact` (`lib/commands/compact_command.py`, регистрация `RuntimePatcher.patch_compact_command`), CLI `/compact` (`console_loop`), tool `compact_context` (`workspace/tools/`), авто-сжатие nanobot (обёртки `runtime_patcher`) → один путь `_notify` → (1) `_write_history_notice` в `agent_conversation_messages` (видно в UI, но не в контексте промпта) и (2) событие `context_compacted` в долговечный `agent_gateway_logs` (через `workspace/utils/event_log.py`), доступное агенту после сжатия через tool `history_search`. Ручные пути = `force=True` (жёстко, игнор порога токенов); при падении `estimate_session_prompt_tokens` — `_estimate_fallback` по символам. Подробности в `docs/ARCHITECTURE.md` § «Управление сжатием контекста».
  - `lib/services/consolidator_locale.py` — переопределение системных шаблонов nanobot из `workspace/overrides/` (monkeypatch Jinja2-loader'а `prompt_templates._environment`: `ChoiceLoader` с приоритетом override-каталога; применяется в `ApplicationContext.start()`, идемпотентно).
  - `lib/services/runtime_patcher.py` — каталог monkey-patch'ей nanobot (exec/tool limits, save_turn, compaction tracking, project tools, compact command и т.д.); применяется через `apply_all`. Полный каталог — `docs/architecture/runtime-patcher-inventory.md`.
  - `lib/services/runtime_health.py` — RuntimeHealth/RuntimeReadiness (READY/DEGRADED/NOT_READY по компонентам); `gateway /health`.
  - `lib/services/db_logging_service.py` + `lib/services/db_logging_bus.py` — `DbLoggingService`: пул-воркер записи событий в `agent_gateway_logs` / `agent_question_runs`, purge по `logging.db.retention_days`.
  - `lib/services/llm_client.py` + `lib/services/llm_config.py` — LLM-клиент/резолв конфига для skill'ов (`call_llm`/`call_llm_async`).
  - `lib/services/vector_index_service.py` — `VectorIndexBuildService`: инкрементальная сборка FAISS-индексов (вызывается из `tools/build_vectors.py`).
  - `lib/services/cache_provider.py` + `cache_provider_impl.py` — `CacheProvider` (protocol) и DuckDB/FAISS-реализация (query_sql/get_schema/explain/search_vector).
  - `lib/services/transcription_service.py` — транскрибация аудио; `lib/services/text_splitter.py` — чанкинг текста; `lib/services/preload_service.py` — прогрев кеша/индексов при старте; `lib/services/session_storage.py` — хранение сессий; `lib/services/subprocess_manager.py` — управление субпроцессами; `lib/services/config_service.py` — резолв `${VAR}` в конфиге.
  - `lib/lifecycle/gateway_runner.py` + `lib/lifecycle/shutdown_coordinator.py` — запуск gateway и graceful shutdown.
  - `lib/session/pg_session_manager.py` — PG-backed менеджер сессий (PGSessionManager, JSONB metadata).
  - `lib/cli/display_config.py` — настройки вывода CLI; `lib/cli/hook_loader.py` — авто-сканирование `workspace/hooks/`.
  - `lib/channels/redis_channel.py` — Redis-канал; `lib/channels/message_exchange.py` — общий формат сообщений каналов.
  - `lib/tools/compact_context_tool.py` — удалён (после переноса в `workspace/tools/`). Теперь tool живёт в `workspace/tools/compact_context.py` и регистрируется через `RuntimePatcher.patch_project_tools` (стандартный путь).
  - `lib/commands/compact_command.py` — slash-команда `/compact` (детерминированное сжатие до LLM). Регистрируется через `RuntimePatcher.patch_compact_command` в `agent.commands` (`CommandRouter`). Shortcut-команды минуют `_assemble_outbound`, поэтому handler обязан ставить `FINAL_TURN_KEY="_final_turn"` в outbound — иначе постгресс-канал не финализирует оборот и задача зависает в `processing`.
- `lib/channels/postgres_channel.py` — канал с мульти-машинным пулом воркеров: аренда задач через таблицу `agent_worker_claims` (UNIQUE PK — arbiter эксклюзивности), lease/heartbeat, reclaim+heal, статусы `error`/`failed`.
- `lib/core/project_settings.py` — pydantic-валидация merged SETTINGS (`ProjectSettings`); вызывается в `ApplicationContext.create()` (fail-fast на типы/значения, `ConfigurationError` со списком всех проблем; неизвестные ключи разрешены).
- `lib/utils/sql_safety.py` — SQL Security Guard: AST-политика read-only SQL на sqlglot (`validate_sql` контракт None|str сохранён; запрет SELECT INTO/опасных функций/системных каталогов/multi-statement; `validate_sql_report` для audit trail). Подробности: docs/DATABASE.md § «Инфраструктурные границы P0».
  - `lib/utils/text_utils.py` — общие утилиты для подготовки текста (sanitize_value, truncate_middle); единственный источник для tool'ов и skill'ов (бывший `_sanitize_value` из `audit_analyzer/scripts/output.py` удалён, оставлен back-compat re-export).
  - `lib/utils/table_utils.py` — `normalize_table_names`: канонизация списков таблиц из project.json (`[[schema, table]]` / `[schema, table]` / `"schema.table"` → `(schema, table)`); используется в `_make_sync_services` и `tools/build_vectors.py` до дедупликации.
  - `lib/utils/project_version.py` — единое чтение `project.json::project.version` (актуальный релизный тег без `v`); используется баннером `gateway.py` при старте. Git-теги отстают из-за release-веток (см. Release Process).
  - `lib/utils/outbound_meta.py` — фильтрация служебных outbound (`OUTBOUND_DROPPED_KEYS`, `FINAL_TURN_KEY`, `is_dropped`/`is_stream_delta`/`is_outbound_noise`); используется каналами.
  - `lib/utils/duckdb_query.py` — низкоуровневые утилиты выполнения SQL в DuckDB; `lib/utils/retry.py` — retry-декоратор; `lib/utils/node_access.py` — доступ к именованным нодам `__nanobot_meta`; `lib/utils/logging_utils.py` — утилиты логирования.
- `workspace/` — кастомное окружение нано-агента: `hooks/`, `tools/`, `utils/`, `skills/` (`audit_analyzer`, `office_files`, `legal_summarizer`), `memory/`, `cron/`, `prompts/`, `overrides/` (переопределения системных шаблонов nanobot; сейчас — `agent/consolidator_archive.md`, подкладывается через `lib/services/consolidator_locale.py`), `*.md` (`AGENTS.md`, `HEARTBEAT.md`, `SOUL.md`, `USER.md`). `workspace/hooks/` — самодостаточные плагины-хуки (контракт `cls(workspace_dir=...)`), подхватываются auto-scan'ом: `session_file_redirect_hook.py` (перенаправление файлов сессии, см. File Storage Policy), `active_files_hook.py` (side-channel активных файлов через `session.metadata`), `recent_files_hook.py` (авто-прикрепление созданных файлов к `OutboundMessage.media`); фреймворковые хуки живут в `lib/hooks/`. `workspace/utils/` — утилиты workspace: `db.py` (пул соединений, `resolve_dsn`, `get_stats`), `media.py` (сериализация media), `jsonb.py` (JSONB-декодер), `event_log.py` (долговечный журнал в `agent_gateway_logs`), `session_file_store.py`, `session_key.py` (`safe_session_key`), `clean_text.py`, `office_files.py` (извлечение текста из DOCX/XLSX/PDF/PPTX), `structure_cache.py`. `workspace/tools/` — кастомные tool'ы (auto-discover через `RuntimePatcher.patch_project_tools`; каждый tool — наследник `nanobot.agent.tools.base.Tool` с `config_key`/`config_cls`/`enabled`/`create`). **Важно:** `ctx.config` — это pydantic `ToolsConfig` из nanobot, которая знает только встроенные подсекции (`web`/`exec`/`file`/...) и отбрасывает неизвестные. Свои настройки читать через `ctx._settings_ref.tools.<config_key>` (или `gateway.<config_key>` для исторических секций). Содержит: `compact_context.py` (ручное сжатие, читает `gateway.compact.*`), `history_search_tool.py` (generic-поиск по истории `agent_gateway_logs`, читает `tools.history_search.*`), `legal_summarizer_query.py` (вопрос-ответ по пакетам документов legal_summarizer), `example.py` (шаблон с правильным паттерном). Tools `duckdb_query`/`vector_search` удалены в фазе 8 — доступ к `audit_analyzer` через `scripts/cli.py --mode predefined`. Документация по кастомным tool'ам (инструкция агента: когда вызывать, какие `event_type`/`параметры`) — в `workspace/TOOLS.md`. Секцию «Vector-инфраструктура» ниже.
- `benchmarks/` — подсистема бенчмарков (runner, evaluator, scorer, reporter).
- `tools/` — утилиты (`build_vectors.py`, `generate_predefined_scripts_sql.py`, `generate_comments_sql.py`, `check_worker_pool_integrity.py`, `migrate.py` — runner миграций схемы, `scan_nanobot_inventory.py` — сканер зависимостей от nanobot, `architecture_guard.py` — проверка архитектурных invariant'ов, `legal_benchmark.py` — бенчмарк legal_summarizer, `legacy_audit.py`/`test_audit.py` — служебные, `extract_office_structure.py` — отчёт по структуре office-файлов).
- `sql/` — DDL (каналы, сессии, логи, бенчмарки, audit_analyzer, векторы, воркеры); `sql/migrations/` — версионные миграции схемы (`schema_migrations` tracking-таблица, применяется через `python tools/migrate.py --apply`; см. `sql/README.md`).
- `tests/` — pytest (включая `tests/integration/` и `tests/contract/` — контрактные тесты поверхности nanobot 0.3.0 для upgrade-readiness, CI job `upgrade-readiness` в `.github/workflows/ci.yml`; `asyncio_mode = "auto"`, `pythonpath = ["."]`).
- `docs/` — каталог дополнительной документации (README.md — навигационный хаб, на который ссылается корневой `README.md`):
  - `docs/architecture/` — инвентаризация зависимостей (`nanobot-inventory.md`/`nanobot-inventory.json`) и monkey-patch'ей (`runtime-patcher-inventory.md`).
  - `docs/skill-tool-architecture.md` — контракт Skill ↔ Tool (что разрешено/запрещено, decision procedure в `SKILL.md`).
  - `docs/skill-tool-inventory.md` — текущее состояние всех skill/tool и история удалённых.
  - `docs/table-registry.md` — реестр таблиц PG → DuckDB, sync-контроль, track-колонки.
  - `docs/architecture/runtime-patcher-inventory.md` — каталог monkey-patch'ей с target/risk/тестами.
  - `docs/TROUBLESHOOTING.md` — диагностический runbook (типовые ошибки и решения).
  - `docs/MIGRATION.md` — сводка изменений между релизами + breaking changes.
  - `docs/refactor_baseline.md` — wip-заметки рефакторингов (например, `refactor/skills-tools-cleanup`).
- `config.py` + `config.json` + `project.json` + `.secrets.env` — иерархия конфига
  (порядок мержа: `project.json` → `config.json` → `.secrets.env`; секреты через `${VAR}`).
- Точки входа: `cli_agent.py` (REPL), `gateway.py` (HTTP-сервер), `streamlit_app.py` (UI на :8501).

## File Storage Policy

- Новые файлы, создаваемые в рамках сессии, сохраняй под `workspace/data_store/cache/sessions/<session_key>/`
  (политика `workspace/AGENTS.md`). Не пиши напрямую в корень проекта.
- Кэш документов legal_summarizer: `workspace/data_store/cache/sessions/<safe_session_key>/documents/<document_id>/`
  (создаётся через `legal_summarizer.scripts.cache.manifest`; детали — в `workspace/skills/legal_summarizer/references/architecture.md`).
  Удаляется вместе с папкой сессии — никакого отдельного cleanup-механизма не нужно. `safe_session_key` — через `workspace.utils.session_key.safe_session_key` (конвенция согласована с `SessionFileRedirectHook` и `SessionFileStore`).
- Для редактирования существующих файлов (`AGENTS.md`, `lib/`, `*.py`) — обычные `edit_file` / `apply_patch`.
- **Не используй `>`, `>>` в `exec` для создания файлов** — `session_file_redirect_hook` их не перехватывает.

## Configuration

- Настройки нано-агента (`channels.*`, `skills.*`, `cli`, `benchmark`, `streamlit`, `gateway`, `logging.db`) — в `project.json` (JSONC, с комментариями).
- Очистка журнала событий `agent_gateway_logs` / `agent_question_runs`: подсекция `logging.db` (`logging.db.enabled` — вкл/выкл записи):
  - `logging.db.retention_days` (целое, дефолт `90`) — возраст в днях, старше которого события и question_runs удаляются фоновым пулом `DbLoggingService` (через `NOW() - (N || ' days')::interval`, совместимо с Greenplum 6.5). `0` или отсутствие — авто-удаление по возрасту выключено (события хранятся вечно).
  - `logging.db.purge_interval_sec` (дефолт `3600.0`) — интервал периодической очистки в worker-цикле `DbLoggingService`.
  - Любые пустые `outbound_final`/`outbound_delta` (пустой `content` и нет `media` — stream-чанки/синтетические финалы) удаляются ВСЕГДА при каждой итерации очистки, независимо от `retention_days`. Реализация: `DbLoggingService.purge_empty_outbound` / `purge_old` (`lib/services/db_logging_service.py`).
  - `logging.db.flush_interval_sec` (float, дефолт `5.0`, диапазон `0.5 ≤ value ≤ 60.0`) — интервал flush'а батча worker-потоком `DbLoggingService` (секунды). Уменьшение ускоряет видимость событий в БД (полезно для отладки/диагностики), увеличение снижает нагрузку на БД при burst-трафике. Тип и диапазон валидируются через `LoggingDbSettings.flush_interval_sec` в `lib/core/project_settings.py`; вне диапазона — `pydantic.ValidationError` на старте `ApplicationContext.create`.
- Настройки nanobot (агенты, провайдеры, API) — в `config.json`.
- Секреты (API-ключи, `DATABASE_URL`) — в `.secrets.env` через `${VAR}`.
- Читай в коде через `get_setting(*keys, default=...)` или `SETTINGS.*` из `config.py`.
- При добавлении новой обязательной настройки — добавь запись в `REQUIRED_KEYS` в `tests/test_config_keys.py`.
- Версия проекта (баннер gateway): `project.version` в `project.json` (актуальный релизный тег без префикса `v`; git-теги и первый релизный блок CHANGELOG на `master` отстают от актуального тега из-за release-веток — см. Release Process). Читается через `lib/utils/project_version.py`.
- Пул воркеров: `channels.postgres.{worker_id, claims_table, lease_interval, error_retry_delay, table_name, messages_table, meta_table}` (мульти-машинная аренда задач; `table_name`/`messages_table`/`meta_table` — настраиваемые имена таблиц канала/сессий); `streamlit.error_window_sec` — окно повтора `error`-задач.
- Режим аренды задач: `channels.postgres.claim_strategy` (`"single"` дефолт | `"worker_pool"`). `single` — захват задачи через `UPDATE ... RETURNING` без таблицы `agent_worker_claims` (как в v2.3.1; для одиночного инстанса). `worker_pool` — захват через `INSERT INTO agent_worker_claims` + lease/heartbeat (для мульти-машинного деплоя). При `single` обращений к `agent_worker_claims` физически нет; `_unstick_processing` для защиты от зависших задач выполняется фоновой задачей с интервалом `channels.postgres.unstick_interval` (дефолт `max(60, processing_timeout/5)` = 120 сек).
- Порог извлечения текста документа в user-промпт: `channels.document_text_threshold` (общий для всех каналов — Postgres/Redis/websocket/streamlit, дефолт `20000` символов извлечённого текста). **Единый механизм**: каналы передают агенту только пути к файлам, текстовое представление документа формирует `nanobot.utils.document.extract_documents` (обёрнут патчем `RuntimePatcher.patch_document_text_threshold`). Унифицированный формат каждого файлового блока: `[File: <basename> (saved at <path>)]\n<text>` (маленький) или `[File: <basename> (saved at <path>)]\n[text omitted (len=… > threshold=…)]` (большой). Путь к файлу присутствует **всегда** — агент в любом случае знает, куда передать файл (skill/`read_file`/`exec`); каналы НЕ дописывают собственных хинтов `[Attachment: … (saved at …)]`, чтобы не дублировать. Действует для всех каналов и subagent-сообщений единообразно. `0` или отсутствие ключа в pydantic — патч пропускается (NO-OP).
- Вывод токенов LLM-итераций в терминал gateway: `gateway.print_llm_calls` (опционально, `false` по умолчанию; CLI включает всегда через `cli_agent.py`).
- Активность пула воркеров в терминал gateway (взял задачу / закончил / размер очереди): `gateway.print_worker_activity` (опционально, `false` по умолчанию).
- Активность db-worker пула соединений в терминал gateway (взял/закончил job с тегом вызывающего): `gateway.print_db_activity` (опционально, `false` по умолчанию).
- Прогрев/проверка пула соединений при старте gateway: `probe_connections` в `workspace/utils/db.py` (вызывается `gateway.py` на старте; метки db-job'ов через `_caller_tag`/`Job.tag`).
- Гейт Streamlit-UI на :8501: `streamlit.enabled` (опционально; по умолчанию не задано (`None`) и трактуется как отключено — явное `true` запускает subprocess и стриминг).
- Потолки вывода инструментов: `gateway.tool_result_limits.*` (опционально; дефолты в `lib/services/runtime_patcher.py` — см. `patch_exec_limits`/`patch_tool_limits`/`patch_save_turn`).
- Ручное и автоматическое сжатие контекста: `gateway.compact.*` (`enabled`, `notify_in_history`, `print_to_terminal`; все опциональны, дефолт `true`/`true`/`false`). Один и тот же сервис `ContextCompactionService` используется: (1) tool'ом `compact_context` (gateway) — вызывается агентом или пользователем; (2) CLI-командой `/compact` (CLI) — перехват в `console_loop.py`; (3) авто-сжатием nanobot — обёртки `runtime_patcher.patch_compaction_tracking` вокруг `AutoCompact._archive` (idle) и `Consolidator.maybe_consolidate_by_tokens` (token-budget). Все три пути пишут служебную заметку в `agent_conversation_messages` (`metadata.kind="context_compact"`, `role='assistant'`, `status='completed'`) одним и тем же методом `_notify`. Streamlit рисует её стилем `.compact-notice`. Заметка видна в истории диалога, но НЕ попадает в контекст промпта (он строится из `PGSessionManager`). Поведение самого сжатия (порог токенов, idle-таймаут) управляется ключами nanobot `consolidationRatio` (дефолт `0.5`) и `idleCompactAfterMinutes` в `config.json` (см. `nanobot/config/schema.py:151-163`); в этом проекте `idleCompactAfterMinutes: 0` — auto-compact idle выключен, активен только token-budget.
- Vector-инфраструктура: `gateway.vector.*` — общий runtime (эмбеддинги + FAISS-индексы), **не привязана к домену skill'а**. Секции:
  - Эмбеддинг-параметры **захардкожены** в `lib/services/cache_provider_impl.py` (`_EMBED_*`: base_url `http://localhost:11434/api/embed`, модель `mxbai-embed-large:latest`, dimension `1024`, http_timeout `60.0`, retries `3`). Bearer-токен для эмбеддеров за reverse proxy берётся из переменной окружения OS **`EMBED_TOKEN`** (`os.environ.get("EMBED_TOKEN")`, соответствует `cache_provider_impl_read_embedding_config().auth_token`); если не задан — запросы без `Authorization`. Секции `gateway.vector.embedding` в `project.json` **больше нет** (удалена, а не legacy).
  - `index.*` (`VectorIndexSettings`): `enable` (гейт), `storage_table` (PG-таблица-хранилище эмбеддингов, формат `schema.table`, напр. `oarb.audit_vectors`), `default_root` (корневая папка FAISS-индексов; путь к индексу = `<default_root>/<index_name>`), `backend` (runtime-бэкенд, `"faiss"` по умолчанию), **`indexes.*`** — декларативный реестр индексов (см. ниже). `storage_table` регистрируется через `lib.core.infra_registration.register_vector_storage` → `TableRegistry.register_infra("vector.storage", ...)` → попадает в DuckDB-кэш через `PgDuckDbSyncService`.
  - `gateway.vector.index.indexes.<name>` (`VectorIndexConfig`, `extra="forbid"`): `table` (source-таблица из PG), `pk`, `source_table` (PG-таблица, чьи данные кэшируются в `table`), `content_columns` (строки для plaintext-поиска), `embedding_columns` (строки `col` или объекты `{column, chunk, chunk_size, chunk_overlap}`), `track_column`, `chunk_size` / `chunk_overlap`, `metric`, `enabled`. **Это единственный источник конфигурации индексов** — PG-реестр `public.agent_vector_index_config` больше НЕ читается кодом (оставлен как legacy-артефакт SQL, см. `docs/VECTOR_INDEXES.md`). Читается через `lib.services.cache_provider_impl.read_vector_index_config({})`, сборка — `tools/build_vectors.py` (chunk/metric берутся из конфига индекса, fallback на `read_embedding_defaults()` = 500/80).
  - Все ключи опциональны; дефолты в `VectorIndexSettings` (для `embedding`/chunk-параметров — константы `_EMBED_*`/`_DEFAULT_CHUNK_*` в `cache_provider_impl`).
  - **Legacy `gateway.vector_index.*` УДАЛЁН.** Обратной совместимости нет: `register_vector_storage` не читает `gateway.vector_index.*`, оставление legacy-секции в `project.json` runtime-mute (fail-fast через runtime-проверку, не через Pydantic). Мигрируйте на `gateway.vector.index.*`.
- Метрика занятости контекстного окна: `metadata.context_window` (`{used, limit, pct, model}`) кладётся в финальный outbound патчем `RuntimePatcher.patch_assemble_outbound` (S1) и обновляется в processing-строке в фоне через `_flush_live_context` (T2). UI: Streamlit рисует `st.progress(pct)` (`streamlit_app._render_context_window`), CLI — однострочную метку (`lib.cli.console_loop._print_context_window`). Гейт: `cli.show_context_window` (`project.json`, дефолт `true`).
- Запуск команд `tools.exec` (окружение субпроцесса, PATH, `pathPrepend`/`allowedEnvKeys`, allow/deny-паттерны) — подробно в `docs/INTERNAL_API.md` (§ «Конфигурация `tools.exec`»).
- Generic infrastructure tools (`workspace/tools/`):
  - `tools.history_search.*` (`enable`, `max_rows`, `max_result_chars`) — tool `history_search`: generic-поиск по долговечному журналу `agent_gateway_logs` (переживает context compaction). Параметры: `query` (ILIKE по `summary`/`payload`), `event_type`, `since`/`until`, `session_scope` (`current`|`all`), `limit`. Ищет в т.ч. `context_compacted`, `tool_call`/`tool_result`, `run_finished`, `llm_call` — чтобы агент мог вернуть выпавшие из контекста детали. Только `%s`-параметры (без интерполяции). Конфиг читается из `ctx._settings_ref.tools.history_search`.
- Наблюдаемость: tool'ы покрываются штатным `lib/hooks/tool_audit_hook.py` (tool_call_id, session_id, duration_ms, status, error_type — docs/TARGET_ARCHITECTURE.md §26). Дополнительное логирование внутри tool'а не требуется.
- **Профили конфигурации (prod / test)**: см. [docs/PROFILES.md](docs/PROFILES.md). Ключевые факты:
  - **Единственный источник профиля — CLI-флаг `--profile`** в argv `application entrypoint`. `gateway.py`, `cli_agent.py` и `streamlit_app.py` БЕЗ `--profile` падают с `exit 2` (`ConfigurationError("--profile is required")`). Default-профиля нет.
  - **Whitelist закрытый: `{"prod", "test"}`.** Любое другое значение (`dev`, `staging`, `foo`) — `ConfigurationError` + exit 2. Введение третьего профиля — отдельный OpenSpec change.
  - Никакого env-based fallback не существует. Устаревшая переменная для передачи профиля (исторически — `NANOBOT_PROFILE`) **не читается runtime-кодом**. Деплои `docker-compose` / k8s / systemd / GitHub Actions должны передавать профиль через `command: python gateway.py --profile=prod`.
  - Application subprocess (`streamlit_app.py`, спавненный из `gateway.py`) получает профиль **через argv** (`--profile=<value>` из `SETTINGS["profile"]` родителя), не через env.
  - Lifecycle-gate: `SETTINGS` публикуется ТОЛЬКО через `config._initialize_settings(profile)`, вызванный из application entrypoint. `import config` — чистый import без side-effects. До явной инициализации доступ к `SETTINGS` поднимает `ConfigurationError`.
  - Режим существует **только во время разрешения конфигурации** — после получения `SETTINGS` исчезает из runtime-модели.
  - В runtime-коде **нет** `if profile == "test"` / `if profile == "prod"` — это баг, не фича.
  - При добавлении новой обязательной настройки — учтите, что она может пересечься с profile overlay; предпочтительно наследовать через `project.json` или вынести в `profiles/<mode>.jsonc`.
  - 6 profile-owned runtime-ключей (immutable после применения профиля): `channels.postgres.{table_name,messages_table,meta_table,claims_table}` + `logging.db.{table_name,question_runs_table}`. Любые другие ключи в `profiles/test.jsonc` → fail-fast `ConfigurationError`.

## Scheduled Reminders

- Перед планированием напоминаний проверь доступные skills и следуй их инструкциям.
- Используй встроенный `cron` tool opencode (не вызывай `nanobot cron` через `exec`).
- `USER_ID` и `CHANNEL` бери из текущей сессии.
- Cron-задачи выполняются как scheduled turns в origin-чате и обычно возвращают результат в этот канал.
  Для фоновых проверок, которые молчат, если нечего сообщить, — используй `HEARTBEAT.md`.

**Не пиши напоминания только в `MEMORY.md`** — это не вызывает уведомлений.

## Heartbeat Tasks

`HEARTBEAT.md` (в `workspace/`) периодически проверяется встроенным cron-job'ом `nanobot gateway`,
когда `gateway.heartbeat.enabled=true` (в этом проекте — `true`, `intervalS: 1800`).
Не создавай дублирующий heartbeat-cron, если встроенный не отключён в `config.json`.

- `apply_patch` — для обычных обновлений списка задач (добавление/удаление/изменение многих строк).
- `edit_file` — для точечных замен, скопированных из текущего `HEARTBEAT.md`.
- `write_file` — для первого создания или намеренной полной перезаписи.

Если пользователь просит recurring/periodic задачу — обнови `HEARTBEAT.md`, а не создавай одноразовый cron.
Используй `cron` tool opencode для явных напоминаний, scheduled-задач с отчётом каждый запуск
или custom-расписаний, не входящих в heartbeat-список.

## Working Conventions

- Python ≥ 3.14, в коде `from __future__ import annotations` обязателен.
- Стиль: только stdlib + уже подключённые зависимости (см. `requirements.txt`).
- Логирование: `from loguru import logger` (не stdlib `logging` в новом коде).
- Импорты: `lib.*` для внутренних модулей, `workspace.utils.*` для утилит workspace.
- Не добавляй комментарии в коде без явной просьбы.
- Не коммить и не пушь без явной просьбы пользователя.
- Перед завершением задачи, если есть lint/typecheck/test команды — запусти их.

## OpenSpec

Спецификации изменений ведутся через [OpenSpec](https://github.com/Fission-AI/OpenSpec):
`openspec/specs/<capability>/spec.md` — канонические спеки; `openspec/changes/<name>/{proposal,design,tasks}.md` + `specs/<cap>/spec.md` (дельты)
— черновики изменений. Любая нетривиальная правка архитектуры (новый модуль
`lib/`, изменение таблицы БД, изменение Skill/Tool-контракта) начинается с
`openspec.cmd new change "<name>"`. Каждый шаг флоу (`proposal` → `specs` →
`design` → `tasks`) делается по инструкции, возвращаемой `openspec.cmd instructions <artifact> --change "<name>" --json`. Перед каждым write'ом —
`openspec.cmd status --change "<name>" --json` для подтверждения зависимостей.
`openspec.cmd validate <name>` должен проходить зелёным до коммита change.

**Язык OpenSpec-артефактов:** гибридный. Тело артефактов (proposal, design,
tasks, отдельные абзацы спеки) — на русском, нормативные ключевые слова,
которые парсит грaмматика OpenSpec (`SHALL`, `SHOULD`, `MAY`, `WHEN`,
`THEN`, `AND`, `OR`, `NOT`, `SHALL NOT`) — на английском. Структурные
заголовки OpenSpec-спек (`## Purpose`, `## Requirements`,
`### Requirement:`, `#### Scenario:`, `## ADDED Requirements`,
`## MODIFIED Requirements`, `## REMOVED Requirements`, `## RENAMED Requirements`,
`FROM:` / `TO:` в `RENAMED Requirements`) — на английском. Имена
собственные (имена таблиц, колонок, файлов, секций конфига, классы,
скрипты) — всегда латиницей, без перевода. Это дополняет правила для
commit-сообщений и позволяет grep'абельность по объектам репо.

## Component Specification System

Каталог компонентных спецификаций (`openspec/specs/`) описывает **архитектурный
контракт** каждого значимого компонента, а не его реализацию. Правила ведения —
`openspec/specs/architecture/component-model/spec.md` (модель + шаблон),
`openspec/specs/documentation/component-registry/spec.md` (правила реестра),
`openspec/specs/validation/component-spec-validation/spec.md` (автоматическая проверка).
Реестр — `openspec/specs/COMPONENTS.md`.

**Разделение ответственности** (чтобы не дублировать):

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные правила и принципы
  (цель, не «as-is»).
- `openspec/specs/<domain>/<component>/spec.md` — нормативный контракт
  конкретного компонента: назначение, граница, требования, запрещённое поведение,
  зависимости, реализация, проверка.
- `docs/*.md` (включая `ARCHITECTURE.md`, `DATABASE.md`, `INTERNAL_API.md`,
  `skill-tool-architecture.md`) — описание **текущей реализации** компонента,
  operational/reference details.
- Код — фактическая реализация.

**Когда создавать / обновлять component spec:**

- Добавил новый архитектурный компонент в `lib/`, `workspace/`, или существенный
  подкомпонент с публичным контрактом / lifecycle / конфигурацией →
  сначала запись в `openspec/specs/COMPONENTS.md` со статусом `missing`,
  затем — отдельная spec в `openspec/specs/<domain>/<component>/spec.md`.
- Изменил публичный контракт, границу или зависимости существующего компонента →
  обнови соответствующую spec **в том же изменении**.
- Изменил **нормативное правило** (принцип зависимости, граница Skill↔Tool,
  антипаттерн, инвариант) → правь `docs/TARGET_ARCHITECTURE.md`, не дублируй в spec.
- Изменил детали реализации существующего компонента (имена полей, внутренние
  helper-функции, локальная рефакторинг-оптимизация) → правь код и/или
  `docs/<соответствующий файл>`, **не** трогай spec (контракт не менялся).

**Структура spec:** обязательные разделы по шаблону `architecture/component-model`:
Назначение, Ответственность, Граница (Owns/Does not own/May depend on/Must not
depend on), Публичный контракт, Требования (с минимум одним сценарием
КОГДА/ТОГДА), Запрещённое поведение, Зависимости, Реализация (ссылки на код),
Проверка. Опциональные — Конфигурация, Жизненный цикл, Состояние, Инварианты,
Поведение при ошибке, Потребители.

**Язык:** component specs пишутся на русском (заголовки разделов, тело).
Имена классов (`ApplicationContext`), методов (`can_handle()`), файлов
(`project.json`), ключей конфига (`gateway.cache.local_path`), API
(`search_vector`) **никогда не переводятся** — это имена собственные для
grep'абельности.

**Что НЕ делать:**

- Не выдавать предположения за контракт (spec основывается на анализе кода
  или явно согласованных решениях).
- Не создавать spec для каждого `.py`-файла — только для архитектурных
  компонентов (см. определение в `architecture/component-model`).
- Не устанавливать статус `complete` без реальной проверки соответствия
  коду (статусы и их критерии — `documentation/component-registry`).
- Не дублировать один контракт в двух spec (single source of truth).

**Статусы:** `missing` → `draft` → `partial` → `complete` (или `deprecated`).
Критерии перехода — в `openspec/specs/documentation/component-registry/spec.md`.

**Валидация:** `openspec.cmd validate <name>` должен проходить зелёным до
коммита change; проверка структуры spec — см.
`openspec/specs/validation/component-spec-validation/spec.md`.

## Commit Messages

Используется [Conventional Commits](https://www.conventionalcommits.org/) с русскими описаниями.
Формат: `<type>(<scope>): <краткое описание>`.

**Основные типы (используются в проекте):**

| Тип | Когда |
|---|---|
| `feat` | Новая функциональность |
| `fix` | Баг-фикс |
| `refactor` | Внутреннее изменение без новой функциональности |
| `docs` | Только документация |
| `test` | Только тесты |
| `chore` | Служебные изменения (зависимости, конфиги, .gitignore) |

**Scope** — короткое имя подсистемы в скобках, нижний регистр: `backfill`, `media`,
`hooks`, `cli`, `config`, `audit`, `sql`, `lib`, `appctx`, `embedding`, `benchmark`, и т.д.

**Примеры из истории:**

```
feat(backfill): AW-миграция legacy-медиа ({data} -> file_id) в agent_conversation_messages
refactor(media): единый кодек media + общий MessageExchange для postgres/redis/streamlit
fix(db): не передавать () вместо None в _CursorProxy.execute
docs(development): описать SessionFileRedirectHook
chore(bench): удалить устаревшие отчёты прогонов бенчмарков
```

Время от времени для релизных коммитов используется составной тип через `+`,
например `docs+fix(2.2.0): docs/SQL-пути/.gitignore, регрессии и CHANGELOG для v2.2.0`
— это «документация + фикс регрессий в одном релизе».

**Правила:**

- Описание — на русском, в нижнем регистре, без заглавной первой буквы.
- В конце — точка не ставится.
- Тело сообщения (если есть) — через пустую строку, отделяется от заголовка.

**Язык описания (type/scope vs description):**

- `type` и `scope` — всегда латиницей (Conventional Commits).
- `description` (часть после `type(scope):`) — на русском, **но** допустимы
  английские объектные имена без перевода: имена таблиц (`audit_vectors`,
  `agent_vector_index_config`), колонок (`auditee_entity`), полей конфига
  (`storage_table`, `default_root`), инструментов (`FAISS`, `DuckDB`,
  `EXPLAIN`, `EXTRACT`), типов релизов (`Phase 6`, `baseline`), классов
  (`IndexIntegrityError`). Это имена собственные — перевод не нужен и
  только ухудшит grep'абельность.
- **Недопустимо** в description: чисто английские описания действий
  («remove X», «pin Y to Z», «enable W by default») — их нужно переводить
  на русский: «убрать X», «закрепить Y на Z», «включить W по умолчанию».

**Ретроспективное исправление старых англоязычных description:**

Не переписывать через `git rebase` поверх уже опубликованных тегов —
это force-push и ломает совместимость по SHA. Вместо этого при выпуске
очередного MINOR/MAJOR релиза:

1. Создать ветку `release/vX.Y` от текущего `master` (см. Release Process).
2. Переписать description старых коммитов через `git commit --amend` после
   cherry-pick (или `git rebase -i` в release-ветке — её история своя,
   теги ниже по истории не ломаются).
3. Закоммитить CHANGELOG и поставить тег `vX.Y.0` уже в release-ветке.
4. После тега release-ветка остаётся в репо как «чистая» история версии.

Англоязычные коммиты в `master` остаются как есть (это исторический
артефакт) — переписываются только их копии в `release/vX.Y`.

## Documentation Maintenance

Документация должна быть **всегда актуальна**. Любое изменение поведения, API или конфигурации
должно сопровождаться правкой соответствующей документации в одном коммите/изменении.

**Где какая документация:**

| Документ | Что описывает |
|---|---|
| `AGENTS.md` (этот файл) | Инструкции для opencode-ассистента: структура, конвенции, политики |
| `workspace/AGENTS.md` | Инструкции для нано-агента: storage policy, cron, heartbeat |
| `README.md` | Общий обзор проекта для пользователя |
| `docs/README.md` | Навигационный индекс каталога `docs/` (разделы, нормативная архитектура, конвенции). Содержимое подсистем — в `ARCHITECTURE.md`, `DATABASE.md`, `VECTOR_INDEXES.md`, `INTERNAL_API.md`, `TESTING.md` |
| `docs/TARGET_ARCHITECTURE.md` | **Нормативный контракт**: принципы, invariant'ы, anti-patterns, decision-чеклист. Не описывает текущую реализацию (это `docs/ARCHITECTURE.md` и др.) |
| `CHANGELOG.md` | История изменений (по разделам) |
| `workspace/HEARTBEAT.md` | Текущие активные периодические задачи |
| `workspace/skills/*/SKILL.md` | Контракт конкретного навыка (входы/выходы/инструменты) |
| `sql/README.md` | DDL-секции и порядок миграций |
| `benchmarks/README.md` | Как устроен бенчмарк-раннер |
| `tools/*.py` — docstring | Назначение и CLI-аргументы утилиты |
| `lib/*/README.md` | Контракт подсистемы (если есть) |

**Когда что править:**

- Изменил публичный API (`lib/...`, `workspace/utils/...`) → обнови `docs/ARCHITECTURE.md` (или соответствующий `docs/*` файл) и docstring.
- Добавил/удалил/переименовал модуль в `lib/` или `workspace/` → обнови секцию «Project Layout» в `AGENTS.md` (этом файле).
- Изменил ключ конфига или порядок мержа → обнови секцию «Configuration» в этом `AGENTS.md` + проверь `tests/test_config_keys.py`.
- Добавил/удалил endpoint канала, sql-таблицу, сессию → обнови `docs/ARCHITECTURE.md` / `docs/DATABASE.md` и при необходимости `sql/README.md`.
- Изменил поведение heartbeat/cron/MEMORY → обнови `workspace/AGENTS.md`.
- Добавил новый skill → опиши в `workspace/skills/<name>/SKILL.md` (входы/выходы/примеры запросов).
- Изменил CLI-аргументы утилиты `tools/*.py` → синхронизируй docstring и `docs/INTERNAL_API.md` (если утилита там упомянута).
- Готовишь релиз → обнови версию в `project.json` → `project.version` (баннер `gateway.py`)
  на актуальный `X.Y.Z` и открой/закрой блок в `CHANGELOG.md` под ближайший раздел.
- Меняешь **архитектурные правила/invariant'ы/anti-patterns** (принципы зависимостей,
  границы Skill↔Tool, контракты) → правь `docs/TARGET_ARCHITECTURE.md` (это норма, не «as-is»).
  Детали **текущей реализации** — только в `docs/*`, правила — только в `docs/TARGET_ARCHITECTURE.md`
  (чтобы не дублировать). Каждое существенное изменение сверяй с `docs/TARGET_ARCHITECTURE.md §30–§31`.

**Принципы:**

- Документация без кода — мусор; код без документации — неподдерживаем.
- Если правка большая (новый модуль, новый канал, новый skill) — сначала опиши в `docs/` (и `docs/README.md` при необходимости),
  затем реализуй. Обратный порядок тоже допустим, но описание появится **в том же изменении**.
- Перекрёстные ссылки (`see also` / таблицы выше) должны оставаться рабочими — при переименовании
  файла пройдись по всем `.md` и поправь относительные пути.
- Если встречаешь устаревшее место в документации по ходу работы (не связанное с задачей) —
  пометь в финальном ответе, не правь без согласования.

## Release Process

Релизная политика проекта зафиксирована в `CHANGELOG.md` (формат Keep a Changelog + SemVer).
Эта секция — практический чек-лист для проведения релиза в репо
`github.com/AlexEgorov85/workspaces_nanobot`.

### Версионирование (SemVer)

- **MAJOR** (`vX.0.0`) — несовместимые изменения API, удаление публичных модулей `lib/`,
  изменение порядка мержа конфига, миграции БД с ручными действиями.
- **MINOR** (`vX.Y.0`) — новая функциональность с обратной совместимостью: новый канал,
  новый skill, новый сервис, новые ключи конфига (с дефолтами).
- **PATCH** (`vX.Y.Z`) — баг-фиксы, мелкие улучшения, регрессии после релиза.
  Допускается несколько тегов в одной ветке `release/vX.Y` (пример: `v2.0.0` → `v2.0.1`).

### Ветвление и теги

- **Разработка ведётся в `master`.** Все PR и feature-коммиты — сюда.
- **Перед MINOR/MAJOR-релизом** делается копия `master` в ветку `release/vX.Y`,
  после чего локально сразу переключаются обратно на `master`.
- **PATCH-релизы** делаются в той же ветке `release/vX.Y` (cherry-pick фиксов из `master`).
- Тег `vX.Y.Z` ставится аннотированным прямо в релизной ветке
  (см. существующий `tag: v2.2.0` на `release/v2.2`).
- Ветка `release/vX.Y` остаётся в репо после релиза (это история версии).
  Обратного мержа в `master` обычно не требуется, если CHANGELOG — единственный артефакт,
  который меняется только в релизной ветке.

### Чек-лист релиза (MAJOR/MINOR)

1. **Подготовка в `master`**
   - Все PR смержены, рабочая копия чистая.
   - Текущая секция `[Unreleased]` в `CHANGELOG.md` полностью заполнена (категории
     Keep a Changelog: `Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`).
   - Документация актуальна: `README.md`, `docs/README.md`, `AGENTS.md` (этот файл),
     `workspace/AGENTS.md`, `workspace/skills/*/SKILL.md`, `sql/README.md`.

2. **Тесты и проверки**
   - `pytest` зелёный (текущий ориентир — **1480 passed, 22 skipped**).
   - Smoke-прогон бенчмарков: `python benchmarks/runner.py --tags simple`.
   - Если менялся SQL — миграция применена локально, create-скрипты подтверждены.
   - Если менялся конфиг — `tests/test_config_keys.py` обновлён, `REQUIRED_KEYS` синхронизирован.
   - Если менялся публичный API `lib/` — пройден smoke через `cli_agent.py` или `gateway.py`.

3. **Создание релизной ветки**
   ```bash
   git checkout master && git pull
   git checkout -b release/vX.Y
   ```
   После создания ветки сразу вернуться на `master`:
   ```bash
   git checkout master
   ```

4. **Версионирование в `CHANGELOG.md` (в ветке `release/vX.Y`)**
   - Переименовать `## [Unreleased]` → `## [X.Y.0] — YYYY-MM-DD`.
   - Добавить свежий пустой блок `## [Unreleased]` сверху (для следующих изменений).
   - В эпиграфе блока указать тип: `**MAJOR-релиз:** …` / **MINOR-релиз:** …`.
   - Обновить `project.json` → `project.version` на `X.Y.Z` (канонический
     источник версии для баннера `gateway.py`; git-теги и первый релизный блок
     CHANGELOG на `master` отстают из-за release-веток).

5. **Коммит и тег**
   ```bash
   git add CHANGELOG.md
   git commit -m "docs+fix(X.Y.0): <краткое описание> и CHANGELOG для vX.Y.0"
   git tag -a vX.Y.0 -m "vX.Y.0 — <краткое описание>"
   git push origin release/vX.Y --follow-tags
   ```

6. **GitHub Release (опционально)**
   - На странице `github.com/AlexEgorov85/workspaces_nanobot/releases/new` выбрать тег `vX.Y.0`.
   - Описание скопировать из блока в CHANGELOG.

### Чек-лист PATCH-релиза

1. Cherry-pick фикса(ов) из `master` в `release/vX.Y`:
   ```bash
   git checkout release/vX.Y
   git cherry-pick <sha1> [<sha2> ...]
   ```
2. Добавить блок в `CHANGELOG.md` (категория `Fixed`, эпиграф `**PATCH-релиз:** …`).
3. Закоммитить, поставить тег `vX.Y.Z`, запушить ветку с тегами.

### Что НЕ делать при релизе

- **Не редактировать CHANGELOG задним числом** для уже опубликованных версий —
  только добавить новый PATCH-релиз сверху.
- **Не забывать про свежий `## [Unreleased]`** — без него следующий релиз не имеет точки привязки.
- **Не пушить тег без коммита с CHANGELOG** — тег обязан указывать на релизный коммит.
- **Не ставить тег в `master` напрямую** — только через ветку `release/vX.Y`.
- **Не продолжать разработку в `release/vX.Y`** — фиксы только через cherry-pick,
  вся новая функциональность — в `master`.
- **Не создавать PATCH-ветку** (`release/vX.Y.Z`) — патчи идут в существующую `release/vX.Y`.
