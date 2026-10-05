# logging-db Specification

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

## Scope

`shared` — единственный writer остался в агенте, а запись идёт транспортом в операцию `log_events` платформы
Реализация: `lib/services/log_transport.py` + `mcp-platform/.../data/tools/log_events.py`

## Requirements

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

> **Поправка 2026-10-03: `log_error` снят из списка.**
> Снятая формулировка: «`log_error` — специализированный builder,
> входящий через `log_event(LogEvent(...))`». Метод удалён из
> `lib/services/db_logging_service.py` целиком.
>
> Основание — не вкусовое решение, а мина на будущее. Имя `error`
> не входило в канонический словарь платформы
> (`mcp-platform/libs/enterprise_common/eventing/types.py`), а
> `data.log_unknown_event_type_policy` переведён в `strict`: первое же
> возвращение `log_error` уронило бы весь батч, в котором оно лежало,
> а вызывающий к этому моменту уже получил «принято». Молчаливую
> потерю соседних событий оборота этот путь уже давал.
>
> Вызывающего не было нигде: ни в `lib/`, ни в `workspace/`, ни в
> `tools/`, ни в точках входа — только в тестах. Проверено статическим
> сканом и подтверждено замером (см. эталон имён в
> `tests/test_journal_event_name_alignment.py`, раздел «ИМЕН, КОТОРЫХ
> ЗДЕСЬ НЕТ»). Снятые тесты: `TestNonBlocking::test_log_error`,
> `TestNamePopulation::test_error_name_is_error`.
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
  `PostgresChannel`) эмитит
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

> **Поправка 2026-10-03: `log_error` в перечне builder'ов больше не
> существует.** Снятая формулировка: «...или специализированный
> builder (`log_tool_call`, `log_tool_result`, `log_llm_call`,
> `log_error`, `log_inbound`, `log_outbound`, `log_sync_event`)».
> Обоснование снятия — в «Поправке 2026-10-03» к списку разрешённых
> путей выше (кратко: вызывающего не было нигде, кроме тестов, а имя
> `error` вне канонического словаря при `strict` роняет батч).
>
> Смысл сценария не изменился: перечень стал короче, а требование
> «никаких прямых `INSERT` помимо `DbLoggingService`» осталось в силе
> и относится ко всем producer'ам без исключения.

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

### Requirement: Событие несёт свой момент и выживаемый ключ порядка

Колонка `agent_gateway_logs.timestamp` SHALL означать момент
**ЗАПИСИ** строки (её ставит база, `DEFAULT CURRENT_TIMESTAMP`, в
момент батч-сброса) и SHALL NOT использоваться как момент
события. Каждое событие, прошедшее через
`DbLoggingService.log_event(...)`, SHALL дополнительно нести
свой момент **СОБЫТИЯ** в `metadata`:

- `metadata->>'occurred_at'` — ISO-8601 **UTC** (микросекунды),
  читаемая форма момента;
- `metadata->>'seq'` — тот же момент в наносекундах
  (`time.time_ns()`), выживаемый ключ **ПОРЯДКА**.

Оба ключа SHALL выводиться из одного мгновения (никаких двух
независимых `now()`), SHALL ставиться/перезаписываться самим
писателем (значение, принесённое producer'ом, SHALL игнорироваться —
иначе гарантия порядка перестала бы выполняться молча), и
SHALL переживать батчирование, поскольку едут вместе с событием
в JSONB `metadata`.

Внутри процесса `seq` SHALL быть строго монотонным: шаг системных
часов назад (NTP) SHALL NOT переворачивать порядок двух событий.
Ключ SHALL выводиться из часов, а не из локального счётчика:
журнал пишут два процесса (агент и отдельный subprocess
`enterprise-mcp`), и только часы у них общие.

**Поправка 2026-10-02: фраза снята, она была ошибочной.**
Снятая формулировка: «DDL-миграция для этого требования SHALL NOT
требоваться: `metadata` — существующая `jsonb`-колонка, оба
писателя (агент напрямую и платформенная операция `log_events`)
передают `metadata` как есть». Ошибочна она по двум причинам, и
обе вскрыты замером, а не спором о вкусе:

1. Обоснование опиралось на **ложное** Greenplum-ограничение
   («избежать миграции Greenplum-совместимого DDL»), которое это
   же требование группы требовало снять. Ссылаться на ограничение,
   которое одновременно требуется отменить, — самопротиворечие.

   > **Поправка 2026-10-03.** Снятая формулировка обосновывала это тем,
   > что «фактическая база — PostgreSQL 13.22, `pg_dist_partition`
   > отсутствует». Обоснование описывало тестовый контур и выдавало его
   > за боевую среду. Боевая среда — Greenplum 6.5, тестовый контур —
   > PostgreSQL 13.22, и DDL обслуживает оба движка. Сам вывод
   > требования (нужны настоящие колонки) остаётся в силе: он вскрыт
   > замером, а не этой формулировкой.
2. Измеренный выбор — **настоящие колонки**: на 48 979 боевых
   строках колонка выиграла 3 сценария из 3, а момент события в
   JSONB вдобавок неиндексируем (`(metadata->>'occurred_at')::timestamptz`
   требует `IMMUTABLE`, а приведение `text → timestamptz` — `STABLE`)
   и не сортируется как хронология.

Действующая формулировка: **DDL-миграция для этого требования
SHALL требоваться** — `agent_gateway_logs` получает `seq BIGINT` и
`occurred_at TIMESTAMPTZ`, порядок обязателен «добавить nullable →
backfill (`UPDATE … SET seq = (metadata->>'seq')::bigint …`) →
очистка строк без ключа → `SET NOT NULL`» (см. «DDL соответствует
фактической СУБД»). Измеренная величина и метод — в требовании
«Хранение момента события и идентификатора оборота выбрано
замером плана запроса».

**Что при этом не меняется.** Требование выше остаётся в силе
целиком: оба ключа выводятся из одного мгновения, ставятся
писателем, переживают батчирование и едут в `metadata`. Меняется
только **хранение**: `metadata` остаётся транспортом батча, а
таблицу читают и фильтруют по колонкам. Сортировка по текстовой
копии момента SHALL NOT использоваться (текстовый порядок не
совпадает с хронологическим).

Событие, отфильтрованное по `min_level` (в журнал оно не
попадёт), SHALL NOT получать метку времени.

#### Scenario: разные моменты остаются разными после батчирования

- **WHEN** события с заведомо разными моментами создания
  (1 мс между соседними) проходят через буфер `DbLoggingService`
  и уходят в БД **одним** батчем
- **THEN** в записанных строках `occurred_at` SHALL
  остаться 12 РАЗНЫХ значений с тем же разнесением в 1 мс,
  а `metadata->>'occurred_at'` SHALL остаться читаемой копией
  тех же моментов
- **AND** `ORDER BY seq, id` по колонке SHALL
  восстановить порядок создания событий
- **AND** ни одна из строк SHALL не нести момент сброса вместо
  своего (проверяется сквозным тестом
  `tests/test_turn_observability_events.py::TestEventTimeThroughRealBuffering`,
  который идёт через настоящий путь батчирования, а не напрямую
  в писатель).

#### Scenario: шаг часов назад не переворачивает порядок

- **WHEN** часы процесса отступают на 5 секунд между двумя
  событиями
- **THEN** `seq` второго события SHALL быть строго больше `seq`
  первого
- **AND** равные `seq` (возможные при потере обновления пола)
  SHALL разводиться `id` (UUID) — порядок восстановим всегда.

### Requirement: Оборот имеет начало, вызов модели и исход

`DatabaseLoggingHook` SHALL писать события жизненного цикла
оборота, отсутствовавшие в журнале: ни события начала оборота, ни
события начала вызова модели, ни исхода оборота с длительностью до
этой правки не существовало ни в коде, ни в таблице.

Имена событий SHALL быть объявлены в словаре платформы
(`mcp-platform/libs/enterprise_common/eventing/types.py`) —
`agent.started`, `agent.completed`, `agent.failed`,
`llm.requested`, `llm.completed`. Агент SHALL NOT импортировать
словарь (граница «агент ↔ платформа» — по протоколу MCP, не через
`sys.path`); соответствие проверяется тестом, читающим словарь как
файл.

| Событие | Точка | Что несёт |
|---|---|---|
| `agent.started` | `before_run` | факт «вопрос взят в обработку» (разница с `inbound` = ожидание в очереди + restore/compact) |
| `llm.requested` | `before_iteration` | номер итерации, модель, размер запроса (полный промпт НЕ дублируется — он в `llm_call`) |
| `llm.completed` | `after_iteration` | номер итерации, модель, `latency_ms`, `finish_reason`, usage |
| `agent.completed` | `after_run` | `outcome=completed`, `latency_ms`, `stop_reason`, `iterations` |
| `agent.failed` | `on_error` | `outcome=failed`, `latency_ms`, текст ошибки |

Терминальное событие оборота SHALL быть ровно одно: либо
`agent.completed`, либо `agent.failed`. `on_error` и `after_run`
могут быть вызваны оба (ошибка, пережившая обёртку итерации), а
при исключении `after_run` вовсе не вызывается — поэтому исход
определяется по `context.error`, а повторная запись блокируется
флагом на инстансе.

`llm.completed` SHALL писаться ДО раннего выхода по пустому
ответу: итерация без ответа тоже была вызовом модели.

#### Scenario: упавший оборот оставляет след без after_run

- **WHEN** оборот падает с исключением (nanobot зовёт `on_error`
  и делает re-raise, `after_run` не вызывается)
- **THEN** в журнале SHALL быть ровно одно `agent.failed` с
  `level=ERROR`, `metadata.outcome="failed"` и непустым
  `metadata.latency_ms`
- **AND** `agent.completed` SHALL NOT быть записан.

#### Scenario: успешный оборот — ровно одно терминальное событие

- **WHEN** `on_error` и `after_run` вызваны последовательно при
  `context.error=None`
- **THEN** записано SHALL быть ровно одно `agent.completed`,
  `agent.failed` SHALL NOT быть записан.

#### Scenario: полный след оборота собирается по порядку

- **WHEN** оборот проходит `inbound → before_run →
  before_iteration → after_iteration → after_run`
- **THEN** последовательность `event_type` SHALL быть
  `inbound, agent.started, llm.requested, llm.completed,
  llm_call, run_finished, agent.completed`
- **AND** все события SHALL нести один `request_id`.

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

### Requirement: Батчевая вставка журнала обязана быть исполнимой драйвером

Запись пачкой событий MUST идти через `execute_values`, и его форма MUST
согласована с контрактом драйвера:

- в SQL ровно **один** плейсхолдер `%s` — он помечает место, куда драйвер
  развернёт список строк; плейсхолдеров больше одного отвергаются на разборе
  текста запроса, без обращения к серверу;
- шаблон строки передаётся **отдельным аргументом**, а не пишется в SQL
  плейсхолдерами: без него драйвер выводит число колонок из длины кортежа и не
  знает про выражения вроде `now()`;
- выражения шаблона MUST идти в порядке колонок списка.

Сброс батча MUST NOT глотать отказ молча: отказ записи журнала — это отказ
записи журнала. Если он всё же переживается, оператор MUST видеть его отдельной
строкой с числом потерянных событий.

Проверка MUST доходить до настоящего разборщика драйвера. Страж на совпадение
счётчика плейсхолдеров с шириной строки на фейковом пуле непригоден: он
закрепляет сломанную форму и драйвера не зовёт
(`mcp-platform/tests/test_data_service.py::TestLogEvent::test_journal_insert_matches_execute_values_contract`).

#### Scenario: Форма с несколькими плейсхолдерами отвергается

- **WHEN** в SQL батчевой вставки больше одного `%s`, а шаблон не передан
- **THEN** драйвер MUST отвергнуть запрос ДО обращения к базе
- **AND** страж MUST падать на такой форме, а не проходить

### Requirement: Локальный след событий появляется всегда, когда платформа недоступна

`LocalFallbackSink` — последний рубеж: он существует затем, чтобы отказ
платформы не стирал расследование отказа. Поэтому каталог файла MUST создаваться
самим при первой записи (`os.makedirs(..., exist_ok=True)`), а не предполагаться
на месте: `open(..., "a")` каталоги не создаёт.

Каталог MUST создаваться **лениво**, при первой записи, а не в конструкторе:
пустой каталог без единого события — это шум, выдающий себя за след. Отказ
записи в след MUST учитываться счётчиком `dropped`, а не ронять сброс.

#### Scenario: Каталога следа нет

- **WHEN** сброс журнала обращается к fallback, а каталога файла не существует
- **THEN** файл MUST быть создан вместе с каталогом, и события MUST в нём
  оказаться
- **AND** счётчик `dropped` MUST остаться нулевым
  (`tests/test_log_transport.py::TestLocalFallback::test_creates_the_directory_it_writes_into`)

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

- **WHEN** тест `tests/test_unified_event_logging_contract.py::TestDbLoggingServiceUnavailableBehavior`
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
`agent_conversation_messages` (заметка видна в чате);
(b) observability-trail в
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

Владение записью личности оборота — отдельный вопрос и решается
требованием «Личность оборота имеет одного владельца»: `DbLoggingService`
MUST оставаться писателем, но MUST NOT быть хранилищем.

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
- Вводить в `DbLoggingService` второй носитель
  личности оборота (индекс вопросов, снимок
  входа или их эквивалент) — владелец один.

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
  (через привязку `session_key → request_id` в
  `TurnIdentityStore`).
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

### Requirement: Настраиваемый flush_interval_sec

The system SHALL принимать параметр `flush_interval_sec`
через конструктор `DbLoggingService` со следующими
инвариантами:

- Диапазон: `0.5 ≤ value ≤ 60.0` (секунды).
- Дефолт: `5.0`.
- Значение SHALL передаваться из resolved `SETTINGS`
  (`logging.db.flush_interval_sec`) через
  `ConfigurationResolver` → `ProjectSettings` →
  `ApplicationContext` → конструктор `DbLoggingService`.
  Сервис НЕ читает `config.json` напрямую.
- **Boundary ошибок валидации** (двухуровневый):
  - На уровне **модели** `LoggingDbSettings` —
    `pydantic.ValidationError` при выходе за диапазон
    (юнит-тест модели ловит именно `ValidationError`).
  - На уровне **resolver / конфигурации** —
    `ConfigurationError` (см. AGENTS.md «Профили
    конфигурации»), если `ConfigurationResolver` /
    `validate_project_settings` не может построить
    `ProjectSettings` (например, опечатка в имени секции,
    битый JSON). Интеграционный тест на пути
    `config.json → ConfigurationResolver → ProjectSettings`
    ловит именно `ConfigurationError`.
  - В обоих случаях — fail-fast, до старта сервиса.
- Поведение worker'а (батчевый flush по `flush_interval_sec`
  или `batch_size`, дедлайн-цикл с таймаутом) SHALL остаться
  как описано в `lib/services/db_logging_service.py:800-876`.

#### Scenario: Дефолтное значение
- **WHEN** в `config.json` отсутствует `logging.db.flush_interval_sec`
- **THEN** валидация `LoggingDbSettings` принимает дефолт `5.0`,
  `DbLoggingService._flush_interval == 5.0`

#### Scenario: Ускоренный flush
- **WHEN** `config.json::logging.db.flush_interval_sec = 1.0`
- **THEN** валидация принимает значение,
  `DbLoggingService._flush_interval == 1.0`,
  события в среднем видны в БД через 1–3 секунды

#### Scenario: Значение вне диапазона — ValidationError на модели
- **WHEN** `LoggingDbSettings(flush_interval_sec=0.1)`
  вызван напрямую (юнит-тест модели)
- **THEN** `pydantic.ValidationError` бросается
  с указанием диапазона, `ApplicationContext` НЕ
  вовлекается

#### Scenario: Битый config.json — ConfigurationError на resolver
- **WHEN** `config.json` содержит невалидный JSON
  или отсутствует обязательная секция, через которую
  валидируется `flush_interval_sec`
- **THEN** `ConfigurationResolver` / `validate_project_settings`
  бросает `ConfigurationError`, `ApplicationContext.start()`
  НЕ создаёт `DbLoggingService` (fail-fast)

