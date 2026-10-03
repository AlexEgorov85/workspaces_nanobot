# skills/legal-summarizer-query Specification

## Purpose

Что остаётся от контракта follow-up вопросов по разобранному документу после
того, как агентская обёртка и IPC-граница исчезли: capability отвечает на тот
же вопрос по той же семантике полей, но вызывается как операция платформы, а не
как tool агента.

Прежняя спека описывала две разные вещи. Первая — контракт IPC между tool'ом
агента и `cli_query.py`: exit code, разбор stdout, типы wrapper-уровневых
ошибок. Вторая — контракт самих данных: три причины недоступности manifest,
неизменность resume-загрузчика, независимость `chunks_total` и `field=chunks`,
формы успешного ответа.

Первая часть снята целиком (change `2026-10-03-mcp-native-tools`, п. D6):
subprocess-границы больше нет, домен вызывается напрямую. Вторая остаётся и
уточняется — она описывает поведение capability, а не обёртки.

## Scope

`platform` — домен и операция живут в capability `legal_summarizer`; в агенте
не осталось ни обёртки, ни её регистрации.

Реализация: `mcp-platform/libs/legal_summarizer/`,
`mcp-platform/servers/enterprise/capabilities/legal_summarizer/`.

Вызов: операция `query_operation`, модели — как
`mcp_enterprise_query_operation` (объявлена в
`config.json → tools.mcpServers.enterprise.enabled_tools`).

## REMOVED Requirements

### Requirement: Subprocess IPC contract between tool wrapper and CLI query

Требование снято. IPC-границы между tool'ом агента и `cli_query.py` больше не
существует: capability вызывает доменную `query_operation` в том же процессе, а
`cli_query.py` остался оболочкой для ручного запуска и в пути вызова модели не
стоит.

Сценарии прежней редакции — `Successful response`, `Domain error with
structured JSON`, `Non-error JSON on non-zero exit treated as process failure`,
`Empty stdout on non-zero exit treated as process failure`, `Empty response on
success exit`, `Non-JSON on success exit` — описывали комбинации exit code и
разбор stdout. Ни одного из этих условий в новом пути вызова не возникает, все
шесть сняты вместе с требованием.

Причина именно та, а не «перепишем позже»: подпроцесс платил за интерпретатор
ради чтения JSON из уже разобранного документа, и на короткий вопрос «сколько
статей?» это стоило дороже самой работы.

#### Scenario: Границы, которой больше нет, не возникает

- **WHEN** вызывается `query_operation`
- **THEN** домен SHALL быть вызван в том же процессе
- **AND** порождение подпроцесса и разбор его stdout SHALL NOT выполняться

#### Scenario: Успешный путь не различает exit code

- **WHEN** домен вернул payload
- **THEN** операция SHALL отдать его сериализованным
- **AND** значение `returncode` подпроцесса SHALL NOT участвовать в решении

### Requirement: Wrapper passes through structured error fields unchanged

Требование снято в части wrapper-уровневых кодов. Типы `timeout`,
`cli_not_found`, `subprocess_error`, `empty_response`, `invalid_json` и код
`cli_failed` описывали отказы обёртки и её подпроцесса. Ни обёртки, ни
подпроцесса не осталось, поэтому перечислять их нечего, и набор кодов отказа
операции SHALL состоять только из кодов конверта платформы.

Сохранённая часть требования — «доменная ошибка не переписывается» — перенесена
в требование «Manifest diagnostic taxonomy» и в общий контракт вызовов
(`workspace/skills/enterprise_mcp/SKILL.md`): модель читает конверт платформы
напрямую, и переписывать его нечем.

#### Scenario: Кодов wrapper-уровня в перечне не осталось

- **WHEN** перечисляются коды отказа операции `query_operation`
- **THEN** перечень SHALL содержать только коды конверта платформы
- **AND** `cli_failed`, `cli_not_found`, `subprocess_error`, `empty_response`,
  `invalid_json` в перечне SHALL NOT встречаться

## MODIFIED Requirements

### Requirement: Manifest diagnostic taxonomy

Требование существует и в прежней редакции. Три причины недоступности manifest
те же, но доставляются они иначе: не конвертом внутри JSON-строки результата, а
исключением домена, которое capability переводит в код конверта.

