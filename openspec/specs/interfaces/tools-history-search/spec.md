# tools-history-search Specification

## Purpose

Контракт поиска по долговечному журналу `agent_gateway_logs`, который переживает
context compaction и остаётся основным источником данных для восстановления
деталей, выпавших из контекста LLM.

Область поиска **не выбирается**: её задаёт вызывающая сторона, и модель не может
её ни увидеть, ни ослабить. Кастомный tool агента с параметром
`session_scope` снят (change `2026-10-03-mcp-native-tools`, п. D6); модель
получает операцию `mcp_enterprise_data_history_search`, которая
применяет предикаты по `session_id` и `user_id` из контекста вызова.

Изоляция данных — денормализация `user_id`, правило наследования, backfill,
индексы — в этом описании не менялась и остаётся в силе.

## Scope

`shared` — область поиска задаёт вызывающая сторона (хук в агенте, контекст
вызова на платформе), а SQL, пагинацию и изоляцию выполняет платформа.

Реализация: `lib/hooks/mcp_identity_hook.py`,
`mcp-platform/servers/enterprise/capabilities/data/tools/history_search.py`,
`mcp-platform/servers/enterprise/capabilities/data/service/main.py`.
Вызов: `mcp_enterprise_data_history_search`.

## Requirements

### Requirement: Scope is the caller session intersected with the caller user

Область поиска SHALL определяться вызывающей стороной и SHALL быть обязательной.
Это заменяет прежний выбор `current` / `all` и делает его невозможным по
построению.

Личность вызова SHALL формироваться на стороне агента хуком `McpIdentityHook` —
безусловной подстановкой `session_id`, `user_id` и `request_id` в аргументы
вызова — и SHALL читаться платформой в контекст вызова. Операция SHALL применять
предикат по `session_id` **и** по `user_id`, каждый из которых берётся из
контекста вызова, а не из аргументов.

Возможность выбрать область SHALL NOT существовать ни в опубликованной схеме,
ни среди неявных умолчаний.

Значения SHALL совпадать с тем, что пишет журнал: `session_id` — ключ сессии
оборота, `user_id` — отправитель оборота (`RequestContext.sender_id`).
Несогласованные значения не должны возникать, потому что оба берутся из одного
оборота.

#### Scenario: Пересечение, а не выбор

- **GIVEN** вызов с `session_id = "telegram:123"`, `user_id = "alice"`
- **WHEN** операция строит SQL
- **THEN** она SHALL добавить предикаты `session_id = %s` и `user_id = %s`
- **AND** SHALL NOT добавлять условие вида `OR TRUE`

#### Scenario: Чужие события не видны ни по одному измерению

- **GIVEN** в журнале есть события с `user_id="alice"` и с `user_id="bob"`
- **WHEN** alice вызывает операцию
- **THEN** ответ SHALL NOT содержать ни одного события с `user_id="bob"`
- **AND** ответ SHALL NOT содержать событий других сессий той же alice

#### Scenario: Область не отключается аргументами

- **WHEN** модель передаёт значения, которые прежний tool трактовал как отключение фильтра
- **THEN** операция SHALL применить предикаты по контексту вызова
- **AND** значения модели SHALL NOT попасть в предикаты

#### Scenario: Выбор области вызовами невозможен

- **WHEN** модель формирует аргументы `mcp_enterprise_data_history_search`
- **THEN** в них SHALL NOT быть параметра, задающего область поиска
- **AND** предикат по `session_id` SHALL применяться всегда

### Requirement: operation identity and scope

Операция `data.history_search` SHALL читать таблицу журнала, имя и схема которой
задаются `platform.json`. Операция MUST NOT выполнять произвольный SQL,
обращаться к другим таблицам или делать `INSERT`/`UPDATE`/`DELETE`. Все
параметры запроса, включая текстовые, MUST передаваться позиционными
`%s`-параметрами без интерполяции в SQL-строку.

#### Scenario: parameters are bound via placeholders

- **WHEN** операция исполняется с произвольными `query`, `event_type`,
  `tool_name`, `since`, `until`, `limit`, `offset`
- **THEN** результирующий SQL SHALL использовать только `%s`-параметры
- **AND** SHALL NOT содержать интерполяцию пользовательских значений в строку
  запроса

#### Scenario: Агент SQL не строит

- **WHEN** модель вызывает `mcp_enterprise_data_history_search`
- **THEN** агент SHALL NOT строить SQL
- **AND** агент SHALL NOT обращаться к таблице журнала напрямую

#### Scenario: data.history_search is read-only

- **WHEN** операция исполняется
- **THEN** её SQL SHALL содержать только `SELECT`
- **AND** SHALL NOT содержать `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`
  или DDL

### Requirement: missing identity is a hard error

Проверки личности стоят на двух уровнях, и разделение существенно: доменная
проверка и проверка на границе конвейера отвечают на разные вопросы.

**Уровень конвейера.** Вызов без полной личности SHALL быть отвергнут ДО входа в
домен кодом `identity_missing` (см. `openspec/specs/runtime/call-contract`).
Для вызовов модели личность подставляет `McpIdentityHook`; неполная — вызов
уходит без неё и отвергается.

**Уровень сервиса.** Поиск без области видимости SHALL быть отвергнут:
`DataService.history_search` SHALL отказать, если не заданы **ни** `user_id`,
**ни** `session_id`. Наличие одного из двух допустимо, отсутствие обоих — нет:
«покажи всё» в журнале, где лежат вопросы пользователей и внутренние события,
— это утечка, а не удобный режим.

#### Scenario: Вызов без личности отвергнут на границе

- **WHEN** модель вызывает операцию, а личность оборота неполна
- **THEN** конвейер SHALL отказать кодом `identity_missing`
- **AND** доменный обработчик SHALL NOT выполниться

#### Scenario: Поиск без области отвергнут сервисом

- **WHEN** `data.history_search` вызван без `user_id` и без `session_id`
- **THEN** сервис SHALL отказать `InvalidRequestError`
- **AND** SQL к журналу SHALL NOT быть выполнен

