# Migration Notes

Сводка ключевых изменений между релизами для тех, кто апгрейдится с предыдущей
версии. **Источник истины** — [CHANGELOG.md](../CHANGELOG.md); здесь — только
краткая выжимка с фокусом на **breaking changes и ручные действия**.

---

## v2.4.0 → v2.4.x (текущая)

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
| `cli.show_context_window` | `true` | Метка занятости контекстного окна в CLI |
| `streamlit.enabled` | `true` | Гейт запуска Streamlit-UI на :8501 |

**Удалённые ключи**:

- `streamlit.failed_window_sec` → `streamlit.error_window_sec`
  (теперь окно повтора `error`-задач, а не `failed`).

**Изменённые пути**:

- DuckDB-снапшот: `workspace/skills/audit_analyzer/cache/audit_cache.duckdb`
  → **`workspace/data_store/duckdb/cache.duckdb`** (публикуется gateway'ом).
  Старое поле `project.json:in_memory_cache_path` больше не читается.

**Изменённое поведение**:

- `patch_save_turn` теперь **сохраняет большие результаты tool'ов полным файлом**
  в `data_store` (через `SessionFileStore`), а не режет до 16K символов в истории.
- `patch_exec_limits` поднял дефолтные потолки `exec`/`shell`.
- `patch_tool_limits` поднял потолки `read_file`/`grep`/`list_dir`.

**Никаких ручных миграций БД** — DDL применяется через
`python tools/migrate.py --apply`.

---

## v2.3.1 → v2.4.0

- Без поломок API. MINOR-релиз.
- Новые кастомные tool'ы из `workspace/tools/*.py` (патч `patch_project_tools`):
  `compact_context`. Дополнительные `audit_run_predefined_script` и
  `audit_search_vector` появились и были удалены в этом же релизе
  (см. [docs/skill-tool-inventory.md](skill-tool-inventory.md)).
- `ApplicationContext.create()` теперь автоматически подключает
  `SessionFileRedirectHook` и фреймворковые хуки из `lib/hooks/`.

## v2.3.0 → v2.3.1

- PATCH-релиз. Полностью обратно совместим.
- Хуки переехали: фреймворковые — в `lib/hooks/`, плагины — в `workspace/hooks/`.
- `office_files` skill — чтение docx/xlsx/xls/pdf/pptx/csv/txt.

## v2.0.0 → v2.3.0

**breaking changes v2.0.0**:

- Конфигурация векторных индексов переехала из файлов `.faiss` и `project.json`
  в таблицу `public.agent_vector_index_config` (управление через SQL).
- DSN разделён на `host`/`port`/`user`/`dbname` + `DB_PASSWORD` (env).
- Все таблицы логов и сессий получили префикс `agent_` (`agent_gateway_logs`,
  `agent_conversation_messages`, `agent_worker_claims`).
- Имя LLM-провайдера — каноническое `LLM_API_KEY` (вместо `MISTRAL_API_KEY`).

---

## До v1.5.0

Legacy-мигратор файлов `.faiss` удалён. Если у вас остались артефакты v1.x —
обращайтесь к [CHANGELOG.md](../CHANGELOG.md) → соответствующая версия.

---

## Ручные действия миграции 1.5.0 → 2.0.0 (из DEVELOPMENT.md)

Краткий таймлайн релизов — в [CHANGELOG.md](CHANGELOG.md). Этот раздел — только то, что **требует ручных действий при миграции**.

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

- **Сессии** (`public.agent_session_meta`, `public.agent_session_messages`) —
  схема та же. DDL: `sql/session/create_public_agent_session_meta.sql`,
  `sql/session/create_public_agent_session_messages.sql`.
- **Канал** (`public.agent_conversation_messages`) — без миграции (имя уже актуально).
  DDL: `sql/channels/create_public_agent_conversation_messages.sql`.
- **`workspace/data_store/duckdb/cache.duckdb`** — gateway пересоздаст автоматически (in-memory → новый snapshot; путь через `table_registry.snapshot_path()`).
- **Векторные индексы** (`oarb.audit_vectors`, `public.agent_vector_index_store`,
  `public.agent_vector_index_config`) — без миграции (1.5.0 уже хранил их в БД);
  DDL в `sql/audit_analyzer/`.
- **Бенчмарки** (`public.agent_benchmark_runs`, `public.agent_benchmark_results`) — без миграции;
  DDL в `sql/benchmarks/`.
- **`agent_gateway_logs` / `agent_question_runs`** — новые таблицы:
  `sql/logs/create_public_agent_gateway_logs.sql`,
  `sql/logs/create_public_agent_question_runs.sql`.

**Что НЕ изменилось:**

- API точек входа: `python gateway.py`, `python cli_agent.py -P`.
- Имена таблиц БД.
- `benchmarks/items/*.yaml` — формат совместим.
- `audit_analyzer` режимы `predefined` / `sql` / `vector`.
- Параметры CLI `audit_analyzer` (`--top-k`, `--threshold`, `--index-name`).

Краткий таймлайн релизов — в [CHANGELOG.md](CHANGELOG.md).

---

