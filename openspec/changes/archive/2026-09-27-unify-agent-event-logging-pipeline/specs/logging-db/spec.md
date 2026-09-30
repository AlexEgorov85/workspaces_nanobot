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

### Requirement: Single writer invariant of agent_gateway_logs

`DbLoggingService` MUST be the sole runtime
owner permitted to persist structured events into
`agent_gateway_logs` (и `agent_question_runs`).
Любой structured event независимо от источника
(agent loop, hook, subagent, context compaction,
sync service, cache service, channel) обязан
превращаться в `LogEvent` и передаваться через
`DbLoggingService`. Прямой SQL-fallback в журнал
запрещён.

Это требование **архитектурное** и проверяется
через ownership-based architecture guard
(requirement «Architecture guard»), а не
через grep одной строки `agent_gateway_logs`
в репозитории (это implementation verification,
не primary invariant).

Запрещено в runtime-коде (за пределами
`lib/services/db_logging_service.py`):

- INSERT/UPDATE/DELETE строк в таблицу,
  заданную `logging.db.table_name`;
- доступ к `logging.db.table_name` /
  `logging.db.schema` для целей INSERT;
- обращение к `channels.postgres.dsn` для
  прямой записи в `agent_gateway_logs`;
- вызов удалённого модуля
  `workspace.utils.event_log` и его публичных
  функций (`record_event` / `record_sync_event` /
  `emit_sync_event`);
- обход через любые другие persistence-хелперы
  (`StructuredEventService`, `EventLogService`,
  `GatewayEventLogger` и т.п.) — их **не должно
  появиться** как слоя поверх `DbLoggingService`.

Разрешённый путь:

- `db_logging_service.log_event(LogEvent(...))`;
- `db_logging_service.log_sync_event(...)`;
- специализированные builder'ы
  (`log_tool_call`, `log_tool_result`,
  `log_llm_call`, `log_error`, `log_inbound`,
  `log_outbound`, `log_sync_event`,
  `register_request`, `finish_request`) — все
  они внутри зовут `log_event(LogEvent(...))`;
- `DbLoggingService.try_log_event(svc, log_event,
  producer, event_type)` — единый helper для
  producer'ов (см. Requirement
  «Uniform logging behavior при недоступности
  сервиса»).

Имя таблицы, схема и DSN берутся из resolved
`SETTINGS` (`logging.db.table_name`,
`logging.db.schema`, `channels.postgres.dsn`)
**внутри `DbLoggingService.__init__`** и
передаются сервису через composition root.
Runtime-producers не читают эти значения напрямую.

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
  в production runtime-коде SHALL NOT быть
  (за пределами `lib/services/db_logging_service.py`).

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

#### Scenario: Динамически формируемый INSERT ловится ownership-проверкой

- **WHEN** producer пытается выполнить
  `cursor.execute(f'INSERT INTO "{schema}"."{table}" ...')`
  где `table = settings.logging.db.table_name`,
  вне `lib/services/db_logging_service.py`
- **THEN** architecture guard
  (`tests/test_unified_event_logging_pipeline.py::TestNoProductionDirectWriters`)
  SHALL обнаружить такой паттерн через
  ownership-проверку (доступ к
  `logging.db.table_name`) и пробросить фикстуру
  через negative-test
  `test_guard_catches_dynamic_table_insert`.
- **AND** `pytest` SHALL упасть с указанием
  файла и нарушенного правила.

### Requirement: try_log_event contract

`DbLoggingService.try_log_event(svc, log_event, *,
producer: str, event_type: str) -> bool` SHALL быть
единой точкой входа producer'ов structured events
в `DbLoggingService`. Контракт:

1. **MUST NOT raise exceptions** — failures в
   logging infrastructure MUST NOT прерывать
   business-операцию (compact / sync / preload).
2. **Различает два состояния dependency** и
   обрабатывает их единообразно:

   | Условие | Поведение |
   | --- | --- |
   | `svc is None` (dependency отсутствует — composition root решил не передавать) | WARNING (с причиной `"dependency is None"`) + возврат `False` |
   | `svc.is_running() == False` (dependency передана, но service unavailable — например, после `stop()`) | WARNING (с причиной `"is not running"`) + возврат `False` |
   | `svc.log_event(log_event)` бросил exception | WARNING (с причиной `"raised: <exc>"`) + возврат `False` |
   | `svc.log_event(log_event)` вернул `False` (например, queue full) | (без WARNING — transient backpressure лечится в `_flush_batch`) + возврат `False` |
   | успех (event в queue) | возврат `True` |

3. **Уровень WARNING** — единственный уровень
   operational logging для всех failure-режимов
   (dependency None / service unavailable /
   exception). НЕ DEBUG, НЕ INFO, НЕ ERROR,
   НЕ EXCEPTION.
