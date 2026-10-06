# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — НЕДОСТУПЕН

Шелл в этом рантайме выключен: `config.json → tools.exec.enable = false`,
и `tools.cliApps.enable = false` вместе с ним (второй путь исполнения кода).
Инструментов `exec` и `write_stdin` в наборе нет. Звать их нельзя — не
«нежелательно», а именно нельзя: вызова не существует.

Поэтому любые инструкции ниже, где действие требовало командной строки,
переписаны на операции платформы. Если задача кажется требующей командной
строки — ищи операцию (`mcp_enterprise_*`) или скажи пользователю, что
действие недоступно. Молча подменять shell чем-то другим не нужно.

## glob — File Discovery

- Use `glob` to find files by pattern
- Simple patterns like `*.py` match recursively by filename
- Use `entry_type="dirs"` when you need matching directories instead of files
- Use `head_limit` and `offset` to page through large result sets
- Это способ узнать пути: командной строки, которой можно было бы
  обойтись `find`, здесь нет

## grep — Content Search

- Use `grep` to search file contents inside the workspace
- Default behavior returns only matching file paths (`output_mode="files_with_matches"`)
- Supports optional `glob` filtering plus `context_before` / `context_after`
- Supports `type="py"`, `type="ts"`, `type="md"` and similar shorthand filters
- Use `fixed_strings=true` for literal keywords containing regex characters
- Use `output_mode="files_with_matches"` to get only matching file paths
- Use `output_mode="count"` to size a search before reading full matches
- Use `head_limit` and `offset` to page across results
- Для поиска по коду и по журналу — `grep` по журналу не годится, см.
  `data.history_search` ниже
- Binary or oversized files may be skipped to keep results readable

## cron — Scheduled Reminders

- Please refer to cron skill for usage.

## data.history_search — поиск по долговечному журналу агента

Операция платформы, а не кастомный tool: прежняя обёртка
`workspace/tools/history_search_tool.py` снята change'ом
`2026-10-03-mcp-native-tools` (п. D6), и модель зовёт операцию напрямую.
Ищет по `agent_gateway_logs` — журналу, который переживает context compaction
(в отличие от `agent_conversation_messages`). Полезно, когда пользователь
ссылается на старое сообщение или результат, который выпал из контекста.

Область поиска задаёт платформа: `session_id` и `user_id` берутся из
контекста вызова, а не из аргументов модели. Поэтому объявлять их не нужно
и передать чужие нельзя — перебор объявления модели не может расширить
видимость.

**Параметры:**

- `query` (опц.) — подстрока для ILIKE-поиска по `summary` и `payload::text`.
- `event_type` (опц.) — один из `context_compacted`, `tool_call`,
  `tool_result`, `llm_call`, `run_finished`, `turn_completed`,
  `subagent_run_finished`, `inbound`.
  * `turn_completed` — метрики оборота (latency_ms, outcome,
    usage_tokens, runtime_model). НЕ содержит `final_content` —
    только статистика; для контента используйте `run_finished`.
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
  `data.history_search(event_type="tool_call", tool_name="read_file")`
- «Когда последний раз сжимался контекст?» →
  `data.history_search(event_type="context_compacted", session_scope="current")`
- «Что я писал про договор аренды?» →
  `data.history_search(query="договор аренды", event_type="llm_call")`
- Пагинация: первая страница → `data.history_search(limit=20)` →
  если `has_more=true`, продолжить с `offset=next_offset` (НЕ `20`).

**Замечания:**

- Для поиска файлов используй `tool_call` / `tool_result` (там аргументы
  и пути), а НЕ выдуманные типы (`file_attached`, `file_created`,
  `document_summarized` — таких нет в журнале).
- Если результат пустой — отвечай «не найдено в истории», не выдумывай.
- `data.history_search` **не выполняет глобальный поиск по всем пользователям**:
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

#### `turn_completed.payload`

Метрики оборота (для observability, не пользовательского контента):

```json
{
  "latency_ms": 1234,
  "outcome": "completed",
  "failure_kind": null,
  "failure_error_kind": null,
  "failure_attempts": null,
  "usage_tokens": 128,
  "runtime_model": "MiniMax-M3"
}
```

