## Purpose

Фиксирует единственный runtime-механизм персистенции
structured agent events: `DbLoggingService` — единственный
writer `agent_gateway_logs` и единственный
механизм upsert в `agent_question_runs`. Любой
structured event независимо от источника
(agent loop, hook, subagent, context compaction,
sync service, cache service, channel) обязан превращаться
в `LogEvent` и передаваться через `DbLoggingService`;
прямой SQL-fallback в журнал запрещён.

## ADDED Requirements

### Requirement: Single writer of agent_gateway_logs

The system SHALL иметь ровно один runtime-механизм
записи в `agent_gateway_logs`: `DbLoggingService`.
Любой другой runtime-модуль SHALL NOT выполнять
`INSERT`, `UPDATE` или `DELETE` против таблицы,
заданной `logging.db.table_name`, в обход
`DbLoggingService`. Имя таблицы и схема берутся
из resolved `SETTINGS` (`logging.db.table_name`,
`logging.db.schema`) и передаются сервису через
composition root; runtime-producers не читают эти
значения напрямую.

#### Scenario: Все события проходят через DbLoggingService

- **WHEN** любой runtime-компонент (agent loop,
  hook, subagent, `ContextCompactionService`,
  `PgDuckDbSyncService`, `DuckDbCacheStore`,
  `PreloadService`, `PostgresChannel`) эмитит
  structured event
- **THEN** запись в `agent_gateway_logs` SHALL
  произойти через `db_logging_service.log_event(...)`
  или специализированный builder (`log_tool_call`,
  `log_tool_result`, `log_llm_call`, `log_error`,
  `log_inbound`, `log_outbound`, `log_sync_event`),
  которые внутри строят `LogEvent` и зовут
  `log_event(LogEvent(...))`.
- **AND** прямых `INSERT INTO "<schema>"."<table>"`
  в runtime-коде SHALL NOT быть (за пределами самого
  `lib/services/db_logging_service.py`).

#### Scenario: request_id link сохраняется

- **WHEN** `DbLoggingService` записывает событие
  в `agent_gateway_logs` и `_QuestionRunRecord`
  был зарегистрирован через `register_request(...)`
  для того же `session_key`
- **THEN** `agent_gateway_logs.request_id` SHALL
  совпадать с `agent_question_runs.request_id` (через
  индекс `session_key → request_id`).
