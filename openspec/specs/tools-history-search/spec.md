# tools-history-search Specification

## Purpose
Контракт кастомного tool `history_search`: параметры, семантика
области поиска (`current` / `all`), правила изоляции данных по
пользователю и поведение при отсутствии идентификатора пользователя.
Инструмент работает поверх долговечного журнала `agent_gateway_logs`,
который переживает context compaction и является основным источником
данных для восстановления деталей, выпавших из контекста LLM.

## Requirements

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

### Requirement: user_id inheritance rule

`LogEvent.user_id` MUST доходить до колонки
`agent_gateway_logs.user_id`. Решение о том, какое значение
записать, определяется тремя ветвями, проверяемыми в `_enqueue`
в указанном порядке:

1. **Explicit value wins.** Если `event.user_id` явно задано
   producer'ом — используется оно, индекс не читается.
2. **Match by request_id.** Если `event.user_id is None` AND
   `event.request_id is not None` AND
   `event.session_id` присутствует в индексе AND
   `index[event.session_id]["request_id"] == event.request_id` —
   подставляется `index[event.session_id]["user_id"]`.
3. **No inference.** Во всех остальных случаях
   (`event.request_id is None`, или `request_id` не совпадает с
   текущим request в индексе, или `session_id` отсутствует в
   индексе) — `event.user_id` остаётся `None`. Событие
   записывается с `user_id IS NULL` и SHALL NOT участвовать
   в результатах `session_scope="all"`.

Правило MUST NOT быть смягчено: если `event.request_id is None`
— `user_id` НЕ выводится только по `session_id`. Это закрывает
класс атак вида «событие без identity получает текущего
пользователя сессии».

#### Scenario: explicit LogEvent.user_id overrides the index

- GIVEN индекс содержит `{"request_id": "req-1",
  "user_id": "alice"}` для `session_key="telegram:123"`
- WHEN `LogEvent(event_type="tool_call",
  session_id="telegram:123", request_id="req-1",
  user_id="bob")` эмиттируется
- THEN `DbLoggingService` SHALL записать строку с `user_id="bob"`.

#### Scenario: empty user_id is filled when request_id matches

- GIVEN `register_request(session_key="telegram:123",
  request_id="req-1", user_id="alice")` уже был вызван
- AND `LogEvent(event_type="tool_call",
  session_id="telegram:123", request_id="req-1",
  user_id=None)` эмиттируется
- THEN `DbLoggingService` SHALL записать строку с `user_id="alice"`.

#### Scenario: stale event does not inherit next request's user_id

- GIVEN `register_request(session_key="telegram:123",
  request_id="req-A", user_id="alice")` уже был вызван
- AND продюсер создал `LogEvent(event_type="tool_call",
  session_id="telegram:123", request_id="req-A",
  user_id=None)`, но ещё НЕ вызвал `_enqueue`
- AND `register_request(session_key="telegram:123",
  request_id="req-B", user_id="bob")` выполнен между
  созданием события и его `_enqueue`
- WHEN этот отложенный `LogEvent` ставится в очередь
- THEN записанная строка SHALL иметь `user_id IS NULL`
- AND SHALL NOT иметь `user_id="bob"`.

#### Scenario: event without request_id does not inherit any user_id

- GIVEN индекс для `session_key="telegram:123"` содержит
  `user_id="alice"`
- WHEN `LogEvent(event_type="tool_call",
  session_id="telegram:123", request_id=None,
  user_id=None)` эмиттируется
- THEN записанная строка SHALL иметь `user_id IS NULL`
- AND SHALL NOT быть показана в `history_search(session_scope="all")`.

### Requirement: register_request atomically updates identity

`DbLoggingService.register_request` MUST атомарно обновлять обе
записи индекса — `request_id` и `user_id` — вместе. Контракт:
индекс `session_key → {request_id, user_id}` — это парная запись,
обновляемая одной операцией под одним lock'ом. Никакого
промежуточного состояния, в котором индекс содержит `request_id`
нового request со старым `user_id`, наблюдаться не должно.

Правило «Match by request_id» из requirement «user_id inheritance
rule» работает **совместно** с этим: событие `req-A` после
перерегистрации `req-B` теряет доступ к `user_id` индекса не
потому, что индекс «закрыт», а потому, что `event.request_id !=
entry.request_id`. Lock в `register_request` обеспечивает
согласованность пары, request_id matching в `_enqueue` —
корректность выбора.

#### Scenario: pair is updated atomically under lock

- WHEN `register_request(session_key="telegram:123",
  request_id="req-B", user_id="bob")` начинает выполняться
- AND параллельный поток пытается прочитать индекс в этот
  момент
- THEN параллельный поток SHALL наблюдать либо полностью
  старое состояние (`request_id="req-A", user_id="alice"`),
  либо полностью новое (`request_id="req-B", user_id="bob"`),
  но не смесь.

#### Scenario: same session_key switches user, current event still correct

- GIVEN `register_request(session_key="telegram:123",
  request_id="req-A", user_id="alice")` уже был вызван
- AND `register_request(session_key="telegram:123",
  request_id="req-B", user_id="bob")` далее вызывается
