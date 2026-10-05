# skills/legal-summarizer-query Specification — дельта

## Purpose

Канон `skills/legal-summarizer-query` владеет read-only follow-up по уже
разобранному документу: одна операция `query_operation`, параметр
`operation_id`, и ответ «состояния нет» при его отсутствии.

Дельта добавляет в неё **вторую половину** — операцию, которая это состояние
производит. Сейчас цепочка разорвана: `query_operation` требует `operation_id`,
`operation_id` рождается только суммаризацией (`application/service.py:330`),
суммаризации как операции нет ни в capability, ни в `enabled_tools`, ни в
`workspace/tools/`, и `config.json → tools.exec.enable = false` закрывает путь
через CLI. `query_operation` — оракул без вопроса.

Тема дельты, а не новая capability, по трём причинам:

1. Обе операции работают с одним и тем же состоянием операции — манифестом по
   `operation_id`. Это один контракт, разорванный надвое; держать вторую половину
   в отдельной capability означало бы две спеки об одном и том же хранилище.
2. Канон уже объявляет и реализацию (`mcp-platform/libs/legal_summarizer/`), и
   модельную поверхность (`enabled_tools`), и границу с агентом. Всё новое
   живёт внутри этих же границ.
3. Отдельная capability потребовала бы второго `CapabilitySettings` ради одной
   операции, а реестр объявляет набор инструментов capability, а не операций.

Общий контракт вызовов — личность, конверт `_execution`, коды отказа — описан в
`workspace/skills/enterprise_mcp/SKILL.md` и
`openspec/specs/runtime/call-contract/spec.md`. Граница доступа к файлам сессии
описана в `openspec/specs/runtime/session-files/spec.md`. Здесь описано только то,
что операция делает с документом и его состоянием.

## Scope

`platform` — обе операции живут в платформе; домен — `mcp-platform/libs/legal_summarizer/`,
модельная поверхность — `mcp-platform/servers/enterprise/capabilities/legal_summarizer/`
(читание) и `mcp-platform/servers/enterprise/tools/` (запуск и работа с файлами).

Новый вызов: операция `analyze_document`, модели — как `mcp_enterprise_analyze_document`.

В агентском дереве по-прежнему ничего: ни `SKILL.md`, ни tool-обёртки, ни
`exec`-пути. Навык как файл не возвращается.

## ADDED Requirements

### Requirement: Операция запуска разбора

Платформа SHALL объявлять операцию запуска разбора юридического документа
(`analyze_document`, модели — `mcp_enterprise_analyze_document`) и SHALL включать
её в `config.json → tools.mcpServers.enterprise.enabled_tools`.

Операция SHALL быть платформенной, то есть объявленной в
`mcp-platform/servers/enterprise/tools/`, а не внутри
`capabilities/legal_summarizer/service/`: файлами сессии владеет платформа, и
путь к ним достаётся слою исполнения, а не контейнеру capability.

Операция SHALL принимать путь к документу в форме `session://` либо относительный
путь внутри `files/` своей сессии, и SHALL отвергать всё прочее.

#### Scenario: Разбор запускается по вложению пользователя

- **WHEN** вызвана `analyze_document` с `session://`-ссылкой на файл в `files/`
- **THEN** операция SHALL вернуть `status: "completed"` и `operation_id`
- **AND** состояние операции SHALL быть доступно последующему `query_operation`

#### Scenario: Путь вне каталога сессии отвергается

- **WHEN** вызвана `analyze_document` с абсолютным путём, путём с буквой диска
  либо `..` за пределы `files/`
- **THEN** операция SHALL отказать с кодом `invalid_params`
- **AND** LLM-вызовов SHALL NOT быть сделано

### Requirement: Разбор укладывается в потолок вызова

Операция SHALL выполнять разбор **по частям, resumable**, и SHALL возвращать
состояние после каждого шага.

Основание — измеримое расхождение: `platform.json → execution.execution_timeout_sec = 120`
против `confirmation_threshold_sec = 120`, `estimated_chunk_duration_sec = 20` и
`max_chunks_for_execution = 50`, то есть оценка до 1000 с. Поток, не уложившийся в
`execution_timeout_sec`, **не прерывается** — его результат отбрасывается целиком
(`execution/pipeline.py:331-349`), то есть ни ответ, ни артефакт, ни `tool.completed`.
Поэтому разбор, не уложившийся в один вызов, SHALL продолжаться следующим вызовом с
тем же `operation_id`, а не начинаться заново.

Операция SHALL NOT делать вид, что разбор завершён, пока состояние операции не
`completed`: промежуточный результат SHALL нести явный признак незавершённости.

