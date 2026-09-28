## 1. Baseline и inventory-снимок

- [ ] 1.1 Зафиксировать baseline текущего состояния. Снять списки:
  `apply_all()` patches (13 имён), `_PATCH_SPECS` keys (14 имён),
  `canonical_runtime_patches()` keys (14 имён),
  `_allowed_hook_names()` (3 имени), `high_risk_required` (4 имени).
  Verify: ручной вывод в issue/PR-описании или в фиксации
  baseline-лога; `git diff --stat` показывает 0 изменений.

## 2. Удаление дубля `patch_assemble_outbound` в CLI

- [ ] 2.1 В `cli_agent.py::_run_patched()` (строки 152–176) удалить
  повторный вызов `ctx.runtime_patcher.patch_assemble_outbound(
  ctx.agent, ctx.tool_audit_hook)`. Verify: `git diff cli_agent.py`
  показывает только удаление строки 174; повторный
  `apply_all` в `_run_patched` отсутствует.

- [ ] 2.2 Добавить регрессионный тест
  `tests/test_application_context.py::test_single_application_point`
  (или эквивалентный), который:
  - вызывает `ApplicationContext.create()` и затем проверяет, что
    `agent._assemble_outbound` обёрнут ровно один раз
    (через `getattr`/`wraps`/маркер `_project_wrapped=True`);
  - имитирует `_run_patched` (повторный `patch_assemble_outbound`)
    и проверяет, что либо функция не существует, либо повторный
    вызов поднимает `RuntimeError("already patched")`.
  Verify: `pytest tests/test_application_context.py -k
  single_application_point -q` зелёный; тест падает на старом коде.

- [ ] 2.3 Добавить явный архитектурный тест
  `tests/test_architecture_*.py` (или в
  `test_dependency_direction.py`) `test_no_repeated_patch_in_cli`,
  который через AST/static-анализ `cli_agent.py` подтверждает
  отсутствие вызова `patch_assemble_outbound` после
  `ApplicationContext.create()`.
  Verify: тест падает на старом коде; зелёный после удаления.

## 3. Синхронизация runtime-patch inventory

- [ ] 3.1 В `lib/services/runtime_patcher.py::_PATCH_SPECS` добавить
  `PatchSpec` для двух отсутствующих патчей, вызываемых из
  `apply_all()`:
  - `turn_delivery_fail` (purpose/nanobot_target/reason/risk по
    существующему `patch_turn_delivery_fail` в строках 1613–1835);
  - `session_dir_watch` (purpose/nanobot_target/reason/risk по
    существующему `patch_session_dir_watch` в строках 1176–1279).
  Verify: `git diff lib/services/runtime_patcher.py` показывает
  два новых `PatchSpec(...)`; exact-match тест ниже зеленеет.

- [ ] 3.2 В `lib/services/runtime_patcher.py::_PATCH_SPECS` удалить
  три DEPRECATED-записи: `compact_tracking`, `compact_command`,
  `idle_guard` (см. блоки 350–397 в `_PATCH_SPECS`).
  Verify: `len(_PATCH_SPECS) == 13` после правки; exact-match тест
  ниже зеленеет.

- [ ] 3.3 Добавить `PatchSpec.required: bool = False` в dataclass
  `PatchSpec` (см. `lib/services/runtime_patcher.py:209-236`) и
  явно проставить `required=True` для четырёх критичных:
  `assemble_outbound`, `save_turn`, `subagent_logging`,
  `context_governor`. Verify: `pytest tests/test_runtime_patcher.py
  -q` зелёный; dataclass по-прежнему frozen.

- [ ] 3.4 В `lib/services/runtime_inventory.py::canonical_runtime_patches`
  (строки 162–186) удалить `high_risk_required` и проекцию через
  `spec.required` (а не через локальный frozenset). Verify:
  `set(canonical_runtime_patches()) == set(RuntimePatcher.patch_specs())`;
  unit-тест в `tests/test_runtime_inventory.py` явно проверяет
  отсутствие `high_risk_required` атрибута в модуле.

- [ ] 3.5 Ужесточить
  `tests/test_runtime_patcher.py::TestPatchSpecs::test_all_patches_have_specs`
  с `for key in expected: assert key in actual` до `assert
  set(actual) == expected` (exact match). Verify: тест падает
  на старом коде (14 vs 13); зеленеет после 3.1–3.3.

