# data/task-queue Specification

## Purpose

Контракт операций очереди задач в capability `data`: захват, смена статуса,
вставка и удаление assistant-сообщения, патч метаданных, возврат зависших.

Спека существует потому, что очередь — единственная подсистема платформы,
дефект в которой не выглядит дефектом. Ошибка захвата не роняет вызов: она
либо обрабатывает одну задачу дважды, либо молча не обрабатывает вовсе, и
оба случая обнаруживаются на стороне пользователя, а не в журнале платформы.

Термины:

- **выдача** — то, что вернул один вызов захвата: одна задача или пачка;
- **курсор** — keyset-метка `(created_at, id)` последней строки выдачи,
  которой выдача следующая продолжается;
- **потолок батча** — `data.max_rows`, объявленный в `platform.json`.

## Scope

`platform` — `mcp-platform/servers/enterprise/capabilities/data/service/main.py`
(`claim_tasks`, `ClaimedBatch`),
`mcp-platform/servers/enterprise/capabilities/data/tools/claim_task.py`,
`mcp-platform/platform.json` (`data.task_table`).

**Часть объявленного состояния ещё не достигнута, и ниже она описана как
целевая.** Сами операции очереди существуют и обслуживают канал
(`lib/channels/queue_ops.py:191` — единственный вызов захвата на стороне
агента), но пул PostgreSQL в агенте жив: он объявляет `configure`, `execute`,
`fetchone`, `transaction` (`lib/utils/db.py:35`), поднимается и останавливается
в composition root (`lib/core/application_context.py:2021`,
`lib/core/application_context.py:2031`), а настраивается при старте хранилища
сессий (`lib/services/session_storage.py:188`). Пул нельзя удалить до закрытия
задач 2.1–2.3 change'а
`openspec/changes/2026-10-04-utils-db-pool-removal/tasks.md:43`: пула-переёмчика
в агенте нет, а `start()` выполняется до входа в живой loop.

Шаги 3 и 4 раскладки queue-перехода **выполнены** — сверено с деревом, а не с
чекбоксами change'а: `workspace/utils/db.py` удалён, tombstone'ов
`_claim_task.py` / `_update_task_status.py` в дереве нет
(`openspec/changes/archive/2026-10-04-task-queue-platform-ops/tasks.md`).
Перенос потребителей **агентского** пула на операции платформы — не тот же
предмет и не сделан: пул обслуживает собственные таблицы агента, и его судьба
ведётся в `2026-10-04-utils-db-pool-removal`.

## Requirements

### Requirement: Захват задачи атомарен и берёт не более одной задачи на чат

Операция `claim_task` MUST захватывать задачу атомарно: два конкурирующих
вызова MUST NOT получать одну строку. Механизм — `UPDATE … SET
status='processing' … RETURNING`, в котором условие отбора повторено и во
внешнем `WHERE`, и в подзапросе. Снятие повтора MUST NOT считаться
оптимизацией: это тихая двойная обработка, которая проявляется не сразу.

Отбор MUST брать `role='user'`, статус `pending` либо `error` старше
`error_retry_delay_sec`, исключать `cancelled` и исключать чаты, где уже есть
`processing`-сообщение. Порядок выдачи MUST быть по времени создания.

Выдача MUST содержать не более одной задачи на чат. При батче это обеспечивается
`DISTINCT ON (chat_id)`: подзапрос читает снимок, каким он был ДО `UPDATE`, и
без этого отбора две ожидающие задачи одного чата попали бы в одну выдачу —
то есть «одна незавершённая задача на чат» держалась бы ровно до того момента,
пока батч шире единицы.

Пустая очередь MUST возвращаться пустым результатом, а не ошибкой: это
нормальное состояние, и оборачивать его в исключение заставило бы канал
отличать «нечего делать» от «сломалось» по тексту ответа.

#### Scenario: два конкурирующих захвата не делят одну задачу

- **WHEN** два вызова `claim_task` выполняются одновременно и в очереди одна
  доступная задача
- **THEN** ровно один MUST вернуть задачу, второй MUST вернуть пустой результат
- **AND** условие отбора MUST присутствовать и во внешнем `WHERE`, и в подзапросе
  (проверяется `TestClaimTask::test_selection_condition_is_repeated_in_outer_where`)

#### Scenario: пачка не берёт вторую задачу занятого чата

- **WHEN** в очереди две задачи одного чата в статусе `pending` и вызван
  `claim_task` с `batch` больше единицы
