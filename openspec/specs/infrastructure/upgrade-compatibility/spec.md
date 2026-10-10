# upgrade-compatibility Specification

## Purpose
Определяет нормативный контракт совместимости проекта с апгрейдами upstream `nanobot-ai`: какие поверхности должны оставаться стабильными, как фиксируется версия, какие тесты обязательны перед merge upgrade-изменения. Цель — сократить цикл `apply_all → pytest` и предотвратить регрессии при каждом следующем релизе nanobot.

## Scope

`agent` — контракт совместимости и состав зависимостей — репозитория агента
Реализация: `requirements.txt`, `tests/contract/`

## Requirements

### Requirement: Фиксация версии nanobot

Проект SHALL ДОЛЖЕН закреплять точную версию `nanobot-ai` в `requirements.txt` через `==` без range-операторов.

#### Scenario: Апгрейд версии зафиксирован в requirements.txt

- **WHEN** выполняется `pip install nanobot-ai==X.Y.Z`
- **THEN** строка `nanobot-ai==X.Y.Z` присутствует в `requirements.txt` и `pip show nanobot-ai` возвращает ровно эту версию

### Requirement: Контрактные тесты на поверхность nanobot

Проект SHALL ДОЛЖЕН содержать каталог `tests/contract/` с тестами, проверяющими внешний контракт используемых символов nanobot (сигнатуры публичных методов, наличие ожидаемых классов/функций).

#### Scenario: Все контрактные тесты зелёные перед merge upgrade

- **WHEN** выполняется `pytest tests/contract -q`
- **THEN** результат SHALL ДОЛЖЕН быть `0 failed`, иначе merge upgrade-изменения запрещён

#### Scenario: Контрактные тесты фиксируют версионно-специфичные имена

- **WHEN** тест ссылается на символ nanobot (например, `AgentLoop._assemble_outbound`, `Consolidator.archive_session`)
- **THEN** тест MUST ДОЛЖЕН быть обновлён в том же изменении при смене версии nanobot, без мокинга или skip-флагов

### Requirement: RuntimePatcher.apply_all без failed-патчей

`RuntimePatcher.apply_all` SHALL ДОЛЖЕН завершаться с `report.failed == []` после прохождения `tests/contract/`.

#### Scenario: Smoke после upgrade

- **WHEN** `RuntimePatcher.apply_all` вызван в новой версии nanobot
- **THEN** для каждого patched target MUST ДОЛЖЕН быть `report.failed` пустой, иначе патч MUST ДОЛЖЕН быть помечен `DEPRECATED` (отключён) или исправлен до merge

#### Scenario: Отключённый патч не возвращает failed

- **WHEN** патч возвращает `False, "<reason>"` из-за отсутствующего upstream-символа
- **THEN** он MUST ДОЛЖЕН быть помечен как `DEPRECATED` в `_PATCH_SPECS` (`RuntimePatcher.patch_specs()`) с явным комментарием о том, какой upstream-механизм заменил функцию

### Requirement: Реактивная миграция на upstream-механизмы

Если upstream добавил встроенную подсистему, дублирующую наш патч или сервис, проект SHALL ДОЛЖЕН удалить дубль и подключиться к upstream-API в том же upgrade-изменении.

#### Scenario: Встроенный `/compact` заменил наш patch_compact_command

- **WHEN** upstream регистрирует `/compact` через `register_builtin_commands`
- **THEN** наши `lib/commands/compact_command.py` и `patch_compact_command` уже удалены (каталога `lib/commands/` нет, символ `patch_compact_command` не встречается ни в одном дереве), а наш `ContextCompactionService._notify` MUST ДОЛЖЕН срабатывать через `AgentHook.after_run` после upstream-обработчика

#### Scenario: Auto-compact-idle guard заменён upstream'ом

- **WHEN** upstream `_is_expired` уже short-circuit'ит при `_ttl <= 0`
- **THEN** наш `patch_auto_compact_idle_guard` MUST ДОЛЖЕН быть удалён без замены

#### Scenario: ToolContext добавил runtime_control

- **WHEN** upstream `ToolContext.__init__` принимает `runtime_control`
- **THEN** наш `patch_project_tools` MUST ДОЛЖЕН передавать `runtime_control=agent._runtime_control` в `ToolContext(...)`, иначе инструменты с зависимостью от runtime-control получают `None`

