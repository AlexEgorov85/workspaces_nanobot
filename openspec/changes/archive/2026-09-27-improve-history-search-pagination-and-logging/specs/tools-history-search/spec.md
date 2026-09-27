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
