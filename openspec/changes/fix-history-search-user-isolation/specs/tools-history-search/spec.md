# tools-history-search

## Purpose

Определяет контракт кастомного tool `history_search`: параметры,
семантику области поиска (`current` / `all`), правила изоляции данных
по пользователю и поведение при отсутствии идентификатора пользователя.
Инструмент работает поверх долговечного журнала `agent_gateway_logs`,
который переживает context compaction и является основным источником
данных для восстановления деталей, выпавших из контекста LLM.

## ADDED Requirements

### Requirement: tool identity and scope

Tool `history_search` SHALL искать события в таблице
`agent_gateway_logs` (имя и схема задаются `logging.db.table_name` и
`logging.db.schema`; дефолт — `public.agent_gateway_logs`). Tool MUST
NOT выполнять произвольный SQL, обращаться к другим таблицам или
делать `INSERT`/`UPDATE`/`DELETE`. Все параметры запроса (включая
текстовые) MUST передаваться позиционными `%s`-параметрами без
интерполяции в SQL-строку.

#### Scenario: parameters are bound via placeholders

- WHEN tool исполняется с произвольным `query`, `event_type`,
  `tool_name`, `since`, `until`, `limit`, `offset`
- THEN результирующий SQL SHALL использовать только `%s`-параметры
- AND SHALL NOT содержать интерполяцию пользовательских значений в
  строку запроса.

### Requirement: session_scope = current

WHEN `session_scope="current"` (значение по умолчанию), tool SHALL
вернуть только события, у которых `session_id` совпадает с ключом
текущего запроса (берётся из `RequestContext.session_key`,
`channel:chat_id`). Tool MUST NOT включать события других сессий
независимо от их `user_id`.

#### Scenario: current scope filters by session_id

- WHEN tool исполняется с `session_scope="current"` в запросе,
  чей `RequestContext.session_key = "telegram:123"`
- THEN результирующий SQL SHALL содержать предикат
  `session_id = %s` с параметром = `"telegram:123"`
- AND SHALL NOT содержать предикат по `user_id` или unscoped
  условие.

### Requirement: session_scope = all filters by user

WHEN `session_scope="all"`, tool SHALL вернуть только события,
у которых `user_id` совпадает с идентификатором пользователя
текущего запроса. Идентификатор пользователя берётся из
`RequestContext.sender_id`. Tool MUST NOT выполнять запрос,
охватывающий события других пользователей.

#### Scenario: all scope filters by user_id

- WHEN tool исполняется с `session_scope="all"` в запросе,
  чей `RequestContext.sender_id = "alice"`
- THEN результирующий SQL SHALL содержать предикат
  `user_id = %s` с параметром = `"alice"`
- AND SHALL NOT содержать предикат `session_id = %s`
- AND SHALL NOT содержать unscoped условие (например, `OR TRUE`).

#### Scenario: cross-user isolation

- GIVEN в `agent_gateway_logs` существуют события с
  `user_id="alice"` и `user_id="bob"`
- WHEN alice вызывает `history_search(session_scope="all")`
- THEN ответ SHALL содержать только события с `user_id="alice"`
- AND SHALL NOT содержать ни одного события с `user_id="bob"`.

### Requirement: missing user identity is a hard error

WHEN `session_scope="all"` AND текущий запрос не имеет
идентификатора пользователя (`RequestContext.sender_id is None`
или `RequestContext` отсутствует), tool SHALL NOT выполнять SQL-запрос
к `agent_gateway_logs`. Tool SHALL вернуть ответ со статусом
`"error"`, `error_type="missing_user_identity"` и человекочитаемым
сообщением.

#### Scenario: all scope without user identity returns error

- WHEN tool исполняется с `session_scope="all"` без
  `RequestContext` (например, в тестах или standalone-утилитах)
- THEN ответ SHALL быть `{"status": "error", "error_type":
  "missing_user_identity", "message": "..."}`
- AND SQL-запрос к `agent_gateway_logs` SHALL NOT быть выполнен.

### Requirement: no unscoped fallback for user filter

Tool MUST NOT реализовывать unscoped fallback вида
`WHERE (%s IS NULL OR user_id = %s)` для `session_scope="all"`.
Tool MUST NOT извлекать `user_id` из `session_id`, `chat_id`,
`actor`, `payload` или любого другого поля события. Источник
`user_id` для фильтрации — единственный и только
`RequestContext.sender_id`.

#### Scenario: no user_id derivation from session_id

- GIVEN `session_id="telegram:123"`
- WHEN tool вычисляет `current_user_id` для `session_scope="all"`
- THEN tool SHALL NOT парсить `session_id` и выводить
  `user_id` из него
- AND при отсутствии `RequestContext.sender_id` SHALL вернуть
  `missing_user_identity` вне зависимости от `session_id`.

### Requirement: pagination and ordering

Tool SHALL поддерживать параметр `offset` (целое ≥ 0, дефолт 0).
Результирующий SQL SHALL использовать
`ORDER BY "timestamp" DESC, "id" DESC LIMIT %s OFFSET %s`,
где `LIMIT = effective_limit + 1` (лишняя строка используется для
определения наличия следующей страницы и не возвращается агенту).
Это требование совместимо с ранее зафиксированным контрактом
(см. tasks `improve-history-search-pagination-and-logging` § 3.2).

#### Scenario: deterministic ordering

