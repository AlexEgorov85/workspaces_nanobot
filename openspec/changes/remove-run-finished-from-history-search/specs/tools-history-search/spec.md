## Purpose

Определяет контракт кастомного tool `history_search` для агента:
параметры запроса, формат ответа, поведение при пустой выборке,
семантика truncation-флагов и пагинация. Это единый нормативный
источник для всех мест, где упоминается `history_search`
(раньше был размазан между `workspace/tools/history_search_tool.py`,
`workspace/TOOLS.md`, `docs/architecture/HISTORY_SEARCH_ANALYSIS.md`).

## ADDED Requirements

### Requirement: Фильтры запроса

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
- `session_scope` — `current` (по умолчанию) или `all`. `current`
  фильтрует события по `session_id` текущего запроса;
  `all` снимает фильтр.
- `limit` — максимум событий в ответе; верхняя граница берётся
  из `tools.history_search.max_rows` в `config.json`
  (дефолт 50, диапазон 1–500).
- `offset` — целое ≥ 0; пропуск первых `offset` событий
  после сортировки `ORDER BY timestamp DESC`. Дефолт 0.
  Наличие `offset` SHALL позволять пагинацию при
  `results_truncated: true`.

#### Scenario: Фильтр по event_type + tool_name
- **WHEN** агент вызывает `history_search(event_type="tool_call", tool_name="history_search", limit=3)`
- **THEN** tool возвращает не более 3 событий, у которых
  `event_type='tool_call'` И `name='history_search'`,
  отсортированных по `timestamp DESC`

#### Scenario: Невалидный event_type
- **WHEN** агент передаёт `event_type="file_created"`
- **THEN** tool отвечает ошибкой валидации параметров
  с сообщением "event_type must be one of [...]" и
  полным списком допустимых значений

#### Scenario: since/until inclusive
- **WHEN** агент передаёт `since="2024-01-01T00:00:00Z", until="2024-01-02T00:00:00Z"`
- **THEN** в выборку включаются события с
  `2024-01-01T00:00:00Z <= timestamp <= 2024-01-02T00:00:00Z`

### Requirement: Формат ответа

The system SHALL возвращать JSON-строку со следующей структурой:

- `status` — `"success"` или `"error"`.
- `count` — количество событий в ответе (после всех truncation-проходов).
- `session_scope` — фактически применённый scope (`"current"` или `"all"`).
- `events` — массив объектов, отсортированных по `timestamp DESC`.
  Каждый объект SHALL содержать:
  - `event_id` — UUID строки `agent_gateway_logs`;
  - `timestamp` — ISO-8601;
  - `event_type`, `name`, `level`, `summary` — как в БД;
  - `payload` — JSON-string (может быть обрезан; см. truncation).
- `results_truncated` — `true`, если были выброшены целые события,
  чтобы влезть в `max_result_chars`. Дефолт `false`.
- На каждом событии дополнительно `payload_truncated: bool` —
  `true`, если payload конкретного события был обрезан до
  `per_event_cap` символов (через `truncate_middle`,
  сохраняющий голову и хвост). Дефолт `false`.

#### Scenario: Успешный ответ
- **WHEN** запрос возвращает 5 событий, ни одно не обрезано
- **THEN** ответ имеет
  `count=5`, `results_truncated=false`,
  каждое событие имеет `payload_truncated=false`

#### Scenario: results_truncated при пагинации
- **WHEN** в БД 100 подходящих событий, `limit=10`, `max_result_chars=1000`
- **THEN** ответ содержит 10 событий, `count=10`,
  `results_truncated=true`, агент может вызвать
  тот же запрос с `offset=10` для следующей страницы

#### Scenario: payload_truncated при большом llm_call
- **WHEN** `llm_call` событие имеет payload длиной 200 000 символов
- **THEN** в ответе payload обрезан до 4000 символов через
  `truncate_middle` (голова + хвост, маркер "(N chars truncated)"
  в середине), `payload_truncated=true`

### Requirement: Старый флаг truncated как deprecated

The system SHALL в течение одного релиза после введения
`results_truncated`/`payload_truncated` сохранять булев
алиас `truncated` в JSON-ответе, равный `results_truncated`,
с предупреждением в `workspace/TOOLS.md` о предстоящем
удалении. Через один релиз поле `truncated` SHALL быть
удалено без алиаса.

#### Scenario: Обратная совместимость в течение deprecation-окна
- **WHEN** агент читает `response.truncated` (старое имя)
- **THEN** поле присутствует и равно `results_truncated`,
  в `workspace/TOOLS.md` упомянуто как deprecated

### Requirement: Пустой результат — честный success

The system SHALL при отсутствии совпадений возвращать
`{"status": "success", "count": 0, "events": [], "results_truncated": false}`
без ошибки. Агент при `count == 0` SHALL интерпретировать это
как «не найдено в истории» (см. `workspace/TOOLS.md`).

#### Scenario: Запрос без совпадений
- **WHEN** агент вызывает `history_search(query="xyz_nonexistent_12345")`
- **THEN** ответ — success с `count=0`, `events=[]`,
  `results_truncated=false`

### Requirement: Схема payload по event_type

The system SHALL задокументировать в `workspace/TOOLS.md`
явную JSON-схему `payload` для каждого допустимого
`event_type` (поля, типы, пример). Минимум:

- `tool_call.payload`: `{tool, args, tool_call_id}`;
- `tool_result.payload`: `{tool, status, result, error}`;
  `result` — JSON-string, при больших объёмах обрезается
  с маркером "(N chars truncated)";
- `llm_call.payload`: `{prompt, response}`;
  `prompt` — массив ролей (system/user/assistant/tool),
  `response` — объект с контентом и метаданными;
- `run_finished.payload`: `{final_content, tools_used, stop_reason, had_injections, request_id?}`;
- `subagent_run_finished.payload`: `{final_content, tools_used, stop_reason, task_id, task, request_id, parent_request_id}`;
- `inbound.payload`: `{content, message_id, sender_id?, chat_id?, media?}`;
- `context_compacted.payload`: `{tokens_before, tokens_after, archived_turns, ...}`
  (фактический контракт — из `lib/services/context_compaction.py`,
  секция описывает текущее на момент документации).

#### Scenario: Агент парсит tool_result
- **WHEN** агент получает событие `event_type="tool_result"`
- **THEN** он может предсказуемо распарсить `payload.tool`,
  `payload.status`, `payload.result` (JSON-string, может
  потребовать `json.loads(json.loads(payload))`)
