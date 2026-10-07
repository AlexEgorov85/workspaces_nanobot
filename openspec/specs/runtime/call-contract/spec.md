# runtime/call-contract Specification

## Purpose
Операции разбора юридического документа вводят коды отказа, которых в каноне
вызовов нет: до сих пор он и не был каноном — его несут два неархивированных
change, `2026-10-03-mcp-native-tools` и `2026-10-04-enterprise-mcp-http-transport`.
Без объявления операция выдумывала бы коды сама, и модель получила бы отказ,
которого нет в её словаре, то есть отказ без смысла.

Объявляются **только вводимые этой дельтой**. Уже объявленные коды — `not_found`,
`invalid_params`, `upstream_unavailable`, `internal` — перечислены ниже и
объявлять их **не надо**: они уже в закрытом списке кодов ответа
(`libs/enterprise_common/execution/errors.py`), и повторное объявление
расходилось бы с каноном, который однажды уже слит.

Коды разделены по **пути**, а не по месту написания, потому что правило
проверки у путей разное, и в этом была сама поломка:

* **тело ответа** (`{"status": "failed", "error": {"code": …}}`) разбирается
  конвейером через `failure_from_payload`, и код обязан быть в `FAILURE_CODES`.
  Кода нет в списке — он молча переписывается в `internal`, и объявленный отказ
  доезжает до модели как «внутренняя ошибка платформы»: объяснение потеряно,
  действие — тоже;
* **исключение** (`EnterpriseError(code=…)`) разбирается через
  `normalize_exception`, который код пропускает как есть. Такие коды в
  `FAILURE_CODES` не обязаны входить, и требовать их оттуда неверно.

## Scope

`shared` — закрытый список кодов и признак повторяемости живут в коде
платформы (`mcp-platform/libs/enterprise_common/execution/errors.py:47`,
`:76`), а агентская нога обязана называть те же ключи и читать те же коды
(`lib/hooks/mcp_identity_hook.py:240`, `workspace/skills/enterprise_mcp/SKILL.md:77`)
Реализация: `mcp-platform/libs/enterprise_common/execution/` (`errors.py`,
`pipeline.py`, `context.py`), `mcp-platform/libs/enterprise_common/loader.py`,
`lib/hooks/mcp_identity_hook.py`, `lib/services/turn_identity.py`,
`lib/services/enterprise_mcp_client.py`, `workspace/skills/enterprise_mcp/SKILL.md`

## Requirements

### Requirement: Коды отказа операций разбора объявлены в каноне вызовов

Операция запуска разбора и операция чтения его состояния вводят коды отказа.
Все они объявлены здесь поимённо — по путям, а не единым списком, потому что
проверяются по-разному.

**Путём тела ответа** — конвейер обязан отдать такой код под своим именем,
а `FAILURE_CODES` (`libs/enterprise_common/execution/errors.py`) обязан его
держать:

* `legal_budget_unreachable` — неделимый шаг стратегии `direct` не помещается в
  потолок вызова;
* `EMPTY_DOCUMENT` — файл прочитан, а текста в нём нет;
* `INVALID_LENGTH` — значение `length` вне перечня (сегодня недостижим:
  `analyze_document` отбирает его перечнем и отказывает `invalid_params`
  раньше; объявлен, чтобы ослабление перечня не стало молчаливым);
* `NO_PARTIALS` — нет per-chunk partials, доехать до ответа нечего;
* `REDUCE_INPUT_EMPTY` — reduce вернул пустой summary;
* `BATCH_FAILED` — ни один батч ограниченного шага не выполнен;
* `LLM_ERROR` — батч упал с ошибкой вызова LLM;
* `LLM_PARSE_ERROR` — батч упал с ошибкой разбора ответа LLM.

**Путём исключения** — `normalize_exception` отдаёт такой код как есть, и
`FAILURE_CODES` его не проверяет:

* `operation_in_progress` — признак занятости удерживается;
* `document_not_found` — файла вложения нет;
* `not_a_file` — по указанному пути каталог, а не файл;
* `unsupported_format` — расширение не входит в `.pdf` / `.docx` / `.txt`;
* `document_unreadable` — файл есть, но парсер не извлёк из него текст;
* `state_write_failed` — состояние операции не записалось: диск, NFS или
  сессия, открытая только на чтение.

