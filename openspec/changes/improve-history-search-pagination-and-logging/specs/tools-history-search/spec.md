## Purpose

Определяет контракт кастомного tool `history_search` для агента:
параметры запроса, формат ответа, поведение при пустой выборке,
семантика truncation-флагов, детерминированная пагинация.
Это единый нормативный источник для всех мест, где упоминается
`history_search` (раньше был размазан между
`workspace/tools/history_search_tool.py`, `workspace/TOOLS.md`,
`docs/architecture/HISTORY_SEARCH_ANALYSIS.md`).

## ADDED Requirements

### Requirement: Параметры запроса

The system SHALL provide кастомный tool `history_search` со
следующими параметрами (все опциональны):

- `query` — подстрока для регистронезависимого поиска (ILIKE)
  по `summary` и `payload::text` колонок журнала
  `agent_gateway_logs`.
- `event_type` — одно из значений enum:
  `context_compacted`, `tool_call`, `tool_result`, `llm_call`,
  `run_finished`, `subagent_run_finished`, `inbound`. Если не
  передано — фильтр по типу не применяется. Передача значения
  вне списка SHALL приводить к ошибке валидации параметров с
  явным указанием допустимых значений.
- `tool_name` — строка-имя инструмента; применимо только
  совместно с `event_type ∈ {tool_call, tool_result}`. Если
  `event_type` имеет другое значение, фильтр `tool_name`
  SHALL просто не давать совпадений (без ошибки).
- `since` / `until` — ISO-8601 таймстампы; нижняя/верхняя
  граница соответственно. Обе границы SHALL трактоваться как
  inclusive (`>=` / `<=`).
- `session_scope` — `current` (по умолчанию) или `all`.
  `current` фильтрует события по `session_id` текущего запроса;
  `all` снимает фильтр.
- `limit` — максимум событий в ответе; верхняя граница берётся
  из `tools.history_search.max_rows` в `config.json`
  (дефолт 50, диапазон 1–500).
- `offset` — целое ≥ 0; пропуск первых `offset` событий
  после сортировки `ORDER BY timestamp DESC, id DESC`.
  Дефолт 0. При `offset > 0` и `session_scope="current"`
  SHALL продолжать применять фильтр по текущему `session_id`.

#### Scenario: Фильтр по event_type + tool_name
- **WHEN** агент вызывает `history_search(event_type="tool_call", tool_name="history_search", limit=3)`
- **THEN** tool возвращает не более 3 событий, у которых
  `event_type='tool_call'` И `name='history_search'`,
  отсортированных по `(timestamp DESC, id DESC)`

#### Scenario: Невалидный event_type
- **WHEN** агент передаёт `event_type="file_created"`
- **THEN** tool отвечает ошибкой валидации параметров
  с сообщением "event_type must be one of [...]" и
  полным списком допустимых значений

#### Scenario: since/until inclusive
- **WHEN** агент передаёт `since="2024-01-01T00:00:00Z", until="2024-01-02T00:00:00Z"`
- **THEN** в выборку включаются события с
  `2024-01-01T00:00:00Z <= timestamp <= 2024-01-02T00:00:00Z`

### Requirement: Формат ответа и пагинация

The system SHALL возвращать JSON-строку со следующей структурой:

- `status` — `"success"` или `"error"`.
- `count` — количество событий в массиве `events` ответа
  (после всех truncation-проходов).
- `session_scope` — фактически применённый scope (`"current"`
  или `"all"`).
- `has_more` — `true`, если в БД есть подходящие события
  после возвращённой страницы. Определяется через
  `LIMIT effective_limit + 1` в SQL: запрос читает на одну
  строку больше, чем `effective_limit`; если фактически
  получено `> effective_limit`, лишняя строка отбрасывается
  и `has_more = true`.
- `results_truncated` — `true`, если из выборки были выброшены
  целые события, чтобы общий JSON влез в `max_result_chars`.
  Дефолт `false`. `results_truncated` MUST NOT
  интерпретироваться как «есть ещё результаты в БД» —
  для этого используется `has_more`.
- `truncated` — **deprecated алиас** `results_truncated`.
  Сохраняется в течение одного MINOR-релиза после введения
  нового контракта; удаляется отдельным follow-up change.
- `events` — массив объектов, отсортированных по
  `(timestamp DESC, id DESC)`. Каждый объект SHALL содержать:
  - `event_id` — UUID строки `agent_gateway_logs`;
  - `timestamp` — ISO-8601;
  - `event_type`, `name`, `level`, `summary` — как в БД;
  - `payload` — JSON-string (может быть обрезан; см. truncation);
  - `payload_truncated: bool` — `true`, если payload этого
    события был обрезан до `per_event_cap` символов через
    `truncate_middle` (сохраняет голову и хвост, маркер
    "(N chars truncated)" в середине). Дефолт `false`.

#### Scenario: Успешный ответ без truncation
- **WHEN** запрос возвращает 5 событий, ни одно не обрезано,
  в БД больше нет подходящих
