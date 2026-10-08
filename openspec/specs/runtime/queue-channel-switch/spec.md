# runtime/queue-channel-switch Specification

## Purpose

Контракт переключения канала очереди задач на операции capability `data`:
форма ответа захвата, батч, отсутствие имени таблицы в аргументах, один путь
вместо переключателя.

Спека существует потому, что расхождение формы ответа в очереди не выглядит
дефектом. Оно не роняет вызов с треком: подмена в тестах повторяла форму,
которой у платформы нет, поэтому разбор и тест были согласованы между собой и
не согласны с платформой. Ошибка обнаруживалась только на боевом вызове — то
есть в проде, и выглядела как «очередь не отвечает», а не как «контракт
разошёлся».

Термины:

- **провод** — то, что вернула операция платформы, до разбора на стороне
  агента;
- **потребитель** — код канала, работающий с одиночной задачей
  (`_claim_one`, разбор ответа бота);
- **подпись** — значение из конфигурации вызывающей стороны, которое
  печатается в диагностике и ничего не выбирает.

## Scope

`agent` — `lib/channels/queue_ops.py` (`QueueOps.claim_task`,
`QueueOps.claim_tasks`, `ClaimedBatch`, `QueueOps._claimed_tasks`,
`QueueOps._next_cursor`), `lib/channels/postgres_channel.py`
(`_declared_table`, баннер старта). Сторона платформы описана в
`data/task-queue` и этим каталогом не меняется.

**Переключение состоялось, но раскладка не завершена.** Прямого SQL по таблице
задач в канале нет: единственный путь — `QueueOps` над клиентом процессов
(`lib/channels/queue_ops.py:129`, создание шва — `lib/channels/postgres_channel.py:194`,
единственный вызов захвата — `lib/channels/postgres_channel.py:1040`). Утверждение
«сегодня очередь обслуживает пул агента» коду не соответствует: в
`lib/channels/` нет ни одного обращения к `lib.utils.db`. Пул в агенте при этом
жив (`lib/utils/db.py:35`) и поднимается в composition root
(`lib/core/application_context.py:2021`), но очередью он не владеет; его
потребители — другие подсистемы, и снять пул нельзя до задач 2.1–2.3 change'а
`openspec/changes/2026-10-04-utils-db-pool-removal/tasks.md:43`. Не доведён
батч в цикле опроса: канал по-прежнему берёт одну задачу за тик
(`lib/channels/postgres_channel.py:1094`) — это отдельная работа про
конкурентность и лимиты слота, а не путь вызова. Объявление имени таблицы в
`config.json` (`config.json:232`) остаётся намеренно: канал читает его как
диагностическую подпись, чтобы оператор видел расхождение своего объявления с
профилем платформы (`lib/channels/postgres_channel.py:144-157`), и ничего не
выбирает (`openspec/changes/archive/2026-10-04-task-queue-channel-switch/tasks.md`).

## Requirements

### Requirement: Потребитель одиночного захвата получает задачу, а не пачку

`QueueOps.claim_task()` MUST возвращать словарь одной задачи либо `None`, и
MUST быть представлением `claim_tasks(batch=1)`, а не отдельным вызовом с
отдельной формой ответа. Потребитель MUST NOT знать о батчах: изменение формы
выдачи на платформе не имеет права менять контракт канала.

Плата за это решение — одно место, где форма ответа известна. Каждый новый
потребитель разбирает ответ одинаково, иначе каждый из них разберётся один раз
и один из них ошибётся.

#### Scenario: успешный захват отдаёт задачу

- **WHEN** платформа отдаёт `claimed` из одной задачи
- **THEN** `claim_task()` MUST вернуть словарь этой задачи
- **AND** в вызове MUST присутствовать `batch = 1` (размер пачки объявлен
  явно, а не подставлен под умолчание платформы)
  (проверяется `TestClaimedFormIsTheDeclaredOne::test_one_wide_batch_is_one_task_for_the_consumer`)