### Requirement: Использование upstream EventSink вместо ручных обёрток

Если upstream предоставляет `EventSink` с типизированными событиями для подсистемы, на которой у нас висит обёртка, проект SHALL ДОЛЖЕН подписываться на события вместо оборачивания внутренних методов.

#### Scenario: ContextCompactionEvent вместо обёртки Consolidator

- **WHEN** upstream публикует `ContextCompactionEvent` через `EventSink`
- **THEN** наш `ContextCompactionService._notify` MUST ДОЛЖЕН подписываться на это событие, а не оборачивать `Consolidator.maybe_consolidate_by_tokens` (которого может не быть в следующих версиях)

### Requirement: Документация runtime-patcher inventory

`docs/architecture/runtime-patcher-inventory.md` SHALL ДОЛЖЕН содержать актуальный каталог каждого патча с колонками: target, status (`OK`/`BROKEN_SIG`/`MISSING`/`DEPRECATED`/`MOVED`), версия nanobot, тесты.

#### Scenario: Каждый патч имеет запись в inventory

- **WHEN** добавляется новый патч в `RuntimePatcher`
- **THEN** запись в `runtime-patcher-inventory.md` MUST ДОЛЖЕН появиться в том же коммите

#### Scenario: Удалённый патч помечен как DEPRECATED

- **WHEN** патч удаляется из-за замены upstream-механизмом
- **THEN** соответствующая запись MUST ДОЛЖЕН сохраниться в inventory со статусом `DEPRECATED` и ссылкой на заменивший upstream-символ

### Requirement: Не ломать профильные smoke-тесты

`tests/test_profile_lifecycle.py`, `tests/test_pg_session_manager.py` (framework contract). Раньше стоил быть `tests/test_history_search_tool.py` (scope-isolation) — файла нет, реализация ушла в `mcp-platform/servers/enterprise/capabilities/data/tools/history_search.py` — SHALL НЕ ДОЛЖНЫ падать после upgrade-изменений; если они падают по причинам, не относящимся к upgrade, они MUST ДОЛЖНЫ быть помечены `xfail` с явной причиной и `TODO` ссылкой в `design.md`.

#### Scenario: Падающий тест не из upgrade-скоупа

- **WHEN** тест не из `tests/contract/` и не относится к `runtime_patcher.py`, падает на новой версии
- **THEN** изменение MUST ДОЛЖЕН быть помечено `xfail` с reason, ссылающимся на задачу вне scope этого изменения

## Responsibility

Контракт **процесса**, а не подсистемы: он отвечает на вопрос «что должно
остаться рабочим после апгрейда `nanobot-ai` и как это проверяется», а не
«что подсистема делает в рантайме». Владелец — репозиторий агента
(`Scope: agent`).

Нормативно спека владеет тремя вещами:

1. фиксацией версии upstream (`requirements.txt`, ровно один `==`);
2. набором проверок, которые обязаны быть зелёными до merge
   upgrade-изменения (`tests/contract/`, `RuntimePatcher.apply_all`,
   профильные smoke-тесты);
3. правилом отката наших патчей на публичные upstream-механизмы, когда те
   появляются.

Сама спека **ничего не выполняет и не наблюдает**. Ни один её раздел не
соответствует классу, сервису или процессу, который можно вызвать.

## Boundary

**Граница — текстовые артефакты репозитория и порядок их обновления.**

Внутри границы:

- `requirements.txt` — единственное место, где закреплена версия;
- `tests/contract/` — единственный каталог контрактных проверок;
- `lib/services/runtime_patcher.py` — реестр патчей (`_PATCH_SPECS`);
- `docs/architecture/runtime-patcher-inventory.md` — human-readable каталог
  того же реестра;
- маркер `contract` в `pyproject.toml`, отделяющий контрактные тесты от
  платных.

Вне границы (то есть **не** её ответственность):

- поведение любой подсистемы агента во время работы — предмет её спеки;
- содержимое `CHANGELOG.md` и `docs/architecture/`;
- сам процесс установки зависимостей (`pip`, `uv`) — его правила задаёт
  `requirements.txt`, а не эта спека;
- версии прочих зависимостей: спека закрепляет **только** `nanobot-ai`.

## Public Contract

Публичного API в обычном смысле у предмета нет: это контракт над
артефактами репозитория. Наблюдаемая поверхность — четыре сущности:

