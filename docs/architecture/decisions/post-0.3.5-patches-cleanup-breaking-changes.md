# ADR-XXX: post-0.3.5-patches-cleanup — breaking changes для unit-тестов

**Дата:** 2026-09-27
**Статус:** Accepted
**Контекст:** OpenSpec change `post-0.3.5-patches-cleanup` (коммиты c0fe1e4, 9509012, 79d5810, 22cbafe, 7a56440, 60e7b8f, 2163f05, efe147e)

## Контекст

Реализация `post-0.3.5-patches-cleanup` привнесла 3 breaking-изменения
для unit-тестов, которые **вызывают `RuntimePatcher.apply_all` напрямую**,
без симуляции `RuntimeEventsSubscriber`:

### 1. `_attach_context_window` больше не имеет fallback `_last_usage`
(коммит 7a56440)

* **До:** `getattr(agent, "_last_usage", None) or {}` — fallback на
  накопленный usage.
* **После:** `ContextWindowNotSeededError` поднимается, если
  bridge не засеян (нет `agent.context_window_tokens` или
  `seed_context_window` не был вызван).

**Затронутые тесты:** все, что вызывают `apply_all` или
`patch_assemble_outbound` без предварительного
`seed_context_window(session_key, limit=40000, model="...")` —
например, `TestPatchAssembleOutbound` (был обновлён в коммите 7a56440),
`TestSaveTurnE2E`, `TestRecentFilesHook`-с-`patcher_*`, smoke-тесты.

### 2. `patch_context_bridge_seed` удалён
(коммит 2163f05)

* **До:** метод существовал как no-op-stub, регистрировался в
  `_PATCH_SPECS` и в `apply_all()` report.
* **После:** метод и spec удалены; `apply_all()` report не содержит
  `context_bridge_seed`.

**Затронутые тесты:** `TestPatchSpecs::test_all_patches_have_specs`,
`TestApplyAll::test_report_contents` (обновлены в коммите 2163f05).

### 3. ActiveFilesHook удалён
(коммит 60e7b8f)

* **До:** `lib/cli/hook_loader.py::scan_and_register` импортировал
  `active_files_hook.py` через `dir(mod)`.
* **После:** allowlist `_allowed_hook_names()` исключает
  `active_files_hook`. Файл удалён.

**Затронутые тесты:** `test_active_files_hook.py::test_file_does_not_exist`
и `test_hook_loader_allowlist_excludes_active_files_hook` —
защитные тесты (добавлены в коммите 60e7b8f).

## Решение

Все breaking changes **зафиксированы в спеке** через
`openspec/.../design.md` (D5, D6, D7) и нормативные требования в
`specs/`. Тесты, которые **не были обновлены**, считаются
**pre-existing failures** и будут исправлены в **отдельном
follow-up change'е**.

## Список тестов, требующих обновления

| Тест | Что нужно сделать |
|---|---|
| `tests/test_runtime_patcher_e2e.py::TestSaveTurnE2E` (2 теста) | Добавить `seed_context_window` в fixture/agent |
| `tests/test_recent_files_hook.py::test_patcher_*` (7 тестов) | Добавить `seed_context_window` в fixture/agent |
| `tests/test_smoke_postgres_channel_media.py::test_patcher_auto_attach_end_to_end` | Добавить `seed_context_window` |
| `tests/test_cli_agent.py::TestHookLoader::test_finds_workspace_hooks_without_hooks_dir_in_syspath` | Обновить (после allowlist-изменения) |
| `tests/test_history_search_tool.py::TestPagination::*` + `TestTruncationFlags::*` (10+ тестов) | Pre-existing DB-проблема: psycopg2 не подключается к host="test". **НЕ относится к этому change'у** — тесты были сломаны до моих правок (см. коммит `188713f` от master). |

## Pre-existing failures (НЕ мои)

На коммите `188713f` (master до моих правок) уже было **10 failures**,
связанных с DB-окружением. Эти тесты **не запускаются в CI** без
работающего Postgres.

## Известные ограничения

* `_attach_context_window` сейчас строго требует bridge. Если
  приложение запускается **без** `RuntimeEventsSubscriber.start()`,
  оно упадёт с `ContextWindowNotSeededError`. **Это by design** —
  fallback скрывал дефекты подписки.

* `patch_active_files_in_context` (упомянутый в docstring
  `active_files_hook.py:67, 290`) **никогда не существовал** —
  это была ложная ссылка, которую никто не реализовал.

## См. также

* `openspec/changes/archive/2026-09-27-post-0.3.5-patches-cleanup/design.md`
* `docs/architecture/decisions/active-files-hook-removal.md`
* коммиты c0fe1e4..efe147e на master