#### Scenario: Одной из двух частей области достаточно

- **WHEN** `data.history_search` вызван с заданным `session_id` и пустым `user_id`
- **THEN** сервис SHALL построить предикат только по `session_id`
- **AND** SHALL NOT построить unscoped условие

### Requirement: no unscoped fallback and no identity derivation

Операция MUST NOT реализовывать unscoped fallback вида
`WHERE (%s IS NULL OR user_id = %s)`. Операция MUST NOT извлекать `user_id` из
`session_id`, `chat_id`, `actor`, `payload`, `name` или любого другого поля
события. Источник идентичности для фильтрации — единственный: контекст вызова,
собранный из личности оборота.

#### Scenario: no user_id derivation from session_id

- **GIVEN** вызов с `session_id = "telegram:123"`
- **WHEN** операция строит предикаты
- **THEN** она SHALL NOT парсить `session_id` и выводить из него `user_id`
- **AND** при пустом `user_id` в контексте SHALL оставить только предикат по
  `session_id`, а не отказать и не ослабить область

#### Scenario: Агентская подстановка личности безусловна

- **WHEN** модель передала `user_id` в аргументах вызова
- **THEN** хук SHALL заменить его значением отправителя оборота
- **AND** смешивания источников SHALL NOT происходить

### Requirement: actor is not a user identity

Операция MUST NOT интерпретировать `actor` (`user`/`agent`/`system`/`sync`) как
идентификатор пользователя. `actor` — роль источника события, а не
пользователь. Поле `user_id` — единственный идентификатор пользователя в
`agent_gateway_logs`.

#### Scenario: actor=user and actor=agent do not change scope

- **GIVEN** в выборке есть события с `actor="user"` и `actor="agent"`, оба с
  `user_id="alice"`
- **WHEN** alice вызывает операцию
- **THEN** оба типа событий SHALL быть возвращены
- **AND** ни одно событие другого `user_id` SHALL NOT быть возвращено

### Requirement: operation API has no identity parameter

Операция SHALL NOT принимать `user_id` (ни прямо, ни косвенно через любой
другой параметр) и SHALL NOT принимать параметр области поиска. `user_id` —
внутренний security attribute, получаемый из контекста вызова. Модель SHALL NOT
иметь возможности выбрать security boundary через параметр операции.

#### Scenario: schema has no user_id parameter

- **WHEN** операция публикует свою JSON-схему
- **THEN** в `properties` SHALL NOT быть поля `user_id` (или эквивалента вида
  `principal_id` / `actor_id` / `owner_id`)
- **AND** в `required` SHALL NOT быть такого поля

#### Scenario: Схема не содержит выбора области

- **WHEN** операция публикует свою JSON-схему
- **THEN** в `properties` SHALL NOT быть параметра, отключающего предикат по
  `session_id` или по `user_id`

#### Scenario: Схема строится из сигнатуры обработчика

- **WHEN** строится опубликованная схема операции
- **THEN** параметр контекста вызова SHALL NOT попасть в `properties`
- **AND** идентичность SHALL NOT быть полем, которое модель заполняет

### Requirement: RequestContext exposes user identity

Источник `user_id` MUST быть `nanobot.agent.tools.context.RequestContext` через поле
`sender_id: str | None`. В агенте единственное обращение к нему —
`McpIdentityHook._sender_id()`: зависимость от конкретной версии nanobot
изолирована в одной функции, и переименование поля чинит одно место.

Если в будущей версии nanobot поле будет переименовано, обратная совместимость
через alias **не** вводится: адаптация делается в отдельном change, который
обновляет зависимость и `_sender_id()` вместе с этим requirement.

#### Scenario: Единственное обращение к identity-store — в хуке

- **WHEN** личность оборота вычисляется для вызова операции
- **THEN** обращение к `RequestContext.sender_id` SHALL происходить в
  `McpIdentityHook._sender_id()`
- **AND** в других местах агента прямой доступ к `sender_id` SHALL NOT
  использоваться для построения вызова операции

#### Scenario: Контракт-тест nanobot-API на месте

- **GIVEN** nanobot установлен согласно `requirements.txt`
- **WHEN** выполняется `tests/contract/test_history_search_identity_contract.py`
- **THEN** импорт `nanobot.agent.tools.context.RequestContext` SHALL быть успешным
- **AND** итерация `dataclasses.fields(RequestContext)` SHALL содержать `sender_id`
- **AND** аннотация `sender_id` SHALL быть совместима с `str | None`
- **AND** тест SHALL падать при отсутствии поля или несовместимой аннотации

### Requirement: Параметры запроса

Операция SHALL принимать следующие параметры; все, кроме `query`, опциональны.
Значения — доменные, идентичности среди них нет.

- `query` — подстрока для регистронезависимого поиска (`ILIKE`) по `summary`
  и `payload::text` журнала; пустая строка фильтр не добавляет.
- `event_type` — строка типа события; применяется как точный фильтр. Значение
  вне перечня SHALL давать пустую выборку, а не ошибку валидации: перечень
  закрыт и живёт в платформе, а модель его не видит.
- `level` — уровень журнала; приводится к регистру, принимаемому
  CHECK-ограничением, иначе SHALL быть отвергнут.
- `tool_name` — строка-имя инструмента, применяется к колонке `name`.
  Смысленно вместе с типами событий инструмента, но фильтр применяется и без
  них: вызывающий не обязан знать схему журнала.
- `since` / `until` — ISO-8601 таймстампы; обе границы SHALL трактоваться как
  inclusive (`>=` / `<=`).
- `limit` — максимум событий в ответе; приводится к диапазону `1..max_rows`,
  где `max_rows` — потолок платформы.
- `offset` — целое ≥ 0; пропуск первых `offset` событий после сортировки.
  Дефолт 0. Фильтры области применяться SHALL NOT переставать.

#### Scenario: Фильтр по event_type + tool_name