#### Scenario: пустая очередь отдаёт пустоту, а не ошибку

- **WHEN** платформа отдаёт `claimed` пустым списком
- **THEN** `claim_task()` MUST вернуть `None`
- **AND** отказом это MUST NOT считаться: пустая очередь — нормальное
  состояние (проверяется
  `TestClaimedFormIsTheDeclaredOne::test_empty_batch_is_an_empty_queue`)

#### Scenario: живой путь канала получает задачу

- **WHEN** канал выполняет `_claim_one()` при одной задаче в очереди
- **THEN** MUST вернуть словарь задачи, а не список
  (проверяется
  `TestPostgresChannelQueueWire::test_claim_one_returns_first_task_of_the_batch`)

### Requirement: Форма ответа захвата — список, и других форм нет

Поле `claimed` MUST разбираться как **список всегда**, в том числе при
`batch=1`: одиночный захват на платформе это представление батча шириной 1, а не
отдельная форма ответа. Значение, не являющееся списком, MUST отвергаться
поимённо, с называнием операции и фактического типа, и MUST NOT приниматься за
задачу.

Строгость — требование, а не строгость ради строгости. Разбор «словаря или
списка» означал бы, что опрос уводит в обработку мусор, заметить который можно
только по содержимому ответа; заметить его нельзя вовсе, пока подмена в тестах
повторяет ту же неверную форму.

Отсутствие ключа `claimed` MUST считаться пустой очередью, а не формой ответа:
операция, отработавшая вхолостую, — нормальное состояние, и отличать его от
«сломано» по наличию ключа нельзя.

Источник истины о форме — объявление платформы, а не подмена в тестах:
аннотация `ClaimedBatch.tasks` в
`mcp-platform/servers/enterprise/capabilities/data/service/main.py` и разбор в
`QueueOps._claimed_tasks` обязаны совпадать.

#### Scenario: платформенный список из одной задачи

- **WHEN** платформа отдаёт `claimed` как список из одной задачи
- **THEN** разбор MUST вернуть эту задачу
  (проверяется
  `TestClaimedFormIsTheDeclaredOne::test_one_wide_batch_is_one_task_for_the_consumer`)

#### Scenario: словарь вместо списка отвергается поимённо

- **WHEN** платформа отдаёт `claimed` как словарь
- **THEN** MUST быть поднят `QueueOpsError`, называющий операцию, фактический
  тип и то, что список обязателен
- **AND** словарь MUST NOT быть принят за задачу
  (проверяется
  `TestClaimedFormIsTheDeclaredOne::test_dict_form_is_refused_by_name` и
  `TestPostgresChannelQueueWire::test_legacy_dict_form_is_refused_by_name`)

#### Scenario: элемент списка, не являющийся задачей, отвергается

- **WHEN** элемент `claimed` не является объектом
- **THEN** MUST быть поднят `QueueOpsError` с указанием индекса элемента
  (проверяется
  `TestClaimedFormIsTheDeclaredOne::test_claimed_element_must_be_a_task`)

#### Scenario: отсутствие ключа — пустая очередь

- **WHEN** в ответе нет ключа `claimed`
- **THEN** разбор MUST вернуть пустой результат без отказа
  (проверяется
  `TestClaimedFormIsTheDeclaredOne::test_absent_claimed_is_an_empty_queue`)

#### Scenario: форма заявлена с двух сторон

- **WHEN** аннотация `ClaimedBatch.tasks` на платформе перестаёт быть списком
- **THEN** страж MUST упасть, а не пройти: разбор агента тогда описывал бы
  форму, которой у платформы нет
- **AND** возврат в `QueueOps` приёма словаря как `claimed` MUST ронять страж
  (проверяется `TestClaimedFormIsTheDeclaredOne::test_platform_declares_claimed_as_a_list`
  и `test_queue_ops_never_accepts_a_task_dict_as_claimed`)

### Requirement: Размер пачки и курсор объявляются в вызове явно