- **THEN** выдача MUST содержать не более одной задачи этого чата
- **AND** отбор MUST содержать `DISTINCT ON (chat_id)`
  (проверяется `TestClaimTaskBatch::test_one_task_per_chat`)

#### Scenario: пустая очередь — пустой результат, а не отказ

- **WHEN** в очереди нет доступных задач
- **THEN** операция MUST вернуть пустой результат, а MUST NOT поднимать исключение
  (проверяется `TestClaimTaskBatch::test_empty_queue_is_empty_list_not_error`)

### Requirement: Захват возвращает пачу, а не одну задачу

Операция `claim_task` MUST принимать параметр `batch` и MUST возвращать до
`batch` задач за один вызов. Значение `batch = 1` MUST сохранять поведение
одиночного захвата: выдача MUST оставаться словарём, а не списком из одного
элемента, иначе вызывающий различал бы «взял задачу» и «взял пустую пачку»
разбором ответа.

Батчинг — требование, а не украшение: очередь обслуживается вызовами по
stdio, и по одной задаче на оборот опрос платил бы больше, чем прямой SQL,
который заменяет, — то есть перенос ускорил бы ровно то, что должен убрать.

`batch` MUST проверяться: значение меньше единицы MUST отклоняться, а не
трактоваться как «ничего не брать» — молчаливая потеря очереди неотличима от
пустой. Значение MUST урезаться потолком платформы (`data.max_rows`), и
урезка MUST быть видна в параметрах запроса, а не применяться молча.

#### Scenario: батч возвращает несколько задач за один вызов

- **WHEN** в очереди несколько доступных задач и вызван `claim_task` с `batch`
  больше единицы
- **THEN** выдача MUST содержать все захваченные задачи
- **AND** потолок батча MUST прийти в запрос параметром
  (проверяется `TestClaimTaskBatch::test_batch_limit_reaches_the_query`)

#### Scenario: одиночный захват остаётся словарём

- **WHEN** вызван `claim_task` без `batch` (либо с `batch = 1`)
- **THEN** MUST быть возвращён словарь задачи, а не список
- **AND** при пустой очереди MUST быть возвращён `None`
  (проверяется `TestClaimTaskBatch::test_single_row_helper_still_returns_dict`)

#### Scenario: неположительный батч отклоняется

- **WHEN** вызван `claim_task` с `batch` равным `0` или отрицательному
- **THEN** операция MUST отклонить вызов, а MUST NOT вернуть пустую выдачу
  (проверяется `TestClaimTaskBatchBounds::test_non_positive_batch_is_rejected`)

#### Scenario: батч шире потолка платформы урезается потолком

- **WHEN** вызван `claim_task` с `batch` больше `data.max_rows`
- **THEN** в запрос MUST уйти потолок платформы, а не запрошенное значение
  (проверяется `TestClaimTaskBatchBounds::test_batch_is_capped_by_platform_max_rows`)

### Requirement: Неполная выдача продолжается курсором

Выдача MUST быть упорядочена по времени создания, и последняя строка упорядоченной
выдачи MUST задавать `next_cursor` — keyset-метку `(created_at, id)`. Порядок
`RETURNING` не обещан, поэтому курсор MUST строиться из упорядоченной выдачи:
взят он из сырого порядка строк, опрос продолжился бы с произвольной точки и
пропустил бы задачи между страницами.

Курсор MUST приниматься следующим вызовом и MUST превращаться в keyset-фильтр
`(created_at, id) > (…, …)`. Смещение (`OFFSET`) для этой роли MUST NOT
использоваться: очередь меняется под ногами опроса, и страницы на смещении
начинают перекрываться или пропускать задачи.

`next_cursor` MUST заполняться **только** по полному батчу. Неполный батч
означает, что очередь дошла до конца, и следующий опрос MUST начаться с головы:
курсор по неполной выдаче заморозил бы опрос, и задачи, пришедшие позже,
оказались бы за курсором навсегда.

Кривой курсор MUST отклоняться. Молча начать с головы на неразбираемом курсоре
означало бы пропустить задачи, не сообщив об этом: очередь выглядела бы пустой,
и потеря была бы безмолвной.

#### Scenario: полный батч возвращает курсор продолжения

- **WHEN** выдача вернула ровно `batch` задач
- **THEN** MUST быть возвращён `next_cursor` по последней задаче в порядке
  очереди
  (проверяется `TestClaimTaskCursor::test_full_batch_returns_cursor`)

#### Scenario: неполный батч курсора не возвращает

