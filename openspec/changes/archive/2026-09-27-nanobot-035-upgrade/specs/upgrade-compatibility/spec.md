# Capability: nanobot-upgrade-compatibility

## Purpose

Определяет нормативный контракт совместимости проекта с апгрейдами upstream `nanobot-ai`: какие поверхности должны оставаться стабильными, как фиксируется версия, какие тесты обязательны перед merge upgrade-изменения. Цель — сократить цикл `apply_all → pytest` и предотвратить регрессии при каждом следующем релизе nanobot.

## ADDED Requirements

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
- **THEN** он MUST ДОЛЖЕН быть помечен как `DEPRECATED` в `_DECLARED_PATCHES` с явным комментарием о том, какой upstream-механизм заменил функцию

### Requirement: Реактивная миграция на upstream-механизмы

Если upstream добавил встроенную подсистему, дублирующую наш патч или сервис, проект SHALL ДОЛЖЕН удалить дубль и подключиться к upstream-API в том же upgrade-изменении.

#### Scenario: Встроенный `/compact` заменил наш patch_compact_command

- **WHEN** upstream регистрирует `/compact` через `register_builtin_commands`
- **THEN** наш `lib/commands/compact_command.py` и `patch_compact_command` MUST ДОЛЖЕН быть удалены, а наш `ContextCompactionService._notify` MUST ДОЛЖЕН срабатывать через `AgentHook.after_run` после upstream-обработчика

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

`tests/test_profile_lifecycle.py`, `tests/test_history_search_tool.py` (scope-isolation), `tests/test_pg_session_manager.py` (framework contract) — SHALL НЕ ДОЛЖНЫ падать после upgrade-изменений; если они падают по причинам, не относящимся к upgrade, они MUST ДОЛЖНЫ быть помечены `xfail` с явной причиной и `TODO` ссылкой в `design.md`.

#### Scenario: Падающий тест не из upgrade-скоупа

- **WHEN** тест не из `tests/contract/` и не относится к `runtime_patcher.py`, падает на новой версии
- **THEN** изменение MUST ДОЛЖЕН быть помечено `xfail` с reason, ссылающимся на задачу вне scope этого изменения

## Запрещённое поведение

Система SHALL НЕ ДОЛЖЕН:

- использовать range-операторы (`>=`, `~=`, `^`) для `nanobot-ai` в `requirements.txt`
- мокать отсутствующие upstream-символы вместо их удаления/переписывания
- хранить deprecated-патчи в `RuntimePatcher._DECLARED_PATCHES` со статусом `OK` — только `DEPRECATED` или явный комментарий
- добавлять profile-specific ветки в `RuntimePatcher.apply_all` (`if profile == "test"`)
- импортировать приватные nanobot-символы вне документации

## Зависимости

- `nanobot-ai` (PyPI, фиксированная версия) — внешняя
- `lib/services/runtime_patcher.py` — основная реализация
- `tests/contract/` — обязательные тесты-индикаторы
- `docs/architecture/runtime-patcher-inventory.md` — документация каталога
- `openspec/specs/runtime/context` — модифицируется этим изменением (ContextCompactionService через EventSink)

## Конфигурация

Отсутствует. Версия nanobot зафиксирована в `requirements.txt`, поведение патчей управляется кодом.

## Жизненный цикл

1. **Upgrade-detection**: review CHANGELOG upstream nanobot или `pip index versions nanobot-ai`
2. **Baseline**: `pytest tests/contract -q` показывает текущее состояние контракта
3. **Inventory update**: для каждого патча проверить target через `introspect` или `inspect`
4. **Patch code**: исправить сломанные сигнатуры, удалить deprecated
5. **Spec update**: обновить `runtime-patcher-inventory.md` и тесты-контракты
6. **Validate**: `openspec validate nanobot-035-upgrade` → `pytest tests/` зелёный

## Состояние

`RuntimePatcher` хранит:
- `_DECLARED_PATCHES` — список зарегистрированных патчей
- `PatchReport` — статус после `apply_all` (ok/failed/skipped)
- `_VERSION_COMPAT` — маппинг `{nanobot_version: {patch_name: status}}`

НЕ хранит per-session данные, не хранит кеш результатов (это в `ContextCompactionService`).

## Инварианты

- `RuntimePatcher.apply_all` завершается без `failed`-патчей
- Версия `nanobot-ai` закреплена в `requirements.txt` через `==`
- `tests/contract/` зелёные
- Каждый активный патч имеет запись в `runtime-patcher-inventory.md`
- Deprecated-патчи помечены `DEPRECATED` и не возвращают `failed`

## Поведение при ошибке

- `RuntimePatcher.apply_all` ловит исключения каждого патча, логирует в `report.failed`, не падает целиком
- Отсутствующий upstream target → `report.failed.append((patch_name, "target missing"))` + loguru warning
- Сигнатурная ошибка → `report.failed.append((patch_name, repr(exc)))` + warning

## Потребители

- `lib/core/application_context.py` — вызывает `RuntimePatcher.apply_all` на старте
- `tests/contract/` — smoke-индикатор совместимости
- `tests/test_runtime_patcher.py` — детальные тесты каждого патча
- Документация: `docs/architecture/runtime-patcher-inventory.md`

## Реализация

- `lib/services/runtime_patcher.py` — основная реализация (1350+ строк, ~16 патчей)
- `tests/contract/test_agent_loop_api.py` — сигнаты AgentLoop
- `tests/contract/test_command_router.py` — CommandRouter
- `tests/contract/test_compaction_api.py` — Consolidator + AgentDefaults
- `tests/test_runtime_patcher.py` — детальные тесты патчей
- `tests/test_runtime_patcher_e2e.py` — e2e (exec limits)
- `tests/test_tools_project_loader.py` — patch_project_tools

## Проверка

1. `pytest tests/contract -q` → `0 failed`
2. `pytest tests/test_runtime_patcher.py -q` → `0 failed`
3. `pytest tests/test_tools_project_loader.py -q` → `0 failed`
4. `RuntimePatcher(...).apply_all()` → `report.failed == []`
5. `pip show nanobot-ai` → версия == строка из `requirements.txt`
6. `openspec validate nanobot-035-upgrade` → зелёный