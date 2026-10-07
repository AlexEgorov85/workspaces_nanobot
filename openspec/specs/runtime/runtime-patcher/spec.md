# runtime/runtime-patcher Specification

## Purpose
Нормативный контракт `RuntimePatcher` — единого компонента применения
monkey-patch'ей к upstream `nanobot.agent.loop.AgentLoop`. Контракт
фиксирует границу ответственности (только upstream runtime patches,
не tool registration), требования к однократности применения, точному
соответствию inventory и единственному источнику `required`/`optional`
метаданных.

## Scope

`agent` — патчи рантайма применяются в агенте
Реализация: `lib/services/runtime_patcher.py`, `lib/services/runtime_inventory.py`

## Requirements

### Requirement: `RuntimePatcher` владеет только адаптацией upstream runtime API

`RuntimePatcher` SHALL содержать только операции monkey-patching
и замены методов upstream `nanobot.agent.loop.AgentLoop`,
`nanobot.agent.autocompact`, `nanobot.agent.tools.shell`,
`nanobot.agent.tools.filesystem`, `nanobot.utils.document`,
`nanobot.session.manager` и аналогичных upstream-целей.

`RuntimePatcher` SHALL NOT содержать:

- discovery модулей из `workspace/tools/*.py`;
- сканирование подклассов `nanobot.agent.tools.base.Tool`;
- построение `nanobot.agent.tools.context.ToolContext`;
- вызов `agent.tools.register(...)`;
- DI-инъекции в `Tool`-подклассы.

Регистрация project tools SHALL выполняться отдельным
stateless helper'ом `lib.services.project_tool_loader` с
единственной публичной функцией `register_project_tools(agent,
workspace_dir, *, settings, cache_store, db_logging_service)
-> ProjectToolsLoadResult`. `ProjectToolLoader` — **не**
компонент (нет lifecycle/state/configuration; см.
`openspec/specs/architecture/component-model/spec.md`), отдельная
canonical spec для loader'а НЕ создаётся, запись в
`openspec/specs/COMPONENTS.md` для loader'а НЕ добавляется.

#### Scenario: Регистрация project tools происходит вне `RuntimePatcher`

- **WHEN** стартует `ApplicationContext.create()`
- **THEN** обнаружение и регистрация project tools SHALL происходить
  через `lib.services.project_tool_loader.register_project_tools(...)`
  сразу после `RuntimePatcher.apply_all()`, и SHALL NOT вызываться
  из `RuntimePatcher.apply_all()` и из любых `patch_*` методов
  `RuntimePatcher`.
- **AND** результат регистрации (экземпляр `ProjectToolsLoadResult`)
  SHALL храниться отдельно от `PatchReport` (например,
  `ctx.project_tools_result`), а `PatchReport.details` SHALL NOT
  содержать ключа `project_tools`.

#### Scenario: Архитектурный тест запрещает discover tools в `RuntimePatcher`

- **WHEN** `tests/test_architecture_*.py` (или
  `tests/test_dependency_direction.py`) запускается
- **THEN** тест SHALL проверить через AST-анализ, что
  `lib/services/runtime_patcher.py` не импортирует
  `workspace.tools.*`, не зовёт `ToolContext(...)` и не зовёт
  `agent.tools.register(...)`.

### Requirement: Однократное применение runtime patches к одному `AgentLoop`

`ApplicationContext.create()` MUST быть единственным production
call site для `RuntimePatcher.apply_all()`. После возврата из
`create()` ни один entrypoint (`cli_agent.py`, `gateway.py`)
SHALL NOT вызывать `apply_all()` или отдельные
`patch_*` методы `RuntimePatcher`, входящие в `apply_all`.

