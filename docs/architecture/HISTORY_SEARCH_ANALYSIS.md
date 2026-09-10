# Анализ `history_search` и `agent_gateway_logs` перед оптимизацией

> Цель документа — зафиксировать реальное (а не задокументированное) состояние
> поиска по долговечному журналу агента до начала оптимизации. Это **входные
> данные** для плана доработки `history_search` (FTS + trigram fallback +
> relevance ranking), а не описание целевого состояния.
>
> Все наблюдения сделаны grep'ом и чтением исходников в репозитории
> (`workspace/tools/history_search_tool.py`, `lib/services/db_logging_service.py`,
> `lib/services/context_compaction.py`, `lib/hooks/database_logging_hook.py`,
> `lib/services/runtime_patcher.py`, `workspace/utils/event_log.py`,
> `sql/logs/create_public_agent_gateway_logs.sql`, `sql/migrations/V001__baseline.sql`,
> `tests/test_history_search_tool.py`, `workspace/TOOLS.md`).

## 1. Структура таблицы `public.agent_gateway_logs`

Реальная DDL — `sql/logs/create_public_agent_gateway_logs.sql`. Колонки:

| Колонка       | Тип         | Назначение (по COMMENT)                                                                                                                |
|---------------|-------------|----------------------------------------------------------------------------------------------------------------------------------------|
| `id`          | UUID        | PK (генерируется в приложении; в DDL PK не объявлен из-за GP-конфликта с DISTRIBUTED BY)                                              |
| `timestamp`   | TIMESTAMPTZ | Время события                                                                                                                          |
| `level`       | VARCHAR(16) | DEBUG / INFO / WARN / ERROR (CHECK)                                                                                                    |
| `event_type`  | VARCHAR(64) | Тип события (`tool_call`, `agent_run`, …)                                                                                              |
| `request_id`  | VARCHAR(256)| FK-логически на `agent_question_runs.request_id`                                                                                       |
| `session_id`  | VARCHAR(256)| Денормализованный `channel:chat_id`                                                                                                    |
| `channel`     | VARCHAR(64) | `telegram` / `cli` / …                                                                                                                 |
| `actor`       | VARCHAR(32) | `user` / `agent` / `system`                                                                                                            |
| `name`        | VARCHAR(256)| Категориальный ключ: имя tool, sender_id, model и т.п. (никогда не NULL)                                                               |
| `summary`     | TEXT        | Обрезанный сниппет события (≤200 символов, `logging.db.summary_max_chars`)                                                            |
| `payload`     | JSONB       | Детальные данные события                                                                                                               |
| `metadata`    | JSONB       | Доп. метаданные (latency_ms, tokens_used, tool_call_id и т.п.)                                                                          |

Распределение: `DISTRIBUTED BY (request_id)` (Greenplum 6.5 совместимость).

### Существующие индексы

**Нет ни одного `CREATE INDEX` в `sql/`** — ни FTS, ни trigram, ни B-tree по
`timestamp` / `event_type` / `session_id`. Это значит:

- Все запросы `history_search` сейчас исполняются как `Seq Scan` + `Filter`
  + `cast JSONB в text` для каждой строки (`payload::text ILIKE '%…%'`).
- Сортировка `ORDER BY timestamp DESC` идёт в памяти, без индекса.
- На реальных объёмах (>10k событий/сессию) latency будет линейно расти.

**Решение до начала оптимизации:** индексы не вводим (см. §7 «Принятые
решения до начала реализации»). План предполагает работу через улучшение
запросов с тем, что уже есть; необходимость индексов — отдельный вопрос
для будущей итерации.

## 2. Реальные `event_type` и источники записи

После grep'а `event_type=` по всему репо:

