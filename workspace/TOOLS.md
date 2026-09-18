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
- `session_scope` (опц., дефолт `current`) — область поиска:
  - `current` — только текущая сессия (по `session_id` из `RequestContext.session_key`).
    При отсутствии identity-store возвращает
    `{"status": "error", "error_type": "missing_session_identity"}`,
    SQL-запрос НЕ выполняется.
  - `all` — все сессии **текущего пользователя** (по `user_id` из
    `RequestContext.sender_id`). Не глобальный поиск по всем пользователям.
    При отсутствии identity-store возвращает
    `{"status": "error", "error_type": "missing_user_identity"}`,
    SQL-запрос НЕ выполняется.
- `limit` (опц.) — максимум событий (по конфигу `max_rows`).
- `offset` (опц., дефолт 0) — пропустить первые `offset` событий
  после сортировки `ORDER BY timestamp DESC, id DESC`. Продолжать
  пагинацию через `next_offset` из предыдущего ответа, **НЕ** через
  `offset + limit` (при `results_truncated=true` часть событий была
  отброшена).

**Ответ (JSON):**

- `count` — количество событий в массиве `events`.
- `has_more` — `true`, если есть следующая страница. Композитная формула
  `db_has_more OR results_truncated`: даже когда `LIMIT N+1` не нашёл
  следующей строки в БД, `results_truncated=true` означает, что
  truncation выбросил часть отобранных событий и следующая страница
  обязательна.
- `next_offset` — целое ≥ 0; `offset` для следующего запроса при
  пагинации. Равно `original_offset + count` (после всех truncation-проходов).
- `results_truncated` — `true`, если из выборки были выброшены целые
  события, чтобы общий JSON влез в `max_result_chars`.
- `payload_truncated` (на каждом событии) — `true`, если `payload`
  конкретного события отличается от БД из-за обрезки через
  `truncate_middle`.