- **WHEN** вызвано `data.history_search(event_type="tool.started", tool_name="data.history_search", limit=3)`
- **THEN** SHALL быть возвращено не более 3 событий, у которых
  `event_type='tool.started'` И `name='data.history_search'`
- **AND** они SHALL быть отсортированы по `(timestamp DESC, id DESC)`

#### Scenario: Неизвестный event_type даёт пустую выборку

- **WHEN** передано `event_type`, которого нет в словаре типов событий
- **THEN** операция SHALL вернуть success с пустым `hits`
- **AND** ошибки валидации SHALL NOT быть

#### Scenario: since/until inclusive

- **WHEN** передано `since="2024-01-01T00:00:00Z", until="2024-01-02T00:00:00Z"`
- **THEN** в выборку включаются события с
  `2024-01-01T00:00:00Z <= timestamp <= 2024-01-02T00:00:00Z`

#### Scenario: limit приводится к допустимому диапазону

- **WHEN** передано `limit` меньше 1 или больше потолка платформы
- **THEN** операция SHALL привести его к границе диапазона
- **AND** SHALL NOT отказать

### Requirement: pagination and ordering

Операция SHALL поддерживать параметр `offset` (целое ≥ 0, дефолт 0).
Результирующий SQL SHALL использовать
`ORDER BY "timestamp" DESC, "id" DESC LIMIT %s OFFSET %s`, где
`LIMIT = effective_limit + 1` — лишняя строка определяет наличие следующей
страницы и не возвращается вызывающей стороне.

#### Scenario: deterministic ordering

- **WHEN** операция выполняет SQL с двумя и более событиями с одинаковым `timestamp`
- **THEN** порядок SHALL быть детерминирован по `id DESC`

#### Scenario: Есть следующая страница

- **WHEN** в выборке больше `limit` строк
- **THEN** `hits` SHALL содержать ровно `limit` элементов
- **AND** `next_offset` SHALL равняться `offset + limit`
- **AND** `truncated` SHALL быть `false`, пока потолок не достигнут

#### Scenario: Следующей страницы нет

- **WHEN** в выборке меньше `limit + 1` строк
- **THEN** `next_offset` SHALL быть `null`

#### Scenario: Выборка упёрлась в потолок

- **WHEN** число возвращённых строк достигло `max_rows`
- **THEN** `truncated` SHALL быть `true`

#### Scenario: Детерминированный порядок страниц

- **WHEN** в одном батче записано 5 событий с одинаковым `timestamp`; `limit=2`;
  вызов с `offset=0` возвращает события A и B
- **THEN** вызов с `offset=2` SHALL вернуть события C и D, а не «D и B снова»
  — **для неизменного набора подходящих строк**

#### Snapshot-consistency

Система SHALL NOT гарантировать snapshot-consistency между независимыми
вызовами при появлении новых записей между запросами: `offset`-пагинация может
сдвинуться, и страница, начатая ранее, может пересечься с только что вставленными
событиями. Строгая консистентность — отдельный future change (cursor-пагинация).

### Requirement: response shape and no user_id leak

Операция SHALL возвращать JSON-объект с полями `hits`, `next_offset`,
`truncated`. Каждый элемент `hits` SHALL содержать `id`, `timestamp`,
`event_type`, `name`, `level`, `summary`, `payload`. Поле `payload` SHALL быть
объектом (JSONB декодируется на чтении), а не JSON-строкой.

Операция SHALL NOT возвращать `user_id` ни в корне ответа, ни в элементах
`hits`. В текущей форме ответа `user_id` не появляется **вовсе**: он служит
только предикатом поиска и в выборку не попадает.

#### Scenario: форма ответа соответствует операции

- **WHEN** операция возвращает результат
- **THEN** корень SHALL содержать `hits`, `next_offset` и `truncated`
- **AND** каждый элемент `hits` SHALL содержать `id`, `timestamp`, `event_type`,
  `name`, `level`, `summary`, `payload`
- **AND** `payload` SHALL быть объектом, а не JSON-строкой

#### Scenario: response does not leak user_id

- **WHEN** операция возвращает JSON-ответ
- **THEN** ни на одном уровне (корень, элементы `hits`, `payload`) SHALL NOT
  быть поля `user_id`
- **AND** `session_scope` SHALL NOT быть полем ответа

#### Scenario: Пустой результат — честный success

- **WHEN** совпадений нет
- **THEN** операция SHALL вернуть `{"hits": [], "next_offset": null,
  "truncated": false}`
- **AND** отказа SHALL NOT быть

### Requirement: Схема payload по event_type

Система SHALL задокументировать в `workspace/TOOLS.md` JSON-схему `payload` для
действующих типов событий. Перечень SHALL соответствовать словарю типов
платформы (`mcp-platform/libs/enterprise_common/eventing/types.py`).

Префиксы `agent.`, `llm.`, `tool.`, `artifact.`, `quality.` SHALL использоваться
вместе с конкретным именем (`tool.started`, `agent.compacted`,
`llm.exchanged`), а опечатка в последнем компоненте SHALL отвергаться проверкой
словаря, а не плодить новый тип.

Имена прежней редакции — `context_compacted`, `tool_call`, `tool_result`,
`llm_call`, `run_finished`, `subagent_run_finished`, `inbound` — устарели и
SHALL NOT использоваться как действующие.

#### Scenario: Документация использует действующие имена

- **WHEN** `workspace/TOOLS.md` приводит примеры фильтра по `event_type`
- **THEN** приведённые имена SHALL принадлежать словарю типов платформы
- **AND** устаревшие имена из прежней редакции спеки SHALL NOT встречаться

#### Scenario: Агент читает результат инструмента

- **WHEN** агент получает событие типа инструмента
- **THEN** он может предсказуемо прочитать `payload.tool` и `payload.status`
  напрямую, без `json.loads`

### Requirement: user_id in agent_gateway_logs (denormalization)