- **WHEN** выдача вернула меньше `batch` задач
- **THEN** `next_cursor` MUST быть `None`, чтобы следующий опрос начался с головы
  (проверяется `TestClaimTaskCursor::test_partial_batch_returns_no_cursor`)

#### Scenario: курсор продолжает выдачу, а не перечитывает начало

- **WHEN** вызван `claim_task` с курсором предыдущей выдачи
- **THEN** отбор MUST содержать keyset-фильтр по этому курсору
  (проверяется `TestClaimTaskCursor::test_cursor_becomes_keyset_filter`)

#### Scenario: неразбираемый курсор отклоняется, а не игнорируется

- **WHEN** вызван `claim_task` с курсором, из которого не читается пара
  `(created_at, id)`
- **THEN** операция MUST отклонить вызов
  (проверяется `TestClaimTaskCursor::test_broken_cursor_is_refused`)

### Requirement: Имя таблицы очереди объявляет платформа, а не вызывающий

Имя таблицы очереди MUST приходить из `platform.json` (`data.task_table`) и MUST
NOT приходить аргументом вызова. Объявляя имя таблицы, вызывающая сторона
выбирала бы, чьи данные трогать, то есть вход в данные агента шёл бы мимо его
конфигурации — это и было основанием удалить обе операции очереди в пункте 2.18
спеки `enterprise-mcp-platform`.

Незаданное имя таблицы MUST приводить к явному отказу с упоминанием
`ENTERPRISE_TASK_TABLE`, а MUST NOT выглядеть как «очередь пуста».

#### Scenario: имя таблицы берётся из объявления платформы

- **WHEN** вызван `claim_task`
- **THEN** имя таблицы MUST прийти из `platform.json::data.task_table`
- **AND** текст запроса MUST NOT содержать имени аргумента `task_table`
  (проверяется `TestClaimTask::test_table_comes_from_settings`)

#### Scenario: незаданное имя таблицы — явный отказ

- **WHEN** `data.task_table` не объявлено и вызван `claim_task`
- **THEN** операция MUST отклонить вызов с упоминанием `ENTERPRISE_TASK_TABLE`
  (проверяется `TestClaimTaskGuards::test_without_task_table_refuses_explicitly`)

### Requirement: Операции очереди недоступны модели

Все операции очереди MUST быть `runtime-only`: профиль вызова модели MUST
отклоняться, а работа MUST выполняться в классе внутреннего потока платформы.
Проверяется на выполненной работе, а не в подписи: подпись спрятана от
вызывающего, а класс работы занимает в пуле слот, отведённый вызовам
инструментов.

Класс работы MUST быть объявлен **поимённо** в
`capabilities/data/service/registry.py::OPERATION_AUDIENCE`, включая батчевый
захват `claim_tasks`. Реестр классов — единственное место, где это записано
поимённо, поэтому новая операция очереди без записи в нём меняет класс работы
молча: пул обслуживает её наравне со всем остальным, и заметить это можно
только на живом контуре.

Объявление MUST сопровождаться стражем, который падает при удалении записи.
Обход «все методы, зовущие пул» для этого недостаточен: он срабатывает, только
пока метод зовёт `self.submit` прямо, и перестаёт видеть операцию после того,
как постановка уходит в помощника. Поимённая запись без поимённой проверки —
ровно тот случай, когда реестр выглядит рабочим, а класса в нём нет.

#### Scenario: вызов с профилем модели отклоняется

- **WHEN** любую операцию очереди вызывают с профилем аудитории модели
- **THEN** вызов MUST быть отклонён
  (проверяется `test_model_audience_is_denied` в каждом классе операций)

#### Scenario: работа захвата идёт во внутреннем классе

- **WHEN** выполняется `claim_task`
- **THEN** работа MUST попасть в класс внутреннего потока платформы
  (проверяется `TestClaimTaskGuards::test_work_reaches_the_pool_in_the_runtime_class`)

#### Scenario: батчевый захват объявлен в реестре классов

- **WHEN** `claim_tasks` зовёт пул
- **THEN** MUST существовать поимённая запись в `OPERATION_AUDIENCE` с классом
  `JOB_AUDIENCE_RUNTIME`
  (проверяется `TestQueueCaptureHasAJobClass::test_queue_capture_is_declared_in_the_registry`
  и `::test_queue_capture_runs_in_the_runtime_class`)

#### Scenario: страж падает, когда запись убрана