#### Scenario: Значение передаётся через resolver chain
- **WHEN** `SETTINGS` сформирован `ConfigurationResolver`
  с `logging.db.flush_interval_sec = 2.0`
- **THEN** runtime-тест проверяет:
  `db_logging_service._flush_interval == 2.0`

### Requirement: Счётчик written_by_type

The system SHALL вести в `get_stats()` счётчик
`written_by_type: dict[str, int]`, где ключ — `event_type`,
значение — количество успешно записанных событий этого
типа.

Инварианты:

- Счётчик инкрементируется **только** после успешного
  `_flush_batch` (то есть когда INSERT в БД прошёл без
  исключения). В `_enqueue` инкремент ЗАПРЕЩЁН —
  это различает «поставлено в очередь» и «реально
  записано».
- При ошибке `_flush_batch` события из батча НЕ учитываются
  в `written_by_type` (они идут в `failed`).
- Срок жизни счётчика = lifetime экземпляра
  `DbLoggingService`. Повторный `start()` после `stop()`
  НЕ сбрасывает счётчик (диагностический сервис не
  теряет историю на restart worker'а).

#### Scenario: Видно, что run_finished пишется
- **WHEN** в течение сессии записано 5 `tool_call`,
  5 `tool_result`, 3 `run_finished`, 0 `subagent_run_finished`
- **THEN** `get_stats()["written_by_type"]` возвращает
  `{"tool_call": 5, "tool_result": 5, "run_finished": 3}`
  без ключа `subagent_run_finished`

#### Scenario: Счётчик не растёт при ошибке flush'а
- **WHEN** `_flush_batch` бросает исключение (например,
  БД недоступна) для батча из 3 `tool_call`
- **THEN** `stats["failed"]` инкрементируется на 3,
  `stats["written_by_type"]["tool_call"]` НЕ изменяется

#### Scenario: Счётчик не сбрасывается при restart
- **WHEN** экземпляр `DbLoggingService` прошёл
  `stop()` (с `written_by_type={"tool_call": 5}`),
  затем `start()` вызван повторно, и записан ещё 1 `tool_call`
- **THEN** `get_stats()["written_by_type"]["tool_call"] == 6`

### Requirement: Метрика oldest_queued_age_sec

The system SHALL публиковать в `get_stats()` поле
`oldest_queued_age_sec: float | None` — возраст самого
старого `LogEvent`, ожидающего записи в
`agent_gateway_logs`, в секундах.

Инварианты:

- Метрика SHALL учитывать только объекты `LogEvent`.
  `_QuestionRunRecord` (отдельная таблица `agent_question_runs`)
  и `_FlushSentinel` (служебный сигнал остановки) MUST NOT
  участвовать в вычислении.
- Если очередь содержит только `_QuestionRunRecord` или
  `_FlushSentinel`, или пуста — SHALL возвращаться `None`.
- Вычисление ленивое (только при вызове `get_stats()`),
  без отдельного потока.
- Для отслеживания возраста `LogEvent` SHALL иметь поле
  `queued_at: float | None`, заполняемое в `_enqueue`
  значением `time.time()`.

#### Scenario: Здоровая очередь
- **WHEN** очередь `LogEvent` пуста
- **THEN** `get_stats()["oldest_queued_age_sec"] is None`

#### Scenario: Задержка flush'а
- **WHEN** `flush_interval_sec=5.0`, событие добавлено в
  очередь 12 секунд назад, но ещё не flush'нуто
- **THEN** `get_stats()["oldest_queued_age_sec"] ≈ 12.0`

#### Scenario: Очередь с _QuestionRunRecord не учитывается
- **WHEN** в очереди есть `_QuestionRunRecord` и пусто
  `LogEvent`
- **THEN** `get_stats()["oldest_queued_age_sec"] is None`

#### Scenario: _FlushSentinel не учитывается
- **WHEN** в очереди только `_FlushSentinel` и нет `LogEvent`
- **THEN** `get_stats()["oldest_queued_age_sec"] is None`

---

# Наблюдаемость оборота: покрытие, словарь, итоги (группа 2026-10-02)

Группа требований ниже описывает связку «журнал фактов
(`agent_gateway_logs`) + таблица итогов оборота
(`agent_question_runs`)» с честным временем, единым
словарём и покрытием всех этапов оборота от постановки
вопроса до доставки ответа.

**Группа не отменяет ни одного требования выше.** Два
требования этой спецификации уже закрывают часть
предмета, и ниже они не переписаны, а взяты за основу:

- **«Событие несёт свой момент и выживаемый ключ порядка»**
  — момент события (`occurred_at`) и ключ порядка (`seq`),
  выводимые из часов. Требование действует и взято за основу, но
  **дополнено поправкой 2026-10-02**: хранение переносится в
  колонки `agent_gateway_logs`, а `metadata` остаётся
  транспортом батча. Запрет DDL-миграции ради этих полей снят
  явно и обоснованно (см. «Поправка 2026-10-02» в нём).
- **Решение о хранении принято замером** («Хранение момента
  события и идентификатора оборота выбрано замером плана
  запроса»): колонки `seq BIGINT` и `occurred_at TIMESTAMPTZ`,
  канонический порядок `ORDER BY seq, id` по колонке, победа 3
  из 3 на 48 979 боевых строк; прежнее обоснование («избежать
  миграции Greenplum-совместимого DDL») отменено как
  недействительное. Поэтому все формулировки ниже описывают
  **вариант (b)**, колонки, и читают момент события и порядок
  по колонкам; `metadata.turn_id` и `metadata.source` по
  тому же решению остаются в `metadata`.
- **«Оборот имеет начало, вызов модели и исход»** — события
  `agent.started`, `llm.requested`, `llm.completed`,
  `agent.completed`, `agent.failed` и правило «ровно одно
  терминальное событие». Ниже они приняты как есть; эта
  группа покрывает остальные этапы оборота.

Остальное в группе — измерено на живой базе
(PostgreSQL 13.22, read-only) либо прочитано в коде; способ
проверки указан в каждом требовании.

### Requirement: Порядок и момент события переживают границу процессов

Требование **уточняет** «Событие несёт свой момент и
выживаемый ключ порядка», который закрывает путь внутри
агента (буфер → батч → база). Оно не описывает вторую
половину журнала.

Журнал пишут два процесса: агент и отдельный subprocess
`enterprise-mcp` (`nanobot` и `enterprise_mcp`,
`mcp-platform/libs/enterprise_common/eventing/models.py:30-31`).
Событие, порождённое процессом платформы, MUST получать
`occurred_at` и `seq` **в момент своего возникновения в
этом процессе**, а не при приёме батча принимающей
стороной.

Операция `log_events` пишет строки напрямую
(`mcp-platform/servers/enterprise/capabilities/data/service/main.py:468-487` — сборка
`rows` и `cur.execute`), минуя конверт `AgentEvent`,
поэтому платформа момент события за агента **не
проставляет**. Отсюда требование: `INSERT` операции
`log_events` SHALL NOT подставлять момент события в колонку
времени записи; колонка `"timestamp"` остаётся моментом
записи строки и заполняется базой. Колонка момента
**события** — другая колонка, `occurred_at` (см. требование о
замере), и к требованию «не подставлять в момент записи» это
не относится: значения приезжают в теле батча в `metadata` и
разбираются в колонку тем же единственным табличным писателем.

Ключ `seq` MUST оставаться сквозным между процессами: обе
половины оборота (агентская и платформенная) упорядочиваются
одним выражением `ORDER BY seq, id` по колонке.

**Проверка:** cross-process тест — событие, порождённое
процессом платформы, и событие агента, разделённые
намеренной задержкой, MUST после записи в журнал
упорядочиваться по `seq` в порядке возникновения; тест
падает, если `log_events` начнёт проставлять момент события
в колонку записи.

#### Scenario: порядок двух процессов восстанавливается

- **WHEN** событие платформы и событие агента одного
  оборота записаны в общий журнал
- **THEN** `ORDER BY seq, id` SHALL
  дать порядок их **возникновения**, независимо от того,
  в каком батче и в каком порядке они пришли

### Requirement: Отсутствие ключа порядка определено и не молчит

Требование **дополняет** «Событие несёт свой момент и
выживаемый ключ порядка»: то требование задаёт, как ключ
ставится, а это — что означает его **отсутствие**. Второе
не было описано, и это главный пробел: PostgreSQL ставит
`NULL` в конец при `ASC`, то есть отсутствие ключа
превращается в ложное утверждение о порядке.

**Поправка 2026-10-02: смена хранилища не отменяет ничего из
этого требования.** Ключ порядка переехал в колонку `seq`
(см. «Хранение момента события и идентификатора оборота выбрано
замером плана запроса»), поэтому читатель смотрит на
`seq IS NULL` в колонке, а не на `metadata->>'seq'`. Семантика
отсутствия, двухчастное чтение и счётчик `unattributed`
сохраняются в силе **буквально**: `NULL` в колонке означает то же
«момент события неизвестен», а «собылось позже всего» не
означает ничего. Пока ограничение `NOT NULL` не применено
(см. ниже), отсутствие ключа представимо и обязано быть видно.

**Замер (живая база, 48 979 строк; сделан до появления колонки,
поэтому ключ считан в `metadata`).** Ключа `seq` нет **ни в
одной** строке — 0. При этом 46 553 строки имеют `metadata`
**без** `seq` и ещё 2 426 строк имеют `metadata IS NULL`;
46 553 + 2 426 = 48 979, то есть таблица целиком предшествует
введению ключа. Из них **9 633** строк имеют непустой
`request_id` — то есть претендуют на принадлежность обороту.
Именно эти 9 633 строки при `ORDER BY (metadata->>'seq')::bigint ASC`
встанут **в конец** оборота: не в своё место, а в конец, молча
и без ошибки. После миграции те же самые `NULL` окажутся
в колонке `seq`, и дефект воспроизведётся ровно тот же, если
читатель не отделит их, — величина замера от этого не меняется.
Остальные 39 346 строк без `request_id` в
сборку оборота не попадают вовсе, и потому повреждения не
наносят.

**Семантика отсутствия.** Отсутствие `seq` SHALL означать
«момент события **неизвестен**», а НЕ «событие произошло позже
всего остального». Помещать событие в конец порядка —
это утверждать о нём то, чего не знаем.

**Fallback-порядок.** Чтение оборота MUST состоять из двух
частей и возвращать обе:

1. **упорядоченная часть** — строки, у которых `seq` есть;
   порядок `seq`, равные значения разводит `id`;
2. **неатрибутированная часть** — строки, у которых `seq`
   нет; они **не** включаются в упорядоченную часть и
   возвращаются отдельным счётчиком (`unattributed`).

Чтение оборота MUST NOT возвращать частичный оборот молча:
если `unattributed > 0`, это MUST быть видно в результате
чтения, а не проявляться отсутствием строк. Иначе дефект
воспроизводит ровно ту же ложь, которую требование устраняет.

Выражение порядка MUST указывать поведение `NULL` явно
(`NULLS LAST` для упорядоченной части выбора недопустим
как неявное умолчание, потому что оно и есть источник
ошибки). Соблазн «сделать `NOT NULL` и не думать о `NULL`»
запрещён: он устраняет представимость дефекта, а читателя,
который дефект видел, — нет.

**Соотношение с `NOT NULL`.** Ограничение `seq bigint NOT NULL`
SHALL применяться **после** backfill и очистки, в указанном
порядке, и **не снимает** контракт чтения этого требования:

- ограничение убирает представимость дефекта в основном профиле,
  но читательская обязанность остаётся: контракт двухчастного
  чтения и счётчика `unattributed` проверяется в тестовой схеме,
  где колонка остаётся nullable, и именно там негативными
  тестами держится;
- ограничение применяется **только после** подтверждения, что оба
  писателя (агентская и платформенная половины) кладут ключ в
  колонку для каждой строки: строка без ключа после `NOT NULL` —
  это уже не «неизвестный момент», а отказ всей записи партии,
  то есть потеря событий целиком;
- `NOT NULL` MUST NOT применяться к строке, момент которой
  неизвестен по существу: неизвестный момент остаётся `NULL`, а
  такие строки удаляются отдельной явной операцией (ниже), и
  это следствие решения очистки, а не следствие ограничения.

**Данные.** Пользователь разрешил удалить исторические логи,
поэтому для строк, которые **никогда** не получат ключ,
правильный ход — **очистить, а не backfill**: ключ
восстанавливается из истории только вымышленно, а
придуманное время события хуже отсутствующего, потому что
оно неотличимо от настоящего. Критерий очистки — **не** тип
события, а наличие ключа: очищаются все строки, у которых
`seq` отсутствует. Известная первая транша — 29 754 строки
устаревшего потокового `outbound_final` (61 % таблицы), но
она **не покрывает** проблему: после неё остаётся 19 225
строки без ключа, и 2 426 из них с `metadata IS NULL`
не восстанавливаются вовсе (брать нечего). Поэтому
требование очистки формулируется по признаку «нет `seq`»,
а объём определяется замером перед операцией, и операция
SHALL отчитаться числом удалённых строк.

**Backfill миграции этому не противоречит.** Обязательный
backfill (`UPDATE … SET seq = (metadata->>'seq')::bigint …`)
**копирует уже существующий** ключ и ничего не придумывает; на
данных замера он затрагивает 0 строк, потому что ключа нет ни в
одной. Он нужен для другого — строк, записанных между переходом
писателя и применением миграции. Строки, у которых ключа нет ни
в колонке, ни в `metadata`, очищаются, а не заполняются.

**Проверка:** (1) unit-тест на трёх составах — все строки с
`seq`; часть без `seq`; `metadata IS NULL` — во всех трёх
случаях чтение оборота SHALL вернуть корректный порядок и
ненулевой `unattributed`, и ни одна строка без ключа SHALL
NOT попасть в упорядоченную часть; (2) мутационная
проверка — если убрать из результата счётчик `unattributed`,
тест SHALL упасть; (3) замер на живых данных после очистки:
`SELECT count(*) FROM agent_gateway_logs WHERE seq IS NULL`
SHALL давать 0 — до применения `NOT NULL` это те же строки,
что `metadata IS NULL OR NOT (metadata ? 'seq')`, — иначе
требование не выполнено, а не «почти выполнено».

#### Scenario: строка без ключа не встаёт в конец оборота

- **WHEN** оборот содержит 10 строк с `seq` и 2 строки без
  `seq` (одна из них с `metadata IS NULL`)
- **THEN** упорядоченная часть SHALL содержать ровно
  10 строк в порядке `seq`
- **AND** `unattributed` SHALL равняться 2
- **AND** ни одна строка без `seq` SHALL NOT оказаться после
  последней строки упорядоченной части как «собывшаяся
  позже»

### Requirement: writer — атрибуция источника события

Каждая строка журнала MUST нести признак того, какой
процесс её породил, чтобы платформенные события
(`tool.*`, `llm.*`) отличались от агентских (`agent.*`).

Признак называется `metadata.source` и SHALL принимать
значения из закрытого множества, объявленного в
`mcp-platform/libs/enterprise_common/eventing/models.py:30-31`:
`nanobot` (агент) и `enterprise_mcp` (платформа).
Дополнительно `metadata.component` (`data`,
`tool_execution`, `llm`) различает подсистему внутри
источника.

**ВНИМАНИЕ на имя.** В переговорах по этому change значение
для агента называлось `agent`. Платформа уже объявила
`nanobot`. Третье написание — это ровно тот дефект, ради
устранения которого написана эта группа (одно и то же
событие под двумя именами), поэтому спецификация требует
**существующего** написания `nanobot` и запрещает заводить
третье.

Требование **не вводит второго writer'а и не размывает**
Requirement «Single writer invariant of agent_gateway_logs».
`source` — атрибуция источника события, а не второй путь
записи: обе половины журнала пишутся одной операцией
`log_events`. Запрещено использовать `source` как оправдание
для прямого SQL из второго процесса.

Признак MUST переживать батчирование тем же транспортом,
что и момент события, — в `metadata`. В колонку он **не**
переезжает: решение о хранении (см. «Хранение момента события и
идентификатора оборота выбрано замером плана запроса») вводит
колонки только `seq` и `occurred_at`, а `source` остаётся
признаком разбора строки, для которого измеренной нужды в
индексе нет.

**Проверка:** guard-тест сверяет множество допустимых
значений `source` с константами `SOURCE_*` (значение вне
множества — падение); замер на живых данных
`SELECT metadata->>'source', count(*) ... GROUP BY 1` MUST
дать непустое распределение, иначе признак не заполняется
и половина строк неотличима от другой.

#### Scenario: платформенная строка отличима от агентской

- **WHEN** в журнале есть события исполнения операций
  платформой и события оборота агентом
- **THEN** их `metadata->>'source'` SHALL различаться, и по
  одному значению можно отличить половину оборота

### Requirement: Покрытие этапов оборота вне хука DatabaseLoggingHook

Оборот MUST быть покрыт по **каждому** из **18** этапов. Для
каждого этапа допустимы ровно **три** исхода: событие с
объявленным именем; факт, живущий не в журнале, а в таблице
итогов (этап называется явно); либо явное решение «не
логируется» с причиной. Этап без того и другого — дефект,
и guard-тест падает.

**Число этапов.** Нормативным является список из 18 этапов,
зафиксированный заказчиком 2 октября. В более ранней
постановке фигурировало число 17; расхождение снято в пользу
18, и эталон ниже разбирает все 18 без остатка. Проверяемость
обеспечивается тем, что таблица является артефактом теста, а не
пересказом: любая правка числа обязана быть правкой таблицы, и
guard-тест падает на рассинхроне.

Состояние на момент замера: полно логируются 5 этапов. Не
логируются шесть: начало оборота, ожидание в очереди, начало
вызова модели, доставка ответа, начало субагента, стоимость
оборота. Причина архитектурная: логирование навешано на хуки
вокруг уже произошедшего (вход, вызов tool'а, финал), а момент
«агент взял вопрос в работу» не наблюдаем ни одной точкой.
Следствие видно в данных: из 36 оборотов в окне только 2 имеют
полную связку вход + финал + конец. Из шести пробелов
требованием «Оборот имеет начало, вызов модели и исход» закрыты
два (этапы 2 и 4); остаются четыре (3, 12, 16, 18).

Этапы 2, 4, 5, 13, 14 закрыты требованием «Оборот имеет
начало, вызов модели и исход» и здесь не дублируются.
Таблица покрытия целиком (эталон; полнота проверяется
тестом, а не человеком):

| # | Этап оборота | Чем закрыт | Статус |
|---|---|---|---|
| 1 | Вопрос получен каналом | `agent.received` | **новое объявление** (было `inbound`); точка — приём в канале |
| 2 | Оборот начат | `agent.started` | закрыто требованием «Оборот имеет начало, вызов модели и исход» |
| 3 | Ожидание в очереди | — | **не логируется сознательно**: измеряется как `agent.started` − `agent.received`; обе границы уже есть, отдельная строка дала бы вторую запись того же факта |
| 4 | Вызов модели начался | `llm.requested` | закрыто требованием «Оборот имеет начало, вызов модели и исход» |
| 5 | Вызов модели завершён | `llm.completed` | закрыто требованием «Оборот имеет начало, вызов модели и исход» |
| 6 | Вызов tool начался | `tool.started` | есть, **но продублировано** агентским `tool_call` (см. «Один факт — одна строка») |
| 7 | Вызов tool завершён | `tool.completed` | есть, **но продублировано** агентским `tool_result` |
| 8 | Ошибка tool | `tool.failed` | пишет та сторона, которая отказ видит, а видящих сторон две: платформа — доменный отказ внутри успешного конверта (`pipeline.py` → `_refuse`), агент — отказ, возникший на проводе до входа в конвейер (`ToolAuditHook`); различает их `metadata.source` |
| 9 | Сжатие контекста | `agent.compacted` | есть как `context_compacted`, вне словаря — **новое объявление** (см. также «context_compacted через DbLoggingService») |
| 10 | Промежуточный ответ | — | **не логируется сознательно**: именно эти строки составляли 61 % таблицы (см. «Вычистка шума не может отключиться молча») |
| 11 | Финальный ответ сформирован | `agent.responded` | есть как `run_finished`, вне словаря — **новое объявление**; несёт текст ответа (см. отдельное требование) |
| 12 | Ответ доставлен | `agent.delivered` | **не логируется**; **новое объявление**, задано решением заказчика (см. отдельное требование) |
| 13 | Оборот завершён | `agent.completed` | закрыто требованием «Оборот имеет начало, вызов модели и исход» |
| 14 | Ошибка оборота | `agent.failed` | закрыто требованием «Оборот имеет начало, вызов модели и исход» |
| 15 | Строка итога записана | `agent_question_runs` | **факт вне журнала**: строка таблицы итогов, а не событие (см. «Гарантия строки итога на каждый оборот»). Сегодня пишется в 0.6 % случаев |
| 16 | Субагент начался | `agent.started` с признаком субагента | **не логируется отдельным именем сознательно**: субагент — оборот со своим идентификатором, родительским `request_id` и признаком `is_subagent`; отдельное событие дало бы вторую запись того же факта |
| 17 | Субагент завершён | `agent.completed` с признаком субагента | есть как `subagent_run_finished`, вне словаря; поглощается каноническим именем |
| 18 | Стоимость оборота | `agent_question_runs` (`cost_amount`, `cost_currency`, `*_tokens`) | **факт вне журнала**: выводится из `usage` в `llm.completed` и пишется итогом. Отдельная строка «стоимость» создала бы вторую запись того же факта |

Три этапа (15, 18 и, косвенно, 3) закрываются не событием
журнала, а таблицей итогов либо разностью соседних событий.
Это осознанно: журнал фактов и таблица итогов — разные
persistence-модели (см. Requirement «agent_question_runs как
отдельная aggregate-модель»), и требование покрытия обязано
это различать, иначе оно заставит писать в журнал то, что по
контракту живёт в итоге.

События 1, 9, 11, 12 требуют **новых объявлений** в
каноническом словаре платформы
(`mcp-platform/libs/enterprise_common/eventing/types.py`).
`agent.delivered` задано решением заказчика; `agent.received`,
`agent.compacted`, `agent.responded` предлагаются этой
спецификацией, и до их объявления словарь считается неполным.
`tool.suppressed` и `llm.exchanged` объявляются по тем же
правилам (см. «Единый словарь имён событий»), но отвечают не
этапам оборота, а классификации событий.

**Проверка:** guard-тест обходит эталонную таблицу и для
каждого этапа требует ровно одного из трёх исходов: имя
объявлено в `EVENT_TYPES` словаря платформы ИЛИ этап
помечен как факт вне журнала с названной таблицей ИЛИ этап
помечен «не логируется» с причиной. При добавлении нового
этапа без решения тест SHALL падать. Отдельно: замер на живых
данных `SELECT DISTINCT event_type` не должен содержать имён
этапов, помеченных «не логируется», а `SELECT count(*)` по
этапам-фактам вне журнала MUST быть сравним с числом оборотов
(сейчас 301 строка итога на 36 оборотов в окне, то есть 0.6 %).

#### Scenario: событие приёма вопроса наблюдаемо

- **WHEN** канал принимает сообщение пользователя
- **THEN** в журнале SHALL появиться строка `agent.received`
  с непустыми `session_id` и `user_id`
- **AND** от её момента до `agent.started` MUST быть
  посчитано ожидание в очереди

#### Scenario: субагент различим без отдельного события

- **WHEN** запускается субагент
- **THEN** его оборот SHALL иметь собственную строку
  `agent.started` с признаком субагента и родительским
  идентификатором
- **AND** отдельного события «запуск субагента» SHALL NOT
  быть записано

### Requirement: agent.delivered — момент доставки ответа

В словаре платформы MUST быть объявлено имя
`agent.delivered`. Это **новое объявление**: в текущем
словаре из 13 имён его нет.

`agent.delivered` MUST эмититься в момент фактической
передачи ответа в канал (в `stdout` для CLI), а не в
момент формирования текста. Разница между этими двумя
моментами — ровно то время, которое пользователь ждёт,
и сейчас оно не измеряется ничем.

Требование **не отменяет** существующего поведения
исходящего сообщения как отдельной записи
(`outbound_final` в шине, см. Requirement «Single writer
invariant of agent_gateway_logs»): доставка (этап 12) и
ответ (этап 11) — разные факты и разные строки.

**Проверка:** unit-тест точки доставки: `agent.delivered`
эмитится ровно один раз на успешную доставку; guard-тест
словаря падает, если имя не объявлено; мутационная
проверка — при отказе доставки событие успешной доставки
SHALL NOT быть записано, а отказ SHALL быть виден.

#### Scenario: доставка отказала — доставки не записано

- **WHEN** канал вернул ошибку доставки
- **THEN** `agent.delivered` SHALL NOT быть записан
- **AND** отказ SHALL быть виден как `agent.failed` либо
  как событие уровня `ERROR`, но не как доставка

### Requirement: agent.responded сохраняет текст финального ответа

Перевод журнала на канонические имена MUST NOT отнять у
`history_search` его основную документированную
возможность — искать **текст прошлых ответов агента**.

Сегодня эту возможность даёт `run_finished`, и она
объявлена в самом tool'е: «run_finished (previous final
answers)» (`workspace/tools/history_search_tool.py:139,279`).
Поэтому `run_finished` MUST отобразиться в `agent.responded`
(**новое объявление**), а не поглотиться `agent.completed`.

Различие существенно: `agent.completed` по требованию
«Оборот имеет начало, вызов модели и исход» несёт
`outcome`, `latency_ms`, `stop_reason`, `iterations` и
текста ответа не содержит. Слить их — значит убрать у
агента возможность найти собственный прошлый ответ после
сжатия контекста, потеряв это молча.

Требование вводит три разных факта оборота:
`agent.responded` (сформировал ответ) →
`agent.completed` (исход оборота) →
`agent.delivered` (доставил). Смешивать их нельзя.

**Проверка:** end-to-end тест tool'а: после оборота
`history_search(event_type="agent.responded")` SHALL найти
текст ответа; мутационная проверка — если `agent.responded`
перестал нести `payload.content`, тест SHALL упасть.

#### Scenario: прошлый ответ находится после сжатия контекста

- **WHEN** агент после сжатия контекста ищет свой прежний
  ответ через `history_search`
- **THEN** поиск по `agent.responded` SHALL вернуть текст
  ответа
- **AND** поиск по `agent.completed` SHALL вернуть исход
  оборота, но не текст ответа — это разные события

### Requirement: turn_id и request_id разделены

Колонка `agent_gateway_logs.request_id` сегодня перегружена:
в одних строках это идентификатор оборота, в других —
идентификатор вызова. Из-за этого группировка журнала по
`request_id` бессмысленна: в одной группе оказываются
разные обороты и разные вызовы.

Смыслы MUST быть разделены: `request_id` — идентификатор
**оборота** (он уже таким и является в `agent_question_runs`,
см. Requirement «agent_question_runs как отдельная
aggregate-модель»), а идентификатор **вызова** MCP-операции,
сегодня занимающий то же поле, MUST уйти в `metadata`
(`metadata.call_id`).

**Ключевое следствие, меняющее исходный план захода.**
Сегодня это разделение выполнимо **только** на стороне
платформы. Операция `log_events` берёт `request_id` из
контекста вызова и пишет его в колонку
(`mcp-platform/servers/enterprise/capabilities/data/service/main.py:542`), а `event_to_wire`
в тело батча его не кладёт
(`lib/services/log_transport.py:163-181`) — то есть
`request_id` в батче **нет физически**. Значит любое решение,
при котором агент должен передать платформе идентификатор
оборота, обязано начаться с того, чтобы этот идентификатор
**появился в теле батча**; иначе платформа подставит своё,
а агент не сможет передать своё.

Разделение выполняется в `metadata`: `metadata.turn_id` —
идентификатор оборота, `metadata.call_id` — идентификатор
вызова, `request_id` — как был. Оба новых значения приходят
от агента в теле батча и переживают батчирование.

**Хранение `turn_id` решено (2026-10-02): остаётся в
`metadata`.** Выбор сделан замером, и он нужен здесь, потому что
именно ограничение выше его и определяет. Колонка `turn_id` в
`agent_gateway_logs` **не вводится**, и обоснование одно:

- наполнить её платформа может только своим значением из контекста
  вызова, а это **чужое** значение: идентификатор оборота агента
  в теле батча нет физически (`lib/services/log_transport.py:163-181`),
  и появление `metadata.turn_id` в батче (пункт 2 заходов) —
  единственный способ его доставить. Колонка, которую заполняет
  не владелец значения, даёт второй источник истины и расхождение
  с `metadata` без единой измеримой выгоды;
- измеренной нужды в ней нет: оборот — 640 строк, сессия — 7915,
  группировка по текстовому ключу в таком объёме обходится без
  индекса. Индекс нужен там, где измерена разница: порядок
  (`seq`) и окно времени (`occurred_at`) — оба получили колонки;
- `request_id` уже даёт группировку оборота, поэтому колонка
  `turn_id` не разблокировала бы ни одного чтения, для которого
  сейчас нет индекса.

Прежнее обоснование («требует наименьших изменений», «избежать
миграции Greenplum-совместимого DDL») **снято**: ложность
Greenplum-объявления доказана, право менять схему выдано явно,
поэтому доводом служит не трудоёмкость, а измеренная нужда и
владение значением. Решение по хранению момента события и ключа
порядка — колонки — принято в Requirement «Хранение момента
события и идентификатора оборота выбрано замером плана запроса».

**Проверка:** замер на живых данных после backfill:
`SELECT metadata->>'turn_id', count(DISTINCT request_id)
FROM agent_gateway_logs GROUP BY 1` SHALL давать по одному
обороту на группу; guard-тест падает, если `event_to_wire`
снова перестанет передавать идентификатор оборота в тело
батча (сегодня это единственный способ его доставить).

#### Scenario: оборот собирается по одному идентификатору

- **WHEN** запрашивается журнал одного оборота
- **THEN** все его строки MUST находиться по одному
  значению идентификатора оборота
- **AND** порядок внутри оборота SHALL задаваться `seq`,
  а равные `seq` — разводиться `id`

### Requirement: Хранение момента события и идентификатора оборота выбрано замером плана запроса

Это требование **заменяет обоснование**, которым до него
отвечали на вопрос «почему момент события и идентификатор
оборота лежат в `metadata`, а не в колонках». Прежнее
обоснование было неверным и снимается.

**Почему прежнее обоснование отменяется.** Оно сводилось к
«избежать миграции Greenplum-совместимого DDL». Но это же
требование группы доказывает обратное: Greenplum-объявление
ложное, база — обычный PostgreSQL 13.22, и DDL **всё равно
придётся переписывать** (снятие `DISTRIBUTED BY` и
ложного комментария, добавление колонок в
`agent_question_runs`). Ссылаться на ложное ограничение, которое
одновременно требуется снять, — самопротиворечие. Право менять
схему таблиц журналов пользователь дал явно, значит
«избежать миграции» **не является** доводом ни за, ни против.

**Соотношение с уже написанным требованием.** «Событие несёт
свой момент и выживаемый ключ порядка» содержало фразу «DDL-миграция
для этого требования SHALL NOT требоваться». Пока выбор не был
сделан, фраза описывала следствие варианта (a) и потому не могла
служить его основанием. **Замер выполнен, вариант (a) отклонён, и
фраза снята явно** — см. «Поправка 2026-10-02» в том требовании и
запись в change `2026-10-02-journal-observability`. Молча она не
снята: измеренная величина приведена ниже.

**Решение принято: вариант (b) — настоящие колонки.**

`agent_gateway_logs` MUST получать колонки `seq BIGINT`
(выживаемый ключ порядка) и `occurred_at TIMESTAMPTZ` (момент
**события**). Канонический порядок чтения — `ORDER BY seq, id` по
**колонке**. Хранение момента события и ключа порядка в `metadata`
как каноническое снимается.

**Метод замера (выполнен 2026-10-02).** Реальные 48 979 строк
боевого журнала `public.agent_gateway_logs` скопированы **только на
чтение** в изолированную схему; два варианта с одинаковыми
колонками и одинаковой шириной строки. Вариант (a) — ключ порядка
в `metadata->>'seq'`, приведённый к `bigint`, индекс-выражение
`(request_id, ((metadata->>'seq')::bigint), id)`. Вариант (b) —
настоящая колонка `seq bigint`, обычный индекс `(request_id, seq,
id)`. Реалистичный ключ порядка сгенерирован как `row_number()
OVER (PARTITION BY request_id ORDER BY "timestamp", id)`. Оба
варианта — 48 979 строк, оборот на 640 строк, сессия на 7915 строк.
`EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)`, лучшее из 5 прогонов.
Изолированная схема удалена, боевая таблица не изменялась.

| сценарий | (a) выражение JSONB | (b) колонка | разница | победитель |
|---|---|---|---|---|
| чтение одного оборота, порядок по ключу | 0.384 мс | 0.242 мс | (a) медленнее на 58.7 % | (b) |
| выборка полей оборота (как читает история) | 0.275 мс | 0.197 мс | (a) медленнее на 39.6 % | (b) |
| все обороты сессии, порядок по ключу | 5.905 мс | 5.548 мс | (a) медленнее на 6.4 % | (b) |

**Счёт: колонка — 3, JSONB — 0.** Оба варианта используют
`Index Scan`, то есть различает стоимость доступа до строки, а не
тип доступа. В третьем сценарии оба плана добавляют шаг сортировки
(quicksort), поэтому разница минимальна: решающими являются первые
два, где порядок приходит прямо из индекса.

**Два блокирующих факта, найденных ДО замера.** Они ломают
вариант (a) на корне, то есть даже при равенстве планов (a) не
выбирается:

1. `(metadata->>'occurred_at')::timestamptz` **нельзя индексировать
   вовсе**. PostgreSQL требует `IMMUTABLE` от функций в индексном
   выражении, а приведение `text → timestamptz` зависит от
   `TimeZone` и потому `STABLE`. Ошибка сервера: «функции в
   индексном выражении должны быть помечены как IMMUTABLE».
   Значит момент события в JSONB требует либо собственной
   `IMMUTABLE`-функции, либо хранения **не как даты**.
2. **Хранить момент ISO-строкой нельзя**: текстовая сортировка
   **не соблюдает порядок**. Проверено на реальных значениях
   журнала: `'2026-10-02T16:42:33.261Z'` встаёт **после**
   `'2026-10-02T16:42:33.261000Z'`, потому что сравнение идёт по
   символам, а `0` меньше `Z`. Порядок получается неверным молча,
   без ошибки.

Вывод держится на обоих основаниях сразу: колонка выигрывает
3 из 3, и момент события в JSONB нереализуем как индексируемая
величина.

**Состав колонок — минимум, а не список пожеланий.** Колонками
становятся `seq` и `occurred_at`. `metadata.turn_id` и
`metadata.source` **остаются в `metadata`**, и обоснование одно:
идентификатор оборота агента и признак источника знает только
агент, а идентификатор оборота в теле батча **нет физически**
(`lib/services/log_transport.py:163-181`) — платформа взяла бы в
колонку собственное значение из контекста вызова, то есть чужое.
Для них нет и измеренной нужды: оборот — 640 строк, сессия — 7915,
группировка по текстовому значению в таком объёме индекса не
требует, тогда как `seq` обслуживает порядок, а `occurred_at` —
диапазон времени, и оба измерены.

**Порядок миграции обязателен.** `ADD COLUMN` (nullable) → backfill
(`UPDATE … SET seq = (metadata->>'seq')::bigint …`) → очистка
строк без ключа → только затем `SET NOT NULL`; обратный порядок
ломают существующие строки. Backfill **копирует уже существующий**
ключ и ничего не выдумывает; строки, у которых ключа нет ни в
колонке, ни в `metadata`, удаляются отдельной явной операцией (см.
«Отсутствие ключа порядка определено и не молчит»), а не
достраиваются.

**Транспорт не меняется.** Значения по-прежнему едут в `metadata`
внутри батча — только так они переживают батчирование и оба
писателя, — и разбирает их в колонки единственный табличный
писатель, операция `log_events` платформы. Агент SQL не пишет и
схему не меняет.

**Текстовая копия момента в `metadata` не годится ни для
порядка, ни для окна времени.** `metadata->>'occurred_at'` SHALL
остаться читаемой копией для человека и для разбора строки, но
сортировка по моменту и выборка по окну MUST идти по колонке
`occurred_at`, иначе вступает в силу блокирующий факт 2.

**Контракт метода, по которому замер выполнен** (сохраняется,
чтобы повторный замер был сравним с этим). На одинаковых данных,
одним и тем же запросом типичного чтения оборота, два варианта и
`EXPLAIN (ANALYZE, BUFFERS)` на каждом:

| | (a) выражение по JSONB | (b) настоящая колонка |
|---|---|---|
| хранение | `metadata->>'seq'`, `metadata->>'occurred_at'` | `seq BIGINT`, `occurred_at TIMESTAMPTZ` |
| индекс | индекс-выражение по тому же выражению | обычный btree |
| запрос | `ORDER BY (metadata->>'seq')::bigint, id` | `ORDER BY seq, id` |

Сравниваются: тип узла плана (`Index Scan` / `Index Only Scan` /
`Seq Scan` / `Bitmap`), фактическое число строк,
`shared hit` / `shared read`, и **наличие либо отсутствие
шага сортировки** — именно она отличает «порядок из индекса»
от «порядок после сортировки всего» при равной цене доступа.

- Если (b) выигрывает — вводятся колонки, и это требование
  дополняется требованием о миграции с backfill
  (`UPDATE … SET seq = (metadata->>'seq')::bigint …` до
  `SET NOT NULL`). **Именно этот исход и получен** (3 из 3).
- Если (a) выигрывает или равны — JSONB фиксируется как
  **осознанный** выбор, и здесь же записывается измеренная
  величина.
- Равные результаты не считаются поводом оставить вопрос
  открытым: при равенстве выбирается (b), потому что колонка
  не зависит от текста запроса, а выражение — зависит (см.
  требование о точном совпадении выражения ниже).

**Риск, который закрывается отдельно.** Смена решения снимает
исходную форму риска: индекс-выражение больше не строится и
зависеть от точного текста выражения в запросе больше нечему.
Остаётся форма, обратная прежней, — при индексе по колонке
запрос, вернувшийся к `(metadata->>'seq')::bigint`, читает индекс
`seq` **не по порядку** и тихо деградирует в сортировку, а при
784 MB таблицы это незаметно.

Канонический порядок, который другие требования этой группы
приводят как `ORDER BY (metadata->>'seq')::bigint, id`, — это
вариант (a), и он **больше не канонический**: те же требования
обновлены в этом же change на `ORDER BY seq, id` по колонке.

Поэтому:

- канонический порядок MUST быть объявлен **ровно в одном
  месте** и переиспользоваться всеми читателями, а не вписываться
  в каждый запрос заново;
- guard-тест MUST сверять, что запросы порядка обращаются к
  **колонке** `seq`, и падать на возврате к выражению по
  `metadata`;
- регрессионный тест плана MUST утверждать `Index Scan`, а не
  просто «запрос выполняется»: смена плана на `Seq Scan` или на
  сортировку обязана ронять тест, иначе подмена обнаружится
  только по жалобе на медленный ответ.

**Проверка:** артефактом требования являются записанный в change
результат `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)` по обоим
вариантам с числами (таблица выше; запись —
`openspec/changes/2026-10-02-journal-observability/proposal.md`)
и guard-тест с регрессионным тестом плана, которые проверяют уже
выбранный вариант (b). Миграция проверяется на тестовой схеме в
порядке «добавить → backfill → очистка → `SET NOT NULL`» (см.
«DDL соответствует фактической СУБД»).

#### Scenario: переписанный запрос не деградирует молча

- **WHEN** канонический запрос порядка уходит от колонки `seq`
  обратно к выражению по `metadata` (или приводит `seq` к типу)
- **THEN** guard-тест сверки носителя порядка SHALL упасть с
  указанием обоих текстов
- **AND** регрессионный тест плана SHALL упасть, если запрос
  перешёл на `Seq Scan` или на сортировку

### Requirement: Единый словарь имён событий

Журнал MUST писаться только каноническими именами из
словаря платформы
(`mcp-platform/libs/enterprise_common/eventing/types.py`).
Сейчас в словаре 13 имён, а агент пишет 20 живых имён мимо
него: `inbound`, `tool_call`, `tool_result`, `llm_call`,
`outbound_final`, `outbound_intermediate`, `run_finished`,
`turn_completed`, `turn_failed`, `subagent_run_finished`,
`context_compacted`, `session_cold_sync_deleted`,
`session_cold_sync_failed`, `session_stale_detected`,
`sync_lag_exceeded`, `channel_poll_error`,
`channel_unstick_error`, `tool_repeat_blocked`,
`tool_repeat_warned` и динамический.

Отображение MUST быть полным с обеих сторон: каждое живое
имя отображается в ровно одно каноническое, и каждое
каноническое имя, помеченное этапом оборота, достижимо из
таблицы покрытия. Храниться соответствие MUST в одном месте
(в тесте как эталон), а не разбросанными литералами по коду.

| Живое имя | Каноническое | Судьба строк |
|---|---|---|
| `inbound` | `agent.received` | новое объявление |
| `tool_call` | `tool.started` | склейка с платформенным (см. «Один факт — одна строка») |
| `tool_result` | `tool.completed` | склейка |
| `llm_call` | `llm.exchanged` | **новое объявление**: событие-носитель тел |
| `usage` из `llm_call` | `llm.completed` | usage уже несёт `llm.completed` |
| `run_finished` | `agent.responded` | новое объявление |
| `turn_completed` | `agent.completed` | один факт — одна строка |
| `turn_failed` | `agent.failed` | — |
| `subagent_run_finished` | `agent.completed` + признак субагента | — |
| `outbound_final` | `agent.delivered` | новое объявление (задано заказчиком) |
| `outbound_intermediate` | — | не логируется (этап 10) |
| `context_compacted` | `agent.compacted` | новое объявление |
| `tool_repeat_blocked` / `tool_repeat_warned` | `tool.suppressed` | новое объявление |
| `channel_poll_error`, `channel_unstick_error`, `sync_lag_exceeded`, `session_stale_detected`, `session_cold_sync_deleted`, `session_cold_sync_failed` | `agent.degraded` | новое объявление; различаются `name` и `metadata.cause` |
| `quality.check` | `quality.check` | без изменений, но с требованием к флагам (см. «Уровни логирования несут смысл») |

`llm_call` требует отдельного объяснения. По замеру его
`payload` — в среднем 62 КБ, максимум 130 КБ, при этом
полезная часть (`response`) в среднем 2.7 КБ, а `usage`
лежит в `metadata`. Остальные ~59 КБ — дословная копия
аргументов tool-вызовов, уже записанных отдельно. То есть
событие **не лишнее**: оно несёт тела, которые больше
негде взять, и именно поэтому `history_search` рекомендует
искать текст диалога именно в нём. Оно не удаляется, а
очищается от дублей (см. «Бюджет тел в журнале») и
переименовывается в `llm.exchanged`.

**Что именно меняется в уже написанной спецификации.**
Требование «Оборот имеет начало, вызов модели и исход»
содержит сценарий «полный след оборота собирается по
порядку», где последовательность имён задана как
`inbound, agent.started, llm.requested, llm.completed,
llm_call, run_finished, agent.completed`. Этот сценарий
**обновляется** вместе с переходом на словарь: `inbound` →
`agent.received`, `run_finished` → `agent.responded`,
`llm_call` → `llm.exchanged`. Сами **точки и порядок не
меняются** — меняются только имена. Это уточнение
приводится здесь явно, потому что иначе переименование
молча рассыплется в рассинхрон со сценарием, который
никто не перечитывает.

Переключатель `data.log_unknown_event_type_policy` уже
реализован и по умолчанию выставлен в `soft` — с
обоснованием в коде: агент шлёт snake_case-имена, которых в
словаре нет, и строгий режим уронил бы весь журнал агента
(`mcp-platform/servers/enterprise/capabilities/data/service/main.py:66-80`). Он проверен на
живом пути: батчевая запись идёт мимо конверта
`AgentEvent` (там же:468-487), а значит проверка словаря
действует именно через политику, а не через
`require_known`.

Переход в `strict` MUST происходить **после** приведения
имён с обеих сторон и MUST быть **отдельным заходом** с
замером: сначала `soft` со сбором расхождений, потом
`strict`. Включать раньше нельзя — это тот самый отказ,
который обоснованно отложен в коде.

**Проверка:** guard-тест полноты соответствия в обе
стороны (неиспользованное живое имя и недостижимое
каноническое имя — оба дефекта); существующий
`TestEventVocabulary::test_hook_event_names_are_declared`
(`tests/test_turn_observability_events.py:337`) расширяется
на весь список живых имён, а не только на имена хука;
замер: `SELECT DISTINCT event_type` не содержит имён вне
`EVENT_TYPES`.

#### Scenario: имя вне словаря отвергается на живом пути

- **WHEN** producer отправляет событие с именем `tool_call`
  при включённом `strict`
- **THEN** операция `log_events` SHALL отклонить вызов
- **AND** отказ SHALL быть виден вызывающему, а не
  растворён в счётчике «принято»

### Requirement: Тихие места словаря синхронизируются вместе с ним

Переименование события MUST NOT ломать места, которые
падают **без ошибки**. Два таких места известны, и оба
закреплены здесь как контракт.

1. **`history_search` — enum имён в схеме tool'а.**
   `workspace/tools/history_search_tool.py:112-121` содержит
   список имён событий прямо в JSON-схеме параметра
   `event_type`, то есть в описании, **видимом модели**.
   При расхождении имён агент отфильтрует по несуществующим
   именам и получит **пустой результат без ошибки**. Список
   MUST порождаться из того же эталона соответствия, что и
   словарь, и расхождение MUST ловиться тестом, а не
   замечанием пользователя.

2. **Код чистки журнала — точные литералы.**
   `EMPTY_OUTBOUND_EVENT_TYPES`
   (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:88-91`) содержит
   `outbound_final` и `outbound_intermediate` литералами.
   Переименование без правки этого кортежа отключает
   вычистку, и таблица растёт неограниченно — без единой
   ошибки. Кортеж MUST выводиться из того же эталона либо
   тест MUST падать, если в нём есть имя, которого агент
   больше не пишет.

Оба места — **тихие отказы**: расхождение не даёт ошибки ни
в одном случае, поэтому guard-тест на них обязателен.

**Проверка:** мутационная проверка на каждом месте — тест
падает, если в enum `history_search` или в
`EMPTY_OUTBOUND_EVENT_TYPES` появится имя, которого нет в
эталоне соответствия; отдельный тест ловит обратное
(эталон объявляет имя, а код чистки о нём не знает).

#### Scenario: history_search не предлагает модели несуществующее имя

- **WHEN** строится схема tool'а `history_search`
- **THEN** список имён в `enum` SHALL совпадать с именами,
  которые агент реально пишет по таблице соответствия
- **AND** расхождение SHALL ронять тест, а не молча
  возвращать пустую выдачу

### Requirement: Один факт — одна строка

Один вызов tool'а MUST порождать ровно одну строку начала и
ровно одну строку завершения — независимо от того,
исполнялся он в процессе платформы или в процессе агента.

Сегодня это не так: агент пишет `tool_call` + `tool_result`,
платформа пишет `tool.started` + `tool.completed` +
`tool.failed`, и на MCP-вызовах это **4 строки на один
вызов**. Два писателя описывают один факт, и по таблице
невозможно ни посчитать число вызовов, ни отличить
платформенный вызов от агентского.

Правило владения: факт вызова пишет **тот, кто его
исполнял**. Операции, исполняемые конвейером платформы,
пишет платформа (`tool.*`); tool'ы, исполняемые в процессе
агента, пишет хук агента. В обоих случаях — одна строка
начала и одна завершения, а не сумма двух писателей.

Требование **не отменяет** ограничения «Skill invocation is
out of scope»: вызов Skill-скрипта через `tools.exec` по
прежнему логируется как вызов tool'а, а не отдельным
`skill_call`.

**Проверка:** замер на живых данных после правки —
`SELECT count(*) FROM agent_gateway_logs WHERE event_type =
'tool.started'` должен совпадать с числом фактических
вызовов (сверяется с `written_by_type` хука за то же
окно); тест на взаимную исключительность: ни одна пара
идентификаторов оборота и вызова не SHALL иметь одновременно
агентскую и платформенную строку начала.

#### Scenario: MCP-вызов описан одной парой строк

- **WHEN** агент вызывает операцию через `enterprise-mcp`
- **THEN** в журнале SHALL быть ровно одна `tool.started` и
  ровно одна `tool.completed` (или `tool.failed`) с одним и
  тем же `tool_call_id`
- **AND** агентские `tool_call` / `tool_result` для этого
  вызова SHALL NOT быть записаны

### Requirement: tool_call_id обязателен на событиях вызова

Каждое событие жизненного цикла вызова tool'а
(`tool.started`, `tool.completed`, `tool.failed`,
`tool.timeout`, `tool.suppressed`) MUST содержать непустой
`tool_call_id` — тот же ключ, который уже использует агент
(`log_tool_call(..., tool_call_id=...)`,
`lib/services/db_logging_service.py:797`).

Сегодня этого нет: 102 платформенных `tool.*` не содержат
`tool_call_id` ни в `metadata`, ни в `payload`, поэтому
начало и конец не склеиваются; 53 различных `tool_call_id`
есть только у агентских строк. Длительность вызова по
журналу не считается ни для одной MCP-операции.

**Проверка:** замер на живых данных — `SELECT count(*)
FROM agent_gateway_logs WHERE event_type LIKE 'tool.%' AND
metadata->>'tool_call_id' IS NULL` SHALL давать 0; тест на
склейку пары начало/конец по `tool_call_id` для одного
вызова.

#### Scenario: вызов склеивается началом и концом

- **WHEN** вызов tool'а завершился
- **THEN** `tool.started` и `tool.completed` с одним
  `tool_call_id` MUST давать разность моментов события
  строго больше нуля

### Requirement: Длительность этапа выводится из момента события

Длительность любого этапа оборота MUST вычисляться как
разность моментов события двух его границ, а не из
`timestamp` (момента записи). Запрещено хранить длительность,
которую нельзя пересчитать из строк журнала: расхождение
сохранённого и вычисленного значения — признак порчи
данных, а не повод доверять сохранённому.

Минимальный набор вычисляемых величин (все — из строк
журнала, без обращения к другим таблицам):

- ожидание в очереди = `agent.started` − `agent.received`;
- вызов модели = `llm.completed` − `llm.requested`;
- вызов tool'а = `tool.completed` − `tool.started`;
- формирование ответа = `agent.responded` − `llm.completed`;
- доставка = `agent.delivered` − `agent.responded`;
- оборот целиком = `agent.delivered` − `agent.received`.

Сегодня ни одна из этих величин по таблице не считается:
внутри строк своего времени не было ни у одного типа
(`tool_call` — 0 из 52, `llm_call` — 0 из 35,
`run_finished` — 0 из 11), а 358 строк легли на 214
уникальных меток, до 14 строк на одну миллисекунду.

**Проверка:** тест-эталон на живом окне: для каждой из шести
величин MUST строиться запрос, и на окне с полными следами
MUST возвращать положительные значения. Отрицательное
значение — дефект данных, тест падает.

#### Scenario: длительность этапа — разность моментов события

- **WHEN** длительность этапа оборота вычисляется по строкам
  журнала
- **THEN** она SHALL вычисляться как разность моментов события
  двух его границ
- **AND** `timestamp` (момент записи) SHALL NOT быть источником
  длительности этапа

#### Scenario: расхождение сохранённого и вычисленного — порча

- **WHEN** сохранённое значение длительности не совпадает
  с вычисленным из моментов события
- **THEN** расхождение SHALL считаться признаком порчи
  данных
- **AND** сохранённое значение SHALL NOT признаваться верным
  на том основании, что оно записано в строке

#### Scenario: тест-эталон строит запросы по всем шести величинам

- **WHEN** выполняется тест-эталон на живом окне
- **THEN** для каждой из шести величин MUST строиться запрос,
  а на окне с полными следами он SHALL возвращать
  положительные значения
- **AND** отрицательное значение SHALL считаться дефектом
  данных, из-за которого тест падает

### Requirement: Итог оборота: длительность, исход и стоимость

`agent_question_runs` — таблица итогов оборота (см.
Requirement «agent_question_runs как отдельная
aggregate-модель»); она MUST отвечать на вопросы «сколько
оборот длился», «чем закончился» и «сколько стоил», не
требуя разбора журнала.

Длительность и исход УЖЕ выводятся из существующих
`created_at` / `updated_at` / `status`, поэтому новые
колонки для них MUST NOT дублировать вычисляемое без
необходимости. Добавляются:

- `started_at TIMESTAMPTZ` — момент `agent.started` оборота
  (`created_at` остаётся моментом регистрации вопроса; это
  разные события, и их разность — время до старта);
- `finished_at TIMESTAMPTZ` — момент терминального события
  оборота;
- `duration_ms BIGINT` — материализованная разность
  `finished_at − started_at`, CHECK `duration_ms >= 0`.
  Должна совпадать с `latency_ms` терминального события
  из требования «Оборот имеет начало, вызов модели и
  исход» — оба выведены из одного и того же момента;
- `outcome VARCHAR(16)` — терминальный исход
  (`completed` / `failed` / `delivered` / `cancelled`) с
  CHECK на закрытое множество; `status` остаётся рабочим
  состоянием (`running` / `finished` / `error`) и не
  подменяется;
- `input_tokens BIGINT`, `output_tokens BIGINT`,
  `total_tokens BIGINT` — сумма `usage` по всем
  `llm.completed` оборота;
- `cost_amount NUMERIC(18,8)` и `cost_currency VARCHAR(8)`
  — стоимость оборота; валюта обязательна вместе с
  суммой, иначе число нельзя прочитать.

Стоимость MUST выводиться из `usage` оборота, а не из
текста ответа; сейчас usage лежит в `metadata` события и в
таблицу итогов не попадает, поэтому стоимость оборота
неизвестна ни по одной из 301 существующей строки.

**Проверка:** тест-эталон на живом окне:
`SELECT duration_ms, EXTRACT(EPOCH FROM (finished_at -
started_at))*1000 FROM agent_question_runs WHERE duration_ms
IS NOT NULL` SHALL давать совпадение в пределах 1 мс; CHECK
`duration_ms >= 0` проверяется тестом на отрицательном
значении; замер: `SELECT count(*) FROM agent_question_runs
WHERE total_tokens IS NOT NULL` после правки SHALL быть
сравним с числом оборотов, а не равен нулю, как сейчас.

#### Scenario: исход оборота не подменяется рабочим статусом

- **WHEN** оборот провалился
- **THEN** `status` SHALL отражать рабочее состояние, а
  `outcome` — терминальный исход
- **AND** одно поле SHALL NOT кодировать оба смысла

### Requirement: Гарантия строки итога на каждый оборот

`agent_question_runs` MUST содержать ровно одну строку на
каждый оборот. Сегодня строка пишется в **0.6 %** случаев,
то есть таблица итогов на 99.4 % оборотов молчит — при том
что на каждый оборот уже пишется множество событий в
`agent_gateway_logs`.

Гарантия MUST обеспечиваться двумя способами одновременно:

1. **Конструктивно** — регистрация оборота (`register_request`)
   обязана вызываться на пути входа в оборот, рядом с
   эмитом `agent.started`, а не в отдельной необязательной
   точке.
2. **Проверкой ответа операции** — операция
   `upsert_question_run` MUST подтверждать факт записи.
   Сегодня она этого не делает: возвращает `{"status": "ok"}`
   безусловно, отбрасывая результат
   `data.upsert_question_run(...)` типа `bool`
   (`capabilities/data/tools/upsert_question_run.py:41-61`).
   Наблюдаемое следствие: платформа рапортует об успехе, не
   сделав ничего — строка в `agent_question_runs_test` не
   появляется (последняя — 29 сентября при полных следах
   событий 2 октября).

Конкретная причина отсутствия строки в этом change **не
установлена**; спека фиксирует наблюдаемый контракт и
требование проверки, а не диагноз. Диагностика — отдельная
задача (см. «Порядок заходов и блокеры»).

**Проверка:** мутационная проверка — тест вызывает
операцию с заведомо непроходимым именем таблицы и SHALL
получить отказ вместо `{"status": "ok"}`; замер на живых
данных: отношение числа строк `agent_question_runs` к
числу `agent.received` за одно окно SHALL стремиться к 1 и
SHALL NOT быть 0.006; end-to-end тест на тестовом профиле
(`infrastructure/test-profile-tables`) обязан увидеть новую
строку.

#### Scenario: отказ записи не выдаётся за успех

- **WHEN** операция `upsert_question_run` не смогла записать
  строку
- **THEN** она SHALL вернуть отказ
- **AND** вызывающий (`DbLoggingService`) SHALL увеличить
  счётчик потерь, а не счётчик записей

### Requirement: DDL обслуживает обе среды — боевую и тестовую

DDL в `sql/logs/` MUST применяться к **обоим** движкам, на которых
работает проект, и не заявлять о совместимости с одним, будучи
неприменимым к другому.

Боевая среда — **Greenplum 6.5** (ядро PostgreSQL 9.4). Тестовый контур —
**PostgreSQL 13.22**. Оба движка получают **одни и те же файлы** из
`sql/`, а не два набора.

> **Поправка 2026-10-03.** Ранее это требование объявляло «фактической
> СУБД» PostgreSQL 13.22 и предписывало **снять** `DISTRIBUTED BY`,
> считая его ложным объявлением. Объявление описывало тестовый контур и
> выдавало его за боевую среду. Решение владельца: Greenplum 6.5 — боевая
> среда, PostgreSQL 13.22 — тестовый контур, DDL обслуживает оба.

Отсюда требование, обратное прежнему по форме: клауза распределения
**обязательна**, но **не в теле `CREATE TABLE`** — на PostgreSQL это
синтаксическая ошибка, и тестовый контур не поднимется. Распределение
объявляется отдельным ограждённым шагом `ALTER TABLE ... SET DISTRIBUTED
BY` внутри `DO`-блока, срабатывающего только при наличии служебного
каталога `pg_dist_partition`; на PostgreSQL шаг становится no-op.

По той же причине идемпотентные конструкции 9.5/9.6
(`CREATE INDEX IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) заменены на
`DO`-блоки с проверкой `pg_indexes` и `information_schema.columns`:
`psql -f` обязан оставаться no-op при повторном применении.

Канон среды и карантин известного долга — `docs/architecture/runtime-environment.md`
и `tests/test_runtime_environment_contract.py`. Требование «не более
одного `UNIQUE`/`PRIMARY KEY` на хеш-распределённой таблице» — там же.

Соотношение с решением о хранении. Запрет DDL-миграции
**ради** `occurred_at`/`seq`, который держало требование
«Событие несёт свой момент и выживаемый ключ порядка»,
**сенят явно** («Поправка 2026-10-02» в нём): замер выбрал
настоящие колонки, а пользователь разрешил менять схему
журналов. Черновик DDL ниже — уже **обязательная** часть
этого требования, а не «черновик на случай выбора».

**Порядок миграции `agent_gateway_logs` обязателен:**
добавить nullable-колонки → backfill → очистка строк без
ключа → только затем `SET NOT NULL`. `SET NOT NULL` до
backfill ломает миграцию существующими строками, а backfill
после него невозможен: ограничение не даёт оставить `NULL`
там, где ключа нет, и превращает «момент неизвестен» в отказ
записи всей партии.

Backfill **копирует уже существующий** ключ и ничего не
выдумывает; на данных замера он затрагивает 0 строк (ключа нет
ни в одной), и нужен для строк, записанных между переходом
писателя и миграцией. Строки без ключа ни в колонке, ни в
`metadata` очищаются, а не заполняются.

Минимум, обязательный в любом случае: ограждённый шаг
`SET DISTRIBUTED BY`, CHECK формы имени и колонки
`agent_question_runs`.

Черновик DDL (проект внутри спецификации; **применять
миграцией нельзя** в рамках этого требования):

```sql
-- Ключ распределения объявляется ограждённым шагом, а не в теле
-- CREATE TABLE: на PostgreSQL 13.22 клауза DISTRIBUTED — синтаксическая
-- ошибка. Шаг выполняется только при наличии служебного каталога
-- pg_dist_partition, то есть только на Greenplum.

-- agent_gateway_logs: момент события и ключ порядка.
-- Шаг 1 — колонки nullable: иначе существующие строки ломают
-- шаг. Шаг 2 (backfill) и шаг 3 (SET NOT NULL) — ниже и
-- обязательны в указанном порядке.
ALTER TABLE public.agent_gateway_logs
    ADD COLUMN IF NOT EXISTS seq         BIGINT,
    ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;

-- Шаг 2 — backfill: копирует УЖЕ СУЩЕСТВУЮЩИЙ ключ из metadata,
-- ничего не выдумывает. На данных замера затрагивает 0 строк.
UPDATE public.agent_gateway_logs
   SET seq         = (metadata->>'seq')::bigint,
       occurred_at = (metadata->>'occurred_at')::timestamptz
 WHERE metadata IS NOT NULL
   AND metadata ? 'seq';

-- Шаг 3 — ограничение. Применяется ТОЛЬКО после backfill и
-- очистки строк без ключа (очистка — отдельная операция по
-- критерию «нет seq», см. «Отсутствие ключа порядка определено
-- и не молчит»); раньше он ломает миграцию.
ALTER TABLE public.agent_gateway_logs
    ALTER COLUMN seq SET NOT NULL;

-- agent_gateway_logs: форма имени события.
ALTER TABLE public.agent_gateway_logs
    ADD CONSTRAINT agent_gateway_logs_event_type_ck
    CHECK (event_type ~ '^(agent|llm|tool|artifact|quality)\.');

-- agent_question_runs: длительность, исход, стоимость.
ALTER TABLE public.agent_question_runs
    ADD COLUMN IF NOT EXISTS started_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS finished_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS duration_ms   BIGINT,
    ADD COLUMN IF NOT EXISTS outcome       VARCHAR(16),
    ADD COLUMN IF NOT EXISTS input_tokens  BIGINT,
    ADD COLUMN IF NOT EXISTS output_tokens BIGINT,
    ADD COLUMN IF NOT EXISTS total_tokens  BIGINT,
    ADD COLUMN IF NOT EXISTS cost_amount   NUMERIC(18,8),
    ADD COLUMN IF NOT EXISTS cost_currency VARCHAR(8);

ALTER TABLE public.agent_question_runs
    ADD CONSTRAINT agent_question_runs_duration_ck
    CHECK (duration_ms IS NULL OR duration_ms >= 0),
    ADD CONSTRAINT agent_question_runs_outcome_ck
    CHECK (outcome IS NULL
           OR outcome IN ('completed', 'failed',
                          'delivered', 'cancelled'));
```

Обратная совместимость DDL обязательна: колонка
`"timestamp"` не переименовывается (её читает
`history_search`), `agent_question_runs.request_id` остаётся
PK, а состав колонок `agent_gateway_logs` меняется
**ровно на две** — `seq` и `occurred_at`. Ни одна колонка не
удаляется и не переименовывается; `metadata` остаётся и
продолжает нести `seq`/`occurred_at` как транспорт батча и
читаемую копию.

**Проверка:** тест, применяющий черновик DDL к тестовой
схеме (`infrastructure/test-profile-tables`) и падающий на
любой строке, не проходящей ни на Greenplum 6.5, ни на
PostgreSQL 13.22; отдельный тест проходит шаги в порядке
«добавить → backfill → очистка → `SET NOT NULL`» и падает,
если `SET NOT NULL` выполнен до очистки; guard-тест
падает, если в `sql/logs/*.sql` клауза `DISTRIBUTED BY`
вернётся в тело `CREATE TABLE`, если `DISTRIBUTED RANDOMLY`
появится рядом с ключом, если появится конструкция новее ядра
9.4 или если `SET DISTRIBUTED BY` будет без ограждения
`pg_dist_partition`; миграции `sql/migrations/*` и
`schema_migrations.sql` этим change MUST NOT применяться.

#### Scenario: клауза распределения ограждена движком

- **WHEN** guard-тест сверяет `sql/logs/*.sql` с обоими движками
- **THEN** он SHALL падать на `DISTRIBUTED BY` в теле
  `CREATE TABLE`: на PostgreSQL 13.22 это синтаксическая ошибка
- **AND** он SHALL требовать `SET DISTRIBUTED BY` внутри
  `DO`-блока с проверкой `pg_dist_partition`, чтобы на Greenplum
  ключ был объявлен явно, а на PostgreSQL шаг стал no-op
- **AND** черновик DDL SHALL применяться только к тестовой
  схеме `infrastructure/test-profile-tables` и падать на любой
  строке, не проходящей ни на одном из двух движков

#### Scenario: SET NOT NULL не опережает backfill и очистку

- **WHEN** выполняется миграция `agent_gateway_logs`
- **THEN** порядок SHALL быть «nullable-колонки → backfill →
  очистка строк без ключа → `SET NOT NULL`»
- **AND** тест SHALL падать, если `SET NOT NULL` выполнен
  до очистки: backfill после него невозможен

#### Scenario: состав колонок меняется ровно на две

- **WHEN** черновик DDL применён к тестовой схеме
- **THEN** колонка `"timestamp"` SHALL NOT быть переименована
  (её читает `history_search`), а
  `agent_question_runs.request_id` SHALL остаться PK
- **AND** состав колонок `agent_gateway_logs` SHALL измениться
  ровно на `seq` и `occurred_at`, а `metadata` SHALL остаться
  носителем обоих
- **AND** миграции `sql/migrations/*` и `schema_migrations.sql`
  этим change SHALL NOT применяться

### Requirement: Индексы журнала по замеренной селективности

Состав индексов журнала MUST оцениваться по **селективности**
(строк на чтение), а не по числу чтений. Число чтений —
обманчивый признак: индекс с 40 чтениями может отдавать по
5 строк, а индекс с 7 чтениями — по 700.

**Замер `pg_stat_user_indexes`, `agent_gateway_logs` — 8
индексов, используются все 8, с нулевым чтением нет ни
одного.** (Ранее в брифе значилось «индексов 7» при
перечне из восьми имён; расхождение снято в пользу восьми.)

| Индекс | Чтений | Строк | Строк на чтение |
|---|---|---|---|
| `gateway_logs_pkey` | 5 | 11 | ~2 |
| `idx_agent_logs_level` | 7 | 4 892 | **~699** |
| `idx_agent_logs_name` | 12 | 101 | ~8 |
| `idx_agent_logs_request` | 21 | 24 978 | ~1 189 |
| `agent_gateway_logs_user_id_timestamp_idx` | 40 | 217 | ~5 |
| `idx_agent_logs_event_type` | 75 | 160 279 | ~2 137 |
| `idx_agent_logs_session` | 142 | 10 862 | ~77 |
| `idx_agent_logs_timestamp` | 667 | 275 491 | ~413 |

**Замер, `agent_question_runs` — 6 индексов, 4 с НУЛЕВЫМ
чтением.** Живыми остаются `question_runs_pkey` (1366 чтений /
760 строк) и `idx_agent_qruns_subagent` (2 / 8). Без
чтения: `idx_agent_qruns_user`, `idx_agent_qruns_session`,
`idx_agent_qruns_parent_request`, `idx_agent_qruns_agent`.

**Вывод, который обязан governs решения.** Ни один индекс
`agent_gateway_logs` НЕ удаляется: все восемь работают, и
низкое число чтений не является основанием для удаления —
`idx_agent_logs_level` при 7 чтениях отдаёт ~699 строк, то
есть работает, но селективность ничтожна, и это **другая**
причина для пересмотра, чем ноль чтений.

Кандидаты на удаление, обоснованные замером:

- `idx_agent_logs_level` — по селективности: в данных
  присутствуют только два уровня из четырёх (см. «Уровни
  логирования несут смысл»), и индекс по 2-значной колонке
  не может сузить выборку; удаление допустимо **только** если
  после правки уровней он всё ещё не различает ничего;
- четыре индекса `agent_question_runs` с нулевым чтением.

**Оговорка, обязательная к исполнению.** Нулевое чтение —
признак слабый, а не приговор: индекс может быть недавно
создан или использоваться редким путём. Удаление MUST
выполняться только после **названного окна наблюдения**,
и окно MUST быть записано в change вместе с замером до и
после. Без записанного окна удаление не обосновано, даже
при нуле.

**Новые индексы — обычные btree по колонкам.** Хранение
выбрано замером (см. «Хранение момента события и идентификатора
оборота выбрано замером плана запроса»): момент события и ключ
порядка лежат в колонках `occurred_at` и `seq`, поэтому
индексы на них — **обычные btree по колонкам**, а не
индексы-выражения. Индекс-выражение исключено вдобавок и
технически: приведение `text → timestamptz` для момента события
`STABLE`, а PostgreSQL требует `IMMUTABLE` от функций в
индексном выражении. Набор индексов целиком это требование по-прежнему
**не фиксирует** — оно фиксирует **что** должно быть обслужено,
а не чем:

- чтение оборота в порядке возникновения — обслуживается
  `(request_id, seq, id)`, то есть формой, под которой замер
  проводился и которая выиграла 3 сценария из 3;
- фильтр по типу события в окне времени;
- `history_search(session_scope="all")` по пользователю в
  окне времени (существующий индекс построен на колонке
  времени **записи**, то есть после введения момента события
  он обслуживает не тот запрос; обслуживать его должна
  колонка `occurred_at`);
- `history_search` по сессии в окне времени — аналогично, по
  `occurred_at`, а не по `"timestamp"`.

Индексы MUST строиться **после** backfill (см. «Порядок заходов
и блокеры»): индекс, построенный по пустому выражению или до
заполнения колонки, обслуживает нулевое число строк молча.

Уникальный индекс по `id` MUST быть сохранён — это
единственный внешний адрес строки журнала.

**Расхождение DDL и факта.** В репозитории объявлен **один**
индекс
(`sql/logs/create_public_agent_gateway_logs.sql:34-35`), а
живая база имеет восемь. DDL в репозитории не является
источником истины о фактических индексах, и guard-тест
сверки состава MUST опираться на тестовую схему, а не на
файл.

**Контекст, который замеры объясняют.** 380 `seq_scan`
против 969 `idx_scan`, но **14 млн** строк прочитано
последовательными сканами против **546 тыс.** по индексам;
таблица 784 MB, из них heap 23 MB, остальные 761 MB — TOAST и
индексы; суммарный payload 1249 MB до сжатия TOAST, причём
10 % строк (4882) держат 1240 MB.

**Проверка:** guard-тест сверяет объявленный в DDL состав
индексов с фактическим в тестовой схеме и падает на
расхождении; замер `pg_stat_user_indexes` до и после правки
записывается в change; для каждого удаляемого индекса в
change MUST быть записаны окно наблюдения и замер до/после;
регрессионный тест плана фиксирует, что удаляемый индекс
не использовался, а оставшиеся обслуживают свои запросы
через `Index Scan`, а не через сортировку.

#### Scenario: состав индексов оценивается по селективности

- **WHEN** оценивается состав индексов журнала
- **THEN** критерием SHALL быть селективность (строк на
  чтение), а не число чтений
- **AND** ни один из восьми живых индексов `agent_gateway_logs`
  SHALL NOT признаваться удаляемым по одному лишь числу
  чтений
- **AND** регрессионный тест плана SHALL падать, если
  оставшиеся индексы обслуживают свои запросы сортировкой
  вместо `Index Scan`

#### Scenario: удаление индекса обосновано окном наблюдения

- **WHEN** индекс признан кандидатом на удаление
- **THEN** удаление MUST выполняться только после **названного
  окна наблюдения**, а в change MUST быть записаны это окно
  и замер до и после
- **AND** `idx_agent_logs_level` SHALL удаляться только если
  после правки уровней он всё ещё не различает ничего

#### Scenario: индексы под момент события строятся после backfill

- **WHEN** строятся индексы под выбранное хранилище
- **THEN** они MUST быть обычными btree по колонкам
  `occurred_at` и `seq`, а не индексами-выражениями
- **AND** индексы MUST строиться после backfill, а
  уникальный индекс по `id` MUST быть сохранён
- **AND** guard-тест сверки состава MUST опираться на
  тестовую схему, а не на файл DDL

### Requirement: Бюджет тел в журнале

Событие-носитель тел (`llm.exchanged`, ныне `llm_call`)
SHALL NOT дублировать тела, уже записанные в другом месте
того же оборота.

По замеру `llm_call` — в среднем 62 КБ `payload`, максимум
130 КБ, при этом полезная часть (`response`) в среднем
2.7 КБ, а `usage` лежит в `metadata`. Остальные ~59 КБ —
дословная копия аргументов tool-вызовов, уже записанных
отдельно (то есть ещё раз, см. «Один факт — одна строка»).

Правило: тело, которое уже есть в другом событии того же
оборота, MUST выноситься в артефакт со ссылкой. Механизм
артефактов `session://results/...` существует, но порог
`max_inline_result_bytes` работает только для MCP-операций —
события журнала под него не попадают, и это MUST быть
исправлено. Порог инлайна SHALL быть явной настройкой, а не
константой в коде.

Тела, которые больше нигде не лежат (промпт и ответ
модели), MUST остаться в журнале: удаление их сломало бы
поиск текста диалога, ради которого `history_search`
рекомендует именно это событие.

**Проверка:** замер на живых данных: средний и максимальный
`pg_column_size(payload)` для события-носителя тел после
правки SHALL упасть ниже 62 КБ / 130 КБ; тест на порог
инлайна падает, если тело больше порога и не вынесено в
артефакт; тест падает, если из события вырезан промпт или
ответ (поиск текста диалога — обязательная способность).

#### Scenario: дублирующее тело выносится в артефакт

- **WHEN** событие-носитель тел (`llm.exchanged`) повторяет
  тело, уже записанное в другом событии того же оборота
- **THEN** это тело MUST выноситься в артефакт со ссылкой
  `session://results/...`
- **AND** порог инлайна SHALL быть явной настройкой, а не
  константой в коде

#### Scenario: порог инлайна действует и на события журнала

- **WHEN** тело события журнала превышает порог инлайна
  (`max_inline_result_bytes`)
- **THEN** оно MUST выноситься в артефакт, а не оставаться
  в событии
- **AND** тест на порог SHALL падать, если тело больше
  порога и не вынесено в артефакт

#### Scenario: промпт и ответ остаются в журнале

- **WHEN** из события-носителя тел вырезаны промпт или ответ
  модели
- **THEN** тест SHALL падать: тела, которые больше нигде не
  лежат, MUST остаться в журнале
- **AND** иначе сломается поиск текста диалога, ради которого
  `history_search` рекомендует именно это событие

### Requirement: Уровни логирования несут смысл

Уровень `level` MUST быть заполнен осмысленно, и события,
не несущие информации, MUST NOT попадать в журнал.

Измерено: в данных только `INFO` (352) и `ERROR` (6) —
других нет, хотя CHECK в схеме разрешает `DEBUG` и `WARN`
(и то же множество объявлено в
`mcp-platform/libs/enterprise_common/eventing/models.py:49`).
То есть либо четверть уровней не используется, либо
используется неверно. `quality.check` — 48 строк, все с
`ok=true`, и **46 из них без единого флага**: 13 % таблицы
несут ноль информации.

Требования:

- `quality.check` MUST содержать хотя бы один флаг,
  объясняющий, что именно проверялось; проверка без флагов
  MUST NOT писаться в журнал;
- `WARN` MUST использоваться для событий, при которых оборот
  продолжается, но деградировал; если таких событий в
  системе нет, `WARN` MUST быть удалён из CHECK и из
  `LEVELS` вместе с пустой строкой в данных, а не оставлен
  как разрешённое, но неиспользуемое значение;
- `DEBUG` — аналогично: либо используется, либо удаляется.

Решение по каждому уровню принимается по **замеру** в живых
данных, а не по догадке, и фиксируется в change.

**Проверка:** замер `SELECT level, count(*) FROM
agent_gateway_logs GROUP BY level` до и после; тест падает,
если `quality.check` записан без флагов; guard-тест сверяет
множество в CHECK и в `LEVELS` с фактически используемыми и
с осознанно оставленными.

#### Scenario: проверка без флагов не попадает в журнал

- **WHEN** `quality.check` записывается без единого флага
- **THEN** такая проверка MUST NOT писаться в журнал
- **AND** тест SHALL падать на такой записи

#### Scenario: уровень без событий удаляется, а не остаётся

- **WHEN** по замеру в живых данных не найдено ни одного
  события, при котором оборот продолжается, но деградировал
- **THEN** `WARN` MUST быть удалён из CHECK и из `LEVELS`,
  а `DEBUG` — либо использоваться, либо быть удалён
- **AND** уровень SHALL NOT оставаться разрешённым, но
  неиспользуемым значением

#### Scenario: решение по уровню принимается по замеру

- **WHEN** принимается решение по каждому уровню логирования
- **THEN** оно MUST приниматься по замеру в живых данных
  (`SELECT level, count(*) … GROUP BY level` до и после),
  а не по догадке, и фиксироваться в change
- **AND** guard-тест SHALL сверять множество в CHECK и в
  `LEVELS` с фактически используемыми и с осознанно
  оставленными

### Requirement: Потеря события всегда видима

Ни одно потерянное событие MUST NOT уйти молча.

История дефекта, который спека закрывает: отказ по
identity в конвейере исполнения был полностью молчалив —
события могли пропадать без следа, и fallback-файл не
создавался. Открытие потери начиналось с того, чего **не**
случилось. Уже исправлено, но без требования регрессия
вернётся.

Механизмы видимости, которые MUST сохраняться (часть
реализована и здесь закрепляется как контракт):

- отказ по неполной личности (`session_id`/`user_id` пусты)
  — событие уходит в локальный fallback-файл и в счётчик
  `dropped`, а не растворяется в группе с чужой личностью
  (`lib/services/log_transport.py:276-281`);
- недоступность транспорта — то же: fallback-файл **и**
  счётчик, потому что счётчик умирает вместе с процессом и
  виден только тому, кто уже смотрит (там же:301-314);
- нераспознанные счётчики ответа `log_events` — WARNING
  (там же:320-343);
- недоступность `DbLoggingService` — WARNING от
  `try_log_event` (см. Requirement «try_log_event contract»);
- **требование этого change:** при гарантии строки итога
  (см. «Гарантия строки итога на каждый оборот») потеря
  записи `upsert_question_run` MUST быть видима так же, как
  потеря события, — иначе невидимая потеря итога хуже
  потери события, потому что итог читает UI.

**Проверка:** мутационная проверка — тест имитирует отказ по
личности и недоступность транспорта и SHALL проверить
одновременно инкремент `dropped` и запись в fallback-файл;
отдельный тест ловит регрессию, при которой отказ по
identity проходит без следа (ровно тот случай, что был
молчаливым).

#### Scenario: отказ по личности виден

- **WHEN** событие нечем подписать (`session_id`/`user_id` пусты)
- **THEN** `dropped` SHALL инкрементироваться
- **AND** fallback-файл SHALL содержать это событие

### Requirement: Пробные события не пишутся в продовую таблицу

События пробного характера (`smoke.*`, `probe_*`,
`live.db_probe`) MUST NOT попадать в продовую таблицу
журнала. Сейчас они в ней есть.

Такие пробы MUST идти в тестовый профиль
(`infrastructure/test-profile-tables`) либо в
operational-лог процесса.

**Проверка:** замер на живых данных — `SELECT count(*)
FROM agent_gateway_logs WHERE event_type LIKE 'smoke.%' OR
event_type LIKE 'probe_%' OR event_type = 'live.db_probe'`
после вычистки SHALL давать 0; guard-тест падает, если в
коде проб появляется запись в журнал без профиля.

#### Scenario: пробное событие уходит из продовой таблицы

- **WHEN** пишется событие пробного характера (`smoke.*`,
  `probe_*`, `live.db_probe`)
- **THEN** оно MUST NOT попадать в продовую таблицу журнала
- **AND** MUST идти в тестовый профиль
  `infrastructure/test-profile-tables` либо в operational-лог
  процесса

#### Scenario: запись пробы без профиля ловится guard'ом

- **WHEN** в коде проб появляется запись в журнал без профиля
- **THEN** guard-тест SHALL падать
- **AND** замер после вычистки (`smoke.%`, `probe_%`,
  `live.db_probe`) SHALL давать 0

### Requirement: Вычистка шума не может отключиться молча

Код чистки журнала MUST оставаться работоспособным после
переименования событий, и это MUST проверяться, а не
предполагаться.

Пользователь разрешил удалить исторические логи. Известная
транша — 29 754 строки устаревшего потокового `outbound_final`
(61 % таблицы), написанные старым кодом, который использовал
это имя как потоковый **чанк**, а не как итог оборота. Это
следствие двусмысленности имени, а не сбой.

**Но этой транши недостаточно, и критерий очистки — не тип
события.** Замер показывает, что ключа порядка нет ни в одной
из 48 979 строк: без `seq` — 46 553, `metadata IS NULL` —
2 426 (46 553 + 2 426 = 48 979, то есть вся таблица).
Строки, лишённые `outbound_final`, этого не лечат: после
удаления 29 754 строк остаётся **19 225** строк без ключа, и
2 426 из них восстановить нечем — брать нечего. Пока такие
строки остаются, чтение оборота продолжает получать ложный
порядок (см. Requirement «Отсутствие ключа порядка определено
и не молчит»), и вычистка шума задачу не закрывает.

Поэтому очистка MUST определяться признаком **«нет `seq`»**, а
не именем события, и MUST быть выполнена до того, как порядок
оборота станет востребованным. Объём определяется замером перед
операцией, и операция SHALL отчитаться числом удалённых
строк.

Вычистка MUST быть идемпотентной и проверяемой: после её
выполнения доля пустых исходящих строк в таблице SHALL
стремиться к нулю, а операция SHALL отчитаться числом
удалённых строк.

**Проверка:** после вычистки `SELECT event_type, count(*)
FROM agent_gateway_logs WHERE event_type IN ('outbound_final',
'outbound_intermediate')` SHALL давать либо 0, либо только
непустые доставки; операция возвращает счётчик удалённых
строк, и тест сверяет его с числом подходящих строк до
вызова; после перехода на `agent.delivered` таблица
`outbound_final` MUST остаться пустой, иначе имя ещё
где-то пишется (см. «Тихие места словаря синхронизируются вместе с ним»).

#### Scenario: вычистка не остаётся вечным no-op

- **WHEN** в `EMPTY_OUTBOUND_EVENT_TYPES` есть имя, которого
  агент больше не пишет
- **THEN** тест SHALL падать: имя в списке не совпадает с
  фактическим словарём агента
- **AND** без этого переименование отключает вычистку
  молча, и таблица растёт неограниченно

### Requirement: Порядок заходов и блокеры

Изменения MUST выполняться в порядке ниже, потому что
каждый следующий заход нечем проверить без предыдущего.
Нарушение порядка ломает не стиль, а проверяемость.

1. **Словарь и новые объявления.** Добавить
   `agent.delivered`, `agent.received`, `agent.responded`,
   `agent.compacted`, `tool.suppressed`, `llm.exchanged`,
   `agent.degraded` в
   `mcp-platform/libs/enterprise_common/eventing/types.py`.
   Без этого нечего проверять: ни strict-режим, ни тест
   словаря, ни таблица покрытия не имеют объекта.
2. **Идентификатор оборота и признак источника в `metadata`.**
   `metadata.turn_id` и `metadata.source` проходят через
   `event_to_wire` и `metadata`. Без них нечем сгруппировать
   строки оборота для замеров «одна пара на вызов» и
   «разные `request_id` на группу». Блокер: сегодня
   `request_id` в теле батча **нет физически**
   (`lib/services/log_transport.py:163-181`), и его появление
   там — первое, что должно быть сделано. Хранилище этих
   двух полей **решено** (2026-10-02): оба остаются в
   `metadata` — платформа не знает идентификатор оборота
   агента, а колонка, которую заполняет не владелец значения,
   заводит второй источник истины (см. «turn_id и request_id
   разделены»).
3. **Обязательный `tool_call_id` и дедупликация вызовов.**
   Замер «4 строки на вызов» и «102 платформенных `tool.*`
   без `tool_call_id`» снимается только после пункта 2.
4. **Покрытие этапов вне хука:** `agent.received` (точка
   приёма каналом), `agent.delivered` (точка доставки),
   `agent.responded` (текст ответа), `llm.failed`.
5. **Синхронизация тихих мест** (enum `history_search`,
   `EMPTY_OUTBOUND_EVENT_TYPES`) — строго сразу за
   переименованием, до него это окно, в котором обе
   конструкции молча перестают работать.
6. **Гарантия строки итога и подтверждение ответа
   `upsert_question_run`.** Блокер: правка операции
   проверяется на `agent_question_runs_test` (тестовый
   профиль) — замеры сделаны на нём, и без него проверить,
   что отказ больше не выдаётся за успех, нечем.
7. **Колонки `agent_question_runs` и CHECK формы имени.**
   Черновик DDL из «DDL соответствует фактической СУБД».
   Применять только к тестовому профилю.
8. **DDL-миграция `agent_gateway_logs`: колонки `seq` и
   `occurred_at`.** Теперь она обязательна (решение замера
   отменяет прежний запрет). Порядок захода **внутри** миграции
   обязателен: добавить nullable-колонки → backfill → очистка
   строк без ключа → `SET NOT NULL`. Применять только к
   тестовому профилю.
9. **Индексы под выбранное хранилище** — строго после
   backfill и строго после блокера A. Хранение решено: это
   обычные btree по колонкам, а не индексы-выражения (см.
   «Индексы журнала по замеренной селективности»). Строить
   их до backfill — значит построить индекс по колонке, в
   которой ключа ещё нет.
10. **Бюджет тел, уровни, запрет проб, вычистка истории.**
   Порядок между ними свободный; каждый пункт проверяется
   своим замером.
11. **Включение `strict`** — строго последним, отдельным
    заходом, после того как пункты 1-5 привели имена к
    объявленным с обеих сторон.

**Добавленные после приёмки блокеры (приоритет выше пунктов
выше).**

- **A. Замер плана запроса — ВЫПОЛНЕН 2026-10-02, блокер закрыт.**
  Замер проведён на 48 979 боевых строках, скопированных
  **только на чтение** в изолированную схему; решение принято
  в пользу колонок, 3 сценария из 3 (величины и два
  блокирующих факта — в требовании «Хранение момента события и
  идентификатора оборота выбрано замером плана запроса» и в
  change `2026-10-02-journal-observability`). Пункты 7-9 выше
  не начинались до замера, и начинаются только после backfill.
- **B. Очистка строк без `seq`** MUST быть выполнена **до**
  того, как порядок оборота будет объявлен рабочим: иначе
  первое же чтение оборота вернёт ложный порядок, а дефект
  будет приписан новому коду, который его не создавал.
  Критерий очистки — отсутствие ключа **в колонке**
  (`seq IS NULL`), а не имя события (см. «Вычистка шума не
  может отключиться молча»): строк без ключа 48 979, из них
  2 426 с `metadata IS NULL` восстановить нечем. По новому
  решению очистка идёт **между** backfill и `SET NOT NULL`:
  до backfill она снесла бы ещё не перенесённые ключи, после
  `SET NOT NULL` — невозможна.
- **C. Поведение при отсутствии ключа** (Requirement «Отсутствие
  ключа порядка определено и не молчит») MUST быть реализовано
  **вместе** с B, а не после: очистка убирает строки, а
  fallback-правило делает их отсутствие заметным, если
  подобные строки появятся снова. Новая колонка этого НЕ
  отменяет: читатель смотрит на `seq IS NULL` и обязан
  вернуть `unattributed`, а не молча поставить строки в конец.
- **D. DDL-миграция `agent_gateway_logs`** (колонки `seq` и
  `occurred_at`) обязательна и применяется только к тестовому
  профилю; к боевой базе — отдельный заход с бэкапом. Порядок
  «добавить → backfill → очистка → `SET NOT NULL`»
  обязателен тестом, падающим на обратном порядке.

Общие блокеры:

- **DDL применяется только к тестовому профилю.**
  `sql/migrations/*` и `schema_migrations.sql` в рамках этих
  требований НЕ применяются: правка схемы боевой БД —
  отдельный заход с бэкапом.
- **Исторические логи удаляются отдельной явной операцией**
  по критерию «нет `seq`». Спецификация не авторизует удаление
  само по себе; объём определяется замером перед операцией, а
  операция отчитывается числом удалённых строк.
- **Ни одно из требований не вводит второго writer'а.** Всё,
  что пишется, идёт через `DbLoggingService` и операцию
  `log_events`; новые требования добавляют поля и индексы,
  а не пути записи.
- **Регистрация компонента не требуется.** По правилу
  «In-flight changes с компонентной семантикой НЕ добавляют
  записи в реестр, пока change не заархивирован»
  (`openspec/specs/COMPONENTS.md`) правка существующей
  спеки `logging-db` не создаёт нового компонента.
  `openspec/specs/OWNERSHIP.md` уже относит `logging-db` к
  классу `shared`, и адресация «кто это чинит» не меняется.

**Проверка:** каждый пункт закреплён за существующим или
требуемым тестом; до появления теста на пункт 2 заходы 2-3 считаются незакрытыми.

#### Scenario: индексы не строятся до backfill

- **WHEN** строится индекс по колонке, в которой ключа ещё
  нет (до backfill)
- **THEN** он SHALL обслуживать нулевое число строк молча,
  а сами индексы MUST строиться строго после backfill
- **AND** пункт с индексами SHALL начинаться только после
  backfill и после закрытия блокера A

#### Scenario: очистка идёт между backfill и SET NOT NULL

- **WHEN** выполняется очистка строк без `seq`
- **THEN** критерием MUST быть `seq IS NULL`, а не имя события
- **AND** очистка MUST идти между backfill и `SET NOT NULL`:
  до backfill она снесла бы ещё не перенесённые ключи,
  после `SET NOT NULL` — невозможна

#### Scenario: strict включается последним отдельным заходом

- **WHEN** включается режим `strict`
- **THEN** он SHALL включаться строго последним, отдельным
  заходом, после того как пункты 1-5 привели имена к
  объявленным с обеих сторон

### Requirement: Принятое `log_event` событие доходит до `agent_gateway_logs`

Событие, прошедшее через `DbLoggingService.log_event` (`lib/services/db_logging_service.py`,
единственная точка входа всех `log_*` и `try_log_event`), MUST быть учтено в
статистике службы как `written` либо как `fallback_written` за ограниченный срок:
`logging.db.flush_interval_sec` (дефолт 5.0) плюс один сброс батча плюс один
круг по MCP. Событие, о котором к этому сроку нет ни одной из двух отметок,
MUST считаться непотерянным только если это видно в счётчиках — иначе оно
потеряно.

Это требование о **полноте**, а не о живости. Сейчас агентская половина журнала
теряет всё: за живой прогон 2026-10-04 в `agent_gateway_logs` лежало 509
`tool.started`, 508 `tool.completed`, 508 `quality.check` (их пишет платформа за
своё исполнение) и **ноль** `agent.degraded` при 169 таких событиях в консоли.
Платформа при этом исправна: прямая отправка одного `agent.degraded` операцией
`log_events` вернула `{"status":"ok","accepted":1,"dropped":0}` и строка появилась.
Значит теряет агентская половина, и инвариант журнала это не замечал: событие
исчезало между `log_event` и сбросом, не оставляя ни строки, ни счётчика.

Точный виновник на момент передачи **не установлен**, и требование его не
называет: оно задаёт конец поиска, а не подсказку. Валидация спекой запрещена
формулировка «исправить строку N»: причина может оказаться в окне между
`attach_transport` и первым сбросом, в потоке `db-logging`, в разборе
идентичности вызова или в ответе транспорта.

Уже исключено и повторно не проверяется: создание каталога локального следа
(`log_transport.py:445-447`, страж
`tests/test_log_transport.py::TestLocalFallback::test_creates_the_directory_it_writes_into`),
исправность платформенной записи, и поведение `queue_ops.py:152`.

#### Scenario: событие агента доезжает до таблицы

- **WHEN** событие прошло через `DbLoggingService.log_event` при подключённом
  транспорте
- **THEN** за время `flush_interval_sec` + один сброс + один круг по MCP оно
  MUST появиться в `agent_gateway_logs`
- **AND** `get_stats()` MUST показать его в `written` либо в
  `fallback_written`
  (проверяется `TestJournalDeliveryContract::test_accepted_event_reaches_the_table`)

#### Scenario: агентские события не теряются пачкой

- **WHEN** за прогон эмитятся события, которые пишет только агент (например
  `agent.degraded`), и транспорт жив
- **THEN** число строк этих типов в `agent_gateway_logs` MUST совпадать с
  числом событий, принятых `log_event`, с точностью до событий, явно
  посчитанных в `dropped`/`failed` с названной причиной
  (проверяется `TestJournalDeliveryContract::test_agent_side_events_are_not_silently_lost`)

#### Scenario: событие без отметки считается потерянным

- **WHEN** по истечении отведённого срока событие не учтено ни в `written`, ни
  в `fallback_written`, ни в `dropped`/`failed`
- **THEN** такая потеря MUST быть видна в `get_stats()` и в консоли, а не
  выглядеть как успешная запись
  (проверяется `TestJournalDeliveryContract::test_unaccounted_event_is_reported`)

### Requirement: Невозможность записи названа, а не посчитана

Любая невозможность доставить событие MUST быть названа: причина в
`get_stats()["last_error"]` и строка в консоли, а не только рост счётчика.
Счётчик без причины отвечает на вопрос «сколько», а инцидент требует ответа
«почему», и по одному числу он не восстанавливается.

Отдельно: **строка о транспорте журнала — измеренный факт, а не объявление
намерения.** Сегодня `lib/core/application_context.py:670` печатает «журнал
агента пишется через enterprise-mcp (log_events)» сразу после `attach_transport`
и безусловно. В прогоне 2026-10-04 эта строка появилась в 13:22:00, и после неё
не доставилось ни одного агентского события. Строка MUST либо подтверждаться
измеренной доставкой, либо читаться как «транспорт выбран, доставка не
проверена» — третий вариант, где строка утверждает доставку и не доставляет,
запрещён.

`is_running()` (`db_logging_service.py:775-776`) MUST NOT сообщать «работает»
при неработающем потоке: это тот же класс дефекта, только в обратную сторону.

#### Scenario: причина названа, а не только счётчик

- **WHEN** сброс батча не удался
- **THEN** `get_stats()["last_error"]` MUST содержать причину, и причина MUST
  появиться в консоли
  (проверяется `TestJournalDeliveryContract::test_failure_carries_a_reason`)

#### Scenario: баннер не обещает непроверенного

- **WHEN** выводится строка о транспорте журнала
- **THEN** она MUST быть подтверждена измеренной доставкой либо MUST явно
  называть доставку непроверенной
  (проверяется `TestJournalDeliveryContract::test_transport_line_is_measured_or_hedged`)

#### Scenario: мёртвый поток не выдаёт себя за живой

- **WHEN** поток `db-logging` не запущен
- **THEN** `is_running()` MUST вернуть `False`
  (проверяется `TestJournalDeliveryContract::test_is_running_reflects_the_thread`)

### Requirement: Отложенный батч доставляется или объявляется брошенным

Батч, сброшенный до выбора транспорта, MUST либо быть доставлен после
подключения транспорта, либо быть объявлен брошенным — явно и поимённо.

Сегодня `_defer_batch()` (`db_logging_service.py:1698-1720`) увеличивает
`dropped`, пишет локальный след, и всё. Докстринг при этом обещает, что событие
«ушло в локальный след и **дожидается решения**» (`:1701-1705`), но кода,
который этот файл читает обратно, нет: `_write_fallback()` (`:1722-1740`) только
пишет, а `mcp_writer.on_fallback` (`:737`) — обратное направление. Событие
брошено, и единственный его след — счётчик в памяти процесса, который умирает
вместе с агентом. Обещание в докстринге и поведение кода MUST совпадать.

Требование НЕ вводит повторной доставки локального следа как второго пути
записи. Доставка — это удержание батча до появления транспорта; если решение
принято в пользу «брошено», то брошеность обязана быть видна, а не
предполагаться.

#### Scenario: батч, ждавший транспорт, не теряется молча

- **WHEN** батч сброшен до выбора транспорта, а транспорт подключён позже
- **THEN** батч MUST быть доставлен либо число событий в нём MUST быть
  объявлено потерянным с причиной
  (проверяется `TestJournalDeliveryContract::test_deferred_batch_is_delivered_or_declared`)

#### Scenario: обещание докстринга соответствует коду

- **WHEN** читается описание `_defer_batch`
- **THEN** оно MUST описывать фактическое поведение: доставку либо явный отказ
  от доставки, а не «дожидается решения» при отсутствии читающего кода
  (проверяется `TestJournalDeliveryContract::test_defer_docstring_matches_behavior`)

### Requirement: Локальный след лежит по абсолютному пути

Путь файла локального следа MUST быть абсолютным и вычисленным один раз. Относительный
путь делает след зависимым от текущего каталога процесса, и тогда, когда след
нужен, он оказывается в другом месте — а «следа нет» и «след в другом каталоге»
неразличимы.

Сегодня путь строится из `data_dir` с откатом на `"."`
(`lib/core/application_context.py:600-613`), то есть может оказаться
относительным. Наблюдавшаяся ошибка `No such file or directory:
workspace\data_store\logs\gateway-events-fallback.jsonl` с этим согласуется.

Это требование **дополняет** «Локальный след событий появляется всегда, когда
платформа недоступна» (`openspec/specs/logging-db/spec.md:434-451`), а не
заменяет его: каталог по-прежнему создаётся лениво, при первой записи, и пустой
каталог без единого события остаётся шумом.

#### Scenario: путь следа не зависит от текущего каталога

- **WHEN** собирается `LocalFallbackSink` при относительном `data_dir`
- **THEN** путь MUST быть абсолютным
  (проверяется `TestJournalDeliveryContract::test_fallback_path_is_absolute`)

#### Scenario: след находится там, где его ищут

- **WHEN** служба сообщает путь локального следа
- **THEN** сообщаемый путь MUST совпадать с тем, куда пишет след, при любом
  текущем каталоге
  (проверяется `TestJournalDeliveryContract::test_reported_path_is_the_written_path`)

### Requirement: Отказ инструмента попадает в журнал как отказ

Отказ MUST попадать в журнал как отказ — и в обоих местах, где он может
возникнуть.

**(а) Доменный отказ внутри успешного конверта.** Отказ доменного кода внутри
успешного MCP-вызова MUST журналироваться как отказ, а не как успех. Сегодня
это верно: `pipeline.py:186-187` уходит в `_refuse()` (`:455-478`), который
зовёт `_logger.failed(...)` (`:461`), а тот пишет
`"status": "timeout" if timed_out else "error"` с `error_code` и
`error_message` (`mcp-platform/libs/enterprise_common/execution/logger.py:246-250`).
Конверт `[code] message` (`mcp-platform/libs/enterprise_common/loader.py:242-246`,
`isError` — `:271`) и его разбор агентом
(`lib/services/enterprise_mcp_client.py:209-214`, где отсутствие префикса даёт
`operation_failed`) остаются как есть. Требование существует ради стража:
сегодня это верно по счастливому совпадению устройства конвейера, а не по
закону.

**(б) Отказ выше конвейера.** Отказ, возникший на проводе **до** входа в
конвейер, MUST быть виден в журнале как отказ. Валидация SDK происходит до
диспетчеризации (`mcp/server/lowlevel/server.py:527-532`), конвейер не начат,
а значит не было ни `tool.started` (`pipeline.py:181`), ни `tool.failed`.
Наблюдение 2026-10-04 (контролируемый опыт, два вызова подряд с разными
метками в `session_id`): вызов с `priority_contents: null` отвергнут и не
оставил в журнале **ни одной строки**; вызов с `priority_contents: []` прошёл
и оставил три — `tool.started`, `quality.check`, `tool.completed status=ok`.
Отказ не порождает даже `tool.started`: конвейер не начат.

Поэтому 164 строки `status=ok` за прогон — это успешные опросы пустой
очереди, а не те же отказавшие вызовы, и «в журнале стоит успех там, где был
отказ» — неверное прочтение. Настоящая форма дефекта хуже: отказ не
оставляет строки ВООБЩЕ, а её отсутствие неотличимо от того, что вызова не
было. Журнал не врёт прямо — он молчит там, где обязан говорить.

Сторона, которая этот отказ видит, — агент. `ToolAuditHook` уже накапливает
`status` и `error` по каждому вызову (`lib/hooks/tool_audit_hook.py:81-83`,
`:109-112`) и всегда присутствует в `ctx.hooks`; он MUST записать отказ в
`DbLoggingService`. Имя — каноническое `tool.failed`, а различающий писателя
признак — `metadata.source`: `enterprise_mcp` у платформы
(`logger.py:104`), `nanobot` у агента (`db_logging_service.py:795`).

Отдельное имя события (`tool.rejected`) MUST NOT заводиться: при
`data.log_unknown_event_type_policy = strict`
(`mcp-platform/platform.json:97`) непризнанное имя не пишется вовсе, то есть
цена нового имени — новое объявление в словаре
(`mcp-platform/libs/enterprise_common/eventing/types.py`).

Ловить ошибку валидации SDK внутри платформы MUST NOT: это потребовало бы
обращения к внутренностям `Server`/`request_handlers`, то есть копии протокольной
обвязки, которую переписывает каждое обновление SDK. Корень — инвариант
`data/operation-schema`: опубликованная схема принимает всё, что принимает
обработчик, и тогда отказ на проводе невозможен для законных аргументов.

Строка 8 таблицы покрытия этапов оборота
(`openspec/specs/logging-db/spec.md:1446`) утверждает, что `tool.failed` пишет
платформа, а агент не пишет. После этого требования утверждение неверно, и
таблица редактируется: отказ инструмента пишет та сторона, которая его видит,
а видящих сторон две.

#### Scenario: доменный отказ журналируется как отказ

- **WHEN** обработчик операции выбросил доменное исключение
- **THEN** в журнале MUST появиться `tool.failed` с `status` `error`/`timeout`,
  `error_code` и `error_message`, а MUST NOT появиться `tool.completed` с
  `status=ok`
  (проверяется `TestToolAuditJournal::test_domain_refusal_is_logged_as_failure`)

#### Scenario: мутация конвейера ломает страж

- **WHEN** `_refuse()` перестаёт звать `logger.failed`
- **THEN** страж MUST упасть
  (проверяется `TestToolAuditJournal::test_guard_fails_when_refusal_is_not_logged`)

#### Scenario: отказ, который платформа не видела, виден агенту

- **WHEN** вызов инструмента вернул отказ
- **THEN** в журнале MUST появиться строка `tool.failed` с
  `metadata.source = nanobot`
  (проверяется `TestToolAuditJournal::test_agent_writes_failure_it_can_see`)

#### Scenario: писатель различим

- **WHEN** отказ записали обе стороны
- **THEN** строки MUST различаться `metadata.source`, и различение MUST
  работать без нового имени события
  (проверяется `TestToolAuditJournal::test_writer_is_distinguishable_without_a_new_name`)

#### Scenario: успех не записан как отказ

- **WHEN** вызов инструмента завершился успешно
- **THEN** `tool.failed` со стороны агента MUST NOT появиться
  (проверяется `TestToolAuditJournal::test_success_is_not_logged_as_failure`)

### Requirement: Payload несёт ограниченную выдержку, а не отпечаток

`payload` событий вызова инструмента MUST содержать **ограниченную по длине
выдержку** аргументов и результата, а не только `*_hash`/`*_size`. Потолки
объявляются в `mcp-platform/platform.json → execution` и применяются к
**сериализованной выдержке** на событие:

- аргументы — `log_argument_excerpt_bytes`, дефолт **512**;
- результат — `log_result_excerpt_bytes`, дефолт **1024**.

Числа выбраны по двум ограничениям, и оба существующие. 1024 меньше
`execution.preview_bytes` (2048, `platform.json:41`) — превью, которое платформа
и так отдаёт в ответе: **журнал не должен держать больше тела, чем вызывающий и
так получил**. Потолок на батч следует из `data.log_batch_size` (1024,
`platform.json:94`): худший случай — порядка 1.5 МБ на сброс.

Потолок применяется к выдержке целиком, а не к одному полю. Иначе число 512
окажется фикцией: существующий механизм белого списка
(`collect_argument_fields`, `logger.py:84-94`) умеет отдать 20 значений по 200
символов (`logger.py:43-46`, `:72-81`), то есть до 4 КБ на событие.

Механизм MUST остаться тем же — белый список полей
(`execution.log_argument_fields`, `platform.json:47`) плюс усечение, — а не
превратиться в запись всех аргументов. Правило «белый список, а не чёрный,
потому что новый параметр операции не должен молча начинать писаться в журнал»
(`logger.py:11-13`) сохраняется.

Усечение MUST быть **видно** в payload маркером. Неотличимо усечённое тело от
целого — это ложь того же класса, что и отсутствие строки об отказе.

Маскирование обязательно и объявляется: `execution.log_redact_keys` — имена
полей, значения которых заменяются маркером; поверх — распознавание по форме
(DSN, `Bearer …`, `sk-…`, PEM-блок, JWT-тройка). Маскирование действует на
payload вызовов инструментов платформы.

Остаётся непокрытым, и это граница, а не недосмотр:

- секрет под безобидным именем и внутри свободного текста — маскирование по
  форме не ловит его, а семантическая редактура нереализуема и даёт ложную
  уверенность; содержимое разговоров и так лежит в
  `agent_conversation_messages` и в зеркале сессий;
- тела длиннее выдержки — это не дыра, контракт артефакта уже есть
  (`mcp-platform/docs/MCP-CONTRACTS.md:282-303`,
  `pipeline.py:347-391`), а `read_result` — штатный путь;
- собственные payload'ы агента (`agent.responded` с текстом ответа,
  `openspec/specs/logging-db/spec.md:1535`) не маскируются: маскирование сломало
  бы документированную возможность `history_search` искать собственные прошлые
  ответы;