- WHEN `LogEvent(event_type="tool_call",
  session_id="telegram:123", user_id=None, request_id="req-B")`
  эмиттируется
- THEN в БД SHALL быть записано `user_id="bob"`.

### Requirement: subagent propagates parent user_id explicitly

`_SubagentLoggingHook` MUST явно пробрасывать `user_id`
родительского request в каждый `LogEvent`, который хук
создаёт вне нормального request-index resolution path
(subagent-pipeline может иметь собственный `session_key`
или сменённый контекст — полагаться на автозаполнение в
`_enqueue` для subagent-событий ненадёжно). Это касается
событий, которые `_SubagentLoggingHook` создаёт сам
(например, `subagent_run_finished`); остальные события
subagent-прогона (tool_call, llm_call, и т.п.), идущие
через стандартные `log_*` методы с явным `session_id` и
`request_id`, проходят через обычный механизм
`DbLoggingService.register_request` + request_id matching
в `_enqueue`.

#### Scenario: subagent_run_finished carries parent user_id

- GIVEN parent request выполняется для user_id="alice"
- AND `_SubagentLoggingHook._finalize` создаёт
  `LogEvent(event_type="subagent_run_finished", ...)`
- WHEN этот `LogEvent` ставится в очередь
- THEN `LogEvent.user_id` SHALL быть `"alice"`
- AND не должно быть способа, при котором хук сменил бы
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

### Requirement: RequestContext exposes user identity

Реализация `history_search` MUST получать идентификатор
пользователя из `nanobot.agent.tools.context.RequestContext`
через поле `sender_id: str | None`. Имя поля фиксируется
через единую точку `_current_user_id()` в
`history_search_tool.py`. Прямой доступ к `sender_id` из
других мест запрещён — это инкапсулирует зависимость от
конкретной версии nanobot 0.3.0 в одной функции.

Если в будущей версии nanobot поле будет переименовано,
эта change **не пытается** поддерживать обратную
совместимость через alias: адаптация делается в отдельном
change, который обновляет nanobot-зависимость и `_current_user_id()`
вместе с этим requirement.

#### Scenario: RequestContext provides sender_id field

- GIVEN nanobot установлен согласно `requirements.txt`
- WHEN выполняется contract test
  `tests/contract/test_history_search_identity_contract.py`
- THEN импорт `nanobot.agent.tools.context.RequestContext`
  SHALL быть успешным
- AND итерация `dataclasses.fields(RequestContext)` SHALL
  содержать поле `sender_id`
- AND аннотация `sender_id` SHALL быть совместима с `str | None`
  (т.е. `str`, `Optional[str]`, `str | None`, `Union[str, None]`)
- AND тест SHALL падать при отсутствии поля или несовместимой
  аннотации — это сигнал, что change не соответствует
  установленной версии nanobot и требует отдельной миграции.

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
- `has_more` — `true`, если существуют подходящие
  события, **которые ещё не представлены в текущем
  ответе** и могут быть получены следующим запросом
  с `offset = next_offset`. Определяется через
  композицию двух признаков:
  - `db_has_more = (len(rows_from_db) > effective_limit)`
    — SQL запрашивает `LIMIT effective_limit + 1` строк;
    если фактически получено больше `effective_limit`,
    лишняя строка отбрасывается и `db_has_more = true`.
  - После truncation-проходов:
    `has_more = db_has_more OR results_truncated`.
  - Семантика: даже если `LIMIT N+1` не обнаружил
    следующей строки в БД (`db_has_more = false`),
    `results_truncated = true` означает, что текущая
    страница была сокращена из-за `max_result_chars`
    и часть отобранных событий **не показана агенту**;
    следующая страница (`offset = next_offset`) существует
    и обязательна для полного покрытия выборки.
    Это касается и **последней DB-страницы**: после неё
    в БД строк может не быть, но в текущем ответе ещё
    остались события, отобранные на этой странице и
    выброшенные truncation'ом.
- `results_truncated` — `true`, если из выборки были выброшены
  целые события, чтобы общий JSON влез в `max_result_chars`.
  Дефолт `false`. `results_truncated` MUST NOT
  интерпретироваться как «есть ещё результаты в БД» —
  для этого используется `has_more`.
- `next_offset` — целое ≥ 0, offset для следующего запроса
  при пагинации. Семантика:
  - `next_offset = offset + count` (где `count` — размер
    массива `events` после truncation-проходов).
  - Агент SHALL продолжать пагинацию через
    `history_search(..., offset=next_offset)`, а НЕ через
    `offset + limit`. При `results_truncated=true` часть
    событий была отброшена из ответа, поэтому
    `offset + limit` пропустит их.
  - При `offset=0` и `count=10` без truncation —
    `next_offset=10`. При `results_truncated=true`,
    `offset=0`, `count=4` (после отбрасывания событий
    из-за `max_result_chars`) — `next_offset=4`.
- `truncated` — **deprecated алиас** `results_truncated`.
  Сохраняется в течение одного MINOR-релиза после введения
  нового контракта; удаляется отдельным follow-up change.