`QueueOps.claim_tasks()` MUST отправлять `batch` и `cursor` в аргументах
вызова. Молчаливое умолчание платформы означало бы, что размер пачки —
решение, о котором в коде не знает никто, и что сменится без предупреждения.

Границы значения (`batch < 1` — отказ, потолок — `data.max_rows`) проверяет
платформа; агент MUST NOT дублировать их, потому что второе место, где
правило обязано совпадать с первым, — это и есть тот класс расхождений,
который перенос устранял.

Курсор продолжения MUST доезжать до потребителя; значение, не являющееся
строкой, MUST отвергаться поимённо.

#### Scenario: батч и курсор доезжают до платформы

- **WHEN** вызывается `claim_tasks(batch=3, cursor=…)`
- **THEN** в аргументах вызова MUST стоять `batch = 3` и указанный курсор
  (проверяется
  `TestClaimedFormIsTheDeclaredOne::test_wide_batch_and_cursor_come_back`)

#### Scenario: курсор доезжает до потребителя

- **WHEN** платформа отдаёт `next_cursor`
- **THEN** MUST вернуться в `ClaimedBatch.next_cursor`
- **AND** нестроковый курсор MUST отвергаться поимённо (проверяется
  `TestClaimedFormIsTheDeclaredOne::test_non_string_cursor_is_refused`)

#### Scenario: объявленный набор параметров не разошёлся с платформой

- **WHEN** платформа убирает `batch` или `cursor` из объявления операции
- **THEN** страж MUST упасть, а не пропустить вызов: параметр, которого у
  операции нет, терялся по дороге, пока объявленная схема не стала
  исполняемой (проверяется
  `TestQueueOpsPayloadMatchesDeclaredSchema::test_claim_sends_batch_and_cursor_explicitly`)

### Requirement: Имя таблицы очереди не приходит от вызывающей стороны

Ни один аргумент вызова операции очереди MUST NOT содержать имя таблицы.
Имя выбирает платформа (`mcp-platform/platform.json::data.task_table` поверх
оверлея профиля). Пока вызывающая сторона могла назвать таблицу, объявление
платформы не было единственным: расхождение двух объявлений обнаруживалось
только содержимым боевого журнала.

Ключ `channels.postgres.table_name` MUST NOT быть обязательным и MUST читаться
как подпись: канал к этому имени не обращается. Баннер старта MUST NOT
объявлять опрос конкретной таблицы — он печатает объявление как подпись, а при
его отсутствии — как `platform-owned`.

#### Scenario: канал поднимается без объявления таблицы

- **WHEN** `channels.postgres.table_name` не объявлен
- **THEN** канал MUST подняться, а `declared table` MUST быть `platform-owned`
  (проверяется
  `TestPostgresChannelInit::test_table_name_is_a_label_not_a_requirement` и
  `TestPostgresChannelQueueWire::test_banner_says_platform_owned_when_nothing_declared`)

#### Scenario: чужое объявление остаётся подписью

- **WHEN** `channels.postgres.table_name` объявлен и отличается от таблицы
  платформы
- **THEN** канал MUST читать его как подпись и печатать её в баннере как
  `declared table`, а не как адрес опроса
  (проверяется
  `TestPostgresChannelQueueWire::test_banner_does_not_claim_to_poll_the_declared_table`)

#### Scenario: ни один аргумент не называет таблицу

- **WHEN** канал проходит путь захвата, возврата зависших, вставки и отказа
- **THEN** ни в одном аргументе вызова операции MUST NOT стоять имя таблицы
- **AND** страж MUST проверять, что вызовы вообще были, иначе он зелен на
  пустом месте (проверяется
  `TestPostgresChannelQueueWire::test_no_call_argument_names_a_table`)

### Requirement: Путь к очереди один, и отказ платформы слышен