- аргументы, заданные моделью (текст запроса, содержимое файла), — по той же
  причине: это данные пользователя, а не секрет.

Метаданные вызова остаются полностью видимыми: `arguments_size`,
`result_size`, `duration_ms`, `capability`, `tool_name`. Это требование делает
видимым тело на ограниченном префиксе, а не делает приватными метаданные.

`log_unknown_event_type_policy` (`platform.json:97`) ограничивает **имена**
событий, а не ключи `payload`, поэтому новые поля с ним не конфликтуют. Writer
MUST по-прежнему принимать произвольные ключи `payload`, и новые ключи MUST NOT
превращаться в белый список `payload`.

#### Scenario: выдержка укладывается в потолок

- **WHEN** аргументы или результат длиннее потолка
- **THEN** выдержка в `payload` MUST быть не длиннее
  `log_argument_excerpt_bytes` / `log_result_excerpt_bytes`, и MUST нести маркер
  усечения
  (проверяется `TestPayloadExcerpt::test_excerpt_respects_the_declared_cap`)

#### Scenario: потолок считается на выдержку, а не на поле

- **WHEN** аргументы содержат больше значений, чем помещается в потолок
- **THEN** суммарный объём выдержки MUST остаться в потолке, а не превышаться
  числом полей
  (проверяется `TestPayloadExcerpt::test_cap_applies_to_the_whole_excerpt`)