- `events` — массив объектов, отсортированных по
  `(timestamp DESC, id DESC)`. Каждый объект SHALL содержать:
  - `event_id` — UUID строки `agent_gateway_logs`;
  - `timestamp` — ISO-8601;
  - `event_type`, `name`, `level`, `summary` — как в БД;
  - `payload` — JSON-string (может быть обрезан; см. truncation);
  - `payload_truncated: bool` — `true`, если итоговый
    `payload` события отличается от payload'а в БД
    вследствие **любого** механизма ограничения размера,
    применённого tool'ом: `per_event_cap`
    (первый проход через `truncate_middle`) ИЛИ
    `max_result_chars` (повторное уменьшение `cap` через
    `cap //= 2` с повторным `truncate_middle`).
    `payload_truncated` SHALL быть `true` независимо от
    того, какой из двух механизмов сработал, и независимо
    от того, сколько раз payload уменьшался. Маркер
    "(N chars truncated)" в середине сохраняется.
    Дефолт `false`.

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
  `next_offset = 0 + N` (например, `N=4` → `next_offset=4`),
  `has_more=true` по формуле `db_has_more OR results_truncated`
  (в данном случае оба `true`: и в БД есть следующие
  строки, и текущая страница сокращена truncation'ом).

#### Scenario: has_more=true при results_truncated, даже если db_has_more=false
- **WHEN** в БД ровно 10 подходящих событий,
  `limit=10, offset=0`; SQL запрашивает `LIMIT 11`,
  получено 10 строк → `db_has_more=false`;
  но `max_result_chars` настолько мал, что
  после truncation остаётся только 4 события
- **THEN** ответ содержит
  `count=4`, `results_truncated=true`,
  `has_more=true` (потому что
  `db_has_more OR results_truncated = false OR true = true`),
  `next_offset=4`. Возврат `has_more=false` при
  `results_truncated=true` SHALL считаться багом
  контракта: агент остановится и не получит события 5..10

#### Scenario: Последняя DB-страница без truncation (5 событий в БД, всё влезает)
- **WHEN** в БД ровно 5 подходящих событий,
  `limit=10, offset=0`; SQL запрашивает `LIMIT 11`,
  получено 5 строк → `db_has_more=false`;
  `max_result_chars` достаточно велик, все 5 событий
  влезли в ответ → `results_truncated=false`
- **THEN** ответ содержит
  `count=5`, `results_truncated=false`,
  `has_more=false` (`db_has_more OR results_truncated
  = false OR false = false`),
  `next_offset = 0 + 5 = 5`. Агент останавливается

#### Scenario: Продолжение пагинации при results_truncated=true
- **WHEN** предыдущий запрос вернул
  `count=4, results_truncated=true, next_offset=4`
- **THEN** следующий запрос с `offset=4` возвращает
  события, **следующие за отброшенными** в предыдущем
  ответе, без пропуска и без дублирования с предыдущей
  страницей. Использование `offset=10` (= `limit`)
  привело бы к пропуску `4..9` и SHALL NOT
  применяться агентом

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

#### Scenario: payload_truncated при повторном ужатии из-за max_result_chars
- **WHEN** в БД ровно одно событие с payload > per_event_cap;
  `limit=10, offset=0`; первый truncation-проход
  сжимает payload до `per_event_cap` (4000), но
  итоговый JSON всё ещё превышает `max_result_chars`;
  срабатывает второй проход с `cap //= 2` —
  payload дополнительно сжимается до ~2000,
  затем до ~1000, и т.д. до вписывания в `max_result_chars`
- **THEN** ответ содержит это единственное событие
  с `payload_truncated=true` (потому что итоговый
  payload отличается от исходного в БД),
  `results_truncated=false` (никакое **целое** событие
  не было выброшено — это НЕ results_truncated;
  уменьшение payload не считается за выброс события),
  `has_more = false` (нет других событий ни в БД,
  ни в выборке), `next_offset = offset + count = offset + 1`.
  Возврат `results_truncated=true` в этом случае
  SHALL считаться багом контракта

### Requirement: Детерминированный порядок страниц (для неизменного набора строк)

The system SHALL использовать SQL-сортировку
`ORDER BY "timestamp" DESC, "id" DESC` для всех запросов
`history_search`. Tie-breaker по `id` (UUID из
`agent_gateway_logs.id`) SHALL гарантировать, что при равных
`timestamp` (например, события из одного батча flush'а)
порядок строк между последовательными вызовами с одним
и тем же фильтром и `offset` остаётся стабильным
**для неизменного набора подходящих строк**: страницы
не пропускают и не дублируют события на границе.

#### Snapshot-consistency

The system SHALL NOT гарантировать snapshot-consistency
между независимыми вызовами `history_search` при появлении
новых записей в `agent_gateway_logs` между запросами.
Если между вызовами в журнал были записаны новые события,
`offset`-пагинация может сдвинуться: более новые строки
попадают в начало выборки, и страница, начатая ранее,
может пересечься с только что вставленными событиями.
Если нужна строгая консистентность — это отдельный
future change (cursor-пагинация).

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