Очередь канала MUST обслуживаться операциями capability `data`. Второй путь к
таблице задач — прямой SQL в канале — MUST NOT появляться вместе с этим
контрактом: он означал бы второго владельца записи и две транзакционные
границы на один и тот же статус задачи, то есть расхождение результатов, а не
две равные реализации.

Переключатель пути (`feature-флаг`) поэтому MUST NOT объявляться, пока
существует ровно один путь: значение, которому не соответствует ни одна строка
кода, в конфигурации — это обещание, которое не выполняется.

Недоступность платформы MUST подниматься как `QueueOpsError` наружу, а
MUST NOT выглядеть как пустая очередь. Иначе канал решил бы, что задач нет, и
продолжил работу, а оператор увидел бы простой без единой записи в журнале.

#### Scenario: платформа недоступна — опрос падает, а не молчит

- **WHEN** вызов операции очереди недоступен
- **THEN** MUST быть поднят `QueueOpsError`, называющий операцию и причину
- **AND** пустой результат вместо отказа MUST NOT возвращаться: «очередь
  пуста» и «очередь недоступна» — разные состояния, и свести их нельзя

#### Scenario: прямой SQL в канале не появляется

- **WHEN** в `lib/channels/` появляется обращение к `lib.utils.db` либо `execute`
  по имени таблицы задач
- **THEN** возврат второго пути MUST требовать отдельного change'а с явным
  регламентом переключения, а не молчаливого появления кода рядом с
  операциями

## Не в этом каталоге

Опрос канала по-прежнему забирает одну задачу за тик (`_claim_one`).
`QueueOps.claim_tasks` доступен, но в цикл опроса не введён: введение батча в
опрос меняет конкурентность и лимиты слота, а это работа за рамками
переключения. Прямой SQL в остальном `lib/` (`db_logging_service`,
`session_cold_sync_service`, `schema_validation`) — шаг 3; tombstone'ы
`_claim_task.py` / `_update_task_status.py` на платформе — шаг 4.

## Responsibility

`QueueOps` отвечает за то, чего в канале больше нет:

- разбор JSON-ответа операции в доменный вид (`lib/channels/queue_ops.py:135`);
- разбор `claimed` как списка и отвержение неверной формы поимённо
  (`lib/channels/queue_ops.py:231`);
- разбор курсора продолжения (`lib/channels/queue_ops.py:262`);
- явную отправку `batch` и `cursor` (`lib/channels/queue_ops.py:200`,
  `lib/channels/queue_ops.py:201`);
- подстановку личности вызова: служебной для опроса и оборота для правок
  задачи (`lib/channels/queue_ops.py:148`, `lib/channels/queue_ops.py:160`);
- превращение отказа платформы в `QueueOpsError`, а не в «пустую очередь»
  (`lib/channels/queue_ops.py:130`).

## Boundary

Владеет:
- формой ответа платформы — единственным местом в дереве агента, где она
  известна (`lib/channels/queue_ops.py:231`);
- идентичностью служебных вызовов (`lib/channels/queue_ops.py:59`,
  `lib/channels/queue_ops.py:66`);
- типом ошибки `QueueOpsError` (`lib/channels/queue_ops.py:69`).

Может зависеть от:
- клиента процессов платформы и `CallIdentity`
  (`lib/channels/queue_ops.py:53`);
- идентичности служебных вызовов из
  `lib/services/service_identity.py` (`lib/channels/queue_ops.py:66`).

Не должен зависеть от:
- `lib.utils.db` — второй путь к таблице задач;
- границ батча: их проверяет платформа, дубль был бы вторым местом, которое
  обязано совпадать с первым;
- объявления имени таблицы из конфигурации агента.

## Public Contract

- `QueueOps.claim_tasks(batch=, cursor=, …) -> ClaimedBatch`
  (`lib/channels/queue_ops.py:168`).
- `QueueOps.claim_task(…) -> dict | None` — представление `batch=1`
  (`lib/channels/queue_ops.py:210`, `lib/channels/queue_ops.py:228`).