#### Scenario: секрет не попадает в журнал

- **WHEN** в аргументах или результате есть значение с маскируемым именем либо
  формой (DSN, `Bearer`, PEM)
- **THEN** в `payload` MUST стоять маркер, а само значение MUST отсутствовать
  (проверяется `TestPayloadExcerpt::test_secrets_are_masked`)

#### Scenario: замаскированное видно

- **WHEN** что-то было замаскировано
- **THEN** число замаскированных значений MUST быть видно, иначе усечение и
  маскирование выглядят как полное тело
  (проверяется `TestPayloadExcerpt::test_masking_is_counted`)

#### Scenario: белый список остаётся белым списком

- **WHEN** у операции появляется новый параметр
- **THEN** он MUST NOT попасть в журнал сам по себе, без объявления в
  `execution.log_argument_fields`
  (проверяется `TestPayloadExcerpt::test_new_argument_is_not_logged_by_default`)

#### Scenario: writer принимает произвольные ключи payload

- **WHEN** событие приходит с ключом `payload`, которого нет в словаре
  платформы
- **THEN** запись MUST состояться — новые поля не превращаются в схему
  (проверяется `TestPayloadExcerpt::test_payload_keys_are_not_validated_against_a_dictionary`)

#### Scenario: агентские payload'ы не маскируются