#### Scenario: Длинный документ не уложился в один вызов

- **WHEN** разбор документа не завершён за время вызова
- **THEN** операция SHALL вернуть признак незавершённости и `operation_id`
- **AND** следующий вызов с тем же `operation_id` SHALL продолжить разбор,
  а не начать его заново
- **AND** стоимость SHALL NOT включать повторную оплату уже выполненных шагов

#### Scenario: Повторный вызов того же документа

- **WHEN** `analyze_document` вызвана повторно с тем же документом, тем же
  `length` и тем же `question`
- **THEN** SHALL быть возвращён уже готовый результат
- **AND** LLM-вызовов SHALL NOT быть сделано

### Requirement: Подтверждение остаётся явным

Операция SHALL возвращать `confirmation_required` с оценкой и вариантами и SHALL
NOT выполнять ни одного LLM-вызова, пока подтверждение не передано явным
аргументом операции.

Порог и варианты (`brief` / `detailed` / вопрос) — существующие, домен их не
меняет: `needs_confirmation` (`application/estimation.py:40`), сборка вариантов
(`output/presenter.py`), `confirmation_threshold_sec = 120`
(`llm/config.py:106`).

Отличие от 2.5.3 названо прямо: там меню рисовала модель и подтверждала вызовом
CLI с `--confirm`. В MCP интерактивного меню нет, поэтому подтверждение —
аргумент операции, и операция обязана отличать «не подтверждено» от «подтверждено
явно» безошибочно.

#### Scenario: Длинный документ без подтверждения

- **WHEN** вызвана `analyze_document` без подтверждения, а оценка превышает порог
- **THEN** операция SHALL вернуть `status: "confirmation_required"` с оценкой и
  вариантами
- **AND** LLM-вызовов SHALL NOT быть сделано
- **AND** `operation_id` SHALL быть возвращён, чтобы подтверждение относилось к
  тому же прогону

#### Scenario: Подтверждение получено

- **WHEN** `analyze_document` вызвана с тем же `operation_id` и явным
  подтверждением
- **THEN** разбор SHALL продолжиться, минуя гейт подтверждения

### Requirement: Наработки остаются в папке сессии

Операция SHALL сохранять состояние операции и результат разбора в подкаталогах
своей сессии и SHALL возвращать модели ссылку вида `session://` на результат.

Источник состояния домена (`legal_summarizer.cache_root`, по умолчанию
`mcp-platform/var/legal_summarizer/operations/`) SHALL остаться и SHALL быть
перенесён в подкаталог сессии либо объявлен как его подкаталог — так, чтобы
«где мои наработки» имело один ответ, а не два.

Существующее ограничение сохраняется: `cache_root` — **вне** дерева сессий и не
создаёт в нём ничего. Сегодня это и есть причина того, что накопленное состояние
не находится (в этом дереве каталога `mcp-platform/var/legal_summarizer` нет
вообще, то есть не накоплено ни одной операции), и потому дельта требует явного
объявления, где живёт состояние, а не молчаливого вывода из расположения модуля.

#### Scenario: Результат прочитан в том же обороте

- **WHEN** разбор завершён и вернул `session://`-ссылку
- **THEN** модель SHALL прочитать файл существующей операцией `read_result`
- **AND** путь на машине платформы наружу SHALL NOT отдаваться

#### Scenario: Накопленное состояние не теряется между вызовами

- **WHEN** разбор приостановлен по таймауту и продолжен следующим вызовом
- **THEN** состояние SHALL читаться из того же места, куда было записано
- **AND** каталог сессии SHALL содержать только файлы своей сессии

### Requirement: Границы доступа capability не расширяются

Ни одна capability SHALL NOT получить доступ к файлам сессии этой дельтой.

Запреты стража `mcp-platform/tests/test_tool_execution_boundaries.py` —
`SessionWorkspace`, `ArtifactStore`, `mkdir`, `write_text`/`write_bytes`/
`writelines`, `container.session_workspace` — SHALL остаться в силе без
исключений.

Дело не в недоверии к capability, а в уже названной в страже причине: собственная
запись внутри capability — это второй каталог файлов одной сессии, и он разошёлся
бы с каталогом, который платформа создаёт.

#### Scenario: Страж остаётся зелёным

- **WHEN** страж границ проверяет capability после дельты
- **THEN** список `FORBIDDEN` SHALL быть неизменным
- **AND** проверка на синтетических нарушениях SHALL продолжать ловить их

#### Scenario: Capability не пишет в папку сессии

- **WHEN** capability пишет файл сама
- **THEN** страж SHALL поймать нарушение