- `ClaimedBatch(tasks, next_cursor)` — frozen dataclass
  (`lib/channels/queue_ops.py:80`, поля `lib/channels/queue_ops.py:89`,
  `lib/channels/queue_ops.py:90`).
- `QueueOpsError` (`lib/channels/queue_ops.py:69`).
- `QueueOps.available` — признак наличия транспорта
  (`lib/channels/queue_ops.py:109`).
- `release_claimed_tasks`, `unstick_tasks`, `update_task_status`,
  `append_assistant_message`, `patch_message_metadata`, `merge_tool_delivery` —
  остальной шов очереди (`lib/channels/queue_ops.py:274`,
  `lib/channels/queue_ops.py:282`).

## Inputs

- `batch: int = 1` — размер пачки, уходит в вызов явно
  (`lib/channels/queue_ops.py:200`);
- `cursor: str | None = None` — keyset-метка предыдущей выдачи
  (`lib/channels/queue_ops.py:201`);
- `error_retry_delay_sec` — backoff из конфигурации канала
  (`lib/channels/queue_ops.py:193`);
- `priority_contents` — команды priority-поллинга; `None` означает «без
  фильтра», пустой список отправлять нельзя
  (`lib/channels/queue_ops.py:199`);
- `client` — клиент процессов платформы, `None` в тестах и standalone
  (`lib/channels/queue_ops.py:103`);
- `worker_id` — номер воркера в листе идентичности служебных вызовов
  (`lib/channels/queue_ops.py:105`).

## Outputs

- `ClaimedBatch` — задачи пачки и курсор (`lib/channels/queue_ops.py:205`);
- словарь задачи либо `None` из `claim_task`
  (`lib/channels/queue_ops.py:228`);
- `QueueOpsError` наружу на любой отказ, непустой JSON или форме, отличной от
  объявленной (`lib/channels/queue_ops.py:131`,
  `lib/channels/queue_ops.py:143`, `lib/channels/queue_ops.py:247`);
- файлов, сетевых вызовов и записей в БД `QueueOps` не производит: он только
  разбирает ответ клиента.

## State

`QueueOps` хранит клиент и номер воркера (`lib/channels/queue_ops.py:104`) и
больше ничего. Состояние захвата задач живёт в строке задачи, а локальный
учёт захваченных идентификаторов ведёт канал для возврата незавершённых задач
при остановке (`lib/channels/postgres_channel.py:200`, возврат —
`lib/channels/postgres_channel.py:408`).

Курсор продолжения — состояние опроса, и оно принадлежит вызывающей стороне:
`QueueOps` его только перевозит.

## Dependencies

- `lib/services/enterprise_mcp_client.py` — `CallIdentity` и живой клиент
  (`lib/channels/queue_ops.py:53`);
- `lib/services/service_identity.py` — имя пользователя служебных вызовов
  (`lib/channels/queue_ops.py:66`);
- процесс платформы `enterprise-mcp` — единственный источник данных очереди;
- `mcp-platform/servers/enterprise/capabilities/data/tools/claim_task.py` —
  объявление операции, форма `claimed` и `next_cursor`
  (`mcp-platform/servers/enterprise/capabilities/data/tools/claim_task.py:138`).

## Configuration

- `channels.postgres.table_name` — **подпись**, не обязательна и ничего не
  выбирает; при отсутствии баннер печатает `platform-owned`
  (`config.json:232` → `lib/services/channel_factory.py:137` →
  `lib/channels/postgres_channel.py:154`, печать
  `lib/channels/postgres_channel.py:371`);
- `channels.postgres.error_retry_delay` — backoff, уходит в вызов захвата
  (`lib/channels/postgres_channel.py:187`);
- `channels.postgres.poll_interval`, `processing_timeout`,
  `max_stuck_retries`, `queue_report_interval` — тайминги опроса
  (`lib/channels/postgres_channel.py:161`,
  `lib/channels/postgres_channel.py:163`,
  `lib/channels/postgres_channel.py:165`,
  `lib/channels/postgres_channel.py:175`);
