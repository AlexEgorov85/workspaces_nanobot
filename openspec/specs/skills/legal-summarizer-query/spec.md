# skills/legal-summarizer-query Specification

## Purpose

Контракт read-only follow-up вопросов по уже разобранному юридическому
документу: capability `legal_summarizer` отвечает на вопрос по `operation_id`,
не разбирая PDF заново.

Спека описывает поведение capability, а не способ вызова. Граница вызова
(subprocess между tool'ом агента и CLI) снята вместе с агентской обёрткой
(change `2026-10-03-mcp-native-tools`, п. D6): домен вызывается в том же
процессе, модель получает операцию `mcp_enterprise_query_operation`, а
`cli_query.py` остался оболочкой для ручного запуска.

Общий контракт вызовов — личность, конверт `_execution`, коды отказа —
описан в `workspace/skills/enterprise_mcp/SKILL.md` и в
`openspec/specs/runtime/call-contract/spec.md`. Здесь описано только то, что
операция делает с сохранённым состоянием операции.

## Scope

`platform` — домен и операция живут в capability `legal_summarizer`; в агенте не
осталось ни обёртки, ни её регистрации.

Реализация: `mcp-platform/libs/legal_summarizer/`,
`mcp-platform/servers/enterprise/capabilities/legal_summarizer/`.

Вызов: операция `query_operation`, модели — как
`mcp_enterprise_query_operation` (объявлена в
`config.json → tools.mcpServers.enterprise.enabled_tools`).

## Requirements

### Requirement: Manifest diagnostic taxonomy

Capability SHALL различать ровно три причины недоступности manifest: файл
отсутствует, файл повреждён, версия файла не поддерживается.

Домен SHALL сообщать их именами `manifest_not_found` /
`manifest_corrupted` / `manifest_unsupported_version`, а capability SHALL
переводить их в коды конверта `not_found` / `internal` /
`upstream_unavailable` по таблице `_ERROR_CODES` в
`.../legal_summarizer/service/main.py`.

Ключи таблицы SHALL совпадать со значениями `cli_query._MANIFEST_ERROR_TYPES`
буквально: перевод идёт по строке `error_type`, и имя «почти то же самое» молча
уходит в `internal`.

`internal` для повреждённого manifest SHALL оставаться осознанным: файл чинит
владелец состояния, ни «проверь имя», ни «повтори» модели не помогают.

Диагностика SHALL отличать три причины на уровне чтения файла, а не схлопывать
их в один «не найден» (функция `diagnose_manifest`, значения `reason`:
`not_found` / `corrupted` / `unsupported_version`).

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

`load_manifest()` в `mcp-platform/libs/legal_summarizer/cache/manifest.py` SHALL
продолжать возвращать `None` по любой из трёх причин недоступности и SHALL NOT
протаскивать диагностику через возвращаемое значение.

Диагностика SHALL оставаться отдельной функцией (`diagnose_manifest`), SHALL
возвращать не больше полей, нужных для построения конверта (`reason`, `path`,
`version_observed`), и SHALL NOT возвращать разобранное содержимое manifest:
для повреждённого файла его и не получить, а для конверта оно не нужно.

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

`chunks_total` из manifest и список из `field=chunks` SHALL оставаться
независимыми источниками: первый — логический/плановый счётчик, посчитанный при
планировании прогона; второй — список физических файлов
`<operation>/chunks/*.json`, обрезанных по `max_chunk_summary_chars`.

Операция SHALL возвращать оба источника независимо и SHALL NOT пытаться их
согласовать. Расхождение объявлено в
`mcp-platform/libs/legal_summarizer/skill/SKILL.md` — документ переехал вместе с
навыком, отдельного `SKILL.md` в агенте для этого больше нет.

#### Scenario: Расхождение chunks_total и списка chunks — не баг

- **WHEN** `field=stats` вернул `chunks_total = N`, а `field=chunks` вернул
  список длины `M`, где `N != M`
- **THEN** система SHALL считать это допустимым состоянием, а не ошибкой
- **AND** документ платформы SHALL явно называть два источника независимыми

### Requirement: Success-path schema

Операция SHALL различать шесть полей — `stats` / `articles` / `chunks` /
`sections` / `tree` / `all` — и отдавать каждому свою форму. Успешный ответ
SHALL содержать `status = "ok"` и `field` с запрошенным значением.

Опубликованная схема строится из сигнатуры обработчика
(`build_input_schema`): `operation_id` обязателен, `field` — строка **без**
`enum`, `max_chunk_summary_chars` — целое **без** `minimum` / `maximum`.
Ограничения на `field` и `max_chunk_summary_chars` проверял argparse снятой
CLI-обёртки и вместе с ней ушли.

**Зафиксированное следствие, а не замысел.** Значение `field`, не совпадающее ни
с одним из шести, отдаёт ветку `all` целиком — молча, без отказа. Валидация
лежала на снятой обёртке; спека фиксирует фактическое поведение, чтобы его
нельзя было прочитать как намерение.

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
