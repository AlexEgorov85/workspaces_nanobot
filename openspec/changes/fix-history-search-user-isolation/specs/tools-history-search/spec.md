# tools-history-search

## Purpose

Контракт кастомного tool `history_search`: параметры, семантика
области поиска (`current` / `all`), правила изоляции данных по
пользователю и поведение при отсутствии идентификатора пользователя.
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

### Requirement: session_scope = current filters by session_id

WHEN `session_scope="current"` (значение по умолчанию), tool SHALL
вернуть только события, у которых `session_id` совпадает с ключом
текущего запроса. Tool MUST NOT включать события других сессий
независимо от их `user_id`. Параметр `user_id` НЕ участвует в
предикате `current`: даже если для строки той же сессии записан
ошибочный `user_id`, `current` всё равно вернёт её — исправление
ownership выполняется в logging pipeline, а не в search-семантике.

#### Scenario: current scope filters by session_id

- WHEN tool исполняется с `session_scope="current"` в запросе,
  чей `session_key = "telegram:123"`
- THEN результирующий SQL SHALL содержать предикат
  `session_id = %s` с параметром = `"telegram:123"`
- AND SHALL NOT содержать предикат по `user_id` или unscoped
  условие.

#### Scenario: current scope with mismatched user_id still returns the row

- GIVEN событие с `session_id="telegram:123"`,
  `user_id="stale_value"`
- WHEN пользователь той же сессии вызывает
  `history_search(session_scope="current")`
- THEN событие SHALL быть возвращено
- AND `user_id` в payload'е события SHALL NOT сравниваться с
  текущим пользователем.

### Requirement: session_scope = all filters by user_id

WHEN `session_scope="all"`, tool SHALL вернуть только события,
у которых `user_id` совпадает с идентификатором пользователя
текущего запроса. Tool MUST NOT выполнять запрос, охватывающий
события других пользователей. Источник `user_id` —
существующий request context; в реализации это поле dataclass,
которое в nanobot 0.3.0 называется `sender_id`, а в будущих
версиях может называться иначе — спека фиксирует **роль**
identity-store, не имя поля.

#### Scenario: all scope filters by user_id

- WHEN tool исполняется с `session_scope="all"` в запросе,
  чей identity-store возвращает `"alice"`
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
идентификатора пользователя (identity-store недоступен или
identity = `None`), tool SHALL NOT выполнять SQL-запрос к
`agent_gateway_logs`. Tool SHALL вернуть ответ со статусом
`"error"`, `error_type="missing_user_identity"` и человекочитаемым
сообщением. Отсутствие identity = отсутствие разрешения на
cross-session search.

#### Scenario: all scope without user identity returns error

- WHEN tool исполняется с `session_scope="all"` без
  request context (например, в тестах или standalone-утилитах)
- THEN ответ SHALL быть `{"status": "error", "error_type":
  "missing_user_identity", "message": "..."}`
- AND SQL-запрос к `agent_gateway_logs` SHALL NOT быть выполнен.

### Requirement: no unscoped fallback and no identity derivation

Tool MUST NOT реализовывать unscoped fallback вида
`WHERE (%s IS NULL OR user_id = %s)` для `session_scope="all"`.
Tool MUST NOT извлекать `user_id` из `session_id`, `chat_id`,
`actor`, `payload`, `name` или любого другого поля события.
Источник `user_id` для фильтрации — единственный: identity-store
текущего request.

#### Scenario: no user_id derivation from session_id

- GIVEN `session_id="telegram:123"`
- WHEN tool вычисляет `current_user_id` для `session_scope="all"`
- THEN tool SHALL NOT парсить `session_id` и выводить `user_id`
  из него
- AND при отсутствии identity-store SHALL вернуть
  `missing_user_identity` вне зависимости от `session_id`.

### Requirement: actor is not a user identity

Tool MUST NOT интерпретировать `actor` (`user`/`agent`/`system`/
`sync`) как идентификатор пользователя. `actor` — роль источника
события, а не пользователь. Поле `user_id` — единственный
идентификатор пользователя в `agent_gateway_logs`.

#### Scenario: actor=user and actor=agent do not change scope

- GIVEN в выборке есть события с `actor="user"` и
  `actor="agent"`, оба с `user_id="alice"`
- WHEN alice вызывает `history_search(session_scope="all")`
- THEN оба типа событий SHALL быть возвращены
- AND ни одно событие другого `user_id` SHALL NOT быть возвращено.

### Requirement: pagination and ordering