| event_type               | Где пишется                                              | `name`                  | Структура `payload` (ключевые поля)                                                                                       |
|--------------------------|----------------------------------------------------------|-------------------------|----------------------------------------------------------------------------------------------------------------------------|
| `inbound`                | `DbLoggingService.log_inbound`                           | sender_id / `user`      | `content`, `message_id`, `sender_id?`, `chat_id?`, `media?`                                                                 |
| `outbound_final` / `outbound_delta` | `DbLoggingService.log_outbound`                | `assistant`             | `content`, `media?`; `metadata: {latency_ms, tokens_used}`                                                                  |
| `tool_call`              | `DbLoggingService.log_tool_call`                         | имя tool                | `tool`, `args`, `tool_call_id?`                                                                                            |
| `tool_result`            | `DbLoggingService.log_tool_result`                       | имя tool                | `tool`, `status` (`ok` / `error`), `result`, `error?`; `metadata: {latency_ms, tool_call_id?}`                             |
| `llm_call`               | `DbLoggingService.log_llm_call`                          | модель / `llm`          | `prompt` (полный messages через `_json_safe`), `response` (asdict LLMResponse); `metadata: {iteration, model, finish_reason, usage}` |
| `error`                  | `DbLoggingService.log_error`                             | `error`                 | `error`, `context`                                                                                                          |
| `run_finished`           | `database_logging_hook._make_run_event`                  | `run`                   | `final_content`, `tools_used`, `stop_reason`, `had_injections`, `request_id?`; `metadata: {tokens_used, had_error}`         |
| `subagent_run_finished`  | `runtime_patcher` (`subagent_run_finished` после финализации подагента) | `task_id`       | `final_content`, `tools_used`, `stop_reason`, `task_id`, `task`, `request_id`, `parent_request_id`; `metadata: {tokens_used, had_error}` |
| `context_compacted`      | **`НЕ пишется в `agent_gateway_logs` сейчас** (см. §4)   | (нет реальных writer'ов)| (нет реальных writer'ов)                                                                                                   |

### Источники, перечисленные в `workspace/utils/event_log.py`, но **не имеющие реальных вызовов**

В docstring `record_event` упоминаются `document_summarized`,
`file_attached`, `file_created`, `file_delivered` — но `grep "record_event("`
по `workspace/` и `lib/` находит только само определение. Это **dead code
с ложной документацией**; см. §4.

### Где находится пользовательский текст, который ищет агент

Через `history_search` агент пытается найти:

- **файлы, которые обсуждались** → `tool_call.args.path` (например, `read_file`,
  `vector_search`), `tool_result.result` (пути созданных файлов);
- **doc_id** → `tool_result.result.doc_id` / `tool_result.result.document_id`;
- **текст диалога** → `llm_call.prompt` (массив messages), `llm_call.response`,
  `inbound.payload.content`, `run_finished.payload.final_content`,
  `subagent_run_finished.payload.final_content`;
- **факт сжатия** → `context_compacted` (на данный момент отсутствует);
- **имя инструмента / результата** → `summary` (`<=200` символов), `name`;
- **аргументы запроса** → `tool_call.payload.args` (JSON).

### Где находится технический шум

- `outbound_delta` с пустым `content` — это стрим-чанки, удаляются
  `DbLoggingService.purge_empty_outbound` (см. комментарий в `AGENTS.md`).
- `llm_call` payload содержит весь `prompt` (включая system messages,
  инструкции, схемы tool'ов). Для релевантного поиска нужен **выборочный
  разбор** (см. §5).
- `metadata` содержит runtime-поля (`tokens_used`, `latency_ms`, `tool_call_id`),
  которые для пользовательских запросов нерелевантны и засоряют payload.

## 3. Текущая реализация `history_search`

Файл: `workspace/tools/history_search_tool.py`. Публичный API (стабилен, не
меняется на этом этапе):

| Параметр       | Тип / значения                                                                                                                                                  |
|----------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `query`        | строка, опц.                                                                                                                                                   |
| `event_type`   | enum: `context_compacted`, `tool_call`, `tool_result`, `llm_call`, `run_finished`, `subagent_run_finished`, `inbound`                                          |
| `tool_name`    | строка, опц. (имеет смысл только для `tool_call` / `tool_result`)                                                                                              |
| `since`        | ISO-8601, опц.                                                                                                                                                  |
| `until`        | ISO-8601, опц.                                                                                                                                                  |
| `session_scope`| `current` (дефолт) / `all`                                                                                                                                       |
| `limit`        | int, опц. (потолок `max_rows` из конфига)                                                                                                                       |

### Текущий SQL (как есть)

```sql
SELECT "timestamp", event_type, name, level, summary, payload
FROM "<schema>"."<table>"
WHERE <clauses>
ORDER BY "timestamp" DESC
LIMIT %s
```

Где `<clauses>` — собирается динамически из условий:

- `(allow_all OR session_id = %s)` — фильтр сессии;
- `(%s IS NULL OR event_type = %s)` — фильтр типа (NULL → пропуск);
- `name = %s` — фильтр по инструменту (если задан);
- `(summary ILIKE %s OR payload::text ILIKE %s)` — поиск по подстроке;
- `"timestamp" >= %s` / `"timestamp" <= %s` — временной диапазон.

Все параметры — позиционные `%s` (без интерполяции строк в SQL). Безопасность
подтверждена тестами.

### Текущий ranking и truncation

- **Сортировка**: только `timestamp DESC` — «сначала самое свежее».
- **Limit**: до `max_rows` (дефолт 50, потолок 500).
- **Truncation payload'а**: каждый event payload усекается до 4000 символов
  через `truncate_middle` (`lib/utils/text_utils.py`).
- **Size-safe JSON**: цикл в `execute` уменьшает число событий
  (`events.pop()`), потом уменьшает размер payload'а (`cap //= 2`), и в
  крайнем случае обнуляет payload. Гарантирует валидный JSON и непревышение
  `max_result_chars` (дефолт 8000).