**Уже объявлены, и эта дельта их не повторяет:** `not_found` (нет состояния
операции либо оно убрано по сроку), `upstream_unavailable` (манифест версии,
которую читатель не понимает), `internal` (доменный `error_type`, не сведённый
`_ERROR_CODES` в словаре читателя), `invalid_params` (аргумент не такой). Все
четыре уже в `FAILURE_CODES`, поэтому объявлять их повторно значило бы
расширить канон без нового кода — и разошлось бы с уже слитым.

Объявление кодом **обязательно и предшествует первой отдаче этого кода**
модели. Порядок наоборот — код отдан, а объявление появилось позже — означал
бы, что модель уже получила отказ, которого не знает: починка объявления не
воскресит отданный ответ, и разбор оборвётся на первом же неожиданном коде.

Операция SHALL NOT отдавать модели код ответа, который здесь не объявлен и не
объявлен ранее, и каждый код отказа SHALL быть различим с `invalid_params`.
Различие задано смыслом, а не числом: `invalid_params` — «аргумент не такой», а
перечисленные — «с таким аргументом отказано по существу» либо «шаг не
выполнен».

#### Scenario: Код, объявленный телом ответа, доезжает под своим именем

- **WHEN** домен отдал отказ телом ответа, то есть `{"status": "failed",
  "error": {"code": …}}`
- **THEN** конвейер SHALL отдать модели код, объявленный в этом разделе, под
  его собственным именем
- **AND** код SHALL NOT быть переписан в `internal`, потому что «внутренняя
  ошибка платформы» не называет причину и не подсказывает действие

#### Scenario: Код исключения не обязан быть в списке кодов ответа

- **WHEN** операция отказала исключением, а не телом ответа
- **THEN** код SHALL быть отдан под своим именем
- **AND** требование внесения его в `FAILURE_CODES` к нему SHALL NOT
  применяться: другой путь отказа проверяет другой список

#### Scenario: Каждый объявленный код назван поимённо

- **WHEN** операция разбора отказывает
- **THEN** код отказа SHALL быть одним из перечисленных здесь по своему пути
  либо одним из уже объявленных `not_found`, `invalid_params`,
  `upstream_unavailable`, `internal`
- **AND** код, не входящий ни в этот перечень, ни в уже объявленные, SHALL NOT
  появиться: операция вводит новый код только вместе с его объявлением здесь

#### Scenario: Отказ по неделимости назван, а не спрятан в таймаут

- **WHEN** вызов попал на ветку `direct`, то есть батчей нет, и неделимый
  шаг не помещается в потолок вызова
- **THEN** конверт SHALL содержать код `legal_budget_unreachable`
- **AND** отказ SHALL прийти до единого LLM-вызова, то есть до любой платной
  работы

#### Scenario: Отказ по занятости назван отдельно от отказа по существу

- **WHEN** вызов пришёл на операцию, чей признак занятости ещё удержан
- **THEN** конверт SHALL содержать код `operation_in_progress`
- **AND** работа SHALL NOT быть продублирована и запущена второй раз

#### Scenario: Отказ загрузки документа различает четыре разные причины

- **WHEN** документ, указанный вызовом, непригоден для разбора
- **THEN** SHALL быть отдан ровно один из кодов: `document_not_found` —
  файла нет, `not_a_file` — по пути каталог, `unsupported_format` — формат
  не поддерживается, `document_unreadable` — файл не открывается парсером
- **AND** модель SHALL различать эти четыре причины: они требуют разных
  действий, и сведение их к одному коду отдало бы ей бессодержательный отказ

#### Scenario: Отказ записи состояния назван, а не выглядит успехом

- **WHEN** результат разбора не удалось записать в папку сессии
- **THEN** конверт SHALL содержать код `state_write_failed`
- **AND** ответ SHALL NOT содержать `status: "completed"`, потому что
  завершённого результата, который не записан, нет

#### Scenario: Объявление в каноне не заменяет объяснения в ответе

- **WHEN** объявлен код отказа
- **THEN** сообщение отказа SHALL называть причину и, где причина требует
  действия, SHALL предлагать это действие
- **AND** код SHALL NOT быть единственным носителем смысла: модель читает
  сообщение, а не различает коды в своей памяти

Объявления в этом каноне недостаточно, пока capability не создан: код,
объявленный в дельте, до архивирования change не виден ни проверяющим
инструментам, ни модели. Поэтому архивирование этого change SHALL идти
**последним** среди change, несущих `runtime/call-contract`, — иначе канон этой
capability напишет тот, кто её объявил последним.

