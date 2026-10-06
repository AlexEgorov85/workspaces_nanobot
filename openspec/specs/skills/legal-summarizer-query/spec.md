# skills/legal-summarizer-query Specification

## Purpose

Контракт read-only follow-up вопросов по уже разобранному юридическому
документу: capability `legal_summarizer` отвечает на вопрос по `operation_id`,
не разбирая PDF заново.

Спека описывает поведение capability, а не способ вызова. Граница вызова
(subprocess между tool'ом агента и CLI) снята вместе с агентской обёрткой
(change `2026-10-03-mcp-native-tools`, п. D6): домен вызывается в том же
процессе, модель получает операцию
`mcp_enterprise_legal_summarizer_query_operation`, а
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

Вызов: операция `legal_summarizer.query_operation`, модели — как
`mcp_enterprise_legal_summarizer_query_operation` (объявлена в
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

- **WHEN** вызвано `legal_summarizer.query_operation` с `field = "stats"` и manifest доступен
- **THEN** ответ SHALL содержать `status = "ok"` и `field = "stats"`
- **AND** SHALL присутствовать `operation_id`, `article_count`, `chunks_total`,
  `sections_total`

#### Scenario: field=chunks отдаёт физические файлы

- **WHEN** вызвано `legal_summarizer.query_operation` с `field = "chunks"` и manifest доступен
- **THEN** ответ SHALL содержать `status = "ok"`, `field = "chunks"` и `chunk_count`
- **AND** каждый элемент `chunks` SHALL иметь `chunk_id`, `section_id`,
  `section_path`, `page_start`, `page_end`, `summary`
- **AND** `summary` SHALL быть обрезан по `max_chunk_summary_chars`

#### Scenario: Каждое поле отвечает своей формой

- **WHEN** вызвано `legal_summarizer.query_operation` с `field` из множества
  `articles` / `sections` / `tree` / `all`
- **THEN** ответ SHALL содержать `status = "ok"` и `field` с запрошенным значением
- **AND** тело SHALL иметь форму, соответствующую этому полю, а не общий JSON

#### Scenario: Неизвестное поле отказывает, а не отдаёт manifest целиком

- **WHEN** вызвано `legal_summarizer.query_operation` с `field`, не совпадающим ни с одним из шести
- **THEN** SHALL быть возвращён доменный отказ `invalid_field`
- **AND** код конверта SHALL быть `invalid_params`
- **AND** сообщение SHALL содержать перечень допустимых полей
- **AND** чтение manifest SHALL NOT выполняться

Раньше это поведение было записано здесь как факт: схема строится из сигнатуры
обработчика, поэтому у `field` нет `enum`, и значение не из перечня уходило в
ветку `all` — модель получала `status = "ok"` и весь manifest целиком вместо
отказа. Это дефект платформы, а не особенность интерфейса, и перечисление
полей переехало в код домена, а не в текст спеки.

#### Scenario: Без сервиса операция не собирается

- **WHEN** сервис `legal_summarizer` отсутствует в контейнере
- **THEN** загрузка операции SHALL отказать ошибкой сборки
- **AND** сервер SHALL NOT подняться с операцией, отвечающей отказом на каждый
  вызов

## Responsibility

Спека отвечает за **чтение сохранённого состояния ранее выполненного
разбора** и за честный отказ, когда состояния нет или оно протухло. Всё
остальное — запуск разбора, его стоимость, LLM — принадлежит операции
`platform.analyze_document`.

Владелец по `## Scope` — `platform`. В дереве агента реализации нет:
`lib/` и `workspace/` не содержат ни одного файла домена, и
`tools.mcpServers.enterprise` в `config.json` объявляет
`platform.query_operation` (строки enabled_tools), а не
`legal_summarizer.*`.

Три обязанности, которые спека закрепляет:

1. таксономия диагностики состояния (четыре доменных `error_type` плюс
   `invalid_field`);
2. backward compatibility resume-пути: диагностика не ломает загрузчик
   resume;
3. форма ответа по `field` — шесть полей, у каждого своя форма.

Чего спека **не** делает: не описывает способ вызова из агента. Граница
вызова (subprocess между tool'ом агента и CLI) снята изменением
`2026-10-03-mcp-native-tools` — см. `## Boundary`.

## Boundary

**Граница — процесс платформы `enterprise-mcp`.** Всё исполняется в одном
процессе: операция платформы вызывает доменный `query_operation`
непосредственно, без subprocess и без CLI.

Внутри границы:

- операция чтения — `mcp-platform/servers/enterprise/tools/query_operation.py`;
- доменный читатель — `mcp-platform/libs/legal_summarizer/cli_query.py::query_operation`
  (`mcp-platform/libs/legal_summarizer/cli_query.py:376`);
- диагностика манифеста — `mcp-platform/libs/legal_summarizer/cache/manifest.py`;
- файлы состояния в папке сессии, корень у писателя
  `platform.analyze_document.state_root`.

Вне границы:

- **агент.** `lib/` и `workspace/` домена не содержат; model-facing имя
  операции приходит из `MCPProvider`, а не из агентского кода;
- **запуск разбора.** Это `platform.analyze_document`; данная операция
  документ не разбирает и не запускает LLM;
- **владелец файлов сессии.** Им является платформа, а не capability:
  страж `mcp-platform/tests/test_tool_execution_boundaries.py` запрещает
  capability касаться `SessionWorkspace`/`ArtifactStore`. Поэтому
  операция объявлена в `servers/enterprise/tools/`, а не внутри capability;
- **CLI-оболочка.** `cli_query.py` остаётся входом для ручного запуска,
  но в пути вызова модели не стоит.

## Public Contract

**Имя операции.** По коду — `platform.query_operation`
(`mcp-platform/servers/enterprise/tools/query_operation.py:233`, поле
`name=` у `ToolDefinition`), capability — `"platform"`
(`query_operation.py:236`). Модели операция приходит как
`mcp_enterprise_platform_query_operation`; это имя объявлено в
`config.json → tools.mcpServers.enterprise.enabled_tools` как
`platform.query_operation`.

Расхождение с прозой спеки, которое следует считать дефектом текста, а не
интерфейса: `## Purpose` и `## Scope` называют
`legal_summarizer.query_operation` /
`mcp_enterprise_legal_summarizer_query_operation`. Такого имени в коде
нет; имя `legal_summarizer` сохранилось только как **тег**
(`query_operation.py:237`, `tags=("legal_summarizer", "session")`). Оно
было именем capability до того, как операцию перенесли в
`servers/enterprise/tools/` (change
`2026-10-05-legal-summarizer-session-scope`), потому что у
capability-операции нет `ctx`, а чтение состояния сессии требует `ctx`.

**Сигнатура обработчика** (`query_operation.py:179-184`):

```
operation_id: str
field: str = "stats"
max_chunk_summary_chars: int = 1500
```

Схема публикуется из сигнатуры: `build_input_schema(definition.handler)`
(`query_operation.py:243`), предварительно `validate_handler`
(`query_operation.py:242`). Следствие, важное для читателя: у `field` нет
`enum` в схеме, поэтому проверка перечня живёт в коде домена.

**Допустимые `field`** — `frozenset` из шести имён
(`mcp-platform/libs/legal_summarizer/cli_query.py:112-114`):
`stats`, `articles`, `chunks`, `sections`, `tree`, `all`. Проверка стоит
**до** чтения с диска (`cli_query.py:418`).

**Таблица кодов конверта** (`query_operation.py:52-57`) — четыре ключа,
буквально совпадающих с ключами `cli_query.DOMAIN_ERROR_TYPES`
(`cli_query.py:121-123`):

| доменный `error_type` | код конверта |
|---|---|
| `manifest_not_found` | `not_found` |
| `manifest_corrupted` | `internal` |
| `manifest_unsupported_version` | `upstream_unavailable` |
| `invalid_field` | `invalid_params` |

## Inputs

Три аргумента, все из опубликованной схемы:

- `operation_id` (обязательный) — идентификатор из ответа
  `platform.analyze_document`. Без него операция бессмысленна, поэтому
  пустое значение отвергается как `invalid_params`
  (`query_operation.py:71-72`).
- `field` (опциональный, дефолт `stats`) — какое срез состояния отдать.
- `max_chunk_summary_chars` (опциональный, дефолт 1500) — обрезка сводки
  чанка; неположительное значение отвергается как `invalid_params`
  (`query_operation.py:75-79`).

Вход, которого нет в схеме и который приходит из контекста: `ctx` с
`ctx.session_id`. Именно он выбирает корень состояния
(`query_operation.py:199-214`).

**Вход из файловой системы:** состояние по `operation_id` в корне,
указанном писателем `state_root(handle)`; при вызове без сессии — в
`fallback_cache_root`, объявленном владельцем и живом **только** при
отсутствии сессии (`query_operation.py:202-211`).

## Outputs

Возврат — **строка JSON** (`query_operation.py:220`,
`json.dumps(..., ensure_ascii=False, default=str)`), то есть MCP-текстовый
результат, а не структурный объект.

Форма зависит от `field`; для `all` отдаётся манифест целиком, для
остальных — свой срез:

- `stats` — метрики и `article_count`;
- `articles` — список статей;
- `chunks` — сводки по чанкам, обрезанные по
  `max_chunk_summary_chars`;
- `sections` — список разделов;
- `tree` — иерархия разделов;
- `all` — манифест целиком.

**Ключевое свойство незавершённого состояния:** отдаётся его собственный
`status` и `progress_report` с `continues: true`, а **не** `status: "ok"`
из полупустого манифеста (`query_operation.py:193-195`). Это то, что
отличает «разбор идёт» от «разбор не начинали».

Побочный эффект успеха — отметка обращения `access_marker` с
`subdir="artifacts"`, от которой уборка знает возраст состояния
(`query_operation.py:101-116`).

## State

Операция **не владеет состоянием и не создаёт его**: она читает то, что
написал `platform.analyze_document`.

Читаемое состояние:

- нормализованный манифест по `operation_id` — источник статуса и
  срезов (`_status_of`, `query_operation.py:123-128`, через
  `load_manifest`);
- надгробие `tombstone` у удалённого по сроку состояния
  (`load_tombstone`, `query_operation.py:156`) — читается, чтобы
  отличить «протухло» от «не существовало».

Записывает операция ровно одно: **отметку обращения**
(`last_access_at`, `status`) в подкаталоге `artifacts`
(`query_operation.py:108-116`). Падение записи отметки проглатывается
(`except OSError: return`, `query_operation.py:117-120`) — без неё
состояние просто уберут по возрасту от начала работы.

Оговорка к формулировке «read-only»: неприменимость относится к
**содержимому разбора**. Строгое утверждение «операция ничего не
записывает» было бы неверно: отметку обращения она пишет. На этом
основании исполнимость `mcp-platform/tests/legal_summarizer/architecture/test_document_cache_boundaries.py`
и разделы `## Boundary` / `## Forbidden Behavior` сформулированы через
запрет трогать чужие хранилища, а не через запрет записи.

## Dependencies

Прямые, все внутри `mcp-platform/`:

- `mcp-platform/servers/enterprise/tools/query_operation.py` — операция,
  перевод доменных отказов в коды конверта, работа с папкой сессии;
- `mcp-platform/libs/legal_summarizer/cli_query.py` — доменный читатель,
  `_FIELDS`, `DOMAIN_ERROR_TYPES`, `LegalQueryError`;
- `mcp-platform/libs/legal_summarizer/cache/manifest.py` — `load_manifest`
  и диагностика состояния манифеста;
- `mcp-platform/servers/enterprise/tools/analyze_document.py` — импорт
  `state_root`, `access_marker`, `load_tombstone`, `ARTIFACTS_SUBDIR`
  (`query_operation.py:42-47`). **Это направление зависимости важно:**
  читатель состояния берёт корень у писателя, а не наоборот;
- `libs.enterprise_common.session.workspace.SessionWorkspace` — владелец
  файлов сессии;
- `libs.enterprise_common.registry` — `ToolDefinition`,
  `build_input_schema`, `validate_handler`.

Косвенная зависимость предметного смысла: LLM-провайдер — только через
писателя. Сама операция LLM не вызывает.

Внешних зависимостей (HTTP, сеть, файлы вне папки сессии) нет.

## Configuration

Настроек у операции нет: ни одного ключа конфигурации ни в
`config.json`, ни в `mcp-platform/platform.json` она не читает.

Параметры, которые влияют на её поведение, объявлены **не здесь**, и
это следует учитывать при чтении:

- `fallback_cache_root` — единственный параметр конструктора
  `create_tool` (`query_operation.py:171`), и он применяется **только**
  при вызове без сессии (`query_operation.py:202-211`). Значение
  приходит от владельца домена, а не из конфига модели;
- набор допустимых `field` — не конфигурация, а `frozenset` в коде домена
  (`cli_query.py:112-114`);
- время жизни состояния и уборка по сроку — в домене, ключи домена
  читаются из `mcp-platform/platform.json`.

Практический вывод: изменить поведение операции переключателем в
`config.json` нельзя. Единственный управляемый снаружи параметр — длина
обрезки сводки, и она передаётся аргументом вызова, а не конфигурацией.

## Lifecycle

Операция **не имеет собственного жизненного цикла**: она не поднимается,
не останавливается, не держит ресурсов. Она регистрируется при сборке
сервера как одна из операций каталога `servers/enterprise/tools/`, и её
жизненный цикл совпадает с жизненным циклом процесса платформы.

Что происходит за один вызов:

1. `_check_arguments` — отказ по аргументу до чтения с диска
   (`query_operation.py:197`);
2. выбор корня: сессия → `workspace.handle(ctx.session_id, create=False)`,
   иначе `fallback_cache_root` (`query_operation.py:199-211`). Флаг
   `create=False` означает, что вызов чтения **не создаёт** папку
   сессии;
3. `_refuse_if_swept` — проверка протухания **только** при отсутствии
   состояния (`query_operation.py:215`, `131-168`);
4. `_touch_access` — отметка обращения (`query_operation.py:216`);
5. `_query` — доменный вызов и сериализация (`query_operation.py:217-220`).

Асимметрия, которую важно не потерять: проверка протухания стоит **до**
чтения состояния, но срабатывает **только** если состояния нет
(`query_operation.py:154-155`). Надгробие рядом с живым состоянием
означало бы устаревшую запись, и объявлять протухание читаемого состояния
значило бы отказать в чтении того, что есть.

## Data Ownership

**Владение данными не у операции, а у платформы.** Операция — читатель с
пратом отметки обращения.

Конкретно по проверенному коду:

| Данные | Владелец | Что делает операция |
|---|---|---|
| манифест состояния (`manifest.json`) | `platform.analyze_document` как writer | читает |
| надгробие убранного состояния | уборка домена | читает |
| отметка обращения | сама операция | пишет, best-effort |
| папка сессии (`SessionWorkspace`) | платформа | `handle(..., create=False)` — создать не может |

Граница владения закреплена стражем
`mcp-platform/tests/test_tool_execution_boundaries.py`, запрещающим
capability касаться `SessionWorkspace`/`ArtifactStore`. Именно поэтому
операция объявлена в `servers/enterprise/tools/`, а не внутри capability
`legal_summarizer`: у capability-операции нет `ctx`, то есть нет доступа
к папке сессии.

## Error Behavior

Пять различимых исходов, все по коду:

1. **Отказ по аргументу** (`_check_arguments`, `query_operation.py:60-79`)
   → `InvalidRequestError` с кодом `invalid_params`. Пустой
   `operation_id` или `field`, неположительный `max_chunk_summary_chars`.
   Проверка до чтения с диска — иначе на несуществующем `operation_id`
   модель получила бы «manifest не найден» и решила бы, что ошиблась в
   имени, а не в поле (`cli_query.py:414-417`).
2. **Неизвестное поле** (`cli_query.py:418-429`) → `LegalQueryError` с
   `error_type: invalid_field`, переводится в `invalid_params`
   (`query_operation.py:56`). Сообщение содержит перечень допустимых
   полей. Чтение манифеста не выполняется.
3. **Состояния нет** → `manifest_not_found` → `not_found`.
4. **Манифест повреждён** → `manifest_corrupted` → `internal`.
5. **Версия не поддерживается** → `manifest_unsupported_version` →
   `upstream_unavailable`.
6. **Состояние убрано по сроку** → тот же код `not_found`, что и у
   состояния, которого не было никогда (`query_operation.py:148-168`).
   Отдельный код **заведомо не заводится**: отличается объяснение, а не
   причина отсутствия, а новый код разошёлся бы со словарём
   `_ERROR_CODES`.

Правило перевода: `_query` ловит `LegalQueryError` и подставляет
`_ERROR_CODES.get(str(payload.get("error_type")), "internal")`
(`query_operation.py:93-98`). Ключ, которого нет в словаре, уходит в
`internal` — поэтому ключи обязаны совпадать с доменными буквально.

Дополнительно: отсутствие сервиса `legal_summarizer` в контейнере — отказ
**сборки**, а не отказ вызова (см. требование «Без сервиса операция не
собирается»).

## Invariants

1. **Имя операции — `platform.query_operation`.** `legal_summarizer`
   осталось только тегом (`query_operation.py:237`), и в наборе
   инструментов модели его нет.
2. **Проверка аргументов предшествует чтению состояния**
   (`query_operation.py:197` до `217`). Нарушение меняет смысл отказа:
   модель получила бы «manifest не найден» вместо «поле неверно».
3. **`field` проверяется по `_FIELDS`, а не по ветвям `if`**
   (`cli_query.py:112-114`). Перечисление в ветвях разъезжалось с ними, и
   неизвестное поле молча уходило в ветку `all` — модель получала
   `status: "ok"` и весь манифест вместо отказа.
4. **Коды `_ERROR_CODES` совпадают с `DOMAIN_ERROR_TYPES` буквально**
   (`query_operation.py:50-51`, `cli_query.py:121-123`). «Почти то же
   самое» имя уходит в `internal`.
5. **`fallback_cache_root` применяется только при отсутствии сессии**
   (`query_operation.py:199-211`). При сессии читать надо там, где пишет
   `analyze_document`, иначе читатель искал бы состояние не там.
6. **Операция не создаёт папку сессии** — `handle(create=False)`.
7. **Незавершённый разбор отдаёт свой `status` и
   `progress_report` с `continues: true`**, а не `status: "ok"`.
8. **Проверка протухания не отказывает в живом состоянии**: она
   срабатывает только когда состояния нет.
9. **Чтение состояния не перезапускает разбор** — ни LLM-вызовов, ни
   записи состояния, ни создания нового `operation_id`.

## Forbidden Behavior

1. **Переименовать операцию в `legal_summarizer.query_operation`.** Имя
   пришло из прошлой архитектуры, где читатель жил внутри capability;
   после переноса в `servers/enterprise/tools/` имя обязано совпадать с
   `config.json → tools.mcpServers.enterprise.enabled_tools`, иначе
   модель получит отказ по инструменту, которого нет в наборе.
2. **Вернуть `manifest` целиком на неизвестное поле.** Это дефект, а не
   особенность интерфейса: модель получит `status: "ok"` вместо отказа.
3. **Читать состояние до проверки аргументов.**
4. **Выводить доменный код отказа, отсутствующий в `_ERROR_CODES`, без
   явного перевода** — он молча станет `internal`.
5. **Применять `fallback_cache_root` при наличии сессии** — читатель будет
   искать состояние не там, где его пишут.
6. **Создавать папку сессии из операции чтения** (`create=False`).
7. **Заводить отдельный код конверта для протухшего состояния** — это
   разошлось бы со словарём `_ERROR_CODES`, который переводит доменные
   `error_type`.
8. **Описывать протухание причиной, отличной от «убрано по сроку».**
   Причина у уборки одна; иная формулировка означала бы потерю следа.
9. **Запускать разбор из операции чтения** или вызывать LLM: разбор —
   работа `platform.analyze_document`.
10. **Касаться файлов сессии из capability.** Операция обязана оставаться
    в `servers/enterprise/tools/`; страж
    `mcp-platform/tests/test_tool_execution_boundaries.py` запрещает
    capability доступ к `SessionWorkspace`/`ArtifactStore`.
11. **Делать падение записи отметки обращения отказом вызова** — оно
    проглатывается намеренно (`query_operation.py:117-120`).

## Consumers

| Потребитель | Как использует | Что получает |
|---|---|---|
| Модель агента | `mcp_enterprise_platform_query_operation` | JSON-срез состояния по `field` |
| `workspace/skills/enterprise_mcp/SKILL.md` | документирует операцию и коды отказа | контракт для агента |
| `mcp-platform/libs/legal_summarizer/skill/SKILL.md` | то же, доменная сторона | перечень полей и отказов |
| `workspace/TOOLS.md` | раздел `platform.query_operation` | пользовательская документация |
| Оператор платформы | стартовый баннер capability | факт, что операция поднялась |

Активных вызовов из кода агента нет: потребитель — модель через
`MCPProvider`, а не импорт. Оболочка `cli_query.py` обслуживает ручной
запуск и в пути вызова модели не стоит.

## Implementation

Все пути проверены `Test-Path`; все существуют.

- `mcp-platform/servers/enterprise/tools/query_operation.py` — операция
  чтения: имя, схема, проверка аргументов, перевод кодов, выбор корня,
  проверка протухания, отметка обращения.
- `mcp-platform/libs/legal_summarizer/cli_query.py` — доменный
  `query_operation` (строка 376), `_FIELDS` (112), `DOMAIN_ERROR_TYPES`
  (121), `LegalQueryError` (126), отказ по полю (418).
- `mcp-platform/libs/legal_summarizer/cache/manifest.py` — `load_manifest`
  и диагностика состояния манифеста, версия манифеста.
- `mcp-platform/servers/enterprise/tools/analyze_document.py` — владелец
  `state_root`, `access_marker`, `load_tombstone`, `ARTIFACTS_SUBDIR`.
- `mcp-platform/servers/enterprise/capabilities/legal_summarizer/service/main.py`
  — точка, где домен встречается с платформой; после переноса операции
  отдаёт домен, а не реестр операций.
- `mcp-platform/libs/legal_summarizer/` — домен в целом: девять
  runtime-слоёв (`application`, `cache`, `chunking`, `document`,
  `execution`, `llm`, `output`, `planning`, `retrieval`) плюс каталог
  `skill`.
- `./config.json` — строка `platform.query_operation` в
  `tools.mcpServers.enterprise.enabled_tools`.

Чего в дереве **не существует** и что не следует искать: флага `--operation`
у `mcp-platform/libs/legal_summarizer/cli.py` (файл существует, но сам флаг
снят — остались `--file`, `--question`, `--operation-id` и другие; запуск из
агента невозможен), каталога
`workspace/skills/legal_summarizer/scripts/`, операции
`legal_summarizer_query` (снята — `mcp-platform/libs/legal_summarizer/skill/SKILL.md:246-248`),
модуля `.../legal_summarizer/service/main.py` с реестром операций домена.

## Verification

Все пути проверены `Test-Path`; все существуют.

- `mcp-platform/tests/test_legal_summarizer_capability.py` — сборка
  capability и поведение операции.
- `mcp-platform/tests/test_declared_failure_codes.py` — сверка таблицы
  кодов с доменными `error_type`. Это страж инварианта 4.
- `mcp-platform/tests/legal_summarizer/test_manifest_diagnose.py` —
  таксономия диагностики манифеста (`not_found` / `corrupted` /
  `unsupported_version`).
- `mcp-platform/tests/legal_summarizer/architecture/test_document_cache_boundaries.py`
  — границы владения файлами состояния.
- `mcp-platform/tests/test_tool_execution_boundaries.py` — запрет
  capability касаться `SessionWorkspace`/`ArtifactStore`.
- `mcp-platform/tests/test_tool_execution_pipeline.py` — сквозной путь
  вызова операции платформы.
- `mcp-platform/tests/test_architecture_boundaries.py` — размещение
  операции в каталоге платформенных tools.

Честная граница: отдельного теста **на имя операции** в
`mcp-platform/tests/` нет — совпадение `platform.query_operation` между
`mcp-platform/servers/enterprise/tools/query_operation.py:233`, `./config.json`
и документацией держится на
`tests/test_agent_facing_docs_contract.py` со стороны агента (он проверяет
провозглашаемые имена) и на ревью со стороны платформы. Расхождение
имени — самый вероятный источник регрессии именно здесь, потому что
оно не роняет ни один из перечисленных тестов: операция с другим именем
просто не попадёт в `enabled_tools`.
