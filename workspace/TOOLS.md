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