| Сущность | Где объявлена | Что это |
|---|---|---|
| Версия upstream | `requirements.txt:43` (`nanobot-ai==0.3.5`) | единственная закреплённая точка |
| Контрактные тесты | `tests/contract/` (24 модуля) | проверки внешнего контракта nanobot |
| Реестр патчей | `_PATCH_SPECS` (`lib/services/runtime_patcher.py:271`) | `dict[str, PatchSpec]`, 4 записи |
| Каталог патчей | `docs/architecture/runtime-patcher-inventory.md` | та же таблица для человека |

`PatchSpec` — `@dataclass(frozen=True)`
(`lib/services/runtime_patcher.py:230-268`) с полями `name`, `purpose`,
`nanobot_target`, `reason`, `alternatives_checked`, `risk`,
`nanobot_version`, `required`. `RuntimePatcher.patch_specs()`
(`lib/services/runtime_patcher.py:514-522`) — единственный читатель
`_PATCH_SPECS`, возвращает копию словаря.

Маркер pytest `contract` объявлен в `pyproject.toml:18-21` и описан как
«runs in CI always (not gated)» — то есть контрактные тесты не требуют
`NANOBOT_LIVE_E2E`/`NANOBOT_INTEGRATION` в отличие от соседних маркеров.

## Inputs

Входов у предмета нет в рантайме: ни одного аргумента командной строки,
ни одного файла конфигурации, ни одной переменной окружения.

Вход, который читают потребители этого контракта, — **версия nanobot,
фактически установленная в окружении**. Именно её сравнивают с
`requirements.txt:43`.

Особый случай — Git: правило «версия зафиксирована точным `==`»
(`Requirements`, строка 15) проверяется **чтением файла**, а не
установкой пакета: репозиторию не нужно ни запускать `pip`, ни менять
окружение, чтобы убедиться в выполнении требования.

## Outputs

Направление — наружу (в CI и в ревью), а не в API вызывающей стороны:

- exit code `pytest tests/contract -q` — нормативный сигнал «merge
  разрешён/запрещён»;
- `PatchReport` (`lib/services/runtime_patcher.py:366-421`) с
  `applied`/`skipped`/`failed` и `to_dict()`/`render()` — то, что
  `apply_all` отдаёт наружу как стартовый отчёт;
- строка-баннер инвентаря патчей, которую печатает старт (через
  `PatchReport.render(specs=...)`).

В поток вызовов агента спека ничего не пишет: ни `OutboundMessage`, ни
событий журнала, ни строк таблиц. Это отличает её, например, от спеки
`interfaces/tools-history-search`.

## State

**Неприменимо: предмет не имеет состояния.** Проверено по коду — в
`_PATCH_SPECS` (`lib/services/runtime_patcher.py:271`) лежат четыре
`PatchSpec`; изменение версии nanobot не меняет их содержимое само по
себе, а `PatchReport` создаётся заново на каждый вызов `apply_all`
(`lib/services/runtime_patcher.py:505`) и никуда не сохраняется.

Единственное, что можно считать персистентным состоянием контракта, —
это **текст артефактов на диске**: `requirements.txt`,
`_PATCH_SPECS`, `docs/architecture/runtime-patcher-inventory.md`. Именно
они и должны меняться в том же коммите, что и апгрейд; это требование
проверяется ревью, а не кодом.

Временное состояние процесса (счётчики, кеш импорта) к предмету не
относится.

## Dependencies

**Прямые (предмет → зависимость):**

- `nanobot-ai==0.3.5` (`requirements.txt:43`) — единственная жёсткая
  зависимость. Комментарий на строке 11 фиксирует, что версия 0.3.5
  «поднимается и работает», и отдельно оговаривает, что сервер платформы
  nanobot не требует;
- `pytest` с маркером `contract` (`pyproject.toml:21`);
- `lib/services/runtime_patcher.py` — единственный модуль агента,
  вовлечённый в контракт (реестр патчей).

**Обратные (зависимость → предмет):**

- любой апгрейд `nanobot-ai` требует прохождения контрактных тестов;
- любое добавление патча в `RuntimePatcher` требует записи в
  inventory (`Requirements`, строки 80-85).

Проверено, что `nanobot` не импортируется ни в одном файле
`tests/contract/` как зависимость от нашей обвязки: контрактные тесты
обращаются к API библиотеки напрямую, иначе они проверяли бы сами себя.