- **WHEN** запись `claim_tasks` удалена из реестра
- **THEN** страж MUST падать, а не оставаться зелёным
  (проверяется `TestQueueCaptureHasAJobClass::test_the_pin_actually_fails_without_the_entry`)

## Responsibility

Capability `data` отвечает за шесть операций самой очереди задач и за объявление её
адреса:

- атомарный захват задачи или пачки, не более одной задачи на чат
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1554`);
- смена статуса, вставка и удаление assistant-плейсолдера, патч метаданных,
  возврат зависших (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1458`);
- выбор имени таблицы очереди из объявления платформы, а не из вызова
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1540`);
- проверка границ `batch` и урезка потолком платформы (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1528`,
  `mcp-platform/servers/enterprise/capabilities/data/service/main.py:1537`);
- keyset-курсор продолжения, а не смещение (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:3348`,
  `mcp-platform/servers/enterprise/capabilities/data/service/main.py:3359`);
- назначение класса работы каждой операции очереди (`mcp-platform/servers/enterprise/capabilities/data/service/registry.py:46`).

Платформа не решает, что делать с задачей: она только выдаёт её и принимает
статус. Решение остаётся за каналом агента, который зовёт операции.

## Boundary

Владеет:
- строкой задачи и её `status` как состоянием захвата (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1556`);
- объявлением имени таблицы очереди (`platform.json:96`);
- порядком выдачи и построением курсора (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:3338`).

Может зависеть от:
- пула соединений платформы через `self.submit` с аудиторией
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1588`);
- оверлея профиля, перекрывающего `data.task_table`
  (`settings.py:1350`).

Не должен зависеть от:
- пула PostgreSQL агента (`lib/utils/db.py`) — второй владелец записи в таблицу
  задач означал бы две транзакционные границы на один статус;
- аргумента вызова с именем таблицы: `task_table` во внутренней подписи сервиса
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1486`) — перекрытие для тестов, а не путь выбора извне;
- аудитории модели (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1451`).

## Public Contract

- `data.claim_task` — операция, возвращающая
  `{"status", "claimed", "next_cursor"}` (`claim_task.py:135`).
- `DataService.claim_tasks(batch=, cursor=) -> ClaimedBatch`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1478`).
- `DataService.claim_task() -> dict | None` — представление `batch=1`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1606`, возврат `mcp-platform/servers/enterprise/capabilities/data/service/main.py:1632`).
- `ClaimedBatch(tasks, next_cursor)` — frozen dataclass
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:359`, поля `mcp-platform/servers/enterprise/capabilities/data/service/main.py:372`, `mcp-platform/servers/enterprise/capabilities/data/service/main.py:373`).

Прочие пять операций очереди задач — `update_task_status`, `unstick_tasks`,
`release_claimed_tasks`, `fail_task`, `queue_stats` — объявлены здесь по общему
контракту очереди: адрес, аудитория и класс работы у них те же. Все шесть помечены
тегом `queue` в `mcp-platform/servers/enterprise/capabilities/data/tools/`.

Под тем же тегом `queue` объявлены ещё восемь операций диалога и зеркала
(`append_assistant_message`, `append_reasoning`, `append_history_notice`,
`delete_assistant_message`, `patch_message_metadata`, `get_message`,
`finalize_turn`, `merge_tool_delivery`) — контракт адреса и класса работы у них
тот же, но к очереди задач они не относятся и в этот перечень не входят; в дереве
тег `queue` стоит на 14 файлах.

## Inputs

- `batch: int = 1` — сколько задач взять за вызов (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1484`);
- `cursor: str | None = None` — keyset-метка предыдущей выдачи
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1485`);
- `error_retry_delay_sec` — backoff для строк в статусе `error`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1482`);
- `priority_contents` — команды, берущиеся раньше очереди (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1483`);
- `audience: str = AUDIENCE_RUNTIME` — профиль вызова (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1481`);
- имя таблицы — из `platform.json::data.task_table` (`platform.json:96`),
  оверлей профиля перекрывает его (`platform.json:83`).

## Outputs

- `ClaimedBatch` со списком задач и курсором (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1592`);
- `next_cursor` — только по полному батче, иначе `None` (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1603`);
- JSON-строка операции с ключами `claimed` и `next_cursor` (`claim_task.py:138`,
  `claim_task.py:139`);
- пустой список задач на пустой очереди — без исключения
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1632`).

## State

Очередь не хранит состояния между вызовами: `DataService` держит только
настройки (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:507` — потолок `max_rows`, `mcp-platform/servers/enterprise/capabilities/data/service/main.py:569` — имя таблицы).

