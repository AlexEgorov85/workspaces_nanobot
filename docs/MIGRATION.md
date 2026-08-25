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