- **WHEN** пишется `agent.responded` с текстом ответа
- **THEN** текст MUST дойти в `payload.content` без маскирования — иначе
  `history_search` перестаёт искать прошлые ответы
  (проверяется `TestPayloadExcerpt::test_agent_payloads_are_not_masked`)

### Requirement: Личность оборота имеет одного владельца

Личность оборота (`session_id` + `user_id` + `request_id`) MUST храниться в
одном носителе, а читаться — из одного места. Владельцем MUST быть
`TurnIdentityStore` (`lib/services/turn_identity.py`): писателем остаётся
`DbLoggingService.register_request` (он один знает `sender_id` в момент
входа), но и индекс вопроса `session_key → request_id`, и снимок личности
ВХОДА MUST быть **частями одной записи** под одним замком.

Две части обязаны иметь разные сроки жизни, и это MUST быть выражено в
записи, а не в двух словарях:

- снимок ВХОДА (`user_id`, `request_id`, `at_seq`) MUST переживать снятие
  привязки вопроса — финальный ответ оборота приходит после конца оборота, и
  подписать его больше нечем;
- привязка вопроса MUST сниматься отдельным действием владельца, после
  которого `request_id` сессии больше не выдаётся.

Чтение личности оборота из фреймворка (`RequestContext`) MUST иметь одну
реализацию на проект. Все места, которым личность нужна для подписи вызова
(`McpIdentityHook`, `EnterpriseMcpClient`, `RuntimeEventsSubscriber`) и
места, которым нужен `sender_id` для подписи своего события, MUST получать
её из этой реализации, а не читать `RequestContext` сами.