Колонка `user_id` SHALL присутствовать в `agent_gateway_logs`. Это **намеренное
исключение** из прежнего правила «identity живёт только в
`agent_question_runs»»: `user_id` стал security boundary для чтения событий, и
без него поиск вёл бы JOIN на каждый проход. Денормализация оправдана, потому
что:

- колонка держится в синхронности через `DbLoggingService` (единственный writer);
- `agent_question_runs.user_id` остаётся первичным источником правды; при
  расхождении — он выигрывает.

#### Scenario: agent_gateway_logs has user_id column

- **GIVEN** DDL `agent_gateway_logs`
- **THEN** таблица SHALL содержать колонку `user_id VARCHAR(256)` рядом с
  `request_id`/`session_id`/`channel`/`actor`/`name`
- **AND** колонка SHALL быть задокументирована через `COMMENT ON COLUMN` с
  явным указанием источника

### Requirement: user_id inheritance rule

`LogEvent.user_id` MUST доходить до колонки `agent_gateway_logs.user_id`.
Решение о том, какое значение записать, определяется тремя ветвями,
проверяемыми в `_enqueue` в указанном порядке:

1. **Explicit value wins.** Если `event.user_id` явно задано producer'ом —
   используется оно, индекс не читается.
2. **Match by request_id.** Если `event.user_id is None` AND
   `event.request_id is not None` AND `event.session_id` присутствует в индексе
   AND `index[event.session_id]["request_id"] == event.request_id` —
   подставляется `index[event.session_id]["user_id"]`.
3. **No inference.** Во всех остальных случаях `event.user_id` остаётся `None`.
   Событие записывается с `user_id IS NULL` и не попадает в результаты поиска,
   отфильтрованные по `user_id`.

Правило MUST NOT быть смягчено: если `event.request_id is None` — `user_id` НЕ
выводится только по `session_id`. Это закрывает класс атак вида «событие без
identity получает текущего пользователя сессии».

#### Scenario: explicit LogEvent.user_id overrides the index

- **GIVEN** индекс содержит `{"request_id": "req-1", "user_id": "alice"}` для
  `session_key="telegram:123"`
- **WHEN** эмитится `LogEvent(event_type="tool.started", session_id="telegram:123",
  request_id="req-1", user_id="bob")`
- **THEN** `DbLoggingService` SHALL записать строку с `user_id="bob"`

#### Scenario: empty user_id is filled when request_id matches

- **GIVEN** `register_request(session_key="telegram:123", request_id="req-1",
  user_id="alice")` уже был вызван
- **AND** эмитится `LogEvent(..., request_id="req-1", user_id=None)`
- **THEN** `DbLoggingService` SHALL записать строку с `user_id="alice"`

#### Scenario: stale event does not inherit next request's user_id

- **GIVEN** `register_request(session_key="telegram:123", request_id="req-A",
  user_id="alice")` уже был вызван
- **AND** продюсер создал отложенный `LogEvent` с `request_id="req-A"` и
  `user_id=None`
- **AND** между созданием события и его `_enqueue` выполнен
  `register_request(session_key="telegram:123", request_id="req-B", user_id="bob")`
- **WHEN** этот отложенный `LogEvent` ставится в очередь
- **THEN** записанная строка SHALL иметь `user_id="alice"`, а не `"bob"`

### Requirement: register_request atomically updates identity

`register_request` SHALL обновлять индекс идентичности под локом и SHALL
записывать `user_id` вместе с `request_id` в одной операции. Расхождение, при
котором `request_id` обновился, а `user_id` — нет, делает правило наследования
непредсказуемым.

#### Scenario: user_id и request_id пишутся вместе

- **WHEN** `register_request` вызывается с новым `request_id`
- **THEN** индекс SHALL содержать новый `request_id` и переданный `user_id`
  одновременно
- **AND** наблюдатель SHALL NOT увидеть состояние с новым `request_id` и старым
  `user_id`

### Requirement: subagent propagates parent user_id explicitly

События субагента SHALL нести `user_id` родительского оборота явно. Полагаться
на наследование по `request_id` здесь нельзя: у субагента своя ветвь оборота, и
совпадение `request_id` не гарантировано.

#### Scenario: Событие субагента не теряет пользователя

- **GIVEN** субагент выполняется в обороте пользователя `alice`
- **WHEN** эмитится событие субагента
- **THEN** записанная строка SHALL иметь `user_id="alice"`

### Requirement: backfill historical events by request_id

События, записанные до появления `user_id` в журнале, SHALL быть восстановлены
связкой по `request_id` через `agent_question_runs`. Совпадение SHALL
устанавливаться по `request_id`, а не по `session_id`: у сессии много оборотов,
и привязка по ней приписала бы событие чужому вопросу.

#### Scenario: Событие без user_id восстанавливается по request_id

- **GIVEN** в `agent_question_runs` есть строка с `request_id="req-1"`,
  `user_id="alice"`
- **AND** в `agent_gateway_logs` есть событие с `request_id="req-1"` и
  `user_id IS NULL`
- **WHEN** выполняется backfill
- **THEN** событие SHALL получить `user_id="alice"`

### Requirement: index for scope access

DDL SHALL содержать индекс на `agent_gateway_logs`, обслуживающий
access-pattern `WHERE user_id = ? AND session_id = ? ORDER BY "timestamp" DESC`.
Имя индекса и колонки — на усмотрение реализации; требование — индекс
**существует и совместим** с этим pattern'ом.

Индекс по `session_id` SHALL NOT использоваться как замена пользовательскому
фильтру, и наоборот: наличие одного из двух предикатов SHALL NOT позволять
обойти другой.

#### Scenario: DDL provides index for the intersection

- **GIVEN** DDL `agent_gateway_logs`
- **THEN** SHALL существовать индекс, обслуживающий фильтр по `user_id` и
  `session_id` с сортировкой по `"timestamp" DESC`
- **AND** его назначение SHALL быть задокументировано через `COMMENT ON INDEX`

## Responsibility

Спека отвечает за **изоляцию области поиска** и за целостность личности в
журнале. Это операция платформы, но предмет пересекает обе стороны: агент
подставляет личность, платформа ограничивает ею выборку.

Владелец по `## Scope` — предмет живёт на двух сторонах: операция и DDL
в `mcp-platform/`, подстановка личности и запись событий — в агенте
(`lib/`). Обе стороны описаны, потому что изоляция держится только в
связке: личность, пришедшая в `_meta`, ограничивает выборку, а выборка
без идентификатора обязана быть отвергнута, а не сужена «по умолчанию».

