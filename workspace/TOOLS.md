# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## glob — File Discovery

- Use `glob` to find files by pattern before falling back to shell commands
- Simple patterns like `*.py` match recursively by filename
- Use `entry_type="dirs"` when you need matching directories instead of files
- Use `head_limit` and `offset` to page through large result sets
- Prefer this over `exec` when you only need file paths

## grep — Content Search

- Use `grep` to search file contents inside the workspace
- Default behavior returns only matching file paths (`output_mode="files_with_matches"`)
- Supports optional `glob` filtering plus `context_before` / `context_after`
- Supports `type="py"`, `type="ts"`, `type="md"` and similar shorthand filters
- Use `fixed_strings=true` for literal keywords containing regex characters
- Use `output_mode="files_with_matches"` to get only matching file paths
- Use `output_mode="count"` to size a search before reading full matches
- Use `head_limit` and `offset` to page across results
- Prefer this over `exec` for code and history searches
- Binary or oversized files may be skipped to keep results readable

## cron — Scheduled Reminders

- Please refer to cron skill for usage.

## history_search — поиск по долговечному журналу агента

`history_search` — кастомный инструмент (см. `workspace/tools/history_search_tool.py`).
Ищет по `agent_gateway_logs` — журналу, который переживает context compaction
(в отличие от `agent_conversation_messages`). Полезно, когда пользователь
ссылается на старое сообщение или результат, который выпал из контекста.

**Параметры:**

- `query` (опц.) — подстрока для ILIKE-поиска по `summary` и `payload::text`.
- `event_type` (опц.) — один из `context_compacted`, `tool_call`,
  `tool_result`, `llm_call`, `run_finished`, `subagent_run_finished`, `inbound`.
- `tool_name` (опц.) — имя инструмента для фильтрации `tool_call` /
  `tool_result`. Удобно для поиска истории конкретного инструмента.
- `since` / `until` (опц.) — ISO-8601 таймстамп.
- `session_scope` (опц., дефолт `current`) — `current` (только текущая
  сессия) или `all` (по всем сессиям).
- `limit` (опц.) — максимум событий (по конфигу `max_rows`).

**Примеры:**

- «Какие файлы я прикладывал?» →
  `history_search(event_type="tool_call", tool_name="read_file")`
- «Когда последний раз сжимался контекст?» →
  `history_search(event_type="context_compacted", session_scope="current")`
- «Что я писал про договор аренды?» →
  `history_search(query="договор аренды", event_type="llm_call")`

**Замечания:**

- Для поиска файлов используй `tool_call` / `tool_result` (там аргументы
  и пути), а НЕ выдуманные типы (`file_attached`, `file_created`,
  `document_summarized` — таких нет в журнале).
- Если результат пустой — отвечай «не найдено в истории», не выдумывай.

## legal_summarizer_query — follow-up по уже проанализированному документу

Кастомный tool (`workspace/tools/legal_summarizer_query.py`). Возвращает
структурные данные по сохранённой `operation_id` **без перепарсинга PDF** —
читает manifest/result/chunks навыка `legal_summarizer` из
`data_store/cache/skills/legal_summarizer/<operation_id>/`.

**Зачем:** иначе на follow-up-вопрос («сколько статей?», «какие разделы?»,
«что в чанке N?») агент вынужден через `exec`+pdfplumber повторно
извлекать текст документа (200+ сек, часто падает на кириллице в Windows-cp1251).

**Параметры:**

- `operation_id` (обяз.) — поле `result.operation_id` из предыдущего ответа `legal_summarizer`.
- `field` (дефолт `stats`) — `stats | articles | chunks | sections | tree | all`.
- `max_chunk_summary_chars` (опц., дефолт 1500) — обрезка summary чанка для `field=chunks`.

**Когда звать:**

- Сразу после `--confirm` саммари вернуло `operation_id` → запомни его для follow-up'ов.
- Любой вопрос про уже проанализированный документ: «сколько статей?», «какие
  разделы?», «что в чанке 5?», «назови все части» и т.п.

**Примеры:**

- «Сколько статей в документе?» → `legal_summarizer_query(operation_id="<op_id>", field="articles")` → `{article_count: N}`
- «Какие разделы?» → `legal_summarizer_query(operation_id="<op_id>", field="sections")`
- «О чём чанк 12?» → `legal_summarizer_query(operation_id="<op_id>", field="chunks")` → массив с `chunk_id`, `summary`, `section_path`.

**Не делать:**

- Не вызывай `pdfplumber`/`pdftotext` через `exec` для подсчёта статей —
  есть `legal_summarizer_query`. Это и быстрее, и кириллица не сломается.
- Не передавай в `field` значения вне списка — будет отказ с понятной ошибкой.

## duckdb_query — read-only SQL по whitelist таблиц

Generic tool (`workspace/tools/duckdb_query_tool.py`). Выполняет
SELECT/WITH/EXPLAIN в общем DuckDB-кеше и возвращает структурированный
результат.

**Архитектура:**

1. `validate_sql` — SELECT-only gate (см. `lib/utils/sql_safety.py`).
2. `DuckDbCacheStore.execute_readonly(sql, params, max_rows)` —
   выполнение в общем кэше через `CacheProvider.query_sql`.
3. JSON-ответ с `{status, columns, rows, row_count, returned_rows, truncated}`.

**Параметры:**