Запись личности MUST быть неизменяемой: подписать чужое событие чужим
отправителем MUST NOT быть возможно по ошибке вызывающего.

#### Scenario: у журнала нет второго носителя личности

- **WHEN** `DbLoggingService` регистрирует вход (`register_request`)
- **THEN** индекс вопроса и снимок входа MUST записываться одной операцией
  в `TurnIdentityStore`
- **AND** `DbLoggingService` MUST NOT содержать собственного словаря или
  структуры с личностью оборота
- **AND** пара «индекс + снимок» MUST NOT требовать сверок на стороне
  читателей для своей атомарности

#### Scenario: чтение личности из фреймворка одно на проект

- **WHEN** личность оборота нужна более чем одному компоненту
- **THEN** все они MUST получать её одной и той же функцией чтения
- **AND** ни один из них MUST NOT читать `RequestContext` напрямую ради
  `session_key` или `sender_id`
- **AND** расхождение имён ключей личности вызова MUST NOT быть возможно:
  набор ключей MUST объявляться рядом со сборкой и кормить её

#### Scenario: снятие привязки не стирает снимок

- **WHEN** привязка вопроса снята в конце оборота
- **THEN** `request_id` сессии MUST перестать выдаваться
- **AND** снимок входа MUST остаться доступным для подписи финального
  ответа, который приходит после этого момента

#### Scenario: личность нельзя подменить чужой

- **WHEN** читатель получил запись личности из хранилища
- **THEN** изменение полей записи MUST отклоняться
- **AND** значение у владельца MUST остаться прежним
