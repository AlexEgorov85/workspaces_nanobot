# tools-history-search Specification

## Purpose

Изменение того, КТО решает область поиска по журналу и КАК модель до неё
доходит. Прежняя спека описывала кастомный tool агента с параметром
`session_scope`, который выбирал `current` или `all`.

Tool снят (change `2026-10-03-mcp-native-tools`, п. D6). Модель получает ту же
операцию как `mcp_enterprise_history_search`, область ей не выбирает и не
видит: её задаёт вызывающая сторона, платформа применяет её принудительно.

Требования прежней спеки делились на три группы. Изоляция данных и её
инфраструктура (денормализация `user_id`, правило наследования, backfill,
индексы) **остаётся в силе полностью** — она не зависела от tool'а. Контракт
вызова (параметр области, форма ответа, валидация `event_type`, enum типов
событий) **снимается или переписывается**. Ниже — только эта вторая группа;
требования первой группы в дельте не упоминаются и остаются действующими.

## Scope

`shared` — область поиска задаёт вызывающая сторона (ходок в агенте, контекст
вызова на платформе), а SQL, пагинацию и изоляцию выполняет платформа.

Реализация: `lib/hooks/mcp_identity_hook.py` (подстановка личности),
`mcp-platform/servers/enterprise/capabilities/data/tools/history_search.py`,
`mcp-platform/servers/enterprise/capabilities/data/service/main.py`.
Вызов: `mcp_enterprise_history_search`.

## REMOVED Requirements

### Requirement: session_scope = current filters by session_id

Требование снято. Параметра `session_scope` не существует, и выбирать область
поиска нечем: у операции нет ни такого параметра, ни какого-либо другого,
которым можно было бы её ослабить.

Предикат по `session_id` не исчез — он стал обязательным и неотключаемым. Оба
сценария прежней редакции описывали поведение значения, которого больше нет
(`current` как режим по умолчанию), и сняты вместе с требованием.

#### Scenario: Выбор области вызовами невозможен

- **WHEN** модель формирует аргументы `mcp_enterprise_history_search`
- **THEN** в них SHALL NOT быть параметра, задающего область поиска
- **AND** предикат по `session_id` SHALL применяться всегда

### Requirement: session_scope = all filters by user_id

Требование снято. Значение `all`, снимавшее фильтр по сессии, больше не
существует: пересечение по сессии и пользователю нельзя отключить, потому что
нечем — ни параметра, ни умолчания, ни значения по умолчанию.

Прежний сценарий «cross-user isolation» **переносится** в требование
«Scope is the caller session intersected with the caller user» в обновлённой
редакции: изоляция между пользователями не ослабла, а стала следствием того,
что область вообще не выбирается.

#### Scenario: Снятие фильтра по сессии недоступно

- **WHEN** модель формирует аргументы `mcp_enterprise_history_search`
- **THEN** SHALL NOT существовать способа отключить предикат по `session_id`
- **AND** SHALL NOT существовать способа отключить предикат по `user_id`

### Requirement: RequestContext exposes user identity

Требование снимается в части, которая относилась к tool'у: функции
`_current_user_id()` в `history_search_tool.py` не существует, и обращение к
`RequestContext.sender_id` из места, которое её вызывало, тоже.

Роль identity-store **переносится** в требование «Scope is the caller session
intersected with the caller user»: источник тот же самый —
`nanobot.agent.tools.context.RequestContext.sender_id`, — но читает его теперь
`McpIdentityHook._sender_id()`, единственное обращение к этому полю в агенте.
Зависимость от версии nanobot по-прежнему изолирована в одной функции.

Контракт-тест `tests/contract/test_history_search_identity_contract.py`
остаётся и по-прежнему проверяет, что поле `sender_id` у `RequestContext`
существует и совместимо с `str | None`; требование к нему переносится целиком.

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

## MODIFIED Requirements

### Requirement: tool identity and scope

Требование существует и в прежней редакции. Набор таблиц и запрет на
произвольный SQL остаются, но исполняет их теперь платформа, а не tool агента,
и имя объекта меняется.

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

### Requirement: missing user identity is a hard error