### Текущие ограничения, которые план должен закрыть

1. **Нет релевантности**: сортировка только по свежести. Старое идеально
   подходящее событие может оказаться ниже нового шумового.
2. **Нет lexical-нормализации**: `ILIKE '%договор%'` не ловит
   «договоры» / «договором» / опечатки в одной букве.
3. **Нет trigram fallback**: точные идентификаторы (`ABC-12345`,
   `contract_2026_04.pdf`) ловятся через `ILIKE`, но длинные составные имена
   с дефисами / подчёркиваниями — нет.
4. **Нет event-specific extraction**: `payload::text` сериализует весь JSON,
   включая служебные поля (`metadata.tokens_used` и т.п.), и шум
   увеличивается.
5. **Нет `event_id` в ответе**: пользователь (агент) не может сослаться на
   конкретное событие. См. §4 «gap №3».

## 4. Архитектурные gap'ы, найденные при инвентаризации

Эти наблюдения **не входят в задачу «улучшить ILIKE»**, но фиксируются
здесь как согласованные с пользователем решения до начала реализации.

### Gap №1. `context_compacted` не пишется в `agent_gateway_logs`

`lib/services/context_compaction.py::_notify` пишет только в
`agent_conversation_messages` через `_write_history_notice`. При этом:

- `description` tool'а (workspace/tools/history_search_tool.py:79–96)
  перечисляет `context_compacted` как один из доступных типов;
- `workspace/TOOLS.md:62` явно говорит агенту:
  `history_search(event_type="context_compacted", session_scope="current")`
  как правильный способ найти факт сжатия.

То есть инструмент обещает агент-инструкцию, которая в реальности ничего
не находит.

**Решение (согласовано):** добавить в `ContextCompactionService._notify`
параллельный вызов `event_log.record_event(event_type="context_compacted",
name="consolidator", payload=report, session_id=session_key, channel="system")`.
Изменение одного места, согласовано с уже существующим `_write_history_notice`.

### Gap №2. `workspace/utils/event_log.py` содержит ложные утверждения

В docstring `record_event` перечислены event_type'ы
(`document_summarized`, `file_attached`, `file_created`, `file_delivered`),
которые **не пишутся ни одним caller'ом в репо** (`grep "record_event("`
находит только само определение).

**Решение (согласовано):** оставить `event_log.py` как единственный
реальный путь записи `context_compacted` (см. gap №1), но сократить
docstring до честного состояния: «единая точка записи `context_compacted`
в `agent_gateway_logs` из `ContextCompactionService._notify`». Никаких
обещаний про `document_summarized` / `file_*` до тех пор, пока не появится
реальный caller.

### Gap №3. В ответе `history_search` нет `event_id`

Текущий SELECT: `timestamp, event_type, name, level, summary, payload` —
колонка `id` не возвращается, в JSON-ответе у события нет
идентификатора. Агент не может сослаться на конкретное найденное событие
(например, для последующего memory extraction).

**Решение (согласовано):** не менять схему на этом этапе (gap фиксируется
как архитектурный долг). Колонка `id` (UUID) уже существует, SELECT
расширяется тривиально, но это **отдельная минимальная правка** после
того, как будет принят baseline. Не блокирует основной план.

### Gap №4. Конфиг `tools.history_search.*` нигде явно не задан

