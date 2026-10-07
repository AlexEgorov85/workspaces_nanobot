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

`shared` — единственный writer остался в агенте, а запись идёт транспортом в операцию `data.log_events` платформы
Реализация: `lib/services/log_transport.py` + `mcp-platform/servers/enterprise/capabilities/data/tools/log_events.py`

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
писателя (агент напрямую и платформенная операция `data.log_events`)
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
  `tools.exec("python workspace/skills/audit_analyzer/SKILL.md ...")`
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
доступно через `data.history_search`).

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
- **AND** `data.history_search(event_type="context_compacted",
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
  в момент X» (для `data.history_search`, observability);
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

### Requirement: Писатель журнала один, и агент им не является

`DbLoggingService` MUST NOT строить SQL, открывать соединение с базой журнала
и импортировать драйвер или пул записи. Отсутствие писателя MUST быть
отражено как потеря с названной причиной (`failed` и `loss_reasons`), а не
компенсировано записью в обход.

#### Scenario: Батч уходит писателю целиком

- **WHEN** событие поставлено в очередь при заданном `mcp_writer`
- **THEN** батч уходит одним вызовом `log_events`
- **AND** `written` и `written_by_type` увеличиваются на число принятых
  событий

#### Scenario: Писателя нет — событие теряется названно

- **WHEN** `_flush_batch` вызван при `mcp_writer is None` и
  `transport_pending` снят
- **THEN** ни один SQL не выполняется
- **AND** `failed` увеличивается на размер батча
- **AND** в `loss_reasons` появляется запись, называющая причину
  (`писатель журнала не задан`), а не `БД недоступна`

#### Scenario: Транспорт ещё выбирается

- **WHEN** `_flush_batch` вызван при `transport_pending` и отсутствующем
  писателе
- **THEN** батч откладывается (`deferred`), а не считается потерей
- **AND** присоединение писателя отдаёт отложенное

#### Scenario: Контекст вопроса уходит тем же писателем

- **WHEN** `_handle_question_run` вызван при заданном `mcp_writer`
- **THEN** запись уходит операцией `upsert_question_run`
- **AND** при отсутствии писателя `question_runs` не растёт, а
  `question_runs_failed` и потери увеличиваются

#### Scenario: Случайный остаток SQL не считается покрытым тестами

- **WHEN** в `db_logging_service.py` появляется строковый SQL- литерал либо
  импорт `psycopg2` или `utils.db`
- **THEN** падает страж, разбирающий дерево модуля, а не ищущий подстроку:
  подстрока искала бы не код, а упоминание в `docstring`

### Requirement: Очистка журнала удаляет строки платформа, а не агент

`purge_empty_outbound()` и `purge_old()` MUST вызывать существующую операцию
платформы `purge_logs` и MUST NOT выполнять `DELETE` из агента. Выбор режима
(`retention_days`, `remove_empty_outbound`) остаётся за агентом; разбор режима
и выбор строк — за платформой.

#### Scenario: Пустые исходящие: retention выключен, мусор убран

- **WHEN** вызван `purge_empty_outbound()`
- **THEN** в платформу уходит `retention_days=0` вместе с
  `remove_empty_outbound=True`
- **AND** возвращается счётчик `empty_outbound` из ответа операции
- **AND** отрицательный `retention_days` не используется: платформа
  отвергает его как `InvalidRequestError`

#### Scenario: Retention: возраст и пустой мусор разделены

- **WHEN** вызван `purge_old(days)` при `days >= 1`
- **THEN** в платформу уходит `retention_days=days` вместе с
  `remove_empty_outbound=False`
- **AND** возвращается пара `(события, question_runs)` из ответа операции

#### Scenario: Выключенный retention не ходит в платформу

- **WHEN** вызван `purge_old(0)` или настроен `retention_days=0`
- **THEN** вызов `purge_logs` не выполняется вовсе
- **AND** возвращается `(0, 0)`

#### Scenario: Отказ чистки виден и назван

- **WHEN** `purge_logs` бросает исключение или писателя нет
- **THEN** `purge_*` возвращает ноль, а `last_error` называет причину
- **AND** строки не удаляются ничем, кроме платформы

### Requirement: Сверка множеств колонок не превращается в сверку двух писателей

Страж колонок журнала MUST проверять, что платформенный писатель пишет
канонический набор полей, и что агент не пишет в журнал ничего. Сравнение
множеств колонок двух писателей MUST NOT возвращаться: второго писателя нет.

#### Scenario: Платформа пишет весь конверт

- **WHEN** страж разбирает `data/service/main.py`
- **THEN** колонки `INSERT` в `agent_gateway_logs` равны `JOURNAL_FIELDS`
  платформы без `timestamp`

#### Scenario: Агент не пишет в журнал

- **WHEN** страж разбирает `db_logging_service.py`
- **THEN** групп колонок `INSERT` с `event_type` не найдено вовсе

## Responsibility

`DbLoggingService` (`lib/services/db_logging_service.py:608`) отвечает за
персистенцию structured agent events и **только** за это. Он единственный writer
`agent_gateway_logs` (`Purpose`) и единственный механизм upsert в
`agent_question_runs`.

Не отвечает за: формирование самих событий (это делает вызывающий), фильтрацию по
уровню сверх заданной конфигурации, решение о политике хранения и очистке — это
владелец данных, а не журнализатор.

## Boundary

- **owns:** сериализация `LogEvent` (`:497`), пакетная отправка, переупорядочивание
  внутри хода, привязка события к запросу и сессии, счётчики потерь.
- **does not own:** содержимое события, транспорт до платформы (`lib/services/log_transport.py`),
  саму запись на стороне платформы (`mcp-platform/servers/enterprise/capabilities/data/tools/log_events.py`).
- **may depend on:** пул БД агента, транспорт MCP, файловый fallback.
- **must not depend on:** прямых обращений к таблице в обход транспорта — это
  прямо запрещено в `## Requirements`.

