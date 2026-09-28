## 1. Baseline и inventory-снимок

- [ ] 1.1 Зафиксировать baseline текущего состояния. Снять списки:
  `apply_all()` patches (13 имён — `context_governor`, `save_turn`,
  `exec_limits`, `exec_timeout_cap`, `tool_limits`, `assemble_outbound`,
  `turn_delivery_fail`, `async_save`, `session_dir_watch`,
  `subagent_logging`, `project_tools`, `document_text_threshold`,
  `session_content_cleanup`), `_PATCH_SPECS` имена (14 —
  11 совпадающих с `apply_all` + `project_tools` + 3 DEPRECATED),
  `canonical_runtime_patches()` имена (те же 14, что и `_PATCH_SPECS`),
  `_allowed_hook_names()` (3 имени), `high_risk_required` (4 имени,
  локальная переменная внутри `canonical_runtime_patches`).
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
  - вызывает `ApplicationContext.create()` и проверяет, что
    `agent._assemble_outbound` обёрнут ровно один раз
    (через `getattr`/`wraps`/маркер `_project_wrapped=True`);
  - **НЕ** проверяет idempotency — контракт держится архитектурно
    (отсутствие повторных call site'ов), а не runtime-флагом.
  Verify: `pytest tests/test_application_context.py -k
  single_application_point -q` зелёный; тест падает на старом коде
  (двойная обёртка в `cli_agent.py:174`).

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
  (строки 162–186) удалить локальный `high_risk_required` и
  **заменить** текущую проекцию на проекцию через `spec.required`
  (а не через локальный frozenset). Verify:
  `{p.name for p in canonical_runtime_patches()} ==
  set(RuntimePatcher.patch_specs())` (сравнение имён, не объектов:
  `canonical_runtime_patches()` возвращает `list[RuntimePatchSpec]`,
  `patch_specs()` — `dict[str, PatchSpec]`); семантический тест
  в `tests/test_runtime_inventory.py::test_required_projects_from_patch_spec`
  (см. task 6.1) явно проверяет отсутствие второго источника истины.

- [ ] 3.5 Ужесточить
  `tests/test_runtime_patcher.py::TestPatchSpecs::test_all_patches_have_specs`
  до exact-match проверки трёх множеств: переименовать в
  `test_inventory_is_exact` и заменить
  `for key in expected: assert key in actual`
  на **попарное равенство трёх множеств** (AST-анализ
  `RuntimePatcher.apply_all` для извлечения реально вызываемых
  имён из аргументов `self._record(report, "<name>", ...)`,
  `set(RuntimePatcher.patch_specs())`, и
  `{p.name for p in canonical_runtime_patches()}`). Финальный
  expected — 12 имён. **Запрещено** создавать четвёртый hardcoded
  список имён в `expected` — иначе тест перестаёт ловить drift
  между `apply_all()` и `_PATCH_SPECS`. Verify: тест падает на
  старом коде (14 vs 12); зеленеет после 3.1–3.3 И 4.2.

- [ ] 3.6 Обновить `docs/architecture/runtime-patcher-inventory.md` —
  пересобрать каталог патчей под новое exact inventory (12 patches),
  удалить упоминания `compact_tracking`/`compact_command`/`idle_guard`,
  добавить `turn_delivery_fail`/`session_dir_watch`.
  Verify:
  ```
  git grep -l "compact_tracking\|compact_command\|idle_guard" \
    -- '*.md' '*.py' | \
    grep -v CHANGELOG.md | \
    grep -v 'openspec/changes/archive/'
  ```
  показывает 0 матчей. Никаких исключений для `_PATCH_SPECS` или
  release-веток — патчи удаляются атомарно (см. Decision 3 и
  Non-Goals про отсутствие deprecation period).

## 4. Вынос `patch_project_tools` в отдельный loader

- [ ] 4.1 Создать `lib/services/project_tool_loader.py` с
  **одним** публичным контрактом:

  ```python
  @dataclass(frozen=True)
  class ProjectToolsLoadResult:
      registered: list[str]
      disabled: list[str]
      duplicate: list[str]
      failed: list[str]
      detail: str

  def register_project_tools(
      agent: AgentLoop,
      workspace_dir: Path,
      *,
      settings: Any = None,
      cache_store: CacheProvider | None = None,
      db_logging_service: DbLoggingService | None = None,
  ) -> ProjectToolsLoadResult: ...
  ```

  Тело `register_project_tools` — копия существующего
  `patch_project_tools` (`lib/services/runtime_patcher.py:2279-2509`)
  без семантических правок: тот же `pkgutil.iter_modules` по
  `workspace/tools/`, тот же `ToolContext(...)` с тем же `setattr`
  DI, тот же `cls.enabled(ctx)` / `cls.create(ctx)` /
  `agent.tools.register(tool)`. Discovery (`pkgutil.iter_modules` +
  `importlib.util`) — приватная функция `_discover(workspace_dir)`,
  не публичная.

  **Семантика — best-effort с частичным успехом, НЕ атомарная:**
  ошибка одного tool (`Tool.enabled` / `Tool.create` /
  `agent.tools.register`) не отменяет успешно зарегистрированные
  остальные. Это поведение уже реализовано в существующем
  `patch_project_tools` (цикл `for cls in candidates` ловит
  исключения per-class, см. `runtime_patcher.py:2470-2472`).
  `ProjectToolsLoadResult` фиксирует детерминированный результат
  через структурные поля `registered`/`disabled`/`duplicate`/
  `failed` (для programmatic consumers) и поле `detail: str`
  (presentation/diagnostic representation — для баннера логов
  и для существующего `parse_project_tools_detail` в
  `runtime_inventory.py`). В рамках этой change сохраняется
  существующий формат `detail`; `diff_project_tools_from_detail()`
  и regex-парсер не переписываются.

  Verify: `python -c "from lib.services.project_tool_loader
  import register_project_tools, ProjectToolsLoadResult; print('OK')"`
  печатает OK.

- [ ] 4.2 В `lib/services/runtime_patcher.py` удалить метод
  `patch_project_tools` и удалить соответствующий вызов из
  `apply_all()` (`lib/services/runtime_patcher.py:650-652`). Никаких
  backward-compat stubs — `RuntimePatcher` **не содержит** методов
  по `project_tools` после Phase 4. Если позже потребуется
  вернуть — это новая change, не legacy-fallback.
  Verify: `git grep -n "patch_project_tools\|project_tools"
  -- lib/services/runtime_patcher.py` показывает 0 матчей.

- [ ] 4.3 В `lib/core/application_context.py` после
  `ctx.runtime_patcher.apply_all(...)` (строки 339–346) добавить
  вызов loader'а:

  ```python
  from lib.services.project_tool_loader import (
      register_project_tools, ProjectToolsLoadResult,
  )

  project_tools_result = register_project_tools(
      agent=ctx.agent,
      workspace_dir=ctx.workspace_dir,
      settings=ctx.settings,
      cache_store=ctx.cache_store,
      db_logging_service=ctx.db_logging_service,
  )
  ctx.project_tools_result = project_tools_result
  ```

  Verify: `pytest tests/test_application_context.py
  tests/test_tools_project_loader.py -q` зелёный; интеграционный
  тест `test_real_compact_context_tool_loads` (см. существующий
  в `tests/test_tools_project_loader.py:431+`) продолжает проходить.

- [ ] 4.4 В `lib/core/application_context.py` обновить
  `_emit_project_tools_inventory_banner` (определена в
  `application_context.py:750-810`, **не** в `runtime_inventory.py`)
  — баннер **больше НЕ читает**
  `patch_report.details["project_tools"]` (этого ключа в
  `PatchReport` больше нет после Phase 4). Источник данных —
  `project_tools_result.detail` (см. task 4.3). Сигнатура
  `_emit_project_tools_inventory_banner` меняется с
  `(patch_report)` на `(project_tools_result)`.
  Verify: `git diff` показывает правки только в `application_context.py`;
  ручной прогон `cli_agent.py --smoke` печатает баннер project
  tools с теми же именами, что и до change; `tools/diagnose_startup.py
  --strict` (если запускается) exit 0.

- [ ] 4.5 Архитектурный тест `tests/test_architecture_*.py`
  (или расширение `test_dependency_direction.py`)
  `test_runtime_patcher_no_project_tools_boundary`:
  через AST-анализ `lib/services/runtime_patcher.py` подтверждает,
  что модуль не импортирует `workspace.tools.*`, не строит
  `ToolContext`, не зовёт `agent.tools.register`. Verify: тест
  падает на старом коде; зеленеет после 4.1–4.3.

- [ ] 4.6 Обновить существующие тестовые fixtures, где
  `project_tools` всё ещё числится в runtime inventory. Список
  обязательных правок (базируется на текущем коде в
  `tests/test_runtime_inventory.py`,
  `tests/test_runtime_patcher.py`, `tests/test_tools_project_loader.py`,
  `tests/test_context_compaction.py`, `tests/test_gateway.py`,
  `tests/test_diagnose_startup.py`):
  - `tests/test_runtime_inventory.py:182` — убрать `"project_tools"`
    из `applied` списка в `test_applied_matches_canonical` (после
    change в `applied` нет `project_tools`; subset-проверка
    продолжает работать, но fixture должен отражать фактическое
    состояние `apply_all`);
  - `tests/test_runtime_patcher.py:1112` — убрать `"project_tools"`
    из `expected` subset в `test_all_patches_have_specs` (после
    change этот тест переписывается на exact-match в task 3.5,
    см. там);
  - `tests/test_tools_project_loader.py` — все вызовы
    `RuntimePatcher().patch_project_tools(...)` или
    `patcher.patch_project_tools(...)` заменяются на
    `register_project_tools(...)` из нового модуля. Сам файл
    **не переименовывается** (`tests/test_tools_project_loader.py`
    остаётся, см. Issue 10 ниже); классы тестов
    (`TestPatchProjectTools` → `TestRegisterProjectTools`)
    переименовываются минимально, docstring'и обновляются;
  - `tests/test_context_compaction.py:625` —
    `RuntimePatcher().patch_project_tools(...)` → вызов
    `register_project_tools(...)` напрямую;
  - `tests/test_gateway.py:235-236` — assertions про
    `patch_report.details["project_tools"]` должны быть
    удалены/обновлены, т.к. этого ключа больше нет в
    `PatchReport` (см. task 4.4);
  - `tests/test_diagnose_startup.py` — если parser читает
    `patch_report.details["project_tools"]`, переключается на
    новый `ProjectToolsLoadResult` (или совместимый формат
    через `project_tools_result.detail`).
  Verify: `pytest tests/test_tools_project_loader.py
  tests/test_runtime_inventory.py tests/test_runtime_patcher.py
  tests/test_context_compaction.py tests/test_gateway.py
  tests/test_diagnose_startup.py -q` — все зелёные; ни в одном
  файле нет упоминаний `patch_project_tools` (как метода
  `RuntimePatcher`) и нет fixture'ов, считающих `project_tools`
  runtime patch'ем.

## 5. Реальный hook allowlist

- [ ] 5.1 В `lib/cli/hook_loader.py::scan_and_register` (строки
  53–83) изменить логику: при `path.stem not in allowed` —
  **continue** (пропуск файла целиком, без импорта, без `exec_module`,
  без `dir(mod)`/`AgentHook`-instantiation), с записью
  INFO-лога через `logger.info("hook %s not in allowlist, skipped",
  path.stem)` (без `rich.console`-warning).
  Verify: `git diff lib/cli/hook_loader.py` показывает только
  изменение логики в ветке `if path.stem not in allowed`.

- [ ] 5.2 Добавить тест `tests/test_hook_allowlist.py`
  (новый файл) `test_non_allowlisted_hook_blocks_module_exec`:
  - `monkeypatch.setattr(hook_loader, "_allowed_hook_names",
    lambda: frozenset())` (полностью пустой allowlist для теста);
  - создать временный hook-файл с module-level side-effect:
    `MARKER_PATH.write_text("imported")`;
  - вызвать `scan_and_register(hooks_dir, workspace_dir)`;
  - assert `not MARKER_PATH.exists()` (модуль **не выполнялся**).
  Verify: тест падает на старом коде (warning + import +
  module exec); зеленеет после 5.1.

- [ ] 5.3 Добавить тест `test_allowlisted_hook_is_registered`:
  - `monkeypatch.setattr(hook_loader, "_allowed_hook_names",
    lambda: frozenset({"session_file_redirect_hook"}))`;
  - создать `session_file_redirect_hook.py` во временной
    `workspace/hooks/` через `_write_hook_module` helper;
  - assert `len(scan_and_register(...)) > 0` и инстанс имеет
    type name `"SessionFileRedirectHook"`.
  Verify: тест зелёный; обеспечивает, что сужение allowlist не
  отрезает production-хуки. Production `_allowed_hook_names()` не
  трогаем в тестах — monkeypatch на функции.

- [ ] 5.4 Добавить тест `test_unknown_hook_silent_skip`:
  - `monkeypatch.setattr(hook_loader, "_allowed_hook_names",
    lambda: frozenset({"known_hook"}))`;
  - два файла в `workspace/hooks/`: `known_hook.py` (в allowlist)
    и `unknown_hook.py` (вне allowlist, без side-effect);
  - assert возвращаемый список содержит только инстанс known hook,
    и нет экземпляров из `unknown_hook`.
  Verify: тест падает на старом коде (оба импортируются);
  зеленеет после 5.1.

## 6. Архитектурные тесты и инварианты

- [ ] 6.1 Добавить тест
  `tests/test_runtime_inventory.py::test_required_projects_from_patch_spec`
  (семантический, не module-attribute):
  - Через `monkeypatch.setattr` подменить
    `RuntimePatcher.patch_specs` на fake-реализацию,
    возвращающую dict с 3 spec'ами:
    - `X_high_required`: `risk="high"`, `required=True`;
    - `Y_high_optional`: `risk="high"`, `required=False`
      (доказывает отсутствие автокорреляции `risk → required`);
    - `Z_low_required`: `risk="low"`, `required=True`.
  - Вызвать `canonical_runtime_patches()`.
  - assert `required=True` для X и Z; `required=False` для Y.
  Verify: тест падает на старом коде (где `high_risk_required` —
  hardcoded set, который бы проигнорировал fake-spec.required);
  зеленеет после 3.4.

- [ ] 6.2 Добавить тест
  `tests/test_runtime_inventory.py::test_critical_patches_marked_required`:
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

- [ ] 7.1 Зарегистрировать в `openspec/specs/COMPONENTS.md` только
  один компонент:
  - `RuntimePatcher` (`lib/services/runtime_patcher.py:RuntimePatcher`) →
    `runtime/runtime-patcher` со статусом `partial`.
  **`ProjectToolLoader` НЕ регистрируется** — это internal
  stateless helper (нет lifecycle, нет state, нет конфигурации,
  одна функция). По критериям `openspec/specs/architecture/component-model/spec.md`
  это не компонент. Loader описывается только в `docs/ARCHITECTURE.md`
  и упоминается в boundary-разделе спеки `runtime-patcher`.
  Verify: `git diff openspec/specs/COMPONENTS.md` показывает
  одну новую строку; таблица категории `runtime` теперь содержит
  3 записи (ранее — 2).

- [ ] 7.2 После архивации change создать canonical-спеку:
  - `openspec/specs/runtime/runtime-patcher/spec.md` (по шаблону
    `architecture/component-model` на русском).
  **Спека `project-tool-loader/spec.md` НЕ создаётся** — loader
  не компонент. Verify: `python tools/validate_component_specs.py`
  зелёный.

- [ ] 7.3 Исправить contract drift в
  `openspec/specs/runtime/context/spec.md`: требование
  «`ApplicationContext.start()` MUST вызывать `RuntimePatcher.apply_all`»
  заменяется на актуальное утверждение
  (см. `specs/runtime/context/spec.md` MODIFIED Requirement в этой
  change + Decision 6 в design.md — полная перезапись, не
  частичная правка). Verify: `git grep "start()" -- openspec/specs/runtime/context/`
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
  финальный каталог из **12 patches** (после 3.1–3.3 И 4.2).

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
  exit 0; баннер «Runtime patches» содержит ровно **12** строк
  с `✓`/`⚠`/`✗`, ни одного `unexpected_applied` в startup-логе;
  баннер «Project tools» печатается отдельным блоком на
  основании `ProjectToolsLoadResult.detail`.
  Verify: ручной прогон или интеграционный тест
  `tests/test_application_context.py::test_smoke_banner`.

- [ ] 9.5 (опционально) `python tools/diagnose_startup.py --strict` —
  exit 0 при доступной test-БД. Verify: см. README
  `tools/diagnose_startup.py`.

## Acceptance Checklist (сводный)

Скопировать в описание PR:

- [ ] `ApplicationContext.create()` применяет runtime patches ровно один раз;
  entrypoint'ы (cli_agent/gateway/streamlit) **не** повторно вызывают
  ни `apply_all`, ни отдельные `patch_*` методы.
- [ ] `cli_agent._run_patched()` не применяет `assemble_outbound` повторно.
- [ ] **`apply_all() == _PATCH_SPECS names == canonical_runtime_patches() == 12`**.
- [ ] `compact_tracking`/`compact_command`/`idle_guard` удалены из
  `_PATCH_SPECS`.
- [ ] `turn_delivery_fail`/`session_dir_watch` имеют `PatchSpec`.
- [ ] `project_tools` удалён из `_PATCH_SPECS` и из `apply_all()`.
- [ ] `RuntimePatcher` не импортирует `workspace.tools.*` и не
  регистрирует tools (AST-тест 4.5).
- [ ] Регистрация project tools живёт в `lib/services/project_tool_loader.py`;
  единственная публичная функция `register_project_tools(...)`;
  возвращает `ProjectToolsLoadResult`.
- [ ] `_emit_project_tools_inventory_banner` читает
  `project_tools_result.detail`, **не** `patch_report.details["project_tools"]`.
- [ ] Hook allowlist не-alwisted файлы не импортируются (модуль
  не выполняется); тесты через `monkeypatch.setattr` на
  `_allowed_hook_names`.
- [ ] `high_risk_required` удалён из `runtime_inventory.py`;
  `PatchSpec.required` — единственный источник; семантический
  тест 6.1 доказывает проекцию.
- [ ] `openspec/specs/runtime/context/spec.md` не противоречит
  фактическому lifecycle (полная перезапись requirement'а,
  не частичная правка).
- [ ] `openspec/specs/COMPONENTS.md` содержит запись **только**
  для `RuntimePatcher` (не для `ProjectToolLoader`).
- [ ] CHANGELOG обновлён, AGENTS.md синхронизирован,
  `docs/ARCHITECTURE.md` отражает loader boundary.