4. **Возвращаемое значение `bool`** — producer
   может игнорировать. Контракт резервирует
   `bool` return для будущих метрик
   (`dropped_events_by_producer` и т.п.).
5. **Текст WARNING** SHALL включать
   `producer` и `event_type` для grep/CI-алёртов,
   и причину (dependency / not running / raised).

`try_log_event` MUST NOT вводить дополнительный
fallback INSERT в `agent_gateway_logs` — failure
SHALL быть только operational WARNING.

#### Scenario: dependency отсутствует — WARNING + False + no exception

- **WHEN** producer зовёт `try_log_event(None,
  log_event, producer="ContextCompactionService",
  event_type="context_compacted")`
- **THEN** функция SHALL вернуть `False`.
- **AND** `logger.warning(...)` SHALL быть вызван
  ровно один раз с текстом, содержащим
  `"ContextCompactionService"`,
  `"context_compacted"`, и причину `"dependency is None"`.
- **AND** НЕ SHALL быть брошено исключение.
- **AND** НЕ SHALL быть выполнен прямой INSERT.

#### Scenario: service unavailable — WARNING + False + no exception

- **WHEN** producer зовёт `try_log_event(svc,
  log_event, ...)` где `svc` non-None, но
  `svc.is_running() == False`
- **THEN** функция SHALL вернуть `False`.
- **AND** `logger.warning(...)` SHALL быть вызван
  с причиной `"is not running"`.
- **AND** НЕ SHALL быть брошено исключение.

#### Scenario: log_event бросил exception — WARNING + False + no exception

- **WHEN** `svc.log_event(log_event)` бросает
  произвольное исключение
- **THEN** функция SHALL вернуть `False` и
  `logger.warning(...)` SHALL быть вызван с
  причиной `"raised: <exc>"`.
- **AND** исключение из `log_event` MUST NOT
  проброситься наружу.

#### Scenario: queue full — False, без WARNING

- **WHEN** `svc.log_event(log_event)` возвращает
  `False` (например, queue переполнена,
  `stats["queue_full"]` инкрементируется внутри
  `DbLoggingService`)
- **THEN** `try_log_event` SHALL вернуть `False`.
- **AND** `logger.warning(...)` SHALL NOT быть
  вызван (queue full — transient backpressure,
  не failure dependency).

#### Scenario: success — True

- **WHEN** `svc.log_event(log_event)` возвращает
  `True` (event поставлен в queue)
- **THEN** `try_log_event` SHALL вернуть `True`.
- **AND** `logger.warning(...)` SHALL NOT быть
  вызван.

### Requirement: Uniform logging behavior при недоступности сервиса

The system SHALL обеспечивать единое поведение
для всех producer'ов structured events в случае,
когда `DbLoggingService` недоступен
(`db_logging_service is None` или
`db_logging_service.is_running() == False`) в
момент попытки записи structured event:

1. **Structured persistence**: no-op for business
   operation — событие не попадает в
   `agent_gateway_logs`, business-операция
   продолжается успешно.
2. **Operational logging**: `logger.warning(...)` на
   уровне `WARNING` с сообщением вида
   `"<producer>: structured event <event_type> not persisted
   (DbLoggingService <reason>)"`. Уровень WARNING —
   **единый** для всех producer'ов (не DEBUG,
   не INFO, не ERROR).
3. **Business operation**: продолжается успешно —
   observability не должна ломать бизнес-операцию.

Никакого fallback direct INSERT в
`agent_gateway_logs` ни при каких обстоятельствах
— это инвариант, не оптимизация.

Реализуется через **единую helper-функцию**
`DbLoggingService.try_log_event(svc, log_event,
*, producer: str, event_type: str) -> bool`,
публикуемую как часть `DbLoggingService` API.
Каждый producer (включая `ContextCompactionService`,
`PgDuckDbSyncService`, `DuckDbCacheStore`,
`PreloadService`, `ApplicationContext`-замены
`_record_sync_skipped`) вызывает именно её, а
не собственную обёртку с собственным уровнем
логирования.

#### Scenario: Producer при недоступности сервиса

- **WHEN** любой producer пытается записать
  structured event и `db_logging_service is None`
  или `db_logging_service.is_running() == False`
- **THEN** `DbLoggingService.try_log_event(...)` SHALL
  обеспечивать no-op for business (событие не
  записано, business-операция продолжается).
- **AND** `logger.warning(...)` SHALL быть вызван
  ровно один раз с producer-префиксом и event_type.
- **AND** producer-вызов (например,
  `ContextCompactionService.compact(...)`,
  `PgDuckDbSyncService._log_sync_event(...)`,
  `PreloadService._emit_health_event(...)`)
  SHALL не бросить исключение и не вызвать
  прямой SQL INSERT.

