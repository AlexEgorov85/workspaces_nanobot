## Why

Текущий `RuntimePatcher` объединяет в одном модуле две разные
ответственности: monkey-patch'и upstream `nanobot.agent.loop.AgentLoop`
и регистрацию пользовательских tool'ов из `workspace/tools/*.py`.
Кроме того, lifecycle применения патчей содержит дубль:
`ApplicationContext.create()` вызывает `RuntimePatcher.apply_all()`
(см. `lib/core/application_context.py:339-346`), а
`cli_agent._run_patched()` после этого ещё раз вызывает
`patch_assemble_outbound` (`cli_agent.py:174`).

Канонический inventory (`_PATCH_SPECS`,
`runtime_inventory.canonical_runtime_patches`) расходится с реальным
набором вызываемых patches: `turn_delivery_fail` и `session_dir_watch`
применяются без `PatchSpec`, тогда как `compact_tracking`,
`compact_command`, `idle_guard` объявлены как DEPRECATED, но формально
остаются в `_PATCH_SPECS` и попадают в `canonical_runtime_patches()`.
Тест `tests/test_runtime_patcher.py::TestPatchSpecs::test_all_patches_have_specs`
проверяет только subset (`for key in expected: assert key in actual`),
а не равенство, поэтому drift остаётся незамеченным.

Hook allowlist в `lib/cli/hook_loader.py` (см. `_allowed_hook_names`)
формально документирован как «защита от случайного добавления плагина»,
но фактически при отсутствии файла в allowlist только печатает
warning и продолжает импорт/исполнение модуля (`lib/cli/hook_loader.py:61-83`).

`runtime_inventory.canonical_runtime_patches()` хранит второй
hardcoded set `high_risk_required` (см.
`lib/services/runtime_inventory.py:170-175`), дублирующий criticality,
которой больше нет в `PatchSpec` (есть только `risk`).

Это создаёт риск двойного применения runtime-обёрток, drift между
диагностикой и реальностью, и нарушение архитектурной границы
`RuntimePatcher` (patch vs registration). Change не вводит новых
абстракций, а приводит фактическую реализацию к уже существующим
архитектурным правилам.

## What Changes

- Применение runtime patch'ей к одному экземпляру `AgentLoop` в рамках
  одного startup lifecycle становится **однократным**: точка применения —
  `RuntimePatcher.apply_all()`, вызываемая из
  `ApplicationContext.create()`. Повторные вызовы отдельных `patch_*`
  методов из CLI/gateway/streamlit запрещаются.
- Существующий дубль `patch_assemble_outbound` в `cli_agent._run_patched()`
  удаляется.
- Регистрация проектных tool'ов из `workspace/tools/*.py` выносится из
  `RuntimePatcher.patch_project_tools` в отдельный узкий loader.
  `RuntimePatcher` более не отвечает за discover / DI / register
  пользовательских tool'ов.
- Канонический runtime-patch inventory синхронизируется с реальным
  набором вызываемых patches:
  - `turn_delivery_fail` и `session_dir_watch` получают `PatchSpec`;
  - `compact_tracking`, `compact_command`, `idle_guard` удаляются из
    `_PATCH_SPECS` (DEPRECATED в nanobot 0.3.5, фактически не
    вызываются);
  - `project_tools` удаляется из `_PATCH_SPECS` после вынесения
    регистрации.
- Hook allowlist становится **действительно** ограничивающим: файл не
  из allowlist не импортируется и не регистрируется.
- Источник истины для критичности (`required` vs optional) —
  единственный: `PatchSpec.required`. `runtime_inventory` только
  проецирует metadata, без второго hardcoded set.
- Существующий contract drift в `openspec/specs/runtime/context/spec.md`
  (`start()` MUST вызывать `apply_all`) исправляется: `apply_all`
  вызывается из `create()`, а `start()` отвечает за фоновое
  lifecycle-оборудование.
- Добавляются регрессионные/архитектурные проверки, фиксирующие
  новое состояние.

## Capabilities

### New Capabilities

- `runtime/runtime-patcher`: нормативный контракт `RuntimePatcher` —
  единая точка применения upstream runtime patches, граница
  ответственности (только upstream patches, не tool registration),
  требования к однократности применения, exact inventory
  correspondence, единый источник `required`.

### Modified Capabilities

- `runtime/context`: требование «`ApplicationContext.start()` MUST
  вызывать `RuntimePatcher.apply_all`» заменяется на нормативное
  утверждение, отражающее реальный lifecycle (`create()` →
  `apply_all()`; `start()` — только фоновое оборудование).

## Impact

- Код:
  - `lib/services/runtime_patcher.py`: убрать `patch_project_tools`,
    метод остаётся в `apply_all` пустой заглушкой NO-OP до полного
    вынесения; добавить `PatchSpec` для `turn_delivery_fail` и
    `session_dir_watch`; удалить `compact_tracking`/`compact_command`/
    `idle_guard` из `_PATCH_SPECS`;
  - `lib/cli/hook_loader.py`: исправить логику allowlist —
    не-alwisted файлы пропускаются без импорта;
  - `cli_agent.py`: убрать повторный `patch_assemble_outbound` из
    `_run_patched()`;
  - новый модуль `lib/services/project_tool_loader.py` —
    discover + DI + register project tools;
  - `lib/services/runtime_inventory.py`: убрать `high_risk_required`,
    проекция из `PatchSpec.required`;
  - `lib/core/application_context.py`: новый вызов loader'а
    project tools вместо `patch_project_tools` через `apply_all`;
    composition-root остаётся `create()`.
- Тесты:
  - `tests/test_runtime_patcher.py`:
    `test_all_patches_have_specs` ужесточается до `==`;
  - `tests/test_tools_project_loader.py`: расширяется (или переносится)
    на новый модуль loader'а;
  - `tests/test_runtime_inventory.py`: тест на отсутствие
    `high_risk_required` в `canonical_runtime_patches`;
  - новые тесты:
    `test_application_context_single_application_point`,
    `test_hook_allowlist_rejects_unknown_hook`,
    `test_patch_assemble_outbound_not_reapplied_in_cli`;
  - `tests/test_architecture_*.py` / `tests/test_dependency_direction.py`:
    добавить проверку, что `RuntimePatcher` не импортирует
    `workspace.tools.*` и не зовёт `ToolRegistry.register`.
- Конфигурация: без изменений.
- SQL/DDL: без изменений.
- API: `RuntimePatcher.patch_project_tools` помечается deprecated и
  выпиливается в release-ветке после архивации change.
- Документация:
  - `openspec/specs/COMPONENTS.md`: добавить записи для
    `RuntimePatcher` и `ProjectToolLoader`;
  - `openspec/specs/runtime/context/spec.md`: исправить требование про
    `apply_all`;
  - `AGENTS.md` / `docs/ARCHITECTURE.md`: синхронизировать описание
    lifecycle и loader boundary;
  - `docs/architecture/runtime-patcher-inventory.md`: пересобрать
    каталог патчей под новый inventory;
  - `CHANGELOG.md`: блок `[Unreleased]`.