НЕ содержит `final_content` / `tools_used` — это user-visible
содержимое живёт в `run_finished`. Пишется через подписку на
`TurnCompleted` в `RuntimeEventsSubscriber`.

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

## platform.analyze_document — разбор документа

Операция capability `legal_summarizer`: разбирает приложенный юридический
документ в статьи, чанки и разделы и возвращает структуру. Это **единственный**
способ разобрать документ в этом рантайме. Файл вложения операция переводит в
текст сама — отдельного tool'а для этого не требуется.

Документ принимается как `session://`-ссылка либо относительный путь внутри
`files/` своей сессии. Поддерживаются `.pdf`, `.docx`, `.txt`.

### Разбор — в два шага, и это нормальный ход

Разбор длинного документа **всегда** начинается с шага 1. Ни одного LLM-вызова
до подтверждения не происходит.

**Шаг 1 — узнать цену и спросить пользователя.** Вызови операцию БЕЗ
подтверждения:

```json
{
  "document": "session://files/contract.pdf",
  "length": "detailed",
  "focus": "аренда"
}
```

Ответ — `status: "confirmation_required"`, и в нём:

- `estimate` — оценка объёма работы (сколько чанков и сколько времени);
- `options` — варианты, которые имеет смысл предложить пользователю;
- `operation_id` — идентификатор состояния этого прогона;
- `progress_report` — `done: 0`, `continues: true`.

Теперь спроси пользователя: сколько это займёт и какие настройки выбрать.
**Это не отказ.** Не говори пользователю, что разбор недоступен или что ты не
умеешь разбирать документы — разбор доступен, он ждёт подтверждения. Отказом
это было бы только в том случае, если бы операции не существовало.

**Шаг 2 — запустить.** Когда пользователь подтвердил, вызови теми же самыми
параметрами плюс:

- `confirm` — `true`;
- `operation_id` — значение из ответа шага 1.

Параметры обязаны совпадать с шагом 1: `document`, `length`, `focus` и
`question` входят в идентичность операции, поэтому тот же
`operation_id` с другим набором параметров не совпадёт и вернёт
`invalid_params`. Пользователь попросил другое — начни с шага 1 заново, это
новый разбор.

### Параметры

- `document` (обяз.) — `session://`-ссылка либо относительный путь в `files/`.
- `length` (дефолт `brief`) — формат сводки: `brief` | `detailed`. Документ
  читается целиком и структура строится один раз, поэтому режим выбирает не
  объём прочитанного, а глубину разбора: `brief` — один структурный chunk
  (схема документа плюс выжимки разделов) и один проход, `detailed` — весь
  документ по чанкам и полный разбор. Опечатка здесь опаснее, чем кажется: без
  проверки домен тихо вернул бы успешный ответ не того формата, поэтому
  значение вне перечисления — `invalid_params`.
- `focus` (опц.) — предмет фокуса, например `аренда`.
- `question` (опц.) — конкретный вопрос к документу.
- `confirm` — `true` запускает разбор; без него операция возвращает
  `confirmation_required` и **делает ноль LLM-вызовов**.
- `operation_id` (опц.) — идентификатор из шага 1; нужен для запуска и для
  повторного входа.

### Ответ (JSON)

- `status` — `confirmation_required` | `completed` | `partial` | `error`.
- `operation_id` — идентификатор состояния разбора; сохрани его, по нему идёт
  `platform.query_operation`.
- `progress_report` — `done` / `remaining` / `continues`. **`continues: true` —
  это «работа не закончена, зови снова с тем же `operation_id`», а не «готово».**
- `estimate` и `options` — при `confirmation_required`.
- `result` — структура разбора (статьи, чанки, разделы, дерево) при
  `completed`. Крупный результат может прийти ссылкой `session://results/...`;
  читается операцией `platform.read_result`.

### Когда звать

- Пользователь приложил документ и просит его разобрать, свести, найти в нём
  статью или пункт, либо ответить на вопрос по нему.
- Документ нужно дочитать до конца: пока разбор не завершён, состояние живо и
  в него можно вернуться тем же `operation_id`.

### Не делать

- Не ставь `confirm: true` сам, от себя. Подтверждение — это согласие
  пользователя на платную работу, а не твоё решение за него.