Контракт держится **архитектурно** (отсутствие повторных
call site'ов), а не runtime-механизмом idempotency. `RuntimePatcher`
НЕ обязан поддерживать повторное применение — каждый отдельный
`patch_*` метод оборачивает upstream-цель в обычную обёртку, и
повторный вызов приведёт к double-wrap (это и есть текущий баг
в `cli_agent.py:174`). Никакого `already_patched` флага не
вводится.

#### Scenario: `ApplicationContext.create()` применяет patches ровно один раз

- **WHEN** `ApplicationContext.create()` создаёт `AgentLoop` и
  завершает composition root
- **THEN** `RuntimePatcher.apply_all()` SHALL быть вызван ровно один
  раз, и `agent._assemble_outbound` SHALL содержать ровно один
  project wrapper layer (не ноль, не два).

#### Scenario: CLI не повторно применяет `assemble_outbound`

- **WHEN** `cli_agent.py::_run_patched()` (или эквивалентный путь
  запуска patched-CLI) выполняется после `ApplicationContext.create()`
- **THEN** код SHALL NOT вызывать
  `ctx.runtime_patcher.patch_assemble_outbound(...)` повторно;
  существующий дубль SHALL быть удалён.

#### Scenario: gateway не повторно применяет patches

- **WHEN** `gateway.py` стартует
- **THEN** он ДОЛЖЕН полагаться на `ApplicationContext.create()` как
  на единственную точку применения patches и SHALL NOT вызывать
  `patch_*` методы `RuntimePatcher` напрямую.

#### Scenario: Регрессионный тест проверяет ровно один вызов `patch_assemble_outbound`

- **WHEN** `tests/test_application_context.py::test_single_application_point`
  (или эквивалентный) запускается
- **THEN** он SHALL spy/mock'нуть
  `RuntimePatcher.patch_assemble_outbound` через
  `unittest.mock.patch.object(RuntimePatcher, "patch_assemble_outbound",
  wraps=original)` (или эквивалентный spy-паттерн),
  вызвать `ApplicationContext.create()` и проверить, что
  `mock.call_count == 1`.
- **AND** дополнительный AST-тест
  (`tests/test_architecture_*.py::test_no_repeated_patch_in_cli`
  или эквивалентный) SHALL проверить, что в `cli_agent.py` нет
  вызова `patch_assemble_outbound` после `ApplicationContext.create()`.
- **AND** никакого дополнительного marker'а (`_project_wrapped=True`
  или эквивалентного) в production-код не вводится — production
  semantics не меняется ради тестов.

### Requirement: Точное соответствие inventory (финально — 4 patches)

Множество имён patches, вызываемых `RuntimePatcher.apply_all()`,
множество ключей `_PATCH_SPECS` и множество имён в
`lib.services.runtime_inventory.canonical_runtime_patches()` SHALL
быть попарно равны. **Финальное состояние: 4 patches в каждом из
трёх множеств** — `exec_timeout_cap`, `assemble_outbound`,
`subagent_logging`, `repeat_guard_block` (`_PATCH_SPECS` в
`lib/services/runtime_patcher.py:271`). Сняты `project_tools`,
`exec_limits` и `tool_limits`: первый удалён вместе с tool'ами агента,
второй и третий — потому что потолки приходят из конфигурации, а не из
патча. Любое расхождение считается drift'ом и SHALL быть обнаружено
архитектурным тестом.

#### Scenario: Дрейф между apply_all и PatchSpec невозможен

- **WHEN** `tests/test_runtime_patcher.py::TestPatchSpecs::test_inventory_is_exact`
  (или эквивалентный) запускается
- **THEN** он SHALL проверять **три множества одновременно**:
  - имена patches, фактически вызываемых из
    `RuntimePatcher.apply_all()` (извлечённые через AST-анализ
    тела метода: все аргументы `name` в вызовах
    `self._record(report, "<name>", ...)`);
  - `set(RuntimePatcher.patch_specs())`;
  - `{p.name for p in canonical_runtime_patches()}`.
  Все три множества SHALL быть попарно равны (финально — по 6
  имён). Это инвариант «three sets exactly equal», а не
  subset-проверка (`assert key in actual` ужесточается до
  `assert set(actual) == expected`).

#### Scenario: Дрейф между PatchSpec и canonical inventory невозможен

- **WHEN** `tests/test_runtime_inventory.py` запускается
- **THEN** он SHALL проверять, что
  `{p.name for p in canonical_runtime_patches()}`
  равно `set(RuntimePatcher.patch_specs())` без исключений
  (сравнение имён, не объектов; `canonical_runtime_patches()`
  возвращает `list[RuntimePatchSpec]`, `patch_specs()` —
  `dict[str, PatchSpec]`).

#### Scenario: Stale DEPRECATED-остатки запрещены

- **WHEN** в `_PATCH_SPECS` присутствует запись, не вызываемая из
  `apply_all()`
- **THEN** тест SHALL упасть, сигнализируя drift; такие записи
  (`compact_tracking`, `compact_command`, `idle_guard`)
  SHALL быть удалены.

#### Scenario: Фактические элементы apply_all без PatchSpec запрещены

- **WHEN** `apply_all()` зовёт `patch_*` метод, для которого нет
  записи в `_PATCH_SPECS`