Требование существует и в прежней редакции. Прежний отказ
(`error_type = "missing_user_identity"` от tool'а агента) сменён двумя
проверками на разных уровнях, и это разделение существенно: доменная проверка
и проверка на границе конвейера отвечают на разные вопросы.

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

Требование существует и в прежней редакции и остаётся в силе без изменений по
существу; меняется только то, что запрет относится к платформенной операции.

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

### Requirement: pagination and ordering

Требование существует и в прежней редакции и остаётся в силе: сортировка и
механика детекции следующей страницы не менялись, переехали на платформу.

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

Требование существует и в прежней редакции. Форма ответа меняется целиком;
запрет на утечку `user_id` не меняется и становится строже.

Операция SHALL возвращать JSON-объект с полями `hits`, `next_offset`,
`truncated`. Каждый элемент `hits` SHALL содержать `id`, `timestamp`,
`event_type`, `name`, `level`, `summary`, `payload`. Поле `payload` SHALL быть
объектом (JSONB декодируется на чтении), а не JSON-строкой.

Операция SHALL NOT возвращать `user_id` ни в корне ответа, ни в элементах
`hits`. В текущей форме ответа `user_id` не появляется **вовсе**: он служит
только предикатом поиска и в выборку не попадает.

Поля прежней формы — `status`, `count`, `session_scope`, `has_more`,
`results_truncated`, `events`, `event_id`, `payload_truncated` — SHALL считаться
снятыми вместе с tool'ом.

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

### Requirement: index for all-scope access

Требование существует и в прежней редакции. Access-pattern меняется: вместо
`WHERE user_id = ?` поиск идёт по пересечению.

DDL SHALL содержать индекс на `agent_gateway_logs`, обслуживающий
access-pattern `WHERE user_id = ? AND session_id = ? ORDER BY "timestamp" DESC`.
Имя индекса и колонки — на усмотрение реализации; требование — индекс
**существует и совместим** с этим pattern'ом. Индекс по `session_id` SHALL NOT
использоваться как замена пользовательскому фильтру, и наоборот: наличие
одного из двух предикатов SHALL NOT позволять обойти другой.

#### Scenario: DDL provides index for the intersection

- **GIVEN** DDL `agent_gateway_logs`
- **THEN** SHALL существовать индекс, обслуживающий фильтр по `user_id` и
  `session_id` с сортировкой по `"timestamp" DESC`
- **AND** его назначение SHALL быть задокументировано через `COMMENT ON INDEX`

### Requirement: tool API has no user_id parameter

Требование существует и в прежней редакции и остаётся в силе; после перехода
оно выполняется конструктивно, а не запретом.

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

### Requirement: Параметры запроса

Требование существует и в прежней редакции. Состав параметров меняется: из
списка уходят `session_scope` и упоминание `tools.history_search.max_rows`,
потому что потолок задаёт платформа.

Операция SHALL принимать следующие параметры (все, кроме `query`, опциональны;
все значения — доменные, идентичности среди них нет):

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

### Requirement: Формат ответа и пагинация

Требование существует и в прежней редакции и переписывается под форму
операции; правила пагинации сохраняются.

Операция SHALL возвращать JSON-объект `{hits, next_offset, truncated}`.
`next_offset` SHALL быть `offset + limit`, если следующая страница существует,
и `null` иначе. `truncated` SHALL быть `true`, когда выборка достигла потолка
`max_rows`. Поле `payload` каждого элемента SHALL быть декодированным JSONB, а
не JSON-строкой.

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

#### Scenario: payload — объект, а не строка

- **WHEN** элемент `hits` содержит JSONB-колонку
- **THEN** `payload` SHALL быть объектом
- **AND** SHALL NOT быть JSON-строкой, требующей разбора вызывающей стороной

### Requirement: Схема payload по event_type

Требование существует и в прежней редакции. Имена типов событий в схеме
**устарели полностью**: перечень переехал в платформу и стал
пространством имён с префиксами.

Система SHALL задокументировать в `workspace/TOOLS.md` JSON-схему `payload` для
действующих типов событий. Перечень SHALL соответствовать словарю типов
платформы (`libs/enterprise_common/eventing/types.py`), а не прежнему списку
`context_compacted` / `tool_call` / `tool_result` / `llm_call` /
`run_finished` / `subagent_run_finished` / `inbound`. Изменение формы данных
требует отдельного change.

Префиксы `agent.`, `llm.`, `tool.`, `artifact.`, `quality.` SHALL использоваться
вместе с конкретным именем (`tool.started`, `agent.compacted`, `llm.exchanged`),
а опечатка в последнем компоненте SHALL отвергаться проверкой словаря, а не
плодить новый тип.

#### Scenario: Документация использует действующие имена

- **WHEN** `workspace/TOOLS.md` приводит примеры фильтра по `event_type`
- **THEN** приведённые имена SHALL принадлежать словарю типов платформы
- **AND** устаревшие имена из прежней редакции спеки SHALL NOT встречаться

#### Scenario: Агент читает результат инструмента

- **WHEN** агент получает событие типа инструмента
- **THEN** он может предсказуемо прочитать `payload.tool` и `payload.status`
  напрямую, без `json.loads`

## ADDED Requirements

### Requirement: Scope is the caller session intersected with the caller user

Область поиска SHALL определяться вызывающей стороной и SHALL быть
обязательной. Это заменяет прежний выбор `current` / `all` и делает его
невозможным по построению.

Личность вызова SHALL формироваться на стороне агента хуком
`McpIdentityHook` — безусловной подстановкой `session_id`, `user_id` и
`request_id` в аргументы вызова — и SHALL читаться платформой в контекст
вызова. Операция SHALL применять предикат по `session_id` **и** по `user_id`,
каждый из которых берётся из контекста вызова, а не из аргументов.

Возможность выбрать область SHALL NOT существовать ни в опубликованной схеме,
ни среди неявных умолчаний.

Значения SHALL совпадать с тем, что пишет журнал: `session_id` — ключ сессии
оборота, `user_id` — отправитель оборота (`RequestContext.sender_id`).
Несогласованные значения (например, `user_id`, не совпадающий с владельцем
сессии) не должны возникать, потому что оба берутся из одного оборота.

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