#### Scenario: Все producer'ы используют один и тот же WARNING-уровень

- **WHEN** тест `tests/test_unified_event_logging_pipeline.py::TestDbLoggingServiceUnavailableBehavior`
  инспектирует `caplog.records` при недоступности
  сервиса для каждого producer'а
- **THEN** ВСЕ зафиксированные сообщения
  SHALL иметь `levelname == "WARNING"`.
- **AND** НЕ должно быть записей уровня `DEBUG`,
  `INFO`, `ERROR` или `EXCEPTION` от producer'ов
  в этом сценарии.

### Requirement: Skill invocation is out of scope

The system SHALL NOT иметь dedicated runtime
event_type для «активации Skill». Skill в
текущей архитектуре — это content (markdown-инструкции
в `SKILL.md`), который загружается в agent context
через `SkillsLoader.load_skills_for_context(...)` /
`build_skills_summary(...)` (`nanobot/agent/skills.py`),
а не runtime-callable сущность.

Границы контракта:

- **Загрузка / обнаружение / инжекция `SKILL.md`
  в system prompt** НЕ порождает `skill_call` (или
  любой другой) structured event в
  `agent_gateway_logs`. Это внутреннее
  техническое состояние runtime.
- **Вызов Skill-скрипта агентом через
  `tools.exec("python skills/<name>/scripts/cli.py ...")`**
  логируется как штатная пара
  `event_type="tool_call"` (`name="exec"`,
  payload содержит `args` с командой) +
  `event_type="tool_result"` (payload содержит
  `result`). Это **не отдельный `skill_call`** —
  это `tool_call`/`tool_result` с характерным
  payload.
- **Если** в будущем nanobot или этот проект
  введёт runtime API вида
  `SkillExecutor.invoke(skill_name, ...)` —
  это отдельное архитектурное изменение,
  которое вводит соответствующий event_type
  через отдельный OpenSpec change. Эта
  спецификация не предвосхищает этот контракт.

Запрещено в runtime-коде:

- Эмитить `event_type="skill_call"` (или
  `skill_invocation`, `skill_activation`,
  любой аналогичный) — нет runtime-call
  site, нет соответствующего contract.
- Вводить `DbLoggingService.log_skill_call(...)` —
  единственный допустимый путь логирования
  Skill-вызовов уже покрыт существующими
  `log_tool_call` / `log_tool_result` (потому
  что `tools.exec` — это tool).
- Эмитить events при `SkillsLoader.list_skills(...)`,
  `load_skill(...)`, `load_skills_for_context(...)`,
  `build_skills_summary(...)` или аналогичных
  чисто-loader методах.

#### Scenario: Skill script execution через tools.exec порождает tool_call, не skill_call

- **WHEN** агент запускает Skill-скрипт через
  `tools.exec("python skills/audit_analyzer/scripts/cli.py ...")`
- **THEN** `DbLoggingService` SHALL получить
  `LogEvent` с `event_type="tool_call"`,
  `name="exec"`, `actor="agent"`, payload
  содержит `args` (с командой запуска).
- **AND** `DbLoggingService` SHALL получить
  соответствующий `LogEvent` с
  `event_type="tool_result"`, `name="exec"`,
  payload содержит `result`.
- **AND** `skill_call` (или любой другой
  skill-typed event) SHALL NOT быть эмитирован.

#### Scenario: Загрузка SKILL.md в context не порождает event

- **WHEN** `SkillsLoader.load_skills_for_context(...)`
  или `build_skills_summary(...)` выполняется
  при построении agent context
- **THEN** `DbLoggingService` SHALL NOT получить
  никакой `LogEvent` (никакой `skill_call`,
  `skill_loaded`, `skill_discovered`, и т.п.).
- **AND** `agent_gateway_logs` SHALL NOT содержать
  записей, привязанных к этому вызову loader'а.

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
- **AND** `DbLoggingService.try_log_event(...)` SHALL
  обеспечивать no-op for business (событие не
  записано, compaction продолжается).
- **AND** `logger.warning(...)` SHALL быть вызван
  ровно один раз с сообщением вида
  `"ContextCompactionService: structured event
  context_compacted not persisted (DbLoggingService
  <reason>)"` (НЕ DEBUG, НЕ INFO, НЕ ERROR).
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
- **THEN** helper SHALL обеспечивать no-op for
  business (без `INSERT` и без `record_sync_event`
  fallback).
- **AND** sync-операция SHALL NOT быть прервана
  (sync-код не должен падать из-за отсутствия
  observability-сервиса).
