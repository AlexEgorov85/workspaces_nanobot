# runtime/runtime-patcher Specification

## Purpose
Нормативный контракт `RuntimePatcher` — единого компонента применения
monkey-patch'ей к upstream `nanobot.agent.loop.AgentLoop`. Контракт
фиксирует границу ответственности (только upstream runtime patches,
не tool registration), требования к однократности применения, точному
соответствию inventory и единственному источнику `required`/`optional`
метаданных.

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
`create()` ни один entrypoint (`cli_agent.py`, `gateway.py`,
`streamlit_app.py`) SHALL NOT вызывать `apply_all()` или отдельные
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

#### Scenario: gateway и streamlit не повторно применяют patches

- **WHEN** `gateway.py` или `streamlit_app.py` стартует
- **THEN** они ДОЛЖНЫ полагаться на `ApplicationContext.create()` как
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

### Requirement: Точное соответствие inventory (финально — 12 patches)

Множество имён patches, вызываемых `RuntimePatcher.apply_all()`,
множество ключей `_PATCH_SPECS` и множество имён в
`lib.services.runtime_inventory.canonical_runtime_patches()` SHALL
быть попарно равны. **Финальное состояние: 12 patches в каждом из
трёх множеств** (после удаления `project_tools`). Любое расхождение
считается drift'ом и SHALL быть обнаружено архитектурным тестом.

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
  Все три множества SHALL быть попарно равны (финально — по 12
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
  SHALL быть удалены в этой change.

#### Scenario: Фактические элементы apply_all без PatchSpec запрещены

- **WHEN** `apply_all()` зовёт `patch_*` метод, для которого нет
  записи в `_PATCH_SPECS`
- **THEN** тест SHALL упасть; такие элементы (`turn_delivery_fail`,
  `session_dir_watch`) SHALL получить `PatchSpec` в этой change.

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