- **THEN** ответ имеет
  `count=5`, `has_more=false`, `results_truncated=false`,
  `truncated=false`, каждое событие имеет `payload_truncated=false`

#### Scenario: has_more=true при наличии следующей страницы
- **WHEN** в БД 25 подходящих событий, `limit=10`, `offset=0`
- **THEN** ответ содержит 10 событий, `count=10`,
  `has_more=true`, `results_truncated=false`,
  агент может вызвать тот же запрос с `offset=10`

#### Scenario: has_more=false на последней странице
- **WHEN** в БД 25 подходящих событий, `limit=10`, `offset=20`
- **THEN** ответ содержит 5 событий, `count=5`,
  `has_more=false`, `results_truncated=false`

#### Scenario: results_truncated при превышении max_result_chars
- **WHEN** в БД 100 подходящих событий, `limit=10`,
  `max_result_chars=1000`, итого JSON 10 событий
  превышает 1000 символов
- **THEN** ответ содержит менее 10 событий,
  `count=N < 10`, `results_truncated=true`,
  `has_more` отражает фактическое наличие следующей страницы
  (для последних страниц `false`, для промежуточных `true`)

#### Scenario: payload_truncated при большом llm_call
- **WHEN** `llm_call` событие имеет payload длиной 200 000 символов
- **THEN** в ответе payload обрезан до `per_event_cap` (4000)
  через `truncate_middle`, на этом событии
  `payload_truncated=true`, остальные события имеют
  `payload_truncated=false` независимо от `results_truncated`

#### Scenario: results_truncated и payload_truncated независимы
- **WHEN** выборка содержит одно событие с payload > per_event_cap,
  и общий JSON влезает в `max_result_chars`
- **THEN** `results_truncated=false`, `payload_truncated=true`
  на этом событии

### Requirement: Детерминированный порядок страниц

The system SHALL использовать SQL-сортировку
`ORDER BY "timestamp" DESC, "id" DESC` для всех запросов
`history_search`. Tie-breaker по `id` (UUID из
`agent_gateway_logs.id`) SHALL гарантировать, что при равных
`timestamp` (например, события из одного батча flush'а)
порядок строк между последовательными вызовами с одним
и тем же фильтром и `offset` остаётся стабильным, и
страницы не пропускают/дублируют события на границе.

#### Scenario: События одного батча на границе страниц
- **WHEN** в одном батче записано 5 событий с одинаковым
  `timestamp`; `limit=2`; вызов с `offset=0` возвращает
  события A и B
- **THEN** вызов с `offset=2` возвращает события C и D
  (а не «D и B снова»)

### Requirement: Пустой результат — честный success

The system SHALL при отсутствии совпадений возвращать
`{"status": "success", "count": 0, "has_more": false,
"results_truncated": false, "truncated": false, "events": []}`
без ошибки. Агент при `count == 0` SHALL интерпретировать это
как «не найдено в истории» (см. `workspace/TOOLS.md`).

#### Scenario: Запрос без совпадений
- **WHEN** агент вызывает `history_search(query="xyz_nonexistent_12345")`
- **THEN** ответ — success с `count=0`, `has_more=false`,
  `events=[]`, `results_truncated=false`, `truncated=false`

### Requirement: Схема payload по event_type

The system SHALL задокументировать в `workspace/TOOLS.md`
явную JSON-схему `payload` для каждого допустимого
`event_type`. Схема описывает **текущую** форму данных
на момент публикации change и явно помечает поля,
сериализованные как JSON-string (например,
`tool_result.payload.result`). Изменение формы данных
требует отдельного change.

Минимум:

- `tool_call.payload`: `{tool, args, tool_call_id}`;
  все поля простых типов или dict'ы.
- `tool_result.payload`: `{tool, status, result, error}`;
  `result` хранится как JSON-string (может требовать
  `json.loads` для получения структуры; `result`
  сериализуется через `psycopg2.extras.Json` и при
  больших объёмах обрезается с маркером
  "(N chars truncated)").
- `llm_call.payload`: `{prompt, response}`;
  `prompt` — массив ролей (system/user/assistant/tool),
  `response` — объект с контентом и метаданными.
- `run_finished.payload`: `{final_content, tools_used,
  stop_reason, had_injections, request_id?}`;
  все поля простых типов или list/str.
- `subagent_run_finished.payload`: `{final_content,
  tools_used, stop_reason, task_id, task, request_id,
  parent_request_id}`.
- `inbound.payload`: `{content, message_id, sender_id?,
  chat_id?, media?}`; `media` — list объектов
  `MediaItem` (см. `workspace/utils/media.py`).
- `context_compacted.payload`: определяется реализацией
  `ContextCompactionService._notify` на момент архивации
  spec (snapshot, а не долгосрочный контракт).

#### Scenario: Агент парсит tool_result
- **WHEN** агент получает событие `event_type="tool_result"`
- **THEN** он может предсказуемо прочитать `payload.tool`
  и `payload.status` напрямую; для `payload.result`
  при наличии структурированного ответа агент применяет
  `json.loads(payload.result)`
