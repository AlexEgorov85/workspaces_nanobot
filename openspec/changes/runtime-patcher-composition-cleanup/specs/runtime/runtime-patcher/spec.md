## Purpose

Нормативный контракт `RuntimePatcher` — единого компонента применения
monkey-patch'ей к upstream `nanobot.agent.loop.AgentLoop`, его
граница ответственности (только upstream runtime patches, не
tool registration), требования к однократности применения, точному
соответствию inventory и единственному источнику `required`/`optional`
метаданных.

## ADDED Requirements

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

#### Scenario: Регистрация project tools происходит вне `RuntimePatcher`

- **WHEN** стартует `ApplicationContext.create()`
- **THEN** обнаружение и регистрация project tools SHALL происходить
  через `lib.services.project_tool_loader` (или эквивалентный узкий
  loader), и SHALL NOT вызываться из `RuntimePatcher.apply_all()` и
  из любых `patch_*` методов `RuntimePatcher`.

#### Scenario: Архитектурный тест запрещает discover tools в `RuntimePatcher`

- **WHEN** `tests/test_architecture_*.py` (или
  `tests/test_dependency_direction.py`) запускается
- **THEN** тест SHALL проверить, что `lib/services/runtime_patcher.py`
  не импортирует `workspace.tools.*`, не зовёт
  `ToolContext(...)` и не зовёт `agent.tools.register(...)`.

### Requirement: Однократное применение runtime patches к одному `AgentLoop`

Каждый enabled patch из `RuntimePatcher.apply_all()` SHALL применяться
не более одного раза к одному экземпляру `AgentLoop` в рамках одного
startup lifecycle. Точка вызова `apply_all()` SHALL быть единственной;
повторный вызов `apply_all()` или прямой вызов любого включённого
в `apply_all` `patch_*` метода после возврата из
`ApplicationContext.create()` SHALL быть запрещён.

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

#### Scenario: Регрессионный тест запрещает дубль

- **WHEN** `tests/test_application_context.py::test_single_application_point`
  (или эквивалентный) запускается
- **THEN** он SHALL проверить, что после полного запуска
  `ApplicationContext.create()` `_assemble_outbound` обёрнут ровно
  один раз, а повторный вызов `apply_all()` на том же `agent`
  либо запрещён, либо идемпотентен (idempotency contract явно
  фиксируется в коде и тесте).

### Requirement: Точное соответствие inventory

Множество имён patches, вызываемых `RuntimePatcher.apply_all()`,
множество ключей `_PATCH_SPECS` и множество имён в
`lib.services.runtime_inventory.canonical_runtime_patches()` SHALL
быть попарно равны. Любое расхождение считается drift'ом и SHALL
быть обнаружено существующим или новым архитектурным тестом.

#### Scenario: Дрейф между apply_all и PatchSpec невозможен

- **WHEN** `tests/test_runtime_patcher.py::TestPatchSpecs` запускается
- **THEN** он SHALL проверять `actual == expected` (exact match),
  а не `key in actual` (subset).

#### Scenario: Дрейф между PatchSpec и canonical inventory невозможен

- **WHEN** `tests/test_runtime_inventory.py` запускается
- **THEN** он SHALL проверять, что `set(canonical_runtime_patches())`
  равно `set(RuntimePatcher.patch_specs())` без исключений.

#### Scenario: Stale DEPRECATED-остатки запрещены

- **WHEN** в `_PATCH_SPECS` присутствует запись, не вызываемая из
  `apply_all()`
- **THEN** тест SHALL упасть, сигнализируя drift; такие записи
  (например, `compact_tracking`, `compact_command`, `idle_guard`)
  SHALL быть удалены в этой change.

#### Scenario: Фактические элементы apply_all без PatchSpec запрещены

- **WHEN** `apply_all()` зовёт `patch_*` метод, для которого нет
  записи в `_PATCH_SPECS`
- **THEN** тест SHALL упасть; такие элементы (например,
  `turn_delivery_fail`, `session_dir_watch`) SHALL получить
  `PatchSpec` в этой change.

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