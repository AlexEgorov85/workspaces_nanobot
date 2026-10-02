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