- [ ] 3.6 Обновить `docs/architecture/runtime-patcher-inventory.md` —
  пересобрать каталог патчей под новое exact inventory, удалить
  упоминания `compact_tracking`/`compact_command`/`idle_guard`,
  добавить `turn_delivery_fail`/`session_dir_watch`.
  Verify: `git grep -l "compact_tracking\|compact_command\|idle_guard"
  -- '*.md' '*.py'` показывает 0 матчей вне CHANGELOG и git
  history (исключения — `_PATCH_SPECS` если остались DEPRECATED
  в release-ветке для миграции, и CHANGELOG).

## 4. Вынос `patch_project_tools` в отдельный loader

- [ ] 4.1 Создать `lib/services/project_tool_loader.py` с функциями
  `discover(workspace_dir) -> list[type]` и
  `register_all(agent, tools, *, cache_store, db_logging_service,
  settings) -> tuple[registered, disabled, duplicate, failed]`.
  Тело функций — копия существующего `patch_project_tools`
  (`lib/services/runtime_patcher.py:2279-2509`) без семантических
  правок: тот же `pkgutil.iter_modules` по `workspace/tools/`, тот же
  `ToolContext(...)` с тем же `setattr` DI, тот же
  `cls.enabled(ctx)` / `cls.create(ctx)` / `agent.tools.register(tool)`.
  Verify: `python -c "from lib.services.project_tool_loader import
  discover, register_all; print('OK')"` печатает OK.

- [ ] 4.2 В `lib/services/runtime_patcher.py` удалить метод
  `patch_project_tools` и удалить соответствующий вызов из
  `apply_all()` (`lib/services/runtime_patcher.py:650-652`). Никаких
  backward-compat stubs — `_PATCH_SPECS` не содержит `project_tools`
  после Phase 3.
  Verify: `git grep -n "patch_project_tools\|project_tools"
  -- lib/` показывает 0 матчей в `lib/services/runtime_patcher.py`
  (допустимы матчи в loader).

- [ ] 4.3 В `lib/core/application_context.py` после
  `ctx.runtime_patcher.apply_all(...)` (строки 339–346) добавить
  вызов loader'а: импорт `lib.services.project_tool_loader` и
  `register_all(agent, discover(workspace_dir), ...)` с теми же
  DI-параметрами, что передавались в `patch_project_tools` ранее.
  Verify: `pytest tests/test_application_context.py
  tests/test_tools_project_loader.py -q` зелёный; интеграционный
  тест `test_real_compact_context_tool_loads` (см. существующий
  в `tests/test_tools_project_loader.py:431+`) продолжает проходить.

- [ ] 4.4 В `lib/services/runtime_inventory.py` обновить
  `_emit_project_tools_inventory_banner` (или эквивалентный
  inventory-banner) — источник данных о project tools остаётся
  тот же `patch_report.details["project_tools"]` (отчёт
  формирует loader, не `RuntimePatcher`). Если banner читает
  напрямую из `_PATCH_SPECS` — переделать на чтение из
  `RuntimePatcher.patch_specs()` минус удалённые/добавленные.
  Verify: `git diff` показывает только косметические правки в
  banner; `tools/diagnose_startup.py --strict` (если запускается)
  exit 0.

- [ ] 4.5 Архитектурный тест `tests/test_architecture_*.py`
  (или расширение `test_dependency_direction.py`)
  `test_runtime_patcher_does_not_register_project_tools`:
  через AST-анализ `lib/services/runtime_patcher.py` подтверждает,
  что модуль не импортирует `workspace.tools`, не строит
  `ToolContext` и не зовёт `agent.tools.register`.
  Verify: тест падает на старом коде; зеленеет после 4.1–4.3.

## 5. Реальный hook allowlist

- [ ] 5.1 В `lib/cli/hook_loader.py::scan_and_register` (строки
  53–83) изменить логику: при `path.stem not in allowed` —
  **continue** (пропуск файла целиком, без импорта, без `exec_module`,
  без `dir(mod)`/`AgentHook`-instantiation), с записью
  INFO-лога через `logger.info("hook %s not in allowlist, skipped",
  path.stem)` (без `rich.console`-warning).
  Verify: `git diff lib/cli/hook_loader.py` показывает только
  изменение логики в ветке `if path.stem not in allowed`.