Состояние захвата живёт в самой строке задачи — `status='processing'`,
проставленный тем же `UPDATE`, который её вернул (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1556`); таблицы
аренды и heartbeat нет. Курсор — единственное состояние, которое несёт вызов,
и он принадлежит вызывающей стороне, а не платформе.

## Dependencies

- пул соединений платформы: работа уходит в него одной транзакцией
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1588`), а аудитория определяет её класс;
- `mcp-platform/servers/enterprise/capabilities/data/service/registry.py::OPERATION_AUDIENCE` — единственное место, где класс работы
  записан поимённо (`mcp-platform/servers/enterprise/capabilities/data/service/registry.py:33`);
- `_fetchall_dicts` — приведение строк к словарям (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:3311`);
- `_ordered_tasks` — порядок выдачи независимо от порядка `RETURNING`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:3338`);
- реестр настроек платформы: `ENTERPRISE_TASK_TABLE` с `file_key`,
  равным `data.task_table` (`settings.py:635`).

## Configuration

- `data.task_table` — имя таблицы очереди; по умолчанию
  `public.agent_conversation_messages` (`platform.json:96`);
- оверлей профиля перекрывает его на тестовом контуре
  (`platform.json:83`); ключ профильный, его нельзя переопределить средой
  агента (`settings.py:1353`);
- `data.max_rows` — потолок батча, по умолчанию 1000 (`platform.json:107`),
  применяется как `min(batch, max_rows)` (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1537`);
- имён колонок в файле нет: список `_TASK_RETURNING` живёт в сервисе
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1467`) — объявление «на всякий случай» было бы вторым местом,
  которое обязано совпадать с первым.

## Lifecycle

1. Процесс платформы поднимает capability `data`; имя таблицы приходит из
   объявления и оверлея профиля при сборке `DataService`.
2. Вызов операции — одна транзакция: `UPDATE … RETURNING` целиком
   (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1554`), а не два обращения «прочитать — потом записать».
3. Очередь не имеет собственного фонового потока: возврат зависших инициирует
   вызывающая сторона операцией `unstick_tasks`.
4. Смерть процесса платформы останавливает и очередь; незавершённые задачи
   вернутся в `pending` по `unstick_tasks` либо при следующем захвате после
   backoff.

## Data Ownership

Владеет строкой задачи целиком: `status`, `updated_at`, `content`, `metadata`
(`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1467`). Читает и пишет её один владелец, поэтому у задачи одна
транзакционная граница статуса.

Не владеет: именем таблицы (оно объявлено, а не вычислено — `platform.json:96`),
моделью обработки задачи (она за каналом агента), пулом PostgreSQL агента
(`lib/utils/db.py:35`) — он жив и обслуживает другие подсистемы, и снос его
заблокирован задачами 2.1–2.3 change'а
`openspec/changes/2026-10-04-utils-db-pool-removal/tasks.md:43`.

## Error Behavior

- `batch` меньше единицы — `InvalidRequestError` с объяснением, что ноль не
  значит «ничего не брать» (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1529`);
- `batch` не целое — `InvalidRequestError` (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1525`);
- кривой курсор — отказ разбора курсора (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:3359`), а не «начать с головы»;
- незаданное имя таблицы — `InfrastructureError` с упоминанием
  `ENTERPRISE_TASK_TABLE` (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:577`), а не пустая очередь;
- профиль аудитории не `runtime` — `InvalidRequestError` (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1453`);
- пустая очередь — **не** ошибка: пустой список и `None`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1632`).

## Invariants

- Условие отбора присутствует и в подзапросе, и во внешнем `WHERE`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1559`, повтор — `mcp-platform/servers/enterprise/capabilities/data/service/main.py:1579`).
- В пачке не более одной задачи на чат: `DISTINCT ON (chat_id)`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1559`).
- `next_cursor` заполнен тогда и только тогда, когда выдача полная
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1603`).
- Курсор берётся из упорядоченной выдачи, а не из порядка `RETURNING`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1591`, `mcp-platform/servers/enterprise/capabilities/data/service/main.py:1603`).
- У операций очереди ровно один класс работы, и он записан поимённо
  (`mcp-platform/servers/enterprise/capabilities/data/service/registry.py:46`, `mcp-platform/servers/enterprise/capabilities/data/service/registry.py:47`).
- Потолок батча уезжает в запрос параметром (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1551`), а не подставляется
  в текст.