- границы батча в конфигурации агента **не объявляются**: `batch < 1` и потолок
  `data.max_rows` проверяет платформа.

## Lifecycle

1. `ChannelFactory._add_postgres` передаёт каналу клиента процессов платформы
   (`lib/services/channel_factory.py:148`).
2. `PostgresChannel.__init__` создаёт единственный шов
   (`lib/channels/postgres_channel.py:194`); клиент `None` допустим — тогда
   `available` ложно (`lib/channels/queue_ops.py:110`), а методы поднимают
   отказ.
3. Канал поднимает циклы опроса и возврата зависших и печатает баннер
   (`lib/channels/postgres_channel.py:361`).
4. Остановка возвращает незавершённые задачи
   (`lib/channels/postgres_channel.py:408`).
5. Клиент закрывается на уровне процесса агента, а не шва: `QueueOps` владения
   соединением не имеет.

## Data Ownership

`QueueOps` не владеет данными: он создаёт доменный вид из ответа платформы и
ничего не пишет. Задачей, её статусом и таблицей владеет capability `data`,
адрес которой объявлен в `mcp-platform/platform.json` и приходит оверлеем
профиля.

Локально канал владеет лишь множеством идентификаторов захваченных задач
(`lib/channels/postgres_channel.py:200`) — это учёт для возврата при остановке,
а не копия данных.

## Error Behavior

- нет клиента — `QueueOpsError` с упоминанием операции и отсутствия транспорта
  (`lib/channels/queue_ops.py:124`);
- отказ вызова — `QueueOpsError` с причиной, `from exc`
  (`lib/channels/queue_ops.py:131`);
- ответ не JSON или не объект — `QueueOpsError` с началом ответа
  (`lib/channels/queue_ops.py:139`);
- `claimed` не список — `QueueOpsError`, называющий фактический тип и то, что
  список обязателен (`lib/channels/queue_ops.py:247`);
- элемент `claimed` не объект — `QueueOpsError` с индексом
  (`lib/channels/queue_ops.py:255`);
- `next_cursor` не строка — `QueueOpsError` с фактическим типом
  (`lib/channels/queue_ops.py:268`);
- **не** отказ: пустой список `claimed`, отсутствие ключа `claimed`
  (`lib/channels/queue_ops.py:242`), пустая строка курсора
  (`lib/channels/queue_ops.py:272`).

## Invariants

- `claim_task()` — представление `claim_tasks(batch=1)`
  (`lib/channels/queue_ops.py:223`), и в вызове `batch` стоит явно
  (`lib/channels/queue_ops.py:224`).
- `claimed` разбирается как список всегда (`lib/channels/queue_ops.py:246`).
- Потребитель одиночного захвата получает словарь либо `None`
  (`lib/channels/queue_ops.py:228`).
- В аргументах вызова операции очереди нет имени таблицы: `_declared_table`
  используется только в баннере (`lib/channels/postgres_channel.py:371`).
- Служебные вызовы подписаны воркером, а не сессией пользователя
  (`lib/channels/queue_ops.py:156`).
- Шов один: `QueueOps` создаётся в канале один раз
  (`lib/channels/postgres_channel.py:194`).

## Forbidden Behavior

- Принимать словарь в `claimed` как задачу «на всякий случай»: расхождение формы
  прошло бы молча, и следующая смена формы вернула бы тот же дефект.
- Отличать «очередь пуста» от «очередь недоступна» — состояния разные, и свести
  их нельзя.
- Имя таблицы в аргументах вызова, в тексте SQL канала или как обязательное
  требование конфигурации.
- Дублировать в агенте границы батча, проверяемые платформой.
- Объявлять переключатель пути (`feature-флаг`) при единственном пути.
- Обращение к `lib.utils.db` или прямой `execute` по имени таблицы задач в
  `lib/channels/`.