- `sql` (обяз.) — SQL-запрос. SELECT/WITH/EXPLAIN. Параметры через `?`
  (позиционно) или `:name` (именованно).
- `params` (опц.) — словарь `{name: value}` для параметров запроса.
- `max_rows` (опц., дефолт `gateway.duckdb_query.max_rows=1000`) —
  локальный лимит строк.

**Когда звать:**

- Точный SELECT по таблице из `references/schema.md` навыка.
- Чтение SQL из `public.agent_predefined_scripts` для выполнения
  predefined (см. `audit_analyzer/SKILL.md`, секция «Predefined SQL»).
- Агрегации, GROUP BY, JOIN, фильтры по колонкам.

**Примеры:**

- «Сводка по статусам аудитов» → Agent читает `sql_template` из
  `public.agent_predefined_scripts WHERE name='audit_status_summary'`,
  затем `duckdb_query(sql=<template>)`.
- «Сколько проверок в 2024?» →
  `duckdb_query(sql="SELECT COUNT(*) FROM oarb.audits WHERE actual_date >= ? AND actual_date < ?", params={...})`.

**Не делать:**

- Не пытайся выполнять DDL/DML — tool зарубит через `validate_sql`.
- Не формируй `LIKE '%...%'` для семантического поиска — для этого
  есть `vector_search`.

**Конфиг (`project.json::gateway.duckdb_query`):**

```json
{
  "gateway": {
    "duckdb_query": {
      "enable": true,
      "max_rows": 1000,
      "max_result_chars": 50000,
      "query_timeout_sec": 30
    }
  }
}
```

## vector_search — семантический поиск по FAISS-индексу

Кастомный tool (`workspace/tools/vector_search_tool.py`). Generic-поиск по
заранее зарегистрированному FAISS-индексу. Tool **не знает про домен** —
`index_name` выбирается Agent'ом на основании каталога в `SKILL.md`
(раздел «Vector indexes»).

**Архитектура:**

1. `CacheProvider.search_vector(query, index_name, top_k, threshold)` —
   абстрактный интерфейс к FAISS; конкретная реализация регистрируется
   runtime'ом.
2. Нормализация результата в JSON-контракт `{status, query, index_name,
   results: [{id, score, text, metadata}], count, truncated}`.
3. STALE/INVALID detection — поставщик (provider) помечает meta через
   `_signature_status` при загрузке индекса; tool пробрасывает
   `index_warning` если индекс требует пересборки.

**Параметры:**

- `query` (обяз.) — поисковый запрос на естественном языке.
- `index_name` (обяз.) — имя FAISS-индекса. Используй только имена из каталога
  в `SKILL.md` (`audits_index`, `violations_index`, `audit_reports_index`).
- `top_k` (опц., дефолт `gateway.vector_search.default_top_k=5`,
  потолок `max_top_k=50`) — сколько ближайших результатов вернуть.
- `threshold` (опц., дефолт `gateway.vector_search.default_threshold=0.0`,
  диапазон `[0.0, 1.0]`) — минимальная cosine-схожесть.

**Когда звать:**

- Семантический поиск: «найди похожие нарушения», «проверки по X»,
  «отчёты с выводами о …».
- Когда важен **смысл**, а не точные числа/фильтры.

**Когда НЕ звать:**

- COUNT / GROUP BY / ORDER BY → `duckdb_query` (свободный SQL).
- Точный `id` → `duckdb_query WHERE id = ?`.
- Фильтры по конкретным колонкам (`severity`, `status`, `date`) →
  `duckdb_query`.
- Сложные JOIN'ы → `duckdb_query`.

**Конфиг (`project.json::gateway.vector_search.*`):**

```json
{
  "gateway": {
    "vector_search": {
      "enable": true,
      "default_top_k": 5,
      "max_top_k": 50,
      "default_threshold": 0.0,
      "max_query_chars": 4000,
      "max_result_chars": 16000,
      "timeout_sec": 30
    }
  }
}
```

**Пример:**

- «Найди нарушения про пожарную безопасность» →
  `vector_search(query="пожарная безопасность", index_name="violations_index", top_k=5, threshold=0.5)`
  → `{status, query, index_name, results: [...], count, truncated}`.

**Замечания:**

- Tool не выбирает `index_name` сам — Agent делает это по каталогу в `SKILL.md`.
- Если `index_name` не зарегистрирован — `error_type: missing_index` или
  `missing_provider`.
- `IndexIntegrityError` (STALE/INVALID) → `error_type: stale_index` /
  `invalid_index` с рекомендацией `rebuild via tools/build_vectors.py`.

## Capability layer для audit_analyzer

В режиме на `audit_analyzer` Agent выбирает один из двух generic tools:

| Tool | Назначение | Когда |
|---|---|---|
| `duckdb_query` | Точный SELECT (включая чтение `sql_template` из `public.agent_predefined_scripts` inline) | Числовые/структурные запросы; predefined-скрипты; свободный SQL |
| `vector_search` | Семантический поиск по FAISS | Запрос про **смысл**, индекс есть в `references/vector_indexes.md` |

Агент сам читает `SKILL.md` и делает выбор. Ни один tool не делает
auto-routing или классификацию запроса. Генерация SQL — через CLI
`scripts/cli.py --mode generated_sql` (skill-side `scripts/generated_sql_mode.py`,
прямой вызов `lib.services.llm_client.call_llm`), используется по желанию Agent'а.