- Порядок параметров совпадает с порядком плейсхолдеров: backoff подзапроса,
  priority, пара курсора, потолок, backoff внешнего `WHERE`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1544`, `mcp-platform/servers/enterprise/capabilities/data/service/main.py:1551`).

## Forbidden Behavior

- `UPDATE` без повтора условия отбора во внешнем `WHERE` — тихая двойная
  обработка.
- Смещение (`OFFSET`) вместо keyset-курсора для продолжения выдачи.
- Снятие `DISTINCT ON (chat_id)` ради «ускорить» — инвариант держится ровно до
  батча шире единицы.
- `batch = 0` или отрицательное значение, трактуемое как «ничего не брать».
- Имя таблицы аргументом вызова либо значением по умолчанию в коде.
- Операция очереди, доступная профилю модели, или не записанная поимённо в
  `OPERATION_AUDIENCE`.
- Хранение состояния захвата вне строки задачи: таблица аренды, lease,
  heartbeat — их владение не объявлено.
- Пул PostgreSQL агента как второй писатель в таблицу задач.

## Consumers

- `lib/channels/queue_ops.py:191` — канал агента, единственный потребитель
  захвата; остальные пять операций очереди задач зовутся из того же шва.
- `mcp-platform/servers/enterprise/capabilities/data/service/registry.py:33` — реестр классов работы, читающий записи поимённо.
- `mcp-platform/platform.json:96` и оверлей профиля (`platform.json:83`) —
  владельцы имени таблицы.
- Модель операции не касается: профиль `model` отклоняется на входе
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1451`), а теги операции помечены `runtime-only`
  (`claim_task.py:157`).

## Implementation

Существующие на диске пути, на которых держится этот контракт:

- `mcp-platform/servers/enterprise/capabilities/data/service/main.py` —
  `DataService`, SQL захвата, курсор, `ClaimedBatch`;
- `mcp-platform/servers/enterprise/capabilities/data/tools/claim_task.py` —
  объявление операции, `INPUT_SCHEMA` (`claim_task.py:43`), теги доступа
  (`claim_task.py:157`);
- `mcp-platform/servers/enterprise/capabilities/data/service/registry.py` —
  реестр классов работы;
- `mcp-platform/platform.json` — `data.task_table` и `data.max_rows`;
- `mcp-platform/libs/enterprise_common/settings.py` — реестр настроек, ключ
  `ENTERPRISE_TASK_TABLE` (`settings.py:635`) и профильные ключи
  (`settings.py:1350`).

**Что из этого ещё не доведено.** Операции реализованы, и очередь ими
обслуживается. Шаги 3 и 4 общей раскладки выполнены: `workspace/utils/db.py`
удалён, tombstone'ов `_claim_task.py` / `_update_task_status.py` в дереве нет
(`openspec/changes/archive/2026-10-04-task-queue-platform-ops/tasks.md`).
Не сделан снос агентского пула — он обслуживает собственные таблицы агента, а не
очередь, и его судьба ведётся в `2026-10-04-utils-db-pool-removal`: снять его
нельзя раньше задач 2.1–2.3
(`openspec/changes/2026-10-04-utils-db-pool-removal/tasks.md:43`) — там же
записано, почему: пула-переёмчика в агенте нет, а `start()` вызывается до
`asyncio.run`. Пока это не сделано, «единственный владелец записи в таблицу
задач» верно для таблицы задач и неверно для процесса в целом.

## Verification

- `mcp-platform/tests/test_data_task_queue.py:264` (`TestClaimTask`) — атомарность
  и имя таблицы из объявления;
- `mcp-platform/tests/test_data_task_queue.py:365` (`TestClaimTaskGuards`) —
  профиль аудитории, класс работы, незаданное имя таблицы;
- `mcp-platform/tests/test_data_task_queue.py:418` (`TestClaimTaskBatch`) —
  одна задача на чат, пустая очередь, словарь при `batch = 1`;
- `mcp-platform/tests/test_data_task_queue.py:473`
  (`TestClaimTaskBatchBounds`) — границы батча и потолок;
- `mcp-platform/tests/test_data_task_queue.py:497` (`TestClaimTaskCursor`) —
  выдача курсора, keyset-фильтр, кривой курсор;
- `mcp-platform/tests/test_db_job_classes.py:548`
  (`TestQueueCaptureHasAJobClass`) — поимённая запись класса работы, включая
  проверку самой проверки;
- `tests/test_queue_ops_payload_contract.py:221` — сторона агента: разбор
  платформенной формы ответа.