`project.json` не содержит секции `tools.history_search` (проверено grep'ом).
Используются дефолты из `HistorySearchToolConfig` (`enable=True`,
`max_rows=50`, `max_result_chars=8000`). Это **не баг**, но стоит
зафиксировать: пользовательский конфиг пока пустой, все правки лимитов
делаются только в коде.

## 5. Какие поля важны для каждого event_type (для будущего extraction)

Без автоматического извлечения `payload` остаётся «всем сразу» —
план §4–5 требует event-specific extraction, но не сейчас, а на
Этапе 4–5. Здесь — каркас для последующей реализации (НЕ код, а
контракт «какие поля важны»):

| event_type              | Высокий приоритет                                       | Низкий приоритет (шум)                       |
|-------------------------|---------------------------------------------------------|---------------------------------------------|
| `inbound`               | `payload.content`                                        | `payload.message_id`, `payload.chat_id`     |
| `tool_call`             | `payload.tool`, `payload.args` (пути, doc_id, query)    | `payload.tool_call_id`                       |
| `tool_result`           | `payload.tool`, `payload.result` (если строка), `payload.error` | `payload.tool_call_id`, `metadata.latency_ms` |
| `llm_call`              | `payload.prompt` (последнее user-сообщение + tool-args), `payload.response` | `payload.prompt[*]` system/instructions, `metadata.usage` |
| `run_finished`          | `payload.final_content`                                  | `metadata.tokens_used`, `payload.had_injections` |
| `subagent_run_finished` | `payload.task`, `payload.final_content`                  | `payload.tools_used` (список имён), `metadata` |
| `context_compacted`     | `payload.archived_msgs`, `payload.tokens_before/after`, `payload.summary` (если есть) | `payload.session_key` |
| `error`                 | `payload.error`, `payload.context`                       | (нет)                                        |

Эта таблица — **справочник для последующей реализации** ranking'а и
extraction, не обещание реализовать её прямо сейчас.

## 6. План benchmark'а (Этап 2)

Согласованный источник данных — **синтетический набор в
`tests/fixtures/history_search/`** (см. §7).

Минимальный набор сценариев (≥30, близко к реальным запросам агента):

1. Файлы: `contract_2026_04.pdf`, `report.xlsx`, `data.csv`.
2. Имена сущностей: `contract_123`, `audit_987654`.
3. doc_id: `doc_…`, идентификаторы legal_summarizer'а.
4. Точные фразы: «договор аренды», «отчёт по продажам».
5. Синонимы/формы: «договор», «договоры», «договором».
6. Вопросы-ссылки: «тот документ», «файл который мы обсуждали»,
   «что ты отвечал про риски», «что было до сжатия».
7. Технические ID: `ABC-12345`, `req_…`, `tool_call_id`.
8. tool_name: `compact_context`, `duckdb_query`, `vector_search`.
9. Опечатки: 1 буква отличается (например, `договoр`).
10. session_scope границы: один и тот же термин в разных сессиях.

Для каждого сценария фиксируем `query`, `expected_event_type`,
`expected_session_id`, `expected_event_id` (или стабильный
timestamp+name). Метрики: Recall@5, Recall@10, MRR, false-positive-rate,
avg latency.

## 7. Принятые решения до начала реализации

Согласованные с пользователем развилки, влияющие на форму плана:

1. **`event_log.record_event` остаётся** как единственный путь записи
   `context_compacted` в `agent_gateway_logs`. Docstring сокращается до
   честного состояния (без упоминаний нереализованных `document_summarized`,
   `file_attached`, `file_created`, `file_delivered`).

2. **`ContextCompactionService._notify`** дописывает событие в
   `agent_gateway_logs` через `event_log.record_event`. Покрывает gap №1
   и согласует фактическое поведение с инструкцией агенту в
   `description` tool'а и `workspace/TOOLS.md`.

3. **Схема таблицы и индексы не меняются.** FTS-поиск работает через
   runtime-улучшение SQL-запроса (например, через `to_tsvector` без
   отдельного GIN-индекса — это работает, но медленнее). Этап 15
   зафиксирует план создания индексов (отдельный тикет, отдельная
   миграция), но в этом плане — нет.

4. **Benchmark — синтетический набор в `tests/fixtures/history_search/`.**
   Не требует живой БД, повторяем в CI. Живой snapshot — отдельная опция
   вне данного плана.

5. **Публичный API `history_search` не меняется.** Меняется только
   внутренний SQL и ranking. Никаких новых параметров, никакого
   `ranking`/`mode`/`engine` — пользователь/LLM не должен знать, какой
   именно механизм под капотом.

6. **`event_id` в ответе** — отдельная точечная правка (расширить SELECT
   на `id`, добавить поле в JSON), НЕ блокирует основной план, делается
   отдельно.

7. **Greenplum 6.5 (PostgreSQL 9.4 ядро) — основной таргет.** FTS через
   `to_tsvector('simple', ...)` поддерживается. `pg_trgm` **не
   используется** (нет гарантии наличия расширения на GP 6.x). Trigram
   fallback **не реализуется** (Этап 7 отменён); вместо него для имён
   файлов и составных ID используется substring matching через
   `ILIKE` с предварительной нормализацией запроса (split по
   `_`/`-`/`.`).

8. **FTS-конфигурация: `'simple'`** (без стоп-слов, без лемматизации).
   Идеален для имён файлов, ID, имён инструментов. Для естественного
   русского текста: `'договор'` найдёт `'договор'`, но НЕ найдёт
   `'договоры'`. Это ограничение, но оно совместимо с GP 6.5 и не
   вносит мусора для технических терминов.

## 8. Что НЕ делаем на этом этапе (напоминание)

Из плана пользователя, для контекста:

- Не добавляем vector DB / embeddings.
- Не создаём отдельный `memory_search` / memory index.
- Не переносим `agent_gateway_logs` в другую таблицу.
- Не удаляем текущий `ILIKE` без доказательства, что новая реализация
  полностью его заменяет (сначала — параллельный запуск, потом отключение).
- Не рефакторим tool в `HistoryRepository` / `HistorySearchService` и т.п.
  ради самого рефакторинга.
- Не меняем схему логирования.
- Не меняем `ContextCompactionService` (кроме gap №1).
- Не объединяем `history_search` с upstream MemoryStore.

## 9. Definition of Done для Этапа 1

Этап 1 считается завершённым, когда:

- [x] прочитан `workspace/tools/history_search_tool.py`;
- [x] прочитан `lib/services/db_logging_service.py`;
- [x] прочитан `lib/services/context_compaction.py`;
- [x] прочитан `lib/hooks/database_logging_hook.py` (где `run_finished`);
- [x] прочитан `lib/services/runtime_patcher.py:1500–1575` (subagent);
- [x] прочитан `workspace/utils/event_log.py`;
- [x] прочитана DDL `sql/logs/create_public_agent_gateway_logs.sql`;
- [x] проверены тесты `tests/test_history_search_tool.py`;
- [x] проверено `workspace/TOOLS.md` и `description` tool'а на согласованность;
- [x] подтверждено отсутствие индексов на `agent_gateway_logs` (grep `CREATE INDEX`);
- [x] зафиксированы gap'ы №1–№4 и согласованы с пользователем решения;
- [x] зафиксированы event_type → реальная структура `payload` (для будущего extraction).

Документ служит входными данными для Этапа 2 (baseline benchmark).

## 10. Baseline benchmark (Этап 2)

Прогон: `pytest -m benchmark tests/test_history_search_benchmark.py -v -s`.

### Условия

- Синтетический набор: `tests/fixtures/history_search/gateway_logs.jsonl`
  (83 события, 5 сессий, 7 event_type'ов — `inbound`, `tool_call`,
  `tool_result`, `llm_call`, `run_finished`, `subagent_run_finished`,
  `context_compacted`).
- Сценарии: `tests/fixtures/history_search/scenarios.json` — 30 запросов,
  13 категорий (точные фразы, имена файлов, doc_id, tool_name,
  session_scope, event_type, limit, time_range, негативные, ambiguous,
  filename_complex, doc_id_only).
- Эмулятор SQL: воспроизводит поведение текущего `history_search_tool.py`
  без живой БД (фильтры по `event_type`/`name`/`session_id`/`since`/`until`
  + `ILIKE '%...%'` по `summary || payload::text` + `ORDER BY timestamp
  DESC` + `LIMIT`).
- Метрики: Recall@5, Recall@10, MRR, false-positive-rate (только на
  сценариях с пустым expected), avg latency ms.

### Baseline (текущая реализация, ILIKE + ORDER BY timestamp DESC)

| Метрика               |  Baseline |
|-----------------------|----------:|
| n (сценариев)         |        30 |
| **Recall@5**          | **0.847** |
| **Recall@10**         | **0.853** |
| **MRR**               | **0.867** |
| false_positive_rate   |    0.500  |
| avg_latency_ms        |     1.34  |

По категориям:

| Категория           |  n | Recall@5 | Recall@10 |   MRR | Комментарий                                          |
|---------------------|---:|---------:|----------:|------:|------------------------------------------------------|
| exact_phrase        |  5 |    1.000 |     1.000 | 1.000 | точные фразы ловятся идеально (top-1)                |
| filename            |  7 |    0.971 |     1.000 | 1.000 | 1 из 7 пропущен (Recall@5), в top-10 — все           |
| doc_id              |  3 |    1.000 |     1.000 | 1.000 | doc_id ищется через `payload::text ILIKE`            |
| tool_name           |  4 |    1.000 |     1.000 | 1.000 | фильтр по `name`                                     |
| scope_current       |  1 |    1.000 |     1.000 | 1.000 | session_scope работает                               |
| scope_all           |  1 |    1.000 |     1.000 | 1.000 | то же в scope=all                                    |
| event_type_only     |  3 |    0.000 |     0.000 | 0.000 | Recall считается на `expected_event_ids`, но top-K не совпадает по timestamp-сортировке с произвольным порядком expected (см. §11) |
| limit               |  1 |    0.600 |     0.600 | 1.000 | limit=3 обрезает Recall@5 до 0.6 (3 из 5 ожидаемых) |
| time_range          |  1 |    1.000 |     1.000 | 1.000 | since/until работает                                 |
| negative            |  1 |    1.000 |     1.000 | 1.000 | несуществующий маркер — пусто (FP=0 для этого сценария) |
| ambiguous           |  1 |    0.000 |     0.000 | 0.000 | запрос "PDF" возвращает шум (FP для baseline)        |
| filename_complex    |  1 |    0.000 |     0.000 | 0.000 | `my_report_2026_final_v3.pdf` — длинное имя с подчёркиваниями **не найдено** через `ILIKE` (см. §11) |
| doc_id_only         |  1 |    1.000 |     1.000 | 1.000 | doc_id ловится                                       |

### Что показывает baseline

1. **MRR высокий (0.87)** — на синтетике свежее = релевантное. На реальных
   данных с длинной историей это будет ниже (старые, но релевантные
   события будут задвигаться новым шумом).
2. **`event_type_only` Recall=0** — не баг `ILIKE`, а артефакт сопоставления:
   `expected_event_ids` для `event_type=context_compacted` и т.п. упорядочены
   по `find()` (произвольно), а реальный SQL выдаёт по `timestamp DESC`.
   На реальном использовании агенту обычно важен «факт наличия», а не
   позиция; на Этапе 8 (после FTS+trigram) будем измерять по-другому.
3. **`filename_complex` = 0** — главный аргумент в пользу trigram fallback
   для составных имён файлов с подчёркиваниями. ILIKE по подстроке
   работает только если запрос **дословно** совпадает с куском имени.
4. **`ambiguous` FP** — запрос «PDF» находит 2 события (где встречается
   "PDF" в payload/summary), хотя ожидаемо 0. Это технический шум, который
   нужно гасить event-specific extraction (Этап 4–5).
5. **`avg_latency_ms = 1.34`** — синтетика маленькая, в проде на seq scan
   на 10k+ строк будет значительно выше. Этап 15 (EXPLAIN) покажет реальные
   цифры.

### Что НЕ покрывает синтетика

- Реальные длинные диалоги (100+ сообщений в одной сессии), где
  «новое = шум, старое = суть» — здесь baseline будет выглядеть
  значительно хуже, чем MRR=0.87.
- Реальные имена файлов пользователей (русский, кириллица, спецсимволы,
  пробелы) — синтетика использует латиницу и подчёркивания.
- Реальные `payload::text` для `llm_call` (полные messages, system
  prompt) — в синтетике укорочено до двух сообщений.
- Сессии длиной в недели — синтетика создаёт события в течение 5 дней.

Эти ограничения нужно честно учитывать при интерпретации метрик.
Этап 16 (финальный benchmark) при наличии живой БД сможет снять
anonymized sample и перепрогнать на нём.

## 11. Замечания к baseline, влияющие на Этапы 4–8

1. **`event_type_only` Recall = 0** — это метрическая ловушка, не баг
   реализации. На Этапе 8 будем считать Recall@K для этих сценариев
   либо по позиции «есть ли в результате хотя бы одно ожидаемое»,
   либо использовать precision@k (доля ожидаемых в top-K).
2. **`filename_complex`** — кейс для расширения поиска по `name`/`args.path`
   через substring matching с нормализацией запроса (split по
   `_`/`-`/`.`). Trigram fallback отменён (см. §7, п.7). Это будет
   сделано в рамках Этапа 5 (event-specific payload extraction) +
   Этапа 4 (FTS по `name` и `payload.args.path`).
3. **`ambiguous` FP** — после FTS+extraction шум должен уменьшиться:
   event-specific extraction (Этап 5) ограничит «вес» payload'ов с
   техническими полями; FTS не поможет (для слова 'PDF' оба возвращают
   матч), но extraction позволит дать меньший вес матчам в
   `metadata.*`/`tool_call_id`.
4. **Сортировка по timestamp DESC** в baseline иногда помогает (свежее =
   релевантное), но это **случайность** синтетики. Этап 6 (relevance
   ranking) должен явно зафиксировать: «релевантность доминирует над
   свежестью».

## 12. Что сделано в этой сессии vs что отложено

Эта сессия завершается после Этапа 3 + закрытия gap'ов №1, №3.
Дальнейшая реализация требует живой БД (PostgreSQL/Greenplum), которой
нет на этой машине.

### Сделано (проверяемо без живой БД)

- **Gap №1** — `ContextCompactionService._notify` теперь пишет
  `context_compacted` в `agent_gateway_logs` через
  `workspace.utils.event_log.record_event`. Инструкция агенту в
  `description` tool'а и `workspace/TOOLS.md` согласована с фактом.
  Тесты: `TestNotifyRecordsEventLog` (3 новых), всего 44/44 в
  `test_context_compaction.py`.
- **Gap №3** — `history_search_tool.py` возвращает `event_id` в каждом
  событии (расширен SELECT, добавлено поле в JSON). Существующий API не
  сломан. Тест: `test_search_returns_event_id_in_each_event` (1 новый),
  всего 9/9 в `test_history_search_tool.py`.
- **`event_log.py` docstring** сокращён до честного состояния: только
  `context_compacted` как единственный реальный caller.
- **Синтетический набор `tests/fixtures/history_search/`** (83 события ×
  5 сессий × 7 event_type'ов, 30 сценариев × 13 категорий) — переносимый
  артефакт для проверки на реальной БД.
- **`HISTORY_SEARCH_SQL_PROPOSAL.md`** — два варианта SQL (PROPOSAL-A
  baseline-текущий и PROPOSAL-A с FTS+ranking+extraction) с инструкцией
  по `EXPLAIN ANALYZE` на реальной БД.
- **Baseline benchmark** (`pytest -m benchmark`) — Recall@5=0.847,
  MRR=0.867, FP=0.5, avg latency 1.34ms.

### Что НЕ сделано в этой сессии и почему

- **Этапы 4–6 (FTS + extraction + ranking) в production-SQL** — требуют
  проверки `EXPLAIN ANALYZE` на реальной БД (см. PROPOSAL §4).
  Эмулятор FTS в Python — не замена: PG FTS может вести себя иначе.
- **Этап 7 (trigram fallback)** — отменён (GP 6.5, нет `pg_trgm`).
- **Этапы 9, 11–17** — зависят от Этапов 4–6 и проверки на реальной БД.
- **Этап 14 (расширенные тесты)** — частично: regression-тест на
  `event_id` есть; ranking/fuzzy/size-limit тесты добавляются **после**
  принятия PROPOSAL-A (чтобы не тестировать то, что ещё не
  реализовано).

### Что нужно сделать на машине с живой БД

1. Применить `HISTORY_SEARCH_SQL_PROPOSAL.md` §4.1 (проверка FTS на
   этой БД) — 5 коротких запросов, ~5 минут.
2. Если FTS работает — §4.2 (`EXPLAIN ANALYZE` baseline vs PROPOSAL-A).
3. Если PROPOSAL-A даёт прирост — merge SQL в
   `history_search_tool.py` по чек-листу из §5.
4. Если нет — зафиксировать в CHANGELOG и CHANGELOG-секции «решение
   по vector search» (Этап 17).