- `truncated` — **deprecated** алиас `results_truncated`. Сохранён ради
  совместимости; удаляется в отдельном follow-up change. Новый код должен
  читать `results_truncated` (выброс целых событий) и `payload_truncated`
  (ужатие payload'а конкретного события) раздельно.
- `events: [{event_id, timestamp, event_type, name, level, summary,
  payload, payload_truncated}]` — `payload` хранится как JSON-string
  (нужен `json.loads` для получения структуры).

**Примеры:**

- «Какие файлы я прикладывал?» →
  `history_search(event_type="tool_call", tool_name="read_file")`
- «Когда последний раз сжимался контекст?» →
  `history_search(event_type="context_compacted", session_scope="current")`
- «Что я писал про договор аренды?» →
  `history_search(query="договор аренды", event_type="llm_call")`
- Пагинация: первая страница → `history_search(limit=20)` →
  если `has_more=true`, продолжить с `offset=next_offset` (НЕ `20`).

**Замечания:**

- Для поиска файлов используй `tool_call` / `tool_result` (там аргументы
  и пути), а НЕ выдуманные типы (`file_attached`, `file_created`,
  `document_summarized` — таких нет в журнале).
- Если результат пустой — отвечай «не найдено в истории», не выдумывай.
- `history_search` **не выполняет глобальный поиск по всем пользователям**:
  `session_scope="all"` — это все сессии текущего пользователя, а не
  вся БД. Без identity-store запрос возвращает структурированную
  ошибку (`missing_user_identity`) и SQL не выполняется. Это
  закрывает cross-user leakage (security boundary).
- `offset`-пагинация **не snapshot-consistent**: при INSERT'е новых
  событий между запросами более новые строки попадают в начало
  выборки. Если нужна строгая консистентность — это отдельный
  future change (cursor-пагинация).

### Структура payload по event_type

Схема `payload` описывает **текущую** форму данных в `agent_gateway_logs`
на момент публикации change и явно помечает поля, сериализованные как
JSON-string. Изменение формы данных требует отдельного change.

#### `tool_call.payload`

```json
{
  "tool": "read_file",
  "args": {"path": "data/report.pdf"},
  "tool_call_id": "toolu_01H..."
}
```

Все поля — простых типов или dict'ы.

#### `tool_result.payload`

```json
{
  "tool": "read_file",
  "status": "ok",
  "result": "{\"path\": \"data/report.pdf\", \"size\": 12345}",
  "error": null
}
```

**Важно:** `result` хранится как JSON-string (сериализуется через
`psycopg2.extras.Json` и при больших объёмах обрезается с маркером
`(N chars truncated)`). Для получения структуры примени
`json.loads(payload.result)`.

#### `llm_call.payload`

```json
{
  "prompt": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "response": {"content": "...", "finish_reason": "stop", "tool_calls": [...]}
}
```

`prompt` — массив ролей (system/user/assistant/tool), `response` —
объект с контентом и метаданными. Размер payload'а сильно варьируется
(большие `llm_call` обрезаются до `per_event_cap=4000` через
`truncate_middle`).

#### `run_finished.payload`

```json
{
  "final_content": "итоговый ответ агента",
  "tools_used": ["read_file", "compact_context"],
  "stop_reason": "stop",
  "had_injections": false,
  "request_id": "uuid-..."
}
```

Все поля — простых типов или list/str. `tools_used` — список имён
инструментов, использованных в прогоне.

#### `subagent_run_finished.payload`

```json
{
  "final_content": "ответ подагента",
  "tools_used": ["compact_context"],
  "stop_reason": "stop",
  "task_id": "task_01H...",
  "task": "первое user-сообщение подагента (краткое описание задачи)",
  "request_id": "subagent:task_01H...",
  "parent_request_id": "uuid-..."
}
```

`task_id` и `request_id` идентичны (= `subagent:<task_id>`),
`parent_request_id` — `request_id` родительского вопроса, из которого
запущен подагент.

#### `inbound.payload`

```json
{
  "content": "сообщение пользователя",
  "message_id": "...",
  "sender_id": "user_42",
  "chat_id": "chat_42",
  "media": [{"filename": "report.pdf", "file_id": "...", "mime_type": "application/pdf", "file_size": 12345}]
}
```

`sender_id` / `chat_id` опциональны (есть не всегда), `media` — list
объектов `MediaItem` (см. `workspace/utils/media.py`). `message_id`
связывает `inbound` с `request_id` вопроса.

#### `context_compacted.payload`

Определяется реализацией `ContextCompactionService._notify`
(`lib/services/context_compaction.py`) на момент архивации spec
(snapshot, не долгосрочный нормативный контракт). Изменение схемы
требует отдельного change.

```json
{
  "mode": "tokens",
  "archived_msgs": 42,
  "kept_msgs": 8,
  "tokens_before": 45000,
  "tokens_after": 12000,
  "summary": "краткое описание заархивированного",
  "raw_dump": false
}
```

`mode` — `tokens` (token-budget авто-сжатие) или `idle` (idle-сжатие;
сейчас отключено, `idleCompactAfterMinutes: 0`). `raw_dump` — был ли
полный дамп сообщений в стороннее хранилище.

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

## audit_analyzer — доступ через CLI

Для работы с `audit_analyzer` Agent вызывает CLI навыка через `exec`
(прямые tools `duckdb_query` / `vector_search` удалены):

```bash
# Predefined script (единственный Agent-контракт)
python workspace/skills/audit_analyzer/scripts/cli.py --mode predefined \
    --script violations_by_type --params '{"date_from": "2024-01-01"}'

# Каталог predefined-скриптов (имя, описание, параметры)
python workspace/skills/audit_analyzer/scripts/cli.py --list-scripts

# Каталог FAISS-индексов
python workspace/skills/audit_analyzer/scripts/cli.py --list-indexes
```

| Способ | Назначение | Когда |
|---|---|---|
| `--mode predefined` | Точный SELECT по 6 predefined-скриптам | Числовые/структурные запросы; predefined-скрипты |
| `--list-scripts` | Актуальный каталог скриптов из БД | Выбор скрипта |
| `--list-indexes` | Актуальный каталог FAISS-индексов | Discovery индексов |

Агент сам читает `SKILL.md` и делает выбор. Ни один режим не делает
auto-routing или классификацию запроса. Режимы `--mode vector` и
`--mode generated_sql` доступны в CLI, но не являются частью контракта
агента.