- Не выдавай `confirmation_required` за ошибку и не отвечай пользователю «не
  могу разобрать документ». Это нормальный первый шаг диалога.
- Не обещай follow-up раньше, чем разбор вернул `operation_id`:
  `platform.query_operation` без готового состояния откажет.
- Не выдумывай `operation_id` — он всегда приходит из ответа операции.

## platform.query_operation — follow-up по уже проанализированному документу

Платформенная операция capability `platform` — как `platform.read_result`. Она была операцией capability `legal_summarizer` и
переехала, потому что чтение состояния операции есть работа с файлами сессии, а у
capability-операции нет доступа к сессии вызова. Прежний кастомный tool
`workspace/tools/legal_summarizer_query.py` снят ещё раньше, change'ом
`2026-10-03-mcp-native-tools` (п. D6); реализация —
`mcp-platform/servers/enterprise/tools/query_operation.py`.

Возвращает структурные данные по сохранённой `operation_id` **без перепарсинга
PDF**: читает manifest/result/chunks разбора из состояния операции.

**Зачем:** иначе на follow-up-вопрос («сколько статей?», «какие разделы?»,
«что в чанке N?») документ пришлось бы разбирать заново. В этом рантайме
сделать это нечем: командной строки нет, а проектный инструмент
`document_read` отдаёт текст, но не структуру — ни статей, ни чанков, ни
иерархии разделов.

**Параметры:**

- `operation_id` (обяз.) — идентификатор состояния разбора. Без него
  операция бессмысленна.
- `field` (дефолт `stats`) — `stats | articles | chunks | sections | tree | all`.
- `max_chunk_summary_chars` (опц., дефолт 1500) — обрезка summary чанка для `field=chunks`.

**Когда звать:**

- Только когда `operation_id` уже есть. Он появляется сам: разбор запускает
  `platform.analyze_document` (раздел выше), и она возвращает
  `operation_id` вместе с результатом. Свой собственный разбор ради
  follow-up делать не надо.
- Любой вопрос про уже проанализированный документ: «сколько статей?», «какие
  разделы?», «что в чанке 5?», «назови все части» и т.п.

**Примеры:**

- «Сколько статей в документе?» → `platform.query_operation(operation_id="<op_id>", field="articles")` → `{article_count: N}`
- «Какие разделы?» → `platform.query_operation(operation_id="<op_id>", field="sections")`
- «О чём чанк 12?» → `platform.query_operation(operation_id="<op_id>", field="chunks")` → массив с `chunk_id`, `summary`, `section_path`.

**Не делать:**

- Не передавай в `field` значения вне списка — будет отказ с понятной ошибкой.
- Не разбирай документ заново ради follow-up-вопроса: результат уже
  посчитан и лежит в состоянии операции.
- Не выдумывай `operation_id` — он всегда приходит из ответа
  `platform.analyze_document`; переданный сверяется с вычисленным из
  документа и его параметров, и чужое значение откажется.
- Не обещай follow-up раньше, чем разбор вернул `operation_id`: без готового
  состояния эта операция откажет.

## audit_analyzer — операции платформы

Данные аудита читаются операциями capability `audit`, а не командами в
консоли. Кликабельный вход — `audit.list_scripts`,
`audit.run_script`, `audit.generate_sql` и
`vectors.vector_search`; доменный разбор — в навыке
`workspace/skills/audit_analyzer/SKILL.md`, общий контракт вызовов — в
навыке `workspace/skills/enterprise_mcp/SKILL.md`.

| Операция | Назначение | Когда |
|---|---|---|
| `audit.generate_sql` | Ответ на вопрос по данным фразой | Числовые и структурные запросы |
| `audit.run_script` | Точный расчёт готовым скриптом из каталога | Есть подходящий скрипт |
| `vectors.vector_search` | Семантический поиск по снимку | Похожие формулировки, «найди похожее» |
| `audit.list_scripts` | Каталог доступных скриптов | Не знаешь, что можно спросить |

**Не делать:** не искать обходной путь к данным аудита. Операции платформы
— единственный доступный: командной строки в этом рантайме нет, а сама
операция даёт изоляцию вызова и запись в журнал. Обходного пути, который
увидел бы то же самое, не существует.