- WHEN tool выполняет SQL с двумя и более событиями с одинаковым
  `timestamp`
- THEN порядок SHALL быть детерминирован по `id DESC`.

### Requirement: response shape

Tool SHALL возвращать JSON-строку с полями `status`, `count`,
`session_scope`, `has_more`, `next_offset`, `results_truncated`,
`events`. Каждое событие SHALL содержать `event_id`, `timestamp`,
`event_type`, `name`, `level`, `summary`, `payload`,
`payload_truncated`. Поле `payload` SHALL быть JSON-строкой.
Tool SHALL NOT возвращать поле `user_id` в payload'е ответа (это
внутренний security attribute, а не часть видимого агенту контракта).

#### Scenario: response does not leak user_id

- WHEN tool возвращает JSON-ответ
- THEN поле `user_id` в payload'е события SHALL быть
  отсутствующим, даже если в БД оно заполнено
- AND `session_scope` SHALL принимать значение `"current"` или
  `"all"` в зависимости от того, что запросил агент.

### Requirement: single writer for agent_gateway_logs

`agent_gateway_logs` SHALL модифицироваться только через
`DbLoggingService` (см. capability `logging-db`). Tool
`history_search` MUST NOT выполнять `INSERT`/`UPDATE`/`DELETE`
в этой таблице. Все события, которые попадают в выборку, созданы
через `LogEvent` → `DbLoggingService._insert_batch`.

#### Scenario: history_search is read-only

- WHEN tool исполняется
- THEN его SQL SHALL содержать только `SELECT` и `FROM` `agent_gateway_logs`
- AND SHALL NOT содержать `INSERT`, `UPDATE`, `DELETE`, `MERGE`,
  `TRUNCATE` или DDL.

### Requirement: user_id plumbed through logging pipeline

`LogEvent.user_id` MUST доходить до колонки
`agent_gateway_logs.user_id`. Когда producer (`log_inbound`,
`log_outbound`, `log_tool_call`, `log_tool_result`,
`log_llm_call`, `log_error`, `ContextCompactionService`) не задаёт
`LogEvent.user_id` явно, `DbLoggingService` SHALL подтянуть
`user_id` из индекса `session_key → {request_id, user_id}`,
заполняемого `DbLoggingService.register_request`.

#### Scenario: empty LogEvent.user_id is filled from request index

- GIVEN `register_request(session_key="telegram:123",
  request_id="req-1", user_id="alice")` уже был вызван
- AND `LogEvent(event_type="tool_call", session_id="telegram:123",
  user_id=None)` эмиттируется в рамках того же request
- THEN `DbLoggingService` SHALL записать в `agent_gateway_logs`
  строку с `user_id="alice"`.

#### Scenario: explicit LogEvent.user_id overrides the index

- GIVEN индекс содержит `user_id="alice"` для
  `session_key="telegram:123"`
- WHEN `LogEvent(event_type="tool_call",
  session_id="telegram:123", user_id="bob")` эмиттируется
- THEN `DbLoggingService` SHALL записать строку с `user_id="bob"`.

### Requirement: backfill historical events

DDL-миграция SHALL заполнить `agent_gateway_logs.user_id` для
существующих строк через JOIN с `agent_question_runs` по
`request_id`. Строки без `request_id` (или без соответствующей
записи в `agent_question_runs`) SHALL остаться с
`user_id IS NULL` и SHALL NOT участвовать в результатах
`session_scope="all"` (по требованию изоляции это безопаснее, чем
выдавать чужие данные).

#### Scenario: historical events with request_id are backfilled

- GIVEN строка `agent_gateway_logs` с `request_id="req-1"`
  и `agent_question_runs` с `request_id="req-1"` и
  `user_id="alice"`
- WHEN применяется миграция
- THEN `agent_gateway_logs.user_id` для этой строки SHALL стать
  `"alice"`.

#### Scenario: historical events without request_id are excluded from all

- GIVEN строка `agent_gateway_logs` с `request_id IS NULL`
  и `user_id IS NULL` после миграции
- WHEN пользователь вызывает `history_search(session_scope="all")`
- THEN эта строка SHALL NOT появиться в ответе.

### Requirement: index for all scope

DDL SHALL содержать индекс `(user_id, "timestamp" DESC)` на
`agent_gateway_logs`, обслуживающий запрос
`session_scope="all"`. Индекс по `session_id` SHALL NOT
использоваться как замена пользовательскому фильтру.

#### Scenario: query plan uses user_id index

- WHEN планировщик СУБД оценивает запрос
  `WHERE user_id = %s ORDER BY "timestamp" DESC`
- THEN ожидаемый план SHOULD использовать индекс
  `(user_id, "timestamp" DESC)`.

### Requirement: actor is not a user identity

Tool MUST NOT интерпретировать `actor` (`user`/`agent`/`system`/
`sync`) как идентификатор пользователя. `actor` — это роль источника
события, а не пользователь. Поле `user_id` — единственный
идентификатор пользователя в `agent_gateway_logs`.

#### Scenario: actor=user and actor=agent do not change scope

- GIVEN в выборке есть события с `actor="user"` и
  `actor="agent"`, оба с `user_id="alice"`
- WHEN alice вызывает `history_search(session_scope="all")`
- THEN оба типа событий SHALL быть возвращены
- AND ни одно событие другого `user_id` НЕ должно быть возвращено.