- **THEN** тест SHALL упасть; такой элемент SHALL получить `PatchSpec`.

#### Scenario: `project_tools` исключён из runtime inventory

- **WHEN** после этой change запрашиваются
  `RuntimePatcher.patch_specs()` или
  `canonical_runtime_patches()`
- **THEN** ни одно из множеств SHALL NOT содержать имени
  `project_tools`; регистрация project tools — ответственность
  `ProjectToolLoader`, не runtime patches.

### Requirement: Hook allowlist действительно ограничивает

`lib.cli.hook_loader.scan_and_register` SHALL пропускать файлы
из `workspace/hooks/`, чьё имя (stem) отсутствует в
`_allowed_hook_names()`, **без импорта модуля** и **без исполнения
его тела**. Прежнее поведение «warn + продолжить импорт» SHALL быть
удалено.

#### Scenario: Не-alwisted hook не импортируется

- **WHEN** `workspace/hooks/unknown_hook.py` существует, и
  `unknown_hook` отсутствует в `_allowed_hook_names()`
- **THEN** `scan_and_register` SHALL записать INFO-лог с маркером
  «hook not in allowlist, skipped», SHALL NOT вызвать
  `importlib.util.spec_from_file_location` или `spec.loader.exec_module`
  для этого файла, и SHALL NOT добавить инстанс класса в возвращаемый
  список.

#### Scenario: Не-alwisted hook не выполняет module body

- **WHEN** `workspace/hooks/unknown_hook.py` содержит module-level
  side-effect (например, `MARKER = open(...).write(...)` или
  `print("imported")`)
- **THEN** после вызова `scan_and_register` ни marker-файл, ни
  stdout-print НЕ ДОЛЖНЫ появиться; этот сценарий покрывается
  регрессионным тестом `test_hook_allowlist_blocks_module_exec`.

#### Scenario: Alwisted hook загружается

- **WHEN** `workspace/hooks/session_file_redirect_hook.py` присутствует
  и входит в `_allowed_hook_names()`
- **THEN** модуль импортируется, инстанс `SessionFileRedirectHook`
  появляется в возвращаемом списке `scan_and_register`.

### Requirement: `PatchSpec.required` — единственный источник истины для criticality

`PatchSpec` SHALL содержать поле `required: bool`, и именно оно
должно использоваться `runtime_inventory.canonical_runtime_patches()`
при проекции. Отдельный hardcoded set имён (например,
`high_risk_required` в `runtime_inventory.py`) SHALL быть удалён.
Корреляция `required=True` и `risk='high'` SHALL NOT выводиться
автоматически — это два независимых измерения (`risk` — цена
апгрейда nanobot, `required` — критичность для runtime).

#### Scenario: Изменение `PatchSpec.required` отражается в inventory

- **WHEN** `PatchSpec.required` для patch X меняется на `True`
- **THEN** `canonical_runtime_patches()` возвращает
  `RuntimePatchSpec(name=X, required=True, ...)` без правки
  какого-либо отдельного hardcoded set'а.

#### Scenario: Старый hardcoded set отсутствует

- **WHEN** запускается тест на `runtime_inventory.canonical_runtime_patches`
- **THEN** тест SHALL проверить, что в модуле
  `lib/services/runtime_inventory.py` нет атрибута
  `high_risk_required` (или эквивалентного hardcoded set'а имён
  для проекции `required`).

#### Scenario: Корреляция risk и required не выводится автоматически

- **WHEN** новый patch добавляется с `risk='high'`, но
  `required=False` (например, opt-in фича, ломающаяся при апгрейде)
- **THEN** `canonical_runtime_patches()` SHALL проецировать
  `required=False`; никакая автоматика не превратит `risk='high'`
  в `required=True`.

## Responsibility

Единый компонент применения monkey-patch'ей к upstream `nanobot` и владелец
метаданных о них. Владелец — `RuntimePatcher`
(`lib/services/runtime_patcher.py:462`); канон ожидаемого состава и сверка
с фактом — `lib/services/runtime_inventory.py`.

## Boundary

- **Внутри:** замена методов и констант upstream, отчёт о применении,
  метаданные патчей (`purpose`, `nanobot_target`, `reason`,
  `alternatives_checked`, `risk`, `required`).
- **Снаружи:** discovery модулей из `workspace/tools/*.py` и регистрация
  tool'ов — `lib/services/project_tool_loader.py`, не здесь; сборка хуков —
  `runtime/agent-hooks`; сверка факта с каноном — `runtime_inventory` и
  `tools/diagnose_startup.py`.