Пять обязанностей:

1. область поиска = **пересечение** сессии вызова и пользователя вызова,
   а не выбор одного из двух;
2. отсутствие личности — жёсткий отказ, а не отказ с сужением;
3. операция не принимает параметров области — модель не может её выбрать;
4. событие журнала всегда подписано `user_id` (правило наследования);
5. ответ не выдаёт `user_id` обратно.

## Boundary

**Граница — операция платформы `data.history_search` и её вход.**

Внутри границы:

- операция — `mcp-platform/servers/enterprise/capabilities/data/tools/history_search.py`;
- чтение — `DataService.history_search`
  (`mcp-platform/servers/enterprise/capabilities/data/service/main.py:1314`);
- DDL — `sql/migrations/V004__agent_gateway_logs_user_id.sql` и семейство
  `V008`/`V009`;
- подстановка личности — `lib/hooks/mcp_identity_hook.py`;
- запись событий — `lib/services/db_logging_service.py`.

Вне границы:

- **выборка чего угодно, кроме журнала.** Операция читает
  `agent_gateway_logs` и только;
- **`agent_question_runs`** — источник истины по `user_id`
  (см. комментарий в `V004`), но операцией не читается;
- **`agent_conversation_messages`** — журнал диалога, не журнал событий;
- **сборка SQL агентом.** Запрос строится на платформе, а модель его не
  видит;
- **права на аудитории** (`AUDIENCE_MODEL` против `AUDIENCE_RUNTIME`) —
  отдельная тема `platform.json → job_classes`; операция объявляет
  `audience=AUDIENCE_MODEL` явно
  (`history_search.py:57`), но предметом спеки не является.

## Public Contract

**Имя операции — `data.history_search`.** По коду это поле `name=` у
`ToolDefinition` (`history_search.py:80`), capability — `"data"`
(`history_search.py:86`). Модели операция приходит как
`mcp_enterprise_data_history_search`; имя объявлено в
`config.json → tools.mcpServers.enterprise.enabled_tools` как
`data.history_search`.

Проверено поиском по дереву: строковый литерал
`"mcp_enterprise_history_search"` в коде объявления операции не
используется — операция объявляется под именем `data.history_search`, а
имя для модели получается по правилу
`mcp_enterprise_<capability>_<operation>`. Запись без префикса `data_`
встречается ровно в одном месте — в докстринге
`tests/test_journal_event_name_alignment.py:542`, где она описана как
история перехода; на поведение это не влияет, но при чтении теста
воспринимается как актуальное имя.

**Сигнатура обработчика** (`history_search.py:27-37`) — девять параметров,
ни один из которых не про область:

```
ctx: ToolExecutionContext      # подпись личности, не поле схемы
query: str = ""
event_type: str | None = None
level: str | None = None
tool_name: str | None = None
since: str | None = None
until: str | None = None
limit: int = 50
offset: int = 0
```

**Форма ответа** (`history_search.py:59-77`) — JSON с тремя ключами:
`hits` (список по семь полей: `id`, `timestamp`, `event_type`, `name`,
`level`, `summary`, `payload`), `next_offset`, `truncated`.
Сериализация — `ensure_ascii=False`.

**Политики качества и теги:** `quality_policy="vector_result"`
(`history_search.py:88`), `tags=("logging", "infrastructure")`
(`history_search.py:87`).

## Inputs

Вход делится на две части, и это деление — суть предмета.

**Задаётся вызывающей стороной, попадает в контекст, не в схему:**

- `ctx.session_id` и `ctx.user_id` — читаются в обработчике и
  передаются в сервис как `session_id=ctx.session_id`,
  `user_id=ctx.user_id` (`history_search.py:51-52`). Докстринг
  `history_search.py:40-45` объясняет, почему их нельзя объявлять
  параметрами обработчика: в опубликованной схеме они стали бы полями,
  которые заполняет модель, а область обязана задаваться вызывающей
  стороной.

**Задаётся моделью (публикуется в схеме):**

- `query` — подстрока, ищется по `summary ILIKE` и по `payload::text ILIKE`
  (`main.py:1370-1373`), оба с `%…%`;
- `event_type`, `level`, `tool_name`, `since`, `until` — точные фильтры;
  `level` приводится `normalize_level` — **тем же** регистром, что при
  записи (`main.py:1357`), иначе фильтр молча ничего не находит;
- `limit` (дефолт 50) прижимается к `[1, max_rows]`
  (`main.py:1339`), `offset` — к неотрицательному (`main.py:1340`);
- `offset` — страницовая навигация.

## Outputs

Выход — строка JSON, как у любой операции платформы
(`history_search.py:59-77`). Структура:

| Ключ | Тип | Смысл |
|---|---|---|
| `hits` | список объектов | по семь полей на событие |
| `next_offset` | число | смещение следующей страницы |
| `truncated` | булево | выборка упёрлась в потолок |

Два свойства выхода, зафиксированные требованиями и подтверждённые кодом:

1. **`user_id` не выдаётся наружу.** Ни одно из семи полей `hits` не
   содержит его — это видно из литерала ответа
   (`history_search.py:61-71`). Требование «response does not leak
   user_id» выполняется структурно, а не фильтрацией.
2. **Пустой результат — честный success:** `{"hits": [],
   "next_offset": null, "truncated": false}`, а не отказ. Отказ означал бы
   «что-то сломалось», а пустая выборка — это ответ на вопрос.