## Configuration

Предмет **не имеет собственной секции конфигурации**.

Это проверено по коду: `UsageStoreSettings`
(`lib/core/project_settings.py:143`), `SessionColdSyncSettings`
(`lib/core/project_settings.py:157`) и прочие секции описаны там, а
раздела вида `upgrade`/`compatibility` в `lib/core/project_settings.py`
нет. Единственное, что читает валидатор, — маркер `contract` в
`pyproject.toml`.

Управление параметром, от которого зависит контракт, — версией —
вынесено в `requirements.txt` намеренно: это не конфигурация процесса, а
объявление зависимости. Требование «через `==`, без range-операторов»
(`Requirements`, строка 15) существенно именно поэтому: диапазон сделал
бы воспроизводимость проверки невозможной.

## Lifecycle

Предмет **не имеет жизненного цикла**: он не создаётся, не запускается и
не останавливается. Ни один экземпляр `UpgradeCompatibility` в дереве
отсутствует — проверено поиском по коду.

Жизненный цикл есть только у двух артефактов, и оба живут в чужом:

1. `RuntimePatcher.apply_all` (`lib/services/runtime_patcher.py:465-512`)
   вызывается на старте агента; он применяет четыре патча по порядку —
   `exec_timeout_cap`, `assemble_outbound`, `subagent_logging`,
   `repeat_guard_block` (`lib/services/runtime_patcher.py:506-511`);
2. `tests/contract/` выполняется в CI, а не в рантайме.

Ключевая асимметрия, которую спека фиксирует: `apply_all` **не роняет
старт**. Это прямо написано в `PatchSpec.required`
(`lib/services/runtime_patcher.py:250-258`): поле — только metadata для
баннера, control flow от него не зависит, а `ApplicationContext.create()`
продолжает работу даже при `required=True`. То есть «`report.failed == []`»
из `Requirements` (строка 38) — это **merge-гейт для человека и CI**, а не
runtime-инвариант.

## Data Ownership

**Неприменимо: предмет не владеет данными.** Он не читает и не пишет ни
одной таблицы, ни одного файла данных.

Подтверждение по коду: `_PATCH_SPECS` — статический словарь литералов
(`lib/services/runtime_patcher.py:271-340`), `requirements.txt` читается
только человеком и `pip`, `PatchReport` живёт в памяти один вызов.

Ближайшие владельцы данных в проекте — и то, что этот контракт к ним
**не** причастен: `DbLoggingService` (владелец `agent_gateway_logs`),
`SessionMirror` (владелец `agent_session_meta`/`agent_session_messages`),
`LLMUsageStore` (владелец SQLite-файла usage).

Оговорка, чтобы правило не читалось шире, чем оно есть: контрактные
тесты **выполняются** против настоящего окружения, то есть читают чужое
состояние. Владения это не создаёт — только чтение, и только на время
теста.

## Error Behavior

У предмета нет обработки ошибок в рантайме, потому что нет рантайма.
Вместо этого спека фиксирует **правила классификации отказа патча**:

- патч вернул `(False, "<reason>")` из-за отсутствующего upstream-символа
  → обязан быть помечен `DEPRECATED` в `_PATCH_SPECS` с комментарием о
  том, какой upstream-механизм его заменил
  (`Requirements`, строки 45-48);
- патч вернул `(True, "[INTERNAL_FAILED] …")` → переклассифицируется из
  `applied` в `failed` даже при успехе (`_record`,
  `lib/services/runtime_patcher.py:525-538`): это частичный успех, и он
  обязан быть виден;
- иначе причина классифицируется функцией `_classify_skip`
  (`lib/services/runtime_patcher.py:352-363`): строка попадает в `skipped`
  только если **в точности** совпадает с одним из `_SKIPPABLE_REASONS`;
  всё прочее, включая `[INTERNAL_FAILED]`, даёт `failed`. То есть skip
  нельзя получить «просто потому, что текст похож на skip».

Отдельно: контрактные тесты, падающие **не** по причине апгрейда, не
чинятся в том же изменении — их помечают `xfail` с явной причиной и
ссылкой на `design.md` (`Requirements`, строки 92-99). Это осознанное
разрешение оставить красный тест, а не зелёный скип.

Проверено по коду: падение теста не роняет `ApplicationContext.create()`
(см. § Lifecycle).

## Invariants