## Public Contract

Класс `DbLoggingService` (`:608`):

| Метод | Строка | Назначение |
|---|---|---|
| `start` / `stop` / `is_running` | `:869`, `:882`, `:931` | lifecycle фонового writer |
| `log_event` | `:1004` | приём одного события |
| `register_request` / `get_request_id` / `clear_request` / `finish_request` | `:1075`–`:1175` | личность запроса и сессии |
| `log_inbound` / `log_outbound` | `:1242`, `:1285` | сообщения шины |
| `log_tool_call` / `log_tool_result` | `:1338`, `:1361` | вызовы инструментов |
| `log_llm_call` | `:1415` | обращения к модели |
| `log_sync_event` | `:1457` | синхронизация |
| `get_stats` / `report_stats` | `:1500`, `:1513` | наблюдаемость |
| `purge_empty_outbound` / `purge_old` | `:2164`, `:2189` | очистка |
| `attach_transport` | `:816` | подмена транспорта |

Модульные: `try_log_event` (`:175`), `next_event_seq` (`:338`),
`normalize_journal_level` (`:107`), `is_probe_event_type` (`:136`).
Отказ уровня — `JournalLevelError` (`:97`).

## Inputs

`LogEvent` (`:497`): тип события, актор, `session_key`, `request_id`, `user_id`,
metadata, произвольные поля полезной нагрузки. Уровень журнала — строка,
нормализующаяся через `normalize_journal_level` (`:107`); значение вне
допустимого перечня отвергается `JournalLevelError` (`:97`).

## Outputs

Две таблицы: `agent_gateway_logs` — журнал событий, `agent_question_runs` — прогон
вопроса. Запись идёт **транспортом в операцию платформы**
(`_flush_batch_via_mcp`, `:1983`), а не прямым SQL. При недоступности транспорта —
файловый fallback (`_write_fallback`, `:1963`), и потеря считается, а не скрывается.

## State

В памяти сервиса: карта «`session_key` → личность запроса»
(`register_request`, `:1075`), счётчик `next_event_seq` (`:338`), очередь
отложенных пакетов (`_release_deferred_batches`, `:848`) и счётчики потерь
(`_note_loss`, `:952`). Персистентного состояния сервис не имеет — он writer.

