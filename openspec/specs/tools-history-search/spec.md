# tools-history-search Specification

## Purpose

Контракт поиска по долговечному журналу `agent_gateway_logs`, который переживает
context compaction и остаётся основным источником данных для восстановления
деталей, выпавших из контекста LLM.

Область поиска **не выбирается**: её задаёт вызывающая сторона, и модель не может
её ни увидеть, ни ослабить. Кастомный tool агента с параметром
`session_scope` снят (change `2026-10-03-mcp-native-tools`, п. D6); модель
получает операцию `mcp_enterprise_history_search`, которая применяет предикаты по
`session_id` и `user_id` из контекста вызова.

Изоляция данных — денормализация `user_id`, правило наследования, backfill,
индексы — в этом описании не менялась и остаётся в силе.

## Scope

`shared` — область поиска задаёт вызывающая сторона (хук в агенте, контекст
вызова на платформе), а SQL, пагинацию и изоляцию выполняет платформа.

Реализация: `lib/hooks/mcp_identity_hook.py`,
`mcp-platform/servers/enterprise/capabilities/data/tools/history_search.py`,
`mcp-platform/servers/enterprise/capabilities/data/service/main.py`.
Вызов: `mcp_enterprise_history_search`.

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

- **WHEN** модель формирует аргументы `mcp_enterprise_history_search`
- **THEN** в них SHALL NOT быть параметра, задающего область поиска
- **AND** предикат по `session_id` SHALL применяться всегда

### Requirement: operation identity and scope

Операция `history_search` SHALL читать таблицу журнала, имя и схема которой
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

- **WHEN** модель вызывает `mcp_enterprise_history_search`
- **THEN** агент SHALL NOT строить SQL
- **AND** агент SHALL NOT обращаться к таблице журнала напрямую

#### Scenario: history_search is read-only

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

- **WHEN** `history_search` вызван без `user_id` и без `session_id`
- **THEN** сервис SHALL отказать `InvalidRequestError`
- **AND** SQL к журналу SHALL NOT быть выполнен

#### Scenario: Одной из двух частей области достаточно

- **WHEN** `history_search` вызван с заданным `session_id` и пустым `user_id`
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

Источник `user_id` — `nanobot.agent.tools.context.RequestContext` через поле
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

- **WHEN** вызвано `history_search(event_type="tool.started", tool_name="history_search", limit=3)`
- **THEN** SHALL быть возвращено не более 3 событий, у которых
  `event_type='tool.started'` И `name='history_search'`
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
платформы (`libs/enterprise_common/eventing/types.py`).

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