## State

Предмет **не владеет состоянием**: операция только читает.

Читаемое состояние:

- таблица `agent_gateway_logs`, имя и схема которой резолвятся
  `_require_log_table("search_logs")` (`main.py:1375`) — то есть
  перекрытие профиля (`platform.json → profiles.<имя> → data.log_table`)
  влияет и на чтение;
- индекс `agent_gateway_logs_user_id_timestamp_idx`
  (`sql/migrations/V004__agent_gateway_logs_user_id.sql`) — access-pattern
  `WHERE user_id = ? ORDER BY "timestamp" DESC`.

Состояние, которое предмет **поддерживает на записи**, но не читает:
колонка `user_id` в `agent_gateway_logs`, объявленная той же миграцией
`V004`, и правило её заполнения, реализованное в
`lib/services/db_logging_service.py::_resolve_event_user_id`
(строка 1739).

Два наблюдения о `user_id`, которые стоит знать:

- колонка **nullable** (`ADD COLUMN IF NOT EXISTS user_id VARCHAR(256)`),
  и событие, которому нечем подписаться, пишется с `user_id IS NULL` и
  просто не участвует в выборке по пользователю
  (`db_logging_service.py:1798-1800`);
- `user_id` — **денормализованная копия** из `agent_question_runs`, где он
  первичен. Это зафиксировано комментарием к миграции `V004` и нарушение
  single-writer-инварианта (`DbLoggingService` — единственный writer
  журнала, `V004` — только backfill).

Внутренних структур состояния у предмета нет: ни кеша выборок, ни курсоров,
ни пагинационных курсоров между вызовами. Каждая страница — независимый
запрос.

## Dependencies

Прямые, все в `mcp-platform/`:

- `libs.enterprise_common.container.ToolContainer` — доступ к сервису
  (`history_search.py:12`, `25`);
- `libs.enterprise_common.execution.context.ToolExecutionContext` — носитель
  личности (`history_search.py:13`);
- `libs.enterprise_common.registry.ToolDefinition`,
  `build_input_schema` — объявление операции (`history_search.py:14`);
- `DataService` и константа `AUDIENCE_MODEL` из
  `mcp-platform/servers/enterprise/capabilities/data/service/main.py`
  (`history_search.py:15-18`);
- `libs.enterprise_data.jsonb.decode_jsonb` — разбор `payload`;
- пул соединений платформы — через `submit(...)` с `audience`
  (`main.py:1387`).

Зависимость, замыкающая контракт, но лежащая в агенте:
`lib/hooks/mcp_identity_hook.py` подставляет `session_id`/`user_id` в
`params` вызова (`mcp_identity_hook.py:174`), поэтому без него операция
получает пустую область и отвергает вызов. Формальной зависимости между
деревьями нет — есть контракт, проверяемый
`tests/contract/test_history_search_identity_contract.py`.

## Configuration

Настроек у операции нет: она не читает ни одного ключа конфигурации и не
принимает их в аргументах.

Параметры, влияющие на её поведение, объявлены вне её:

- **таблица журнала** — `platform.json → data.log_table`, и профиль может
  её переопределить (`platform.json → profiles.<имя>`); операция берёт её
  через `_require_log_table("search_logs")` (`main.py:1375`). Именно
  поэтому перекрытие профиля проверяется на старте: читать из одной
  таблицы, а писать в другую — значит показывать пустую выдачу;
- **потолок выборки** — `max_rows` конструктора `DataService`
  (дефолт 1000, `main.py:465`), то есть конфигурация платформы, а не
  аргумент вызова;
- **идентичность на стороне агента** — результат
  `McpIdentityHook.before_execute_tool`; выключить подстановку ключом
  конфигурации нельзя.

Практический вывод: изменить область поиска переключателем **нельзя ни
одним способом**, и это ровно то свойство, которое спека защищает (см.
`## Invariants`, пункт 2).

## Lifecycle

Операция **не имеет собственного жизненного цикла**: она не создаёт
соединений, не держит ресурсов и не пишет. Её цикл совпадает с процессом
платформы; каждый вызов — независимый транзакционный запрос пула.

Что происходит за один вызов:

1. `create_tool` замыкает сервис в момент сборки
   (`history_search.py:24-25`) — это сознательное решение: модульная
   глобальная переменная зависела бы от порядка регистрации, и вторая
   регистрация тихо переписала бы первую
   (`history_search.py:21-23`);
2. проверка области — в сервисе, до SQL (`main.py:1335-1338`);
3. приведение `limit`/`offset` и сборка условий (`main.py:1339-1373`);
4. `submit(...)` — работа пула с `audience=AUDIENCE_MODEL` и
   `statement_timeout` по классу работы (`main.py:1387`, `804-819`);
5. детект следующей страницы: `LIMIT N+1`, лишняя строка отбрасывается
   (`main.py:1385-1389`).

Асимметрия жизненных циклов, ради которой стоит читать этот раздел:
**подпись личности живёт на стороне агента, выборка — на стороне
платформы.** Обе фазы обязаны присутствовать; отказ в любой из них даёт
разные результаты — отказ хука (модель получает синтетический
tool-результат с объяснением) или отказ сервиса (тот же исход, но с
кодом `invalid_request`). Ни одна из них не деградирует в «покажи всё».

## Data Ownership

Владелец данных журнала — **`DbLoggingService`** (агент) плюс пул
платформы на записи. Операция — чистый читатель и не становится владельцем
ничего.

| Данные | Владелец | Что делает операция |
|---|---|---|
| `agent_gateway_logs` | `DbLoggingService` (единственный writer) | читает по `session_id`/`user_id` |
| `agent_question_runs.user_id` | `DbLoggingService` (`register_request`) | не читает; служит источником истины для backfill |
| `agent_gateway_logs.user_id` | денормализация из `question_runs` | читает как фильтр |