## Public Contract

- `PatchSpec` — frozen dataclass с полями `name`, `purpose`,
  `nanobot_target`, `reason`, `alternatives_checked`, `risk`,
  `nanobot_version="0.3.0"`, `required=False`
  (`lib/services/runtime_patcher.py:230-268`).
- `PatchReport` (`:366`) — `applied`, `skipped`, `failed`, `details`;
  `to_dict()` (`:386`), `render(*, specs=...)` (`:394`).
- `RuntimePatcher.apply_all(config, settings, workspace_dir, agent, tool_audit_hook, *, db_logging_service=None, session_manager=None, recent_files_hook=None, bus=None) -> PatchReport`
  (`:465-477`).
- `RuntimePatcher.patch_specs() -> dict[str, PatchSpec]` (`:515-522`).
- Зарегистрированные патчи — четыре: `exec_timeout_cap` (`:272`),
  `assemble_outbound` (`:286`), `subagent_logging` (`:303`),
  `repeat_guard_block` (`:315`); ровно их зовёт `apply_all`
  (`:506-511`).
- Отдельного доменного типа исключения для патчей нет: каждый патч
  возвращает кортеж `(bool, detail)`.

## Inputs

- `config`, `settings`, `workspace_dir` — контекст агента; из `settings`
  читается `exec_timeout_cap_sec` (`:482-484`).
- `agent` — `AgentLoop` (для `assemble_outbound`);
  `tool_audit_hook` — тот же.
- `recent_files_hook` — опционально (`:488-490`).
- `db_logging_service` — `None` означает, что `subagent_logging`
  пропускается (`:491-492`); `session_manager`, `bus` — для персиста истории
  подагентов.
- Параметр `cache_store` снят: ни один патч его не читал (`:498-500`).

## Outputs

- `PatchReport` со списками `applied` / `skipped` (с причиной) / `failed`
  (`:502-503`).
- `details[name]` — деталь по каждому патчу, включая успешные, для дампа в
  startup-логе и диагностики при апгрейде nanobot (`:376-377`).
- `render(specs=...)` — человекочитаемая сводка «Runtime patches»
  (`:394-401`).

## State

Состояния между вызовами у патчера нет: каждый патч проверяет, не помечен ли
уже целевой атрибут, и возвращает «already patched». Единственное
долговременное состояние — модульный словарь `_PATCH_SPECS`
(`:271`), объявление которого является данными, а не результатом работы.
Патчи меняют состояние **процесса** — функции и классы upstream.

## Dependencies

- `nanobot.agent.tools.shell.ExecTool` — потолок таймаута (`:277-278`);
- `nanobot.agent.loop.AgentLoop._assemble_outbound` (`:291`);
- `nanobot.agent.subagent._SubagentHook` (`:307`);
- `nanobot.agent.tools.execution._execute_tool_call` (`:319`);
- `lib.hooks.tool_audit_hook.ToolAuditHook` — внедряемый в outbound;
- `lib.services.runtime_inventory.diff_runtime_patches`
  (`lib/services/runtime_inventory.py:305`) — сверка с каноном;
- `lib.services.project_tool_loader` — соседняя подсистема, не зависимость
  патчера.

## Configuration

- `gateway.exec_timeout_cap_sec` — читается в `patch_exec_timeout_cap`
  (`:580`); порог = 0 означает skip по конфигурации, а не дефект.
- Секция `tool_result_limits` больше не читается ни одним патчем: её патчи
  сняты (`:483-484`).
- `required` в `PatchSpec` — только метаданные для диагностики; control flow
  от него **не** зависит: failed-патч, включая `required=True`,
  логируется warning'ом, и `ApplicationContext.create()` продолжает работу
  (`:250-258`).
- Отдельной секции настроек именно патчей в `config.json` нет.

## Lifecycle

1. `ApplicationContext` создаёт `RuntimePatcher()` и зовёт `apply_all`
   (`lib/core/application_context.py:521-529`) — после сборки хуков, потому
   что патч внедряет аудит в outbound (`:483`).
2. `apply_all` зовёт четыре патча подряд, каждый результат уходит в `_record`
   (`lib/services/runtime_patcher.py:506-511`).
3. Отчёт печатается в стартовый блок:
   `patch_report.render(specs=RuntimePatcher.patch_specs())`
   (`lib/core/application_context.py:535`).