Capability объявляют три change: `2026-10-03-mcp-native-tools` и
`2026-10-04-enterprise-mcp-http-transport` создают её, этот — дополняет одним
требованием. Канон пишет создатель, поэтому этот change архивируется после них,
а не раньше: его дельта объявляет **только** `ADDED` и создать capability в
одиночку может, но тогда канон получит требование о кодах разбора вместо
контракта вызова, и следующий архивируемый change будет править чужую
каноническую заготовку.

Состояние на момент написания: `runtime/call-contract` в каноне
(`openspec/specs/runtime/`) ещё нет, и архивирование любого из трёх идёт не
очередью, а отдельным решением по каждому — очередь упёрлась бы в то, что
`2026-10-03-mcp-native-tools` объявляет `MODIFIED` против ещё не существующей
цели и потому не может создать capability первым. Это его дефект, а не
следствие порядка, и он зафиксирован отдельно (задача 7.4 `tasks.md`).

## Responsibility

Канон владеет тем, чего нет ни в одной операции: закрытым списком кодов ответа
и правилом, по которому код либо объявлен заранее, либо не появится.

- `FAILURE_CODES`
  (`mcp-platform/libs/enterprise_common/execution/errors.py:47`) — единственный
  фильтр **обоих** путей отказа: и тела ответа, и исключения. Список закрыт
  намеренно, потому что агент разбирает коды по веткам, а молчаливый новый код
  он обработает как «неизвестно» (`:23-31`).
- `RETRYABLE_CODES` (`:76`) — подмножество, задающее признак `retryable` в
  теле отказа; выводится механически, `retryable=code in RETRYABLE_CODES`
  (`:108`), поэтому признак и код не могут разойтись.
- Два пути отказа разбираются двумя разными функциями и потому требуют разных
  проверок: `failure_from_payload` (`:148`) переписывает незнакомый код в
  `internal` (`:162-163`), а `normalize_exception` (`:112`) код домена
  пропускает как есть (`:129-130`).
- Фильтрация текста: внутренние отказы не отдают наружу ни тип, ни текст
  исключения — вместо них `_INTERNAL_HINT` (`:81`, `:143-145`).
- Порядок провозглашения: объявление предшествует первой отдаче кода модели
  (требование выше). Это не пожелание: починка объявления не воскресит уже
  отданный ответ.

## Boundary

- **Внутри:** закрытый перечень кодов, признак повторяемости, форма конверта
  отказа `{"error": {...}}` (`enterprise_common/execution/errors.py:104-105`), перевод доменного кода в
  код ответа там, где домен говорит `error_type`, а не `code`
  (`mcp-platform/servers/enterprise/tools/query_operation.py:52-57`).
- **Снаружи:** словарь операций и проверка аргументов
  (`data/operation-schema`), слой исполнения целиком (`pipeline.py`), домен
  разбора (`mcp-platform/libs/legal_summarizer/`, спека
  `skills/legal-summarizer-query`), навык, читающий коды моделью
  (`workspace/skills/enterprise_mcp/SKILL.md:71-95`).

## Public Contract

Форма отказа — единственное, что читает вызывающая:

```json
{"error": {"code": "<код из закрытого списка>", "message": "<текст>", "retryable": false}}
```

`Failure.to_json` (`mcp-platform/libs/enterprise_common/execution/errors.py:104`)
собирает её побайтово; наружу отдаётся она как текстовый блок `CallToolResult`
с флагом `isError`
(`mcp-platform/libs/enterprise_common/loader.py:397-400`).

Свойства формы, на которые опирается канон:

- `code` — из `FAILURE_CODES` (`:47`) либо `internal`; иного в ответе быть не
  может (`:162-163`);
- `message` — читаемый текст: код не единственный носитель смысла, и пустой
  текст подменяется `_INTERNAL_HINT` (`:164`);
- `retryable` — следствие членства кода в `RETRYABLE_CODES` (`:108`), поле
  вызывающая сторона не заполняет сама.

Перевод доменного `error_type` в код отказа живёт у операции-читателя, а не в
списке: `_ERROR_CODES`
(`mcp-platform/servers/enterprise/tools/query_operation.py:52-57`) и
`_ERROR_CODES.get(str(payload.get("error_type")), "internal")` (`:97`) — то есть
`error_type`, которому не досталось строки в таблице, уходит в `internal`
намеренно, а не по недосмотру.