1. **Версия закреплена ровно одним `==`.** `requirements.txt:43` —
   `nanobot-ai==0.3.5`; range-оператор там отсутствует.
2. **Реестр патчей и inventory — одно и то же.** Каждый ключ
   `_PATCH_SPECS` имеет строку в
   `docs/architecture/runtime-patcher-inventory.md`, и наоборот.
3. **Патч не роняет старт.** `required=True` — это только подсветка в
   баннере, а не условие abort
   (`lib/services/runtime_patcher.py:250-258`).
4. **Успех с маркером считается отказом.** `[INTERNAL_FAILED]` в detail
   переводит патч из `applied` в `failed`
   (`lib/services/runtime_patcher.py:537`).
5. **Отключённый патч не даёт `failed` в отчёт** — он даёт `skipped`
   с причиной (`_classify_skip`, `lib/services/runtime_patcher.py:352`).
6. **Контрактные тесты не требуют живого окружения** — маркер `contract`
   объявлен как «runs in CI always (not gated)»
   (`pyproject.toml:21`).
7. **Контрактный тест обновляется в том же изменении, что и версия.**
   Смена версии nanobot без правки тестов, ссылающихся на
   версионно-специфичные имена, нарушает контракт
   (`Requirements`, строки 31-34).

## Forbidden Behavior

1. **Закреплять версию диапазоном** (`>=`, `~=`, `<`) — требование,
   делающее воспроизводимость проверки невозможной.
2. **Молча оставлять патч в `failed` после апгрейда.** Единственные
   допустимые исходы — починить, либо снять патч и пометить его
   `DEPRECATED` с указанием заменившего upstream-символа.
3. **Удалять патч из кода, стирая его след в inventory.** Запись с
   статусом `DEPRECATED` обязана остаться.
4. **Добавлять заменяющий патч без записи в inventory** в том же коммите.
5. **Помечать сломанный патч `required=True` и рассчитывать на это как на
   защиту старта** — поле metadata, control flow от него не зависит.
6. **Оправдывать падение теста сменой версии nanobot, если причина вне
   upgrade-скоупа** — вместо этого требуется `xfail` с явной причиной.
7. **Закреплять в этом же контракте версии прочих зависимостей**: спека
   владеет только версией `nanobot-ai`.
8. **Держать дубль upstream-подсистемы.** Если upstream добавил
   встроенный механизм, дубль удаляется в том же upgrade-изменении
   (`Requirements`, строки 50-52).

## Consumers

Потребители контракта — процессы и роли, а не модули:

| Потребитель | Что читает | Зачем |
|---|---|---|
| CI | маркер `contract` в `pyproject.toml:18-21` | гонять контрактные тесты всегда, без гейта |
| Человек перед merge апгрейда | `requirements.txt`, `tests/contract/`, `apply_all` | merge-гейт |
| `RuntimePatcher` startup-лог | `_PATCH_SPECS` через `patch_specs()` | баннер инвентаря и диагностика |
| `PatchReport.render(specs=…)` | `PatchSpec` | печать target/version/status |
| Аудитор зависимости от nanobot | `runtime-patcher-inventory.md` | ручной разбор риска при апгрейде |

Активных вызовов **из кода агента** у предмета нет: ни один модуль не
обращается к «контракту совместимости» как к сервису. Это подтверждено
поиском по дереву: единственные носители перечисленного — сами артефакты.

## Implementation

Отдельного модуля, класса или пакета, реализующего предмет, **нет** —
и это установлено проверкой дерева, а не предположением. Единственное,
что нашлось по запросу «upgrade|compatib» в `lib/` и `workspace/`, —
`RuntimePatcher` и `runtime_health`, которые принадлежат другим спекам.

Файлы, которые составляют предмет (все пути проверены `Test-Path`):

- `./requirements.txt` — существует; версия на строке 43.
- `tests/contract/` — существует; 24 модуля `*.py`.
- `lib/services/runtime_patcher.py` — существует; владеет `_PATCH_SPECS`
  и `apply_all`. Принадлежит предмету **по содержанию** (реестр и
  отчёт патчей), но специфицируется в другой спеке — здесь он источник
  фактов, а не объект.
- `docs/architecture/runtime-patcher-inventory.md` — существует;
  каталог патчей.
- `./pyproject.toml` — существует; объявляет маркер `contract`.
- `./CHANGELOG.md` — существует; упоминается как место фиксации изменений.