- **AND** upsert в `agent_question_runs` SHALL
  тоже идти через `DbLoggingService` (тот же сервис,
  специализированные методы `register_request` /
  `finish_request`, без второго writer'а).

### Requirement: context_compacted через DbLoggingService

The system SHALL записывать событие `context_compacted`
в `agent_gateway_logs` через `DbLoggingService.log_event`
(LogEvent с `event_type="context_compacted"`). Никаких
прямых `INSERT` из `ContextCompactionService`
или из `workspace.utils.event_log` (этот модуль
удалён) SHALL NOT происходить.

#### Scenario: Ручной /compact пишет context_compacted

- **WHEN** `ContextCompactionService.compact()` (slash,
  CLI `/compact`, tool `compact_context`) завершился
  с `archived_msgs > 0`
- **THEN** `DbLoggingService.log_event(LogEvent(...))`
  SHALL быть вызван с `event_type="context_compacted"`,
  `actor="system"`, `name="consolidator"`, payload
  содержит `mode` / `archived_msgs` / `kept_msgs` /
  `tokens_before` / `tokens_after` / `summary` /
  `raw_dump`.

#### Scenario: Авто compact пишет context_compacted

- **WHEN** `runtime_patcher._wrap_auto_compact_archive`
  или `_wrap_maybe_consolidate_by_tokens` вызвал
  `ContextCompactionService.record_external_compaction(...)`
  после успешной архивации
- **THEN** `record_external_compaction` SHALL
  делегировать в `_notify` так же, как `compact()`,
  и `context_compacted` SHALL быть записан через
  `DbLoggingService.log_event(...)`.

#### Scenario: compaction не падает при недоступности сервиса

- **WHEN** `db_logging_service is None` ИЛИ
  `db_logging_service.is_running() == False`
- **AND WHEN** `ContextCompactionService.compact(...)`
  завершил сжатие успешно
- **THEN** `compact(...)` SHALL вернуть успешный
  отчёт (`ok=True`, `archived_msgs > 0`).
- **AND** `DbLoggingService.log_event(...)` SHALL
  быть **no-op** (событие не записано).
- **AND** loguru-логгер SHALL зафиксировать
  факт «structured event persistence unavailable»
  (без `agent_gateway_logs` write).
- **AND** прямой `INSERT INTO "<schema>"."<table>"`
  SHALL NOT быть выполнен.

### Requirement: Sync-события через DbLoggingService

The system SHALL записывать все sync-события PG→DuckDB
(`sync_service_started`, `sync_initial_load_started`,
`sync_table_loaded`, `sync_initial_load_done`,
`sync_initial_load_error`, `sync_table_missing`,
`sync_publish_failed`, `sync_publish_skipped`,
`sync_dispatch_skipped`, `sync_dispatch_failed`)
через `DbLoggingService.log_sync_event(...)`.
Никаких прямых `INSERT` или fallback-обёрток
(`emit_sync_event` / `record_sync_event` /
`record_event`) SHALL NOT быть.

#### Scenario: Sync-событие через DbLoggingService

- **WHEN** `PgDuckDbSyncService` или `DuckDbCacheStore`
  эмитят sync-событие и `db_logging_service`
  сконфигурирован и запущен
- **THEN** ровно один `LogEvent` SHALL быть поставлен
  в очередь `DbLoggingService` (через `log_sync_event`).
- **AND** `get_stats()["written_by_type"][event_type]`
  SHALL инкрементироваться после успешного flush'а.

#### Scenario: Sync-событие при недоступности сервиса — no-op

- **WHEN** `db_logging_service is None` ИЛИ
  `db_logging_service.is_running() == False`
- **AND WHEN** sync-код вызывает helper для эмита
  (`PgDuckDbSyncService._log_sync_event` или
  `PreloadService._emit_health_event` или
  `DuckDbCacheStore` caller's)
- **THEN** helper SHALL быть silent no-op (без
  `INSERT` и без `record_sync_event` fallback).
- **AND** sync-операция SHALL NOT быть прервана
  (sync-код не должен падать из-за отсутствия
  observability-сервиса).
- **AND** loguru SHALL зафиксировать потерю события
  на уровне `DEBUG` (не `WARN` / `ERROR` —
  это штатный режим для тестов и standalone).

#### Scenario: preload health-summary через DbLoggingService

- **WHEN** `PreloadService.preload_vector_indexes(store)`
  завершил прогрев FAISS-индексов (или упал)
- **THEN** ровно один `LogEvent` с
  `event_type="vector_index_preload_health"` SHALL
  быть записан через
  `db_logging_service.log_sync_event(...)` с payload
  `declared` / `loaded` / `missing` / `orphan` /
  `stale` (snapshot текущей реализации
  `PreloadService.compute_index_health`).
- **AND** payload SHALL содержать **те же** ключи,
  что и существующий snapshot в
  `workspace/TOOLS.md` секции
  «vector_index_preload_health» —
  изменение контракта payload отдельный change.

### Requirement: No fallback writer при недоступности DbLoggingService

The system SHALL NOT иметь fallback-механизма
записи в `agent_gateway_logs` через прямой SQL,
когда `DbLoggingService` отсутствует или не запущен.
Если `DbLoggingService` недоступен, structured event
SHALL NOT быть записан (silent no-op + loguru-фиксация).
Любой runtime-код, который раньше «падал» в
`event_log.record_event` / `record_sync_event` /
`emit_sync_event` как fallback, SHALL быть переписан
на silent no-op.

#### Scenario: Приложение стартует до готовности DbLoggingService

- **WHEN** `ApplicationContext.start()` ещё не вызвал
  `db_logging_service.start()` (ранний startup,
  конфигурация резолвится, sync-сервисы ещё не инициализированы)
- **AND WHEN** какой-либо runtime-компонент пытается
  записать structured event
- **THEN** запись SHALL быть silent no-op (без прямого
  `INSERT` в `agent_gateway_logs`).
- **AND** `DbLoggingService` (когда будет стартован
  позднее) SHALL обработать события только того
  периода, в котором он запущен — события, возникшие
  до его `start()`, SHALL NOT быть восстановлены
  через fallback.

#### Scenario: Standalone-утилита без DbLoggingService

- **WHEN** standalone-утилита
  (`tools/build_vectors.py` или иная) не создаёт
  `ApplicationContext` и `DbLoggingService`
  соответственно отсутствует
- **THEN** утилита SHALL использовать только
  loguru (`logger.info` / `logger.warning`) для
  диагностики.
- **AND** утилита SHALL NOT импортировать
  `workspace.utils.event_log` (модуль удалён) и
  SHALL NOT выполнять прямой `INSERT INTO
  "<schema>"."<table>"`.

#### Scenario: Тесты без DbLoggingService

- **WHEN** unit-тест выполняет операцию, которая
  обычно эмитит structured event
  (например, `ContextCompactionService.compact(...)`
  в `tests/test_context_compaction.py`)
- **THEN** тест SHALL пройти успешно и без
  `DbLoggingService`.
- **AND** тест SHALL НЕ мокать и НЕ вызывать
  удалённый `workspace.utils.event_log`.

### Requirement: notify_in_history не управляет structured event logging

The system SHALL разделять два concerns:
(a) UI-уведомление о сжатии в
`agent_conversation_messages` (заметка видна в чате
Streamlit); (b) observability-trail в
`agent_gateway_logs` (событие `context_compacted`
доступно через `history_search`).

Настройка `gateway.compact.notify_in_history` SHALL
управлять **только** concern (a) — записью
`_write_history_notice` в `agent_conversation_messages`.
Событие `context_compacted` SHALL записываться через
`DbLoggingService.log_event(...)` **всегда**, пока
`gateway.compact.enabled=True`, независимо от
`notify_in_history`.

#### Scenario: notify_in_history=true — оба side-effect'а

- **WHEN** `gateway.compact.notify_in_history=true`
- **AND WHEN** compaction завершился с
  `archived_msgs > 0`
- **THEN** `_write_history_notice` SHALL быть вызван
  и SHALL записать строку в
  `agent_conversation_messages`
  (`metadata.kind="context_compact"`).
- **AND** `DbLoggingService.log_event` SHALL быть
  вызван и SHALL поставить `context_compacted`
  в очередь.

#### Scenario: notify_in_history=false — только structured event

- **WHEN** `gateway.compact.notify_in_history=false`
- **AND WHEN** compaction завершился с
  `archived_msgs > 0`
- **THEN** `_write_history_notice` SHALL NOT быть
  вызван (никакой записи в
  `agent_conversation_messages`).
- **AND** `DbLoggingService.log_event` SHALL всё
  равно быть вызван и SHALL поставить
  `context_compacted` в очередь.
- **AND** `history_search(event_type="context_compacted",
  session_scope="current")` SHALL находить событие
  для recovery после compaction.

#### Scenario: record_external_compaction наследует decoupled поведение

- **WHEN** `runtime_patcher` вызывает
  `ContextCompactionService.record_external_compaction(...)`
  с `archived_msgs > 0`
- **THEN** `record_external_compaction` SHALL
  делегировать в `_notify`, и `_record_event_log`
  SHALL быть вызван **даже** если
  `notify_in_history=false` (как и для `compact()`).

### Requirement: Producers не читают logging-DB конфиг

Runtime-producers structured events
(`ContextCompactionService`, `PgDuckDbSyncService`,
`DuckDbCacheStore`, `PreloadService`,
`PostgresChannel`, hook'и) SHALL NOT читать
`logging.db.*` или `channels.postgres.dsn` напрямую
для целей INSERT в `agent_gateway_logs`. Они SHALL
получать уже сконфигурированный `db_logging_service`
через composition root (`ApplicationContext._make_*`)
и вызывать его методы без знания DSN, `table_name`,
`schema`.

#### Scenario: Producer не импортирует SETTINGS для logging

- **WHEN** runtime-компонент пишет structured event
- **THEN** импорт `from config import SETTINGS`
  в producer'е SHALL быть только для чтения
  **собственных** доменных настроек (например,
  `gateway.compact.*` для `ContextCompactionService`,
  `skills.*` для skill'ов).
- **AND** producer SHALL NOT выполнять
  `SETTINGS.get("logging", ...)`, `SETTINGS.get(
  "channels", {}).get("postgres", {}).get("dsn", ...)`
  или аналогичные lookup'ы, относящиеся к
  logging-DB.

#### Scenario: Producer не читает utils.db.execute для журнала

- **WHEN** runtime-компонент пишет structured event
- **THEN** producer SHALL NOT вызывать
  `utils.db.execute('INSERT INTO ... agent_gateway_logs ...')`
  или аналогичные прямые SQL-команды.
- **AND** producer SHALL NOT использовать
  `psycopg2.extras.Json(...)` для сериализации
  payload в `agent_gateway_logs` напрямую — этим
  владеет только `DbLoggingService._insert_batch`.

### Requirement: Architecture guard на прямые INSERT и обходные пути

The system SHALL иметь автоматический guard
(repository-level test), который проверяет:

(a) в runtime-коде (любой файл вне
`lib/services/db_logging_service.py`) нет ни одного
`INSERT INTO ... <table_name>` где `<table_name>`
совпадает с `logging.db.table_name` (по умолчанию
`agent_gateway_logs`);

(b) в runtime-коде нет импорта
`workspace.utils.event_log` (модуль удалён, поэтому
любой такой импорт — ошибка);

(c) в runtime-коде нет вызовов
`record_event` / `record_sync_event` /
`emit_sync_event` как имён функций (не как
substrings в docstring'ах или комментариях).

Guard SHALL запускаться в `pytest` и SHALL падать
с понятным сообщением, какое правило нарушено и в
каком файле.

#### Scenario: Guard ловит новый прямой INSERT

- **WHEN** разработчик добавляет новый файл
  `lib/services/<some_module>.py`, который содержит
  строку `INSERT INTO "public"."agent_gateway_logs"`
- **THEN** `pytest tests/test_unified_event_logging_pipeline.py`
  SHALL упасть с указанием файла, номера строки и
  нарушенного правила (a).

#### Scenario: Guard ловит импорт удалённого модуля

- **WHEN** разработчик добавляет
  `from workspace.utils.event_log import record_event`
  в любой runtime-файл (включая тесты, исключая
  `tests/test_event_log.py`, который удаляется этим
  change)
- **THEN** `pytest tests/test_unified_event_logging_pipeline.py`
  SHALL упасть с указанием файла, номера строки и
  нарушенного правила (b).

#### Scenario: Guard не ловит docstring-упоминания

- **WHEN** docstring или комментарий содержит
  substring `INSERT INTO ... agent_gateway_logs`
  или имя `record_event` без вызова
  (т.е. упоминание исторического контекста)
- **THEN** guard SHALL NOT падать (regex ловит
  только синтаксические конструкции, не plain text
  в строках документации — для этого используется
  парсинг AST или regex с anchoring).