Tool SHALL поддерживать параметр `offset` (целое ≥ 0, дефолт 0).
Результирующий SQL SHALL использовать
`ORDER BY "timestamp" DESC, "id" DESC LIMIT %s OFFSET %s`,
где `LIMIT = effective_limit + 1` (лишняя строка используется для
определения наличия следующей страницы и не возвращается агенту).

#### Scenario: deterministic ordering

- WHEN tool выполняет SQL с двумя и более событиями с одинаковым
  `timestamp`
- THEN порядок SHALL быть детерминирован по `id DESC`.

### Requirement: response shape and no user_id leak

Tool SHALL возвращать JSON-строку с полями `status`, `count`,
`session_scope`, `has_more`, `next_offset`, `results_truncated`,
`events`. Каждое событие SHALL содержать `event_id`, `timestamp`,
`event_type`, `name`, `level`, `summary`, `payload`,
`payload_truncated`. Поле `payload` SHALL быть JSON-строкой.
Tool SHALL NOT возвращать поле `user_id` ни в payload'е ответа, ни
в событиях — это внутренний security attribute, а не часть
видимого агенту контракта.

#### Scenario: response does not leak user_id

- WHEN tool возвращает JSON-ответ
- THEN ни на одном уровне (корень, события, payload'ы) SHALL NOT
  быть поля `user_id`
- AND `session_scope` SHALL принимать значение `"current"` или
  `"all"` в зависимости от того, что запросил агент.

### Requirement: history_search is read-only

Tool `history_search` MUST NOT выполнять `INSERT`/`UPDATE`/`DELETE`/
`MERGE`/`TRUNCATE`/DDL против `agent_gateway_logs` или любых
других таблиц. Все события, попадающие в выборку, созданы через
`LogEvent` → `DbLoggingService._insert_batch` (см. capability
`logging-db`).

#### Scenario: history_search is read-only

- WHEN tool исполняется
- THEN его SQL SHALL содержать только `SELECT` и `FROM
  "..."."agent_gateway_logs"`
- AND SHALL NOT содержать `INSERT`, `UPDATE`, `DELETE`, `MERGE`,
  `TRUNCATE` или DDL.

### Requirement: user_id in agent_gateway_logs (denormalization)

Колонка `user_id` SHALL присутствовать в `agent_gateway_logs`.
Это **намеренное исключение** из прежнего правила «identity живёт
только в `agent_question_runs`»: `user_id` стал security boundary
для чтения событий, и без него `session_scope="all"` требует JOIN
на каждый поиск. Денормализация оправдана, потому что:
- колонка держится в синхронности через `DbLoggingService`
  (единственный writer);
- `agent_question_runs.user_id` остаётся первичным источником
  правды; при расхождении — он выигрывает (правило см. в
  requirement «Backfill historical events»).

#### Scenario: agent_gateway_logs has user_id column

- GIVEN DDL `agent_gateway_logs`
- THEN таблица SHALL содержать колонку `user_id VARCHAR(256)`
  рядом с `request_id`/`session_id`/`channel`/`actor`/`name`
- AND колонка SHALL быть задокументирована через `COMMENT ON
  COLUMN` с явным указанием источника.

### Requirement: user_id plumbed through LogEvent

`LogEvent.user_id` MUST доходить до колонки
`agent_gateway_logs.user_id`. Когда producer (`log_inbound`,
`log_outbound`, `log_tool_call`, `log_tool_result`,
`log_llm_call`, `log_error`, `ContextCompactionService`) не задаёт
`LogEvent.user_id` явно, `DbLoggingService` SHALL подтянуть
`user_id` из внутреннего индекса `session_key → {request_id, user_id}`,
заполняемого `DbLoggingService.register_request`. Индекс MUST NOT
быть публичным API — это внутренний механизм `_enqueue`.

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

### Requirement: register_request atomically updates identity

`DbLoggingService.register_request` MUST атомарно обновлять обе
записи индекса — `request_id` и `user_id` — вместе. После вызова
`register_request` для `session_key` никакие события предыдущего
request не должны наследовать `user_id` от предыдущего request
в той же сессии. Контракт: индекс `session_key → {request_id, user_id}`
— это парная запись, обновляемая одной операцией под одним lock'ом.

#### Scenario: same session_key switches user atomically

- GIVEN `register_request(session_key="telegram:123",
  request_id="req-A", user_id="alice")` уже был вызван
- AND `register_request(session_key="telegram:123",
  request_id="req-B", user_id="bob")` далее вызывается
- WHEN `LogEvent(event_type="tool_call",
  session_id="telegram:123", user_id=None, request_id="req-B")`
  эмиттируется
- THEN в БД SHALL быть записано `user_id="bob"`
- AND SHALL NOT быть записано `user_id="alice"`.

### Requirement: subagent inherits parent user_id

Когда subagent эмиттирует событие `subagent_run_finished` (или
любое событие в рамках subagent-прогона), оно SHALL нести
`user_id` родительского request. Subagent MUST NOT полагаться на
автозаполнение через `_request_index` для своего `session_key`,
потому что под-pipeline может менять контекст — единственный
надёжный путь — явная передача `user_id` родителя в `LogEvent`.

#### Scenario: subagent inherits parent user_id

- GIVEN parent request выполняется для user_id="alice"
- AND subagent запускается через
  `RuntimePatcher._SubagentLoggingHook`
- WHEN `_SubagentLoggingHook._finalize` эмиттирует
  `subagent_run_finished`
- THEN `LogEvent.user_id` SHALL быть `"alice"`
- AND не должно быть способа, при котором subagent сменил бы
  `user_id` на собственный identity-store.

#### Scenario: previous request user_id does not leak into next request

- GIVEN `register_request(session_key="telegram:123",
  request_id="req-A", user_id="alice")` уже был вызван
- AND `clear_request("telegram:123")` выполнен
- AND новый `register_request(session_key="telegram:123",
  request_id="req-B", user_id="bob")` зарегистрирован
- WHEN `LogEvent(event_type="tool_call",
  session_id="telegram:123", user_id=None)` эмиттируется
  в рамках req-B
- THEN `user_id` SHALL быть `"bob"`, не `"alice"`.

### Requirement: backfill historical events by request_id

DDL-миграция SHALL заполнить `agent_gateway_logs.user_id` для
существующих строк через JOIN с `agent_question_runs` по
`request_id`, и **только** когда `agent_question_runs.user_id IS
NOT NULL`. Строки без `request_id`, или без соответствующей записи
в `agent_question_runs`, или с `agent_question_runs.user_id IS
NULL`, SHALL остаться с `agent_gateway_logs.user_id IS NULL`.
Эти строки SHALL NOT участвовать в результатах
`session_scope="all"`.

#### Scenario: backfilled events get their owner

- GIVEN строка `agent_gateway_logs` с `request_id="req-1"`
  и `agent_question_runs` с `request_id="req-1"` и
  `user_id="alice"`
- WHEN применяется миграция
- THEN `agent_gateway_logs.user_id` для этой строки SHALL стать
  `"alice"`.

#### Scenario: question_run with NULL user_id does not leak into gateway_log

- GIVEN строка `agent_gateway_logs` с `request_id="req-1"`
  и `agent_question_runs` с `request_id="req-1"` и
  `user_id IS NULL`
- WHEN применяется миграция
- THEN `agent_gateway_logs.user_id` SHALL остаться `NULL`
- AND при `history_search(session_scope="all")` эта строка
  SHALL NOT быть возвращена ни одному пользователю.

#### Scenario: historical events without request_id are excluded from all

- GIVEN строка `agent_gateway_logs` с `request_id IS NULL`
  и `user_id IS NULL` после миграции
- WHEN пользователь вызывает `history_search(session_scope="all")`
- THEN эта строка SHALL NOT появиться в ответе.

### Requirement: index for all-scope access

DDL SHALL содержать индекс на `agent_gateway_logs`, обслуживающий
access-pattern `WHERE user_id = ? ORDER BY "timestamp" DESC`. Имя
индекса и колонки — на усмотрение реализации; требование — индекс
**существует и совместим** с этим pattern'ом. Индекс по `session_id`
SHALL NOT использоваться как замена пользовательскому фильтру.

#### Scenario: DDL provides user_id access index

- GIVEN DDL `agent_gateway_logs`
- THEN SHALL существовать индекс с ведущей колонкой `user_id`
  и поддержкой сортировки по `"timestamp" DESC`
- AND его назначение SHALL быть задокументировано через
  `COMMENT ON INDEX`.

### Requirement: tool API has no user_id parameter

Tool API SHALL NOT принимать `user_id` (ни прямо, ни косвенно через
любой другой параметр). `user_id` — внутренний security attribute,
получаемый из request context. LLM не должна иметь возможность
выбрать security boundary через параметр tool'а.

#### Scenario: schema has no user_id parameter

- WHEN tool публикует свою JSON-schema
- THEN в `properties` SHALL NOT быть поля `user_id` (или
  эквивалента вида `principal_id` / `actor_id` / `owner_id`)
- AND в `required` SHALL NOT быть такого поля.