Правило, которое спека делает нормативным и которое легко нарушить
неосторожно: **поиск без области — не «широкий поиск», а отказ.** Реализация
единственная и она на второй строке метода
(`main.py:1335-1338`): нет ни `user_id`, ни `session_id` →
`InvalidRequestError`. Обход этого запрещён структурно, потому что модель
не может передать ни одного из этих двух параметров — их нет в схеме.

Отдельная оговорка о владении на записи: `V004` была написана так, чтобы
**не размножать writer'ов** — `ADD COLUMN IF NOT EXISTS`, один backfill
`UPDATE … WHERE l.request_id = r.request_id AND l.user_id IS NULL`,
`CREATE INDEX IF NOT EXISTS`, и всё. Повторное применение идемпотентно, а
совместное применение V004 и backfill'а кода считается нарушением
single-writer (это написано в шапке миграции).

## Error Behavior

Три различимых отказа, все на стороне платформы; агентский отказ
описан отдельно.

**1. Нет области** (`main.py:1335-1338`) → `InvalidRequestError` с текстом
«нужен user_id или session_id: поиск по журналу без области видимости
запрещён». Проверка стоит **до** нормализации `limit` и до сборки SQL,
то есть до обращения к базе вообще.

**2. Нет части личности на границе вызова** — агентский отказ в
`McpIdentityHook.before_execute_tool` (`mcp_identity_hook.py:146-171`):
если не собралась личность оборота, поднимается `McpIdentityRefused`,
модель получает синтетический tool-результат с объяснением, что повтор не
поможет. Исключение наследует `RepeatGuardBlocked`, и это не совпадение:
существующий патч `repeat_guard_block` превращает такой отказ в
синтетический результат вместо обрыва оборота
(`mcp_identity_hook.py:84-99`).

**3. Неполная личность** (`mcp_identity_hook.py:180-204`): отсутствие
`session_id` или `user_id` — отказ; отсутствие `request_id` — **не** отказ,
потому что вызывающая сторона не обязана иметь идентификатор конверта.
Отсутствующий `request_id` досылается отдельно
(`mcp_identity_hook.py:206-219`).

**4. На стороне записи** — отказов нет: `_resolve_event_user_id` при
невозможности подписи оставляет `user_id` равным `None` и **не роняет
событие** (`db_logging_service.py:1798-1800`). Это отказ от подписи, а не
потеря строки.

Особый случай — выравнивание регистра `level`: приведение выполняется на
той же стороне, что и запись, иначе фильтр молча не нашёл бы ничего
(`main.py:1355-1357`). Это не отказ, а предотвращение ложного пустого
результата.

Чего **нет**: ни одного «мягкого» режима, ни флага
`session_scope="all"`, ни параметра, ослабляющего область. Снятие
`session_scope` отражено в прозе спеки (строка 11) и подтверждено
отсутствием параметра в сигнатуре.

## Invariants

1. **Область — пересечение, а не выбор.** Оба предиката добавляются в
   `clauses` независимо (`main.py:1344-1349`); при двух заданных значениях
   работает `AND`. Наличие одного из двух **не** даёт права не применять
   другой — это прямо оговорено требованием «index for scope access».
2. **Модель не может выбрать область.** В схеме нет ни `user_id`, ни
   `session_id`, ни какого-либо `scope`; схема строится из сигнатуры
   обработчика, где параметров области нет
   (`history_search.py:27-37`, `89`).
3. **Поиск без области — отказ, а не пустая выдача**
   (`main.py:1335-1338`).
4. **Личность выводится только из оборота.** Единственное обращение к
   identity-store — в хуке (`mcp_identity_hook.py:193-204`); вывода
   `user_id` из `session_id` нет ни в агенте, ни на платформе.
5. **`user_id` не выводится только по `session_id`** — это закрывает класс
   атак «событие-сирота получает текущего пользователя сессии»
   (`db_logging_service.py:1748-1749`).
6. **Явный `LogEvent.user_id` имеет приоритет над индексом** — резолвер
   выходит сразу, если значение уже задано
   (`db_logging_service.py:1754-1755`).
7. **Событие без `request_id` подписывается снимком личности входа** — но
   только если снимок сам не содержит вопроса и появился **до** события
   (`db_logging_service.py:1813-1819`). Вторая сверка обязательна: без неё
   отложенное событие унаследовало бы личность следующего входа той же
   сессии.
8. **Ответ не содержит `user_id`.** Ни одного из семи полей `hits`
   (`history_search.py:61-71`).
9. **Порядок детерминирован:** `ORDER BY "timestamp" DESC, id DESC`
   (`main.py:1382`) — сортировка по id обязательна, иначе страницы могли бы
   повторять и терять строки на равных метках времени.
10. **Наличие следующей страницы детектируется одним запросом** через
    `LIMIT N+1` (`main.py:1385-1388`), без отдельного счётчика.
11. **`actor` не является личностью пользователя** и область не меняет.
12. **Субагент наследует `user_id` родителя явно**, а не по индексу.

## Forbidden Behavior

1. **Возвращать события при отсутствии `user_id` и `session_id`.**
   Это утечка, а не удобный режим: в журнале лежат вопросы пользователей
   (`history_search.py:3-5`, `main.py:1331-1333`).
2. **Объявлять `user_id`, `session_id` или любой иной параметр области в
   опубликованной схеме.** В схеме они стали бы полем, которое заполняет
   модель.
3. **Выводить `user_id` из `session_id`** — ни в агенте, ни в сервисе, ни в
   SQL.
4. **Выводить `user_id` только по `session_id` или только по `request_id`
   без сверки** (сверка `request_id` обязательна в ветке с вопросом,
   сверка `at_seq` — в ветке без вопроса).
5. **Возвращать «первую доступную» личность из индекса**, игнорируя, что
   событие может принадлежать другому вопросу той же сессии.
6. **Возвращать `user_id` в ответе.** Это подрывает саму идею, что область
   — фильтр чтения, а не поле выдачи.