Чего **не существует** и не должно выдумываться при чтении этой спеки:
модуля с классом `UpgradeCompatibilityService` (модуль не создавался, и проза
требований не фиксировала для него каталог, поэтому проверяемый путь —
`./upgrade_compatibility.py`, `Test-Path: False`), сервиса
`UpgradeCompatibilityService`, команды `tools/upgrade.py` (путь
не существует), реестра «проверок совместимости» как
исполняемого объекта. Отдельной точки исполнения у предмета нет — есть
артефакты, которые читает человек и CI.

## Verification

Пути проверены `Test-Path`; все существуют.

**Контрактные тесты (`tests/contract/`)** — основной гейт:

- `tests/contract/test_usage_store_api.py` — API `LLMUsageStore`;
- `tests/contract/test_llm_observer_api.py` — API наблюдателя LLM-вызовов;
- `tests/contract/test_usage_to_dict_contract.py` — сериализация usage;
- `tests/contract/test_session_manager.py`,
  `tests/contract/test_session_manager_api.py`,
  `tests/contract/test_session_manager_contract.py` — контракт
  `SessionManager` (покрывает требование «Контрактные тесты на upstream
  SessionManager API» из спеки `sessions/session-hybridization`);
- `tests/contract/test_agent_loop_api.py`,
  `tests/contract/test_compaction_api.py`,
  `tests/contract/test_runtime_events_api.py`,
  `tests/contract/test_message_bus.py`,
  `tests/contract/test_hook_protocol.py`,
  `tests/contract/test_hook_subclass_contract.py`,
  `tests/contract/test_import_surface.py`,
  `tests/contract/test_nanobot_cli_compat.py`,
  `tests/contract/test_tools_and_context.py`,
  `tests/contract/test_command_router.py`,
  `tests/contract/test_channels_base.py`,
  `tests/contract/test_database_logging_get_model.py`,
  `tests/contract/test_repeat_guard_hook_contract.py`,
  `tests/contract/test_session_dir_name_contract.py`,
  `tests/contract/test_composite_hook_lifecycle.py`,
  `tests/contract/test_history_search_identity_contract.py`
  — остальные контракты поверхности nanobot.

**Профильные smoke-тесты** (требование `Requirements`, строки 92-94):

- `tests/test_profile_lifecycle.py` — существует.
- `tests/test_pg_session_manager.py` — существует; framework contract.
- `tests/test_history_search_tool.py` — **не существует**, и это расхождение
  с прозой спеки зафиксировано в § Forbidden Behavior: имя теста в
  `Requirements` устарело. Фактические проверки scope-изоляции живут в
  `tests/test_history_search_user_isolation_guards.py` (существует) и
  `tests/contract/test_history_search_identity_contract.py` (существует).

**Про патчер:**

- `tests/test_patch_spec_consistency.py` — существует; единственный
  автоматический страж реестра: разбирает `_PATCH_SPECS` через AST и
  требует, чтобы множество ключей совпадало с множеством, которое реально
  применяет `apply_all` (`test_patch_specs_matches_module_constant`,
  строка 80; `TestPatchMethodsAreDeclared`, строка 105), и чтобы патчи,
  объявленные в реестре, применялись. Это же закрывает требование
  «каждый патч имеет запись» **на стороне кода**.
- `tests/test_runtime_patcher.py` — существует; проверяет `apply_all`,
  `_classify_skip` и классификацию `[INTERNAL_FAILED]`.

Честная граница: **стороны документа** требование «каждый патч имеет
запись в inventory» автоматического стража не имеет. Поиск по дереву на
`runtime-patcher-inventory` даёт только прозу (`./AGENTS.md`, `docs/`,
`./CHANGELOG.md`) и упоминание внутри текста ошибки
`tests/test_patch_spec_consistency.py:180` — то есть сверки
`_PATCH_SPECS` с таблицей в документе не выполняет никто. Проверено
также, что `tests/test_docs_consistency.py` каталог патчей не упоминает
вовсе. Соответствие кода и документа проверяется ревью.

Что проверкой **не** покрыто: автоматического стража того, что версия в
`./requirements.txt` совпадает с фактически установленной, и того, что
после прогона контрактных тестов в `_PATCH_SPECS` не появилось
`failed`. Оба требования проверяются человеком или вручную настроенным
CI-шагом.