- [ ] 5.2 Добавить тест
  `tests/test_session_file_redirect_hook.py` (или
  `tests/test_application_context.py` / новый
  `tests/test_hook_allowlist.py`)
  `test_non_allowlisted_hook_is_not_imported`:
  - создать временный `workspace/hooks/_test_blocked_hook.py` с
    module-level side-effect: `MARKER_PATH.write_text("imported")`;
  - вызвать `scan_and_register(hooks_dir, workspace_dir)`;
  - assert `not MARKER_PATH.exists()` (модуль не выполнялся).
  Verify: тест падает на старом коде (warning + import +
  module exec); зеленеет после 5.1.

- [ ] 5.3 Добавить тест `test_allowlisted_hook_is_registered`:
  - `session_file_redirect_hook.py` присутствует и входит в
    `_allowed_hook_names()`;
  - assert `len(scan_and_register(...)) > 0` и инстанс имеет
    type name `"SessionFileRedirectHook"`.
  Verify: тест зелёный; обеспечивает, что сужение allowlist не
  отрезает production-хуки.

- [ ] 5.4 Добавить тест `test_unknown_hook_silent_skip`:
  - два файла в `workspace/hooks/`: `_test_known.py` (в allowlist)
    и `_test_unknown.py` (вне allowlist, без side-effect);
  - assert возвращаемый список содержит только инстанс known hook.
  Verify: тест падает на старом коде (оба импортируются);
  зеленеет после 5.1.

## 6. Архитектурные тесты и инварианты

- [ ] 6.1 Добавить тест
  `tests/test_runtime_inventory.py::test_no_high_risk_required_set`:
  - `from lib.services.runtime_inventory import high_risk_required` →
    `ImportError`;
  - `set(canonical_runtime_patches()) == set(RuntimePatcher.patch_specs())`.
  Verify: тест зелёный; проверяет Decision 5.

- [ ] 6.2 Добавить тест
  `tests/test_runtime_inventory.py::test_required_in_patch_spec_for_critical_patches`:
  - `required=True` для `assemble_outbound`, `save_turn`,
    `subagent_logging`, `context_governor`;
  - никаких других патчей с `required=True` (защита от ложного
    «risk=high → required=true» автоприведения).
  Verify: тест зелёный.

- [ ] 6.3 Обновить `tests/test_architecture_tool_domain_free.py`
  (или эквивалентный архитектурный тест):
  `test_runtime_patcher_is_domain_free` — статический анализ
  `lib/services/runtime_patcher.py` не содержит импортов
  `workspace.tools.*`, `nanobot.agent.tools.base`, упоминаний
  skill-имён.
  Verify: тест зелёный.

- [ ] 6.4 Расширить `tests/test_skill_tool_independence.py` (или
  эквивалентный) `test_no_skill_to_tool_coupling_from_runtime_patcher`:
  `RuntimePatcher.apply_all()` не вызывает ни один модуль из
  `workspace/skills/*`.
  Verify: тест зелёный.

## 7. Спецификации и реестр

- [ ] 7.1 Зарегистрировать в `openspec/specs/COMPONENTS.md` два
  компонента:
  - `RuntimePatcher` (`lib/services/runtime_patcher.py:RuntimePatcher`) →
    `runtime/runtime-patcher` со статусом `partial`;
  - `ProjectToolLoader` (`lib/services/project_tool_loader.py`) →
    категория `runtime` со статусом `partial`.
  Verify: `git diff openspec/specs/COMPONENTS.md` показывает две
  новые строки; таблица категории `runtime` теперь содержит
  4 записи (ранее — 2).

- [ ] 7.2 После архивации change создать canonical-спеки:
  - `openspec/specs/runtime/runtime-patcher/spec.md` (по шаблону
    `architecture/component-model` на русском);
  - `openspec/specs/runtime/project-tool-loader/spec.md` (если
    loader признаётся компонентом — см. ниже).
  Если `ProjectToolLoader` — узкий stateless модуль из 2–3
  функций без lifecycle/state/configuration — отдельная
  спека НЕ создаётся, loader описывается только в
  `docs/ARCHITECTURE.md`.
  Verify: `python tools/validate_component_specs.py` зелёный;
  см. `openspec/specs/architecture/component-model/spec.md`
  для критериев «что считается компонентом».

- [ ] 7.3 Исправить contract drift в
  `openspec/specs/runtime/context/spec.md`: требование
  «`ApplicationContext.start()` MUST вызывать `RuntimePatcher.apply_all`»
  заменяется на актуальное утверждение
  (см. `specs/runtime/context/spec.md` MODIFIED Requirement в этой
  change). Verify: `git grep "start()" -- openspec/specs/runtime/context/`
  показывает только корректные ссылки; новая формулировка
  соответствует `Decision 1` и `Decision 6` в design.md.