7. **Возвращать отказ на пустую выборку.** Пустой `hits` — честный success.
8. **Строить SQL по значениям из аргументов строковой склейкой.** Все
   значения идут плейсхолдерами (`main.py:1345-1385`); единственные
   интерполируемые части — имя таблицы из `_require_log_table` и колонки,
   то есть не пользовательский ввод.
9. **Сортировать по одной лишь метке времени** без вторичного ключа.
10. **Делать отдельный запрос-счётчик** для обнаружения следующей
    страницы.
11. **Разрешать области быть «пустой», когда один из предикатов задан.**
12. **Вводить обратно `session_scope`** (снят change'ом
    `2026-10-03-mcp-native-tools`, п. D6).
13. **Хранить личность в модульной глобальной переменной** — она зависит
    от порядка регистрации операций (`history_search.py:21-23`).
14. **Возвращать `user_id` из строки `name`/`actor`:** `actor=user` и
    `actor=agent` область не меняют.

## Consumers

| Потребитель | Как использует | Что получает |
|---|---|---|
| Модель агента | `mcp_enterprise_data_history_search` | срез своего журнала |
| `DbLoggingService` | пишет события, которые затем ищутся | — |
| `McpIdentityHook` | подставляет личность в аргументы вызова | — |
| `ContextCompactionService` | пишет `context_compacted` через `try_log_event`; после сжатия агент читает своё состояние этим поиском | срез по `event_type` |
| Внешний аудит | доступ к строке журнала вне модели | полная таблица |

Потребитель, который стоит выделить: `ContextCompactionService` — он
использует журнал как устойчивое хранилище факта «контекст сжимали», и
именно поэтому операция обязана быть доступна модели **после** сжатия, когда
часть контекста уже потеряна.

## Implementation

Все пути проверены `Test-Path`; все существуют.

**Платформа:**

- `mcp-platform/servers/enterprise/capabilities/data/tools/history_search.py`
  — операция: имя, схема, передача личности из `ctx`, форма ответа,
  политика качества.
- `mcp-platform/servers/enterprise/capabilities/data/service/main.py` —
  `DataService.history_search` (строка 1314), `SearchPage` (350),
  `SearchHit` (337), `_require_log_table`, `submit`, `_guarded`;
  `AUDIENCE_MODEL` (63).
- `sql/migrations/V004__agent_gateway_logs_user_id.sql` — колонка
  `user_id`, backfill, индекс, `COMMENT ON COLUMN` и `COMMENT ON INDEX`.
- `sql/migrations/V008__agent_gateway_logs_event_time_columns.sql`,
  `sql/migrations/V009__agent_gateway_logs_event_time_indexes.sql` —
  событийное время и индексы под него.
- `./config.json` — `data.history_search` в
  `tools.mcpServers.enterprise.enabled_tools`.

**Агент:**

- `lib/hooks/mcp_identity_hook.py` — подстановка личности в аргументы
  вызова; отказ `McpIdentityRefused`.
- `lib/services/db_logging_service.py` — единственный writer журнала;
  `_resolve_event_user_id` (1739),
  `_resolve_event_user_id_without_request` (1770), `register_request`
  (1075).
- `lib/services/context_compaction.py` — потребитель журнала через
  `try_log_event`.
- `lib/services/compaction_event_subscriber.py` — подписка на
  `ContextCompactionEvent`.

**Чего в дереве нет**, хотя проза документации на это намекает:
`workspace/tools/history_search_tool.py` (файл снят — проверено
`Test-Path: False`), и параметра `session_scope` в коде нет ни в одном
месте. Снятие отражено в `## Purpose` спеки (строка 11).

## Verification

Все пути проверены `Test-Path`; все существуют.

**Контракт (сквозная изоляция личности):**

- `tests/contract/test_history_search_identity_contract.py` — страж
  границы между деревьями: подпись, схема без параметров области,
  отказ при неполной личности.

**Стражи изоляции и прозы:**

- `tests/test_history_search_user_isolation_guards.py` — пересечение
  области, отсутствие вывода `user_id` из `session_id`, отказ без области.
- `tests/test_mcp_health_wiring.py`, `tests/test_mcp_platform_declaration.py`
  — объявление операции и её имени.
- `tests/test_journal_event_name_alignment.py`,
  `tests/test_journal_level_canonical.py` — выравнивание имён событий и
  регистра уровней между записью и поиском.
- `tests/test_agent_facing_docs_contract.py` — документация провозглашает
  только реальные операции
  (`test_documents_a_mcp_tool_mention_a_real_operation`,
  `test_mcp_tools_are_in_enabled_list`).
- `tests/test_docs_consistency.py` — согласованность документации.
- `tests/test_enterprise_mcp_identity.py`, `tests/test_enterprise_mcp_client.py`
  — личность на стороне клиента платформы.
- `tests/test_queue_anchor_identity.py`, `tests/test_hooks_database_logging.py`
  — якорь идентичности в очереди и запись событий хуком.
- `tests/test_db_logging_service.py` — правила наследования `user_id` при
  записи.

**Платформа:**

- `mcp-platform/tests/test_data_service.py` — чтение журнала на стороне
  сервиса.
- `mcp-platform/tests/test_journal_argument_fields.py` — поля аргументов и
  разбор payload.
- `mcp-platform/tests/test_journal_fabricated_event_names.py` — отказ от
  выдуманных имён событий.
- `mcp-platform/tests/test_payload_excerpt.py` — форма `payload` в выдаче.
- `mcp-platform/tests/test_tool_execution_pipeline.py` — сквозной путь
  вызова операции.

Честная граница покрытия, найденная по ходу: стражи проверяют **имена
операций** в документации, но не **имена её параметров**. Именно поэтому
`workspace/TOOLS.md` до сих пор документирует `session_scope` (строки 71,
116, 129) — параметра, которого в коде нет уже после change'а
`2026-10-03-mcp-native-tools`; ни один из перечисленных тестов это не
замечает. Расхождение зафиксировано в `## Forbidden Behavior` (пункт 12) и
относится к документации, а не к операции.