## Dependencies

`lib/services/db_logging_service.py`, транспорт `lib/services/log_transport.py`,
операция платформы `mcp-platform/servers/enterprise/capabilities/data/tools/log_events.py`, пул БД агента,
`asyncio` для фонового writer (`_worker`, `:1824`).

## Configuration

`logging.db.min_level` — единственная настройка, объявленная как `JOURNAL_MIN_LEVEL_PATH`
в `config.py:302`. Политика хранения и очистка приходит аргументом в `purge_old`
(`:2189`), а не конфигурацией.

## Lifecycle

`start` (`:869`) поднимает фонового writer и включает приём; `stop(timeout_sec=15.0)`
(`:882`) дренирует очередь и завершает writer. Между `stop` и `start` приём
событий не ведёт к записи: `is_running` (`:931`) сообщает состояние, и клиент
решает сам.

## Data Ownership

Сервис **не владеет** содержимым таблиц: он их заполняет, а не определяет, что
в них лежит. Политика хранения, очистка и отбор по профилю — вне его
ответственности. Прямой доступ к данным в обход транспорта запрещён.

## Error Behavior

Отказ не бросается наружу: бизнес-операция продолжается, потеря считается через
`_note_loss` (`:952`) и отражается в `get_stats` (`:1500`). Недопустимый уровень —
единственный случай явного отказа, и он поднимает `JournalLevelError` (`:1665`).
Транспорт недоступен — пакет уходит в файловый fallback (`:1936`, `:1963`).

## Invariants

- Порядок событий внутри хода сохраняется: `_stamp_event_time` (`:970`) и
  `event_time_columns` (`:393`) не дают более позднему событию выглядеть ранним.
- Событие, не прошедшее фильтр уровня, не попадает в очередь.
- Потеря события всегда учтена в счётчиках, даже при fallback — fallback не
  «успешная» запись.
- Прямого INSERT в журнал из кода агента нет.

## Forbidden Behavior

- Писать в `agent_gateway_logs` мимо `DbLoggingService` — прямо запрещено в `## Requirements`.
- Бросать исключение из фонового writer наружу в event loop.
- Считать событие записанным при недоступном транспорте без учёта потери.
- Менять личность запроса после `finish_request` (`:1175`) — личность неизменяема.

## Consumers

- Хуки и `McpIdentityHook` — подставляют личность в аргументы вызовов.
- Подсистема сжатия контекста (`lib/services/context_compaction.py`) — пишет
  `agent.compacted`.
- Каналы — `log_inbound` / `log_outbound`.
- UI и операция `data.history_search` — читают журнал после сжатия контекста.

## Implementation

Пути ниже — от корня репозитория; `./` означает файл в его корне. Настройки
живут в файле **в корне репозитория**, и имя без `./` здесь неразрешимо: в
репозитории есть ещё четыре файла с таким именем (capability `llm`, `vectors`
и два в `legal_summarizer`).

| Что | Где |
|---|---|
| Сервис и writer | `lib/services/db_logging_service.py` |
| Транспорт агента | `lib/services/log_transport.py` |
| Запись на стороне платформы | `mcp-platform/servers/enterprise/capabilities/data/tools/log_events.py` |
| Путь настроек | `./config.py:302` (`JOURNAL_MIN_LEVEL_PATH`) |

## Verification

| Тест | Что держит |
|---|---|
| `tests/test_db_logging_service.py` | сервис целиком |
| `tests/test_unified_event_logging_contract.py` | контракт записи |
| `tests/test_unified_event_logging_pipeline.py` | путь события до журнала |
| `tests/test_unified_event_logging_lifecycle.py` | start/stop |
| `tests/test_logging_bridge.py`, `tests/test_database_logging_bridge.py` | мост к платформе |
| `tests/test_hooks_database_logging.py` | запись из хуков |
| `tests/test_subagent_logging.py` | запись из субагентов |
| `tests/test_application_context_logging.py` | подключение при старте |
| `tests/contract/test_database_logging_get_model.py` | контракт модели события |