4. Непустой `failed` даёт warning и баннер инвентаря через
   `_emit_patch_inventory_banner` → `diff_runtime_patches`
   (`lib/core/application_context.py:540-546`, `:1146-1157`).
5. Отдельным шагом рядом с `apply_all` идёт регистрация project tools
   (`lib/core/application_context.py:574`).

## Data Ownership

Патчер не владеет данными: он владеет **кодом процесса**. Ни файлов, ни
таблиц, ни снимков он не создаёт и не читает. Данные, проходящие через
патчи, принадлежат своим подсистемам: аудит — `ToolAuditHook`, история
подагентов — `DbLoggingService` и менеджер сессий, синтетический отказ
защитника — `runtime/anti-loop`.

## Error Behavior

- Каждый патч возвращает `(bool, detail)`, а не бросает: `import failed: …`,
  `_execute_tool_call is missing`, `patch failed: …` — это `False` с причиной
  (`lib/services/runtime_patcher.py:1241-1242`, `:1246`, `:1316`).
- `_classify_skip` решает, сбой это или конфигуративный skip (`:352-363`);
  деталь из `skipped` — не дефект.
- Маркер `[INTERNAL_FAILED]` в detail переклассифицирует успешный патч с
  частичным успехом в `failed` (`:531-537`).
- Уже применённый патч возвращает `True` с пометкой `already patched`, а не
  применяется второй раз (`:1247-1248`).
- Failed-патч не роняет старт: он виден в баннере и в
  `tools/diagnose_startup.py`.

## Invariants

- `PatchSpec` — единственный источник метаданных `required`/`optional` и
  прочих полей (`:239-268`).
- Однократность: повторное применение патча невозможно — обёртка несёт свой
  маркер (`:1247`, `:1312`).
- `apply_all` зовёт ровно зарегистрированные патчи; ключи `_PATCH_SPECS`,
  имена в `PatchReport` и `patch_specs()` совпадают (`:519-521`).
- Метаданные каждого патча содержат `alternatives_checked` — почему не
  публичный API (`:244-247`).
- Отсутствие patch'а не меняет control flow старта (`:250-258`).
- Патчер не регистрирует tool'ы: соседняя подсистема
  `lib/services/project_tool_loader.py`.

## Forbidden Behavior

- Регистрировать tool'ы из `workspace/tools/*.py`, сканировать подклассы
  `nanobot.agent.tools.base.Tool` или строить `ToolContext` в патчере —
  это граница спеки, а не украшение.
- Бросать исключение вместо `(bool, detail)`: вызывающий ждёт отчёт, а не
  падение.
- Применять один патч дважды.
- Считать skip по конфигурации дефектом.
- Связывать control flow старта с флагом `required`.
- Объявлять `required` в отчёте вручную: он приходит из `PatchSpec`.
- Молчать о failed-патче: `PatchReport.failed` обязан быть виден в баннере
  и в `tools/diagnose_startup.py`.

## Consumers

- `lib/core/application_context.py:521-529` — единственный вызывающий
  `apply_all`; `:535` — рендер отчёта; `:540-546` — баннер по `failed`.
- `lib/services/runtime_inventory.py:182` (`canonical_runtime_patches`) —
  канон ожидаемого состава; `:305` (`diff_runtime_patches`) — сверка.
- `tools/diagnose_startup.py:205-217` — диагностика drift'а.
- Оператор — по блоку «Runtime patches» в стартовом выводе.

## Implementation

Существующие на диске пути:

- `lib/services/runtime_patcher.py` — `PatchSpec`, `_PATCH_SPECS`,
  `PatchReport`, `RuntimePatcher`, четыре патча;
- `lib/services/runtime_inventory.py` — канон состава и `diff_*`;
- `lib/services/project_tool_loader.py` — соседняя подсистема, границу
  которой патчер не пересекает;
- `lib/core/application_context.py` — вызов, отчёт и баннер;
- `lib/hooks/repeat_guard_hook.py` — тип отказа, который перехватывает
  `patch_repeat_guard_block`;
- `tools/diagnose_startup.py` — отчёт о drift'е.

## Verification

- `tests/test_runtime_patcher.py` — `PatchSpec`, `PatchReport`, однократность,
  классификация skip/failed;
- `tests/test_runtime_patcher_no_project_tools_boundary.py` — патчер не
  регистрирует tool'ы;
- `tests/test_runtime_inventory.py` — соответствие `canonical_runtime_patches`
  фактическому составу;
- `tests/test_hook_allowlist.py` — канонические списки инвентаря не
  разъезжаются с фактом.