- **AND** `DbLoggingService.try_log_event(...)` SHALL
  зафиксировать потерю события на уровне
  `WARNING` (НЕ DEBUG, НЕ INFO, НЕ ERROR) —
  единый уровень для всех producer'ов согласно
  Requirement «Uniform logging behavior при
  недоступности сервиса».

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
SHALL NOT быть записан (no-op for business +
operational WARNING на уровне `WARNING` через
`DbLoggingService.try_log_event`). Любой runtime-код,
который раньше «падал» в
`event_log.record_event` / `record_sync_event` /
`emit_sync_event` как fallback, SHALL быть переписан
на no-op for business + WARNING через `try_log_event`
(см. requirement «try_log_event contract»).

Запрещено вводить **любые другие persistence-хелперы**,
которые могли бы обойти `DbLoggingService` —
ни в виде deprecated-обёрток, ни в виде
«infrastructure safety net», ни в виде
fallback на `agent_question_runs`-таблицу
(это та же `DbLoggingService` ответственность).

#### Scenario: Приложение стартует до готовности DbLoggingService

- **WHEN** `ApplicationContext.start()` ещё не вызвал
  `db_logging_service.start()` (ранний startup,
  конфигурация резолвится, sync-сервисы ещё не инициализированы)
- **AND WHEN** какой-либо runtime-компонент пытается
  записать structured event
- **THEN** запись SHALL обеспечивать no-op for
  business (без прямого `INSERT` в
  `agent_gateway_logs`).
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
  операционной диагностики в терминал.
- **AND** утилита SHALL NOT импортировать
  `workspace.utils.event_log` (модуль удалён) и
  SHALL NOT выполнять прямой `INSERT INTO
  "<schema>"."<table>"`.
- **AND** если утилита желает структурно
  залогировать событие — она обязана создать
  собственный экземпляр `DbLoggingService` через
  composition root (не global singleton, не
  fallback на прямой INSERT).

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

### Requirement: agent_question_runs как отдельная aggregate-модель

`DbLoggingService` MUST владеть двумя разными
persistence-моделями (как разные aggregate-контракты
в одном сервисе):

1. **`agent_gateway_logs`** — event timeline
   (immutable-ish журнал structured agent events;
   строки добавляются, не обновляются);
2. **`agent_question_runs`** — request aggregate
   (per-request контекст: `user_id`, `agent_id`,
   `parent_request_id`, `is_subagent`, `status`,
   `summary`, `question`, `media`; обновляется
   через upsert по `request_id`).

`agent_question_runs` НЕ объединяется с
`agent_gateway_logs` в одну таблицу и НЕ
превращается в часть event timeline. Это
**другая persistence-модель**, отвечающая на
другие вопросы:
- event timeline: «что произошло в системе
  в момент X» (для `history_search`, observability);
- request aggregate: «какой вопрос сейчас
  обрабатывается и в каком он статусе» (для
  UI, отображения текущего request, маршрутизации).

`DbLoggingService` является владельцем обоих —
через специализированные методы
`register_request` / `finish_request` для
`agent_question_runs` (через
`_QuestionRunRecord` + `_handle_question_run` +
`_upsert_question_run`) и `log_event(LogEvent(...))`
для `agent_gateway_logs`. **Никаких вторых
writer'ов для обоих таблиц** вне `DbLoggingService`.

Запрещено:

- Вводить отдельный «run-store service»,
  «run-tracker», «run-state-manager» или
  аналогичные слои над `agent_question_runs`
  (или под ним).
- Сливать `agent_question_runs` с
  `agent_gateway_logs` в одну таблицу через
  JSONB-поле `request_state` — это другой
  persistence contract.
- Эмитить `agent_question_runs` rows через
  `record_event` / `record_sync_event` /
  `emit_sync_event` (или через прямой SQL) — это
  контрактно разные persistence-модели.

#### Scenario: agent_question_runs update через DbLoggingService

- **WHEN** agent регистрирует начало нового
  вопроса через `db_logging_service.register_request(...)`
- **THEN** строка SHALL быть вставлена в
  `agent_question_runs` через `DbLoggingService._handle_question_run`,
  NOT через прямой SQL.
- **AND** `agent_question_runs` SHALL остаться
  отдельной таблицей (не объединена с
  `agent_gateway_logs`).

#### Scenario: agent_question_runs row идентифицируется по request_id

- **WHEN** строка в `agent_gateway_logs`
  ссылается на `request_id`
- **THEN** соответствующий row в
  `agent_question_runs` SHALL существовать
  (через индекс `session_key → request_id` в
  `DbLoggingService._request_index`).
- **AND** обновление статуса
  (`finish_request(...)`) SHALL идти через
  `DbLoggingService` (`_handle_question_run` →
  `_upsert_question_run`), не через прямой SQL.

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
