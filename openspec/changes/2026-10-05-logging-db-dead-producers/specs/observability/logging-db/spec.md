# logging-db Specification — дельта

## MODIFIED Requirements

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
Каждый producer вызывает именно её, а не собственную
обёртку с собственным уровнем логирования.

Перечень producer'ов в требовании — **примеры, а не
закрытый список**, и он обязан состоять из
существующих. Правило держится вызовами
`try_log_event` в `ContextCompactionService`,
`MirrorPoller`, `FallbackTurnDeliveryFactory`,
`RepeatGuardHook` и `PostgresChannel`; именно они
перечислены ниже. Прежний перечень называл
`PgDuckDbSyncService`, `DuckDbCacheStore`,
`PreloadService` и `ApplicationContext._record_sync_skipped`
— все четыре удалены, и канон объяснял действующий
инвариант через классы, которых в проекте нет.

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
  `RepeatGuardHook.before_execute_tool(...)`)
  SHALL не бросить исключение и не вызвать
  прямой SQL INSERT.

#### Scenario: Все producer'ы используют один и тот же WARNING-уровень

- **WHEN** тест `tests/test_unified_event_logging_contract.py::TestDbLoggingServiceUnavailableBehavior`
  инспектирует `caplog.records` при недоступности
  сервиса для каждого producer'а
- **THEN** ВСЕ зафиксированные сообщения
  SHALL иметь `levelname == "WARNING"`.
- **AND** НЕ должно быть записей уровня `DEBUG`,
  `INFO`, `ERROR` или `EXCEPTION` от producer'ов
  в этом сценарии.

#### Scenario: Примеры в требовании — существующие классы

- **WHEN** канон перечисляет producer'ов правила
- **THEN** каждый названный класс и метод SHALL
  находиться в дереве агента
- **AND** правило, требующее перечисления, SHALL
  перечислять только живые примеры, иначе читатель
  идёт проверять инвариант и не находит ни носителя,
  ни примера

### Requirement: Producers не читают logging-DB конфиг

Runtime-producers structured events
(`ContextCompactionService`, `MirrorPoller`,
`FallbackTurnDeliveryFactory`, `RepeatGuardHook`,
`PostgresChannel`) SHALL NOT читать
`logging.db.*` или `channels.postgres.dsn` напрямую
для целей INSERT в `agent_gateway_logs`. Они SHALL
получать уже сконфигурированный `db_logging_service`
через composition root (`ApplicationContext._make_*`)
и вызывать его методы без знания DSN, `table_name`,
`schema`.

Перечень — примеры живых носителей инварианта;
прежний называл `PgDuckDbSyncService`,
`DuckDbCacheStore` и `PreloadService`, которых в
проекте нет.

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

## REMOVED Requirements

### Requirement: Sync-события через DbLoggingService

**Reason**: синхронизация PG→DuckDB снята целиком
(change `drop-local-cache-read-from-pg`, фаза 5), а вместе с ней
`PgDuckDbSyncService` и `DuckDbCacheStore`. Перечисленные
десять `event_type` не может эмитить никто: локального
снимка, который их наполнял, больше нет. API, на который
требование ссылается, без единого вызывающего:
`DbLoggingService.log_sync_event` (`lib/services/db_logging_service.py:1457`)
объявлен и не вызывается ниоткуда, а `record_sync_event`,
который требование запрещает как обходной путь, в проекте
не существует вовсе. Запрет запрещал несуществующее.

Третий сценарий требования — «preload health-summary через
DbLoggingService» — требовал ровно один `LogEvent` с
`event_type="vector_index_preload_health"` от
`PreloadService.preload_vector_indexes(...)` и payload
`declared` / `loaded` / `missing` / `orphan` / `stale`
(snapshot `PreloadService.compute_index_health`). И сервиса,
и метода нет: FAISS-прогрев уехал на платформу, чистая
функция расчёта живёт в `mcp-platform/libs/vectors/preload.py`,
а стартового прогрева индексов в агенте нет by design
(ленивая постройка). Требование требовало публикации от
удалённого сервиса и в этом виде исполнить его было нечем.

**Расчёт не удаляется вместе с требованием.**
`compute_index_health` остаётся в платформе и остаётся
посчитанным. Не решённым остаётся **адресат** публикации
(кто публикует сводку и с каким порогом) — это открытое
решение владельца, пункт **5.1** change
`2026-10-05-vector-indexes-canon-gap` и отдельный change.