- Отправлять `priority_contents` пустым списком вместо `None`: платформа добавит
  `AND content = ANY(%s)` и отбор не найдёт ничего, то есть очередь молча
  покажется пустой (`lib/channels/queue_ops.py:199`).
- Подставлять сессию пользователя в служебный вызов опроса.

## Consumers

- `lib/channels/postgres_channel.py` — единственный владелец шва: опрос
  (`lib/channels/postgres_channel.py:1040`), priority-поллинг
  (`lib/channels/postgres_channel.py:773`), возврат зависших
  (`lib/channels/postgres_channel.py:940`), возврат при остановке
  (`lib/channels/postgres_channel.py:408`), правки задачи
  (`lib/channels/postgres_channel.py:824`, `lib/channels/postgres_channel.py:1251`).
- `lib/services/channel_factory.py:148` — сборка канала с клиентом платформы.
- Тесты, подставляющие клиента процесса: форма ответа в них обязана совпадать с
  платформенной, иначе подмена подтверждает саму себя
  (`tests/conftest.py`).

## Implementation

Существующие на диске пути, на которых держится этот контракт:

- `lib/channels/queue_ops.py` — разбор ответа, `ClaimedBatch`, `QueueOpsError`,
  личность вызова;
- `lib/channels/postgres_channel.py` — единственный потребитель шва, `_claim_one`
  (`lib/channels/postgres_channel.py:991`), подпись таблицы
  (`lib/channels/postgres_channel.py:154`) и баннер
  (`lib/channels/postgres_channel.py:361`);
- `lib/services/channel_factory.py:103` — сборка канала;
- `lib/services/enterprise_mcp_client.py` — клиент и `CallIdentity`;
- `mcp-platform/servers/enterprise/capabilities/data/tools/claim_task.py` —
  объявление формы, эталон строки разбора.

**Что из этого ещё не доведено.** Батч доступен, но в цикл опроса не введён:
канал берёт одну задачу за тик (`lib/channels/postgres_channel.py:1094`), и это
отдельная работа про конкурентность и лимиты слота
(`openspec/changes/archive/2026-10-04-task-queue-channel-switch/tasks.md`).
`config.json::channels.postgres.table_name` объявлен и ничего не выбирает
(`config.json:232`) — **оставлен намеренно**: канал читает его как
диагностическую подпись, чтобы расхождение объявления оператора с профилем
платформы было видно, а не молчало
(`lib/channels/postgres_channel.py:144-157`).
Пул `lib/utils/db` жив (`lib/utils/db.py:35`, старт —
`lib/core/application_context.py:2021`), и очередью он не владеет: его снос
заблокирован задачами 2.1–2.3 change'а
`openspec/changes/2026-10-04-utils-db-pool-removal/tasks.md:43`. Переключатель
пути не объявлен **решением владельца**: второго пути в дереве нет, а объявлять
ключ, которому не соответствует ни одна строка кода, — обещание без исполнения;
откатная страховка, если понадобится, оформляется отдельным change'ем
(`openspec/changes/archive/2026-10-04-task-queue-channel-switch/tasks.md`).

## Verification

- `tests/test_queue_ops_payload_contract.py:221`
  (`TestClaimedFormIsTheDeclaredOne`) — форма ответа заявлена с двух сторон,
  разбор словаря и элемента-не-объекта отвергается поимённо, курсор доезжает;
- `tests/test_queue_ops_payload_contract.py:134`
  (`TestQueueOpsPayloadMatchesDeclaredSchema`) — набор отправляемых параметров
  совпадает с объявлением операции;
- `tests/test_postgres_channel.py:253` (`TestPostgresChannelQueueWire`) — живой
  `_claim_one` на платформенной форме, отсутствие имени таблицы в аргументах,
  баннер;
- `tests/test_postgres_channel.py:212` (`TestPostgresChannelInit`) — имя таблицы
  как подпись, а не требование;
- `tests/test_single_mode_audit.py` — подмена отдаёт платформенную форму.