## Inputs

- Исключение домена или платформы — на вход `normalize_exception`
  (`enterprise_common/execution/errors.py:112`). Базовый тип: `EnterpriseError`
  (`mcp-platform/libs/enterprise_common/errors.py:11`), `PathDeniedError`
  (`mcp-platform/libs/enterprise_common/session/security.py:58`),
  `IdentityMissingError`
  (`mcp-platform/libs/enterprise_common/execution/context.py:53`).
- Тело результата операции — на вход `failure_from_payload` (`enterprise_common/execution/errors.py:148`);
  домен кладёт отказ в `{"error": {...}}` сам, а не бросает.
- `identity_missing` приходит не из домена: его порождает разбор личности
  вызова `McpCallContext.from_meta`
  (`mcp-platform/libs/enterprise_common/execution/context.py:77-100`) при
  отсутствии обязательных ключей (`context.py:37`).

## Outputs

Один артефакт на отказ: конверт `{"error": {...}}`. Конвейер отдаёт его и при
отказе на границе идентичности, и при отказе домена
(`mcp-platform/libs/enterprise_common/execution/pipeline.py:160-168`,
`:484-492`), и MCP-адаптер ставит по нему `isError`
(`mcp-platform/libs/enterprise_common/loader.py:397-400`).

Дополнительно отказ наблюдаем в журнале: `tool.failed`, а при истечении срока —
`tool.timeout` (`enterprise_common/execution/pipeline.py:437-464`), код при этом пишется в `payload`
(`:447`).

## State

Канон состояния не хранит. `FAILURE_CODES` и `RETRYABLE_CODES` — модульные
константы `frozenset` (`enterprise_common/execution/errors.py:47`, `:76`), то есть они неизменяемы по
построению и общей ссылки не имеют.

Состояние вызова, к которому привязан отказ, живёт в двух чужих местах:

- личность вызова — в `McpCallContext`
  (`mcp-platform/libs/enterprise_common/execution/context.py:65-75`);
- состояние операции разбора — в файлах сессии, у capability
  (`mcp-platform/servers/enterprise/tools/analyze_document.py:170-200`).

Отказ по занятости и по невозможности записи состояния — два следствия этого
чужого состояния, а не его следствие: `_take_or_refuse`
(`analyze_document.py:563`) и `_write_marker` (`:524`).

## Dependencies

- `mcp-platform/libs/enterprise_common/execution/errors.py` — список кодов,
  `Failure`, обе функции нормализации;
- `mcp-platform/libs/enterprise_common/execution/pipeline.py` — единственный
  вызывающий обе функции (`:191`, `:196`, `:158`);
- `mcp-platform/libs/enterprise_common/execution/context.py` — `IdentityMissingError`
  и ключи личности (`:31-37`);
- `mcp-platform/libs/enterprise_common/errors.py` — `EnterpriseError` и код по
  умолчанию `enterprise_error` (`:11-20`);
- `mcp-platform/libs/enterprise_common/session/security.py` — `PathDeniedError`,
  приводится к `invalid_params` (`enterprise_common/execution/errors.py:127-128`);
- `mcp-platform/libs/enterprise_common/loader.py` — доставка конверта и флага
  `isError`;
- домен-поставщик кодов: `mcp-platform/libs/legal_summarizer/`,
  `mcp-platform/servers/enterprise/tools/analyze_document.py`,
  `mcp-platform/servers/enterprise/tools/query_operation.py`.

## Configuration

- `execution.require_call_meta` (`mcp-platform/platform.json:52`) — объявлен
  `false`, и это режим, при котором канон проверяем вживую: личность приходит
  плоскими ключами и вырезается из аргументов
  (`enterprise_common/execution/pipeline.py:301-303`, `:307-319`). Открытый путь — `true`, при котором
  чтение идёт только из `params._meta`, а отказ приходит `identity_missing`.
- `execution.execution_timeout_sec` (`mcp-platform/platform.json:42`) — потолок,
  по которому возникает код `timeout`: `ExecutionTimeout`
  (`enterprise_common/execution/errors.py:84`, `:93`) и `PipelineResult.status`
  (`enterprise_common/execution/pipeline.py:490`).
- Списка кодов в конфигурации нет и быть не должно: он закрыт и объявлен
  один раз в коде. Второе место для перечня означало бы две редактируемые
  копии одного словаря.

## Lifecycle

1. Операция отказывает — исключением или телом ответа.
2. Конвейер разбирает отказ: исключение через `normalize_exception`
   (`enterprise_common/execution/pipeline.py:191`), тело ответа через `failure_from_payload` (`:196`).
3. Отказ приводится к `Failure`; код неизвестного тела переписывается в
   `internal` (`enterprise_common/execution/errors.py:162-163`), а код исключения сохраняется (`:129-130`).
4. `_refuse` (`enterprise_common/execution/pipeline.py:467`) пишет событие в журнал и собирает `PipelineResult`
   с `is_error=True` (`:484-492`).
5. MCP-адаптер отдаёт конверт с `isError`
   (`mcp-platform/libs/enterprise_common/loader.py:397-400`).

## Data Ownership

Владеет: перечнем кодов ответа, признаком повторяемости, формой конверта
отказа.

Не владеет и не имеет права менять:

- коды домена — их объявляет владелец домена, а канон их принимает;
- тексты сообщений — их пишет операция, и канон требует лишь, чтобы текст
  называл причину, а не чтобы он был задан здесь;
- события журнала и их полис (`enterprise_common/execution/pipeline.py:437-464`);
- личность вызова — она собирается вызывающей стороной
  (`lib/services/turn_identity.py:158`) и вырезается сервером из аргументов.

## Error Behavior

Разбор отказа нормализован: тип и текст внутреннего исключения наружу не
уходят (`:143-145`), иначе модель получила бы `KeyError: 'rows'` или текст
psycopg2 вместо кода. Проверки идут от узких к широким, и порядок значим:
`IdentityMissingError` — подкласс `Exception`, а не `EnterpriseError`, поэтому
он проверяется раньше (`:123-130`).

Сверх нормализации код меняется только в одном месте — неизвестный код тела
ответа становится `internal` (`:162-163`). Это не потеря: пустой или
неизвестный код не названный код ответа не названный и есть, а объявить его
значило бы расширить словарь из данных, которые кто-то прислал извне.

Признак `retryable` вычисляется, а не задаётся вызывающей стороной: тот же
`_failure` (`:108`) обслуживает оба пути, поэтому код с `retryable=false`
повторять бессмысленно — те же аргументы дадут тот же отказ.

## Invariants

- `FAILURE_CODES` — единственный фильтр обоих путей отказа
  (`enterprise_common/execution/errors.py:26-31`), а не только пути тела ответа.
- `RETRYABLE_CODES` ⊂ `FAILURE_CODES`: признак `retryable` выводится из
  членства в нём (`:76`, `:108`), поэтому код вне списка не может получить
  `retryable: true` и наоборот.
- `code` в ответе всегда из списка либо `internal` (`:162-163`); ни один путь
  не выпускает наружу код, которого в списке нет.
- `message` непуст: пустой текст подменяется `_INTERNAL_HINT` (`:164`).
- `failure_from_payload` отличает «отказ» от «успеха без поля `error`»:
  не-словарь и словарь без словаря `error` дают `None` (`:156-160`), поэтому
  успешный ответ операции отказом не становится.
- Каждый код отказа различим с `invalid_params` — «аргумент не такой» против
  «с таким аргументом отказано по существу» (требование выше).
- Ключи личности вырезаются из аргументов **всегда**, независимо от режима
  (`enterprise_common/execution/pipeline.py:301-303`): в опубликованной схеме таких полей нет, и оставленное
  в аргументах значение дошло бы до операции полем, которого не существует.

## Forbidden Behavior

- Операция отдаёт код ответа, который не объявлен здесь и не объявлен ранее —
  модель получит отказ, которого нет в её словаре (требование выше).
- Вводить код ради слоя исполнения, не укладывая его в словарь
  `docs/MCP-CONTRACTS.md` §2: вызывающему пришлось бы различать «слой
  исполнения» и «домен», а он не знает, какой из них отказал
  (`enterprise_common/execution/errors.py:7-10`).
- Требовать внесения кода исключения в `FAILURE_CODES` — другой путь отказа
  проверяет другой список (`:129-130`).