## 8. Документация и CHANGELOG

- [ ] 8.1 Обновить `AGENTS.md` секция «Project Layout»: добавить
  `lib/services/project_tool_loader.py`; обновить описание
  `lib/services/runtime_patcher.py` (убрать «project tools» из
  границ ответственности). Verify: `git diff AGENTS.md`
  показывает точечные правки.

- [ ] 8.2 Обновить `docs/ARCHITECTURE.md` секции про
  `RuntimePatcher` и про lifecycle `create()`/`start()` —
  `apply_all` в `create()`, не в `start()`; loader для project
  tools живёт отдельно. Verify: ручная сверка с
  `lib/core/application_context.py:339-346`.

- [ ] 8.3 Обновить `docs/architecture/runtime-patcher-inventory.md` —
  финальный каталог из 13 patches (после 3.1–3.3).

- [ ] 8.4 Обновить `CHANGELOG.md` — блок `[Unreleased]`,
  категории `Changed` (composition cleanup), `Removed`
  (`compact_tracking`, `compact_command`, `idle_guard` PatchSpec),
  `Fixed` (duplicate `patch_assemble_outbound` в CLI, real
  allowlist для hooks). Verify: `git diff CHANGELOG.md` показывает
  3 категории Keep a Changelog.

## 9. Валидация перед merge

- [ ] 9.1 `openspec.cmd validate runtime-patcher-composition-cleanup` —
  exit 0, нет ERROR. Verify: `& openspec.cmd validate
  runtime-patcher-composition-cleanup --no-interactive --strict`
  зелёный; WARN про отсутствие SHALL/MUST в требованиях
  недопустим (если есть — переписать формулировки).

- [ ] 9.2 Запустить минимальный набор тестов:
  `pytest tests/test_runtime_patcher.py tests/test_tools_project_loader.py
  tests/test_runtime_inventory.py tests/test_application_context.py
  tests/test_agent_factory.py tests/test_dependency_direction.py
  tests/test_skill_tool_independence.py -q` — все зелёные.
  Verify: exit 0; ноль failed/errored.

- [ ] 9.3 `python tools/validate_component_specs.py` (если
  применимо после 7.2) — exit 0. Verify: см. секцию
  «Валидация спецификаций» в `architecture/component-model/spec.md`.

- [ ] 9.4 Smoke: `python cli_agent.py --profile=test --smoke` —
  exit 0; баннер «Runtime patches» содержит ровно 13 строк
  с `✓`/`⚠`/`✗`, ни одного `unexpected_applied` в startup-логе.
  Verify: ручной прогон или интеграционный тест
  `tests/test_application_context.py::test_smoke_banner`.

- [ ] 9.5 (опционально) `python tools/diagnose_startup.py --strict` —
  exit 0 при доступной test-БД. Verify: см. README
  `tools/diagnose_startup.py`.

## Acceptance Checklist (сводный)

Скопировать в описание PR:

- [ ] `ApplicationContext.create()` применяет patches ровно один раз.
- [ ] `cli_agent._run_patched()` не применяет `assemble_outbound`
  повторно.
- [ ] `apply_all() == _PATCH_SPECS keys == canonical_runtime_patches()`.
- [ ] `compact_tracking`/`compact_command`/`idle_guard` удалены из
  `_PATCH_SPECS`.
- [ ] `turn_delivery_fail`/`session_dir_watch` имеют `PatchSpec`.
- [ ] `RuntimePatcher` не импортирует `workspace.tools.*` и не
  регистрирует tools.
- [ ] Регистрация project tools живёт в `lib/services/project_tool_loader.py`.
- [ ] Hook allowlist не-alwisted файлы не импортируются (модуль
  не выполняется).
- [ ] `high_risk_required` удалён; `PatchSpec.required` —
  единственный источник.
- [ ] `openspec/specs/runtime/context/spec.md` не противоречит
  фактическому lifecycle.
- [ ] `openspec/specs/COMPONENTS.md` содержит записи для
  `RuntimePatcher` и `ProjectToolLoader`.
- [ ] CHANGELOG обновлён, AGENTS.md синхронизирован,
  `docs/ARCHITECTURE.md` отражает loader boundary.