Capability SHALL различать ровно три причины: manifest отсутствует, manifest
повреждён, версия manifest не поддерживается. Домен SHALL сообщать их именами
`manifest_not_found` / `manifest_corrupted` /
`manifest_unsupported_version`, а capability SHALL переводить их в коды
конверта `not_found` / `internal` / `upstream_unavailable` по таблице
`_ERROR_CODES` в `.../legal_summarizer/service/main.py`.

Ключи таблицы SHALL совпадать со значениями `cli_query._MANIFEST_ERROR_TYPES`
буквально: перевод идёт по строке `error_type`, и имя «почти то же самое» молча
уходит в `internal` — это уже случалось, и код отказа модели врал.

`internal` для повреждённого manifest SHALL оставаться осознанным: файл чинит
владелец состояния, ни «проверь имя», ни «повтори» модели не помогают.

Диагностика SHALL продолжать отличать эти три причины на уровне чтения файла, а
не схлопывать их в один «не найден» (функция `diagnose_manifest`, значения
`reason`: `not_found` / `corrupted` / `unsupported_version`).

#### Scenario: Manifest отсутствует

- **WHEN** для указанного `operation_id` файла manifest нет
- **THEN** домен SHALL сообщить `manifest_not_found`
- **AND** конверт SHALL нести код `not_found`
- **AND** payload ответа SHALL NOT быть построен

#### Scenario: Manifest повреждён

- **WHEN** файл manifest существует и не разбирается как валидный JSON
- **THEN** домен SHALL сообщить `manifest_corrupted`
- **AND** конверт SHALL нести код `internal`

#### Scenario: Версия manifest не поддерживается

- **WHEN** manifest разбирается, но его `version` не равна текущей (`2`)
- **THEN** домен SHALL сообщить `manifest_unsupported_version`
- **AND** конверт SHALL нести код `upstream_unavailable`
- **AND** текст сообщения SHALL называть наблюдаемую версию

#### Scenario: Версия отсутствует или не приводится к целому

- **WHEN** поле `version` отсутствует либо не приводится к `int`
- **THEN** домен SHALL сообщить `manifest_unsupported_version`
- **AND** `version_observed` SHALL быть `null`
- **AND** конверт SHALL нести код `upstream_unavailable`

#### Scenario: Таблица перевода не разошлась с доменом

- **WHEN** множество ключей `_ERROR_CODES` сравнивается со значениями
  `cli_query._MANIFEST_ERROR_TYPES`
- **THEN** множества SHALL совпадать
- **AND** лишний ключ SHALL считаться ошибкой, а недостающий — молчаливым
  откатом в `internal`

### Requirement: Backward compatibility of resume-path manifest loading

Требование существует и в прежней редакции. Поведение `load_manifest()` не
меняется, меняется его место:
`mcp-platform/libs/legal_summarizer/cache/manifest.py`.

`load_manifest()` SHALL продолжать возвращать `None` по любой из трёх причин
недоступности и SHALL NOT протаскивать диагностику через возвращаемое
значение. Диагностика SHALL оставаться отдельной функцией
(`diagnose_manifest`), SHALL возвращать не больше полей, нужных для
построения конверта (`reason`, `path`, `version_observed`), и SHALL NOT
возвращать разобранное содержимое manifest: для повреждённого файла его и не
получить, а для конверта оно не нужно.

#### Scenario: Resume-загрузчик не изменился

- **WHEN** resume-конвейер читает manifest через `load_manifest()`
- **THEN** функция SHALL вернуть `None` при отсутствующем, повреждённом или
  неподдерживаемом manifest, ровно как прежде
- **AND** диагностика SHALL быть доступна через отдельную функцию
- **AND** разобранное содержимое manifest SHALL NOT попадать в результат
  диагностики

#### Scenario: Manifest исчез между диагностикой и чтением

- **WHEN** диагностика вернула `ok`, а последующая нормализация вернула `None`
- **THEN** домен SHALL сообщить `manifest_not_found`
- **AND** сообщение SHALL называть, что файл стал недоступен между чтением и
  нормализацией

### Requirement: Documented semantics for chunks_total vs field=chunks

Требование существует и в прежней редакции. Само расхождение остаётся, меняется
место, где оно объявлено: `workspace/skills/legal_summarizer/SKILL.md` снят
вместе с навыком, и документом объявления стал
`mcp-platform/libs/legal_summarizer/skill/SKILL.md`.

`chunks_total` из manifest и список из `field=chunks` SHALL оставаться
независимыми источниками: первый — логический/плановый счётчик, посчитанный при
планировании прогона; второй — список физических файлов
`<operation>/chunks/*.json`, обрезанных по `max_chunk_summary_chars`. Операция
SHALL возвращать оба источника независимо и SHALL NOT пытаться их
согласовать.

#### Scenario: Расхождение chunks_total и списка chunks — не баг

- **WHEN** `field=stats` вернул `chunks_total = N`, а `field=chunks` вернул
  список длины `M`, где `N != M`
- **THEN** система SHALL считать это допустимым состоянием, а не ошибкой
- **AND** документ платформы SHALL явно называть два источника независимыми

### Requirement: No change to success-path schema

Требование существует и в прежней редакции, но **границы схемы в нём больше не
верны**, и восстанавливать их не следует: опубликованная схема операции
стротся из сигнатуры обработчика (`build_input_schema`), а обработчик
объявляет `operation_id: str`, `field: str = "stats"` и
`max_chunk_summary_chars: int = 1500`.

Поэтому `field` SHALL оставаться строкой **без** `enum` в опубликованной
схеме, а `max_chunk_summary_chars` — целым числом **без** `minimum` /
`maximum`. Прежние ограничения (перечень из шести значений, `minimum = 100`,
`maximum = 10000`) проверял argparse снятой CLI-обёртки.

Операция SHALL по-прежнему различать шесть полей и отдавать каждому свою форму,
а успешный ответ SHALL содержать `status = "ok"` и `field` с запрошенным
значением.

**Зафиксированное следствие, а не замысел.** Значение `field`, не совпадающее
ни с одним из шести, SHALL отдавать ветку `all` целиком — молча, без отказа.
Валидация легла на снятую обёртку и вместе с ней ушла; спека фиксирует
фактическое поведение, чтобы его нельзя было прочитать как намерение.

#### Scenario: Обязательный и необязательные параметры

- **WHEN** строится опубликованная схема операции
- **THEN** `required` SHALL быть ровно `["operation_id"]`
- **AND** `operation_id` SHALL быть `{"type": "string"}`
- **AND** `field` SHALL быть `{"type": "string"}` без `enum`
- **AND** `max_chunk_summary_chars` SHALL быть `{"type": "integer"}` без границ

#### Scenario: field=stats отдаёт метрики

- **WHEN** вызвано `query_operation` с `field = "stats"` и manifest доступен
- **THEN** ответ SHALL содержать `status = "ok"` и `field = "stats"`
- **AND** SHALL присутствовать `operation_id`, `article_count`, `chunks_total`,
  `sections_total`

#### Scenario: field=chunks отдаёт физические файлы

- **WHEN** вызвано `query_operation` с `field = "chunks"` и manifest доступен
- **THEN** ответ SHALL содержать `status = "ok"`, `field = "chunks"` и `chunk_count`
- **AND** каждый элемент `chunks` SHALL иметь `chunk_id`, `section_id`,
  `section_path`, `page_start`, `page_end`, `summary`
- **AND** `summary` SHALL быть обрезан по `max_chunk_summary_chars`

#### Scenario: Каждое поле отвечает своей формой

- **WHEN** вызвано `query_operation` с `field` из множества
  `articles` / `sections` / `tree` / `all`
- **THEN** ответ SHALL содержать `status = "ok"` и `field` с запрошенным значением
- **AND** тело SHALL иметь форму, соответствующую этому полю, а не общий JSON

#### Scenario: Неизвестное поле молча отдаёт manifest целиком

- **WHEN** вызвано `query_operation` с `field`, не совпадающим ни с одним из шести
- **THEN** ответ SHALL содержать `status = "ok"` и `field` с переданным значением
- **AND** ответ SHALL содержать ключ `manifest` целиком
- **AND** отказа SHALL NOT быть

#### Scenario: Без сервиса операция не собирается

- **WHEN** сервис `legal_summarizer` отсутствует в контейнере
- **THEN** загрузка операции SHALL отказать ошибкой сборки
- **AND** сервер SHALL NOT подняться с операцией, отвечающей отказом на каждый
  вызов