- Объявлять код **после** первой его отдачи модели (требование выше).
- Выпускать наружу тип или текст внутреннего исключения (`:143-145`).
- Считать объявление кода достаточным без объяснения в ответе: модель читает
  сообщение, а не различает коды в своей памяти (сценарий «Объявление в каноне
  не заменяет объяснения в ответе»).
- Задавать `retryable` вручную — признак выводится из `RETRYABLE_CODES` (`:108`).
- Оставлять в аргументах вызова плоские ключи личности
  (`enterprise_common/execution/pipeline.py:290-303`).

## Consumers

- `mcp-platform/libs/enterprise_common/execution/pipeline.py:191`, `:196` —
  единственный вызывающий обе функции нормализации.
- `mcp-platform/libs/enterprise_common/loader.py:397-400` — доставляет конверт
  и ставит `isError`.
- `mcp-platform/servers/enterprise/tools/analyze_document.py` — поставщик кодов
  разбора: `document_not_found` (`:372`), `not_a_file` (`:376`),
  `unsupported_format` (`:382`), `document_unreadable` (`:399`),
  `EMPTY_DOCUMENT` (`:403`), `state_write_failed` (`:531`),
  `operation_in_progress` (`:588`).
- `mcp-platform/servers/enterprise/tools/query_operation.py:52-57` — словарь
  перевода `error_type` в код ответа.
- `mcp-platform/libs/legal_summarizer/application/service.py:672` — поставщик
  `legal_budget_unreachable`.
- `workspace/skills/enterprise_mcp/SKILL.md:71-95` — таблица кодов и правило
  повтора, которые читает модель; это второй носитель словаря, и он обязан
  совпадать с `FAILURE_CODES`.
- `mcp-platform/tests/test_declared_failure_codes.py` — страж: коды домена и
  операций обязаны быть объявлены в каноне, иначе страж падает.

## Implementation

- `mcp-platform/libs/enterprise_common/execution/errors.py` — `FAILURE_CODES`
  (`:47`), `RETRYABLE_CODES` (`:76`), `_INTERNAL_HINT` (`:81`),
  `ExecutionTimeout` (`:84`), `Failure` (`:97`), `normalize_exception` (`:112`),
  `failure_from_payload` (`:148`);
- `mcp-platform/libs/enterprise_common/execution/pipeline.py` — `EXECUTION_KEY`
  (`:70`), `LEGACY_IDENTITY_KEYS` (`:75`), разбор личности (`:301-320`),
  предел времени (`:338-349`), `_refuse` (`:467`), `_as_meta` (`:498`);
- `mcp-platform/libs/enterprise_common/execution/context.py` — ключи личности
  (`:31-37`), `IdentityMissingError` (`:53`), `McpCallContext` (`:65`);
- `mcp-platform/libs/enterprise_common/errors.py` — `EnterpriseError` (`:11`);
- `mcp-platform/libs/enterprise_common/session/security.py` —
  `PathDeniedError` (`:58`);
- `mcp-platform/libs/enterprise_common/loader.py` — `CallToolResult` и
  `isError` (`:397-400`);
- `mcp-platform/servers/enterprise/tools/analyze_document.py`,
  `mcp-platform/servers/enterprise/tools/query_operation.py`,
  `mcp-platform/libs/legal_summarizer/` — поставщики кодов;
- `workspace/skills/enterprise_mcp/SKILL.md` — модельное чтение кодов.

## Verification

- `mcp-platform/tests/test_declared_failure_codes.py` — код, который домен или
  операция отдают, обязан быть объявлен в каноне; страж ловит введение кода
  без объявления.
- `mcp-platform/tests/test_tool_execution_pipeline.py` — пути отказа,
  нормализация и форма конверта на уровне конвейера.
- `mcp-platform/tests/legal_summarizer/test_direct_budget.py` —
  `legal_budget_unreachable` приходит до платной работы.
- `mcp-platform/tests/test_legal_summarizer_capability.py` — перевод доменного
  `error_type` в код конверта и покрытие всех `error_type` таблицей.
- `tests/test_mcp_platform_declaration.py` — `require_call_meta` объявлен
  `false` (`:513-514`) и имена ключей хука совпадают с
  `LEGACY_IDENTITY_KEYS` (`:522-524`).
- `tests/test_mcp_identity_hook.py` — подстановка личности вызова на
  агентской ноге.
- `tools/validate_component_specs.py` — наличие и порядок разделов этой спеки.
