# Модель компонента (Component Model)

## Purpose

Этот документ определяет модель архитектурного компонента проекта workspaces_nanobot и устанавливает единый шаблон для всех компонентных спецификаций.

## ADDED Requirements

### Requirement: каждый значимый компонент имеет спецификацию

Система ДОЛЖНА поддерживать реестр архитектурных компонентов. Каждый production-компонент, включённый в реестр, ДОЛЖЕН иметь отдельную OpenSpec specification.

#### Scenario: обнаружен новый компонент

- **WHEN** в коде выявлен новый архитектурный компонент
- **THEN** он ДОЛЖЕН быть добавлен в COMPONENTS.md со статусом `missing`
- **AND** для него ДОЛЖНА быть создана спецификация

### Requirement: спецификация на русском языке

Все новые и изменяемые component specifications ДОЛЖНЫ быть написаны на русском языке. Технические идентификаторы, имена классов, методов, файлов, конфигурационных ключей и API ДОЛЖНЫ сохранять оригинальное написание.

#### Scenario: создание новой спецификации

- **WHEN** создаётся новая component spec
- **THEN** все заголовки разделов ДОЛЖНЫ быть на русском языке
- **AND** имена классов (`ApplicationContext`), методов (`can_handle()`), файлов (`project.json`) остаются на английском

### Requirement: единый шаблон

Каждая component specification ДОЛЖНА содержать следующие обязательные разделы:

1. Назначение (Purpose)
2. Ответственность (Responsibility)
3. Граница (Boundary) — с подразделами owns / does not own / may depend on / must not depend on
4. Публичный контракт (Public Contract)
5. Требования (Requirements) — минимум одно требование с сценарием
6. Запрещённое поведение (Negative Requirements)
7. Зависимости (Dependencies)
8. Реализация (Implementation Reference)
9. Проверка (Verification)

Следующие разделы заполняются если применимо: Конфигурация, Жизненный цикл, Состояние, Инварианты, Поведение при ошибке, Потребители.

#### Scenario: проверка структуры spec

- **WHEN** запускается валидация (`tools/validate_component_specs.py`)
- **THEN** spec, в которой отсутствует один из обязательных разделов, ДОЛЖНА быть обнаружена
- **AND** отчёт ДОЛЖЕН содержать имя раздела и путь к файлу

### Requirement: разделение ответственности

Component specification ДОЛЖНА описывать contractual behavior. Implementation details ДОЛЖНЫ оставаться в `docs/` и source code, если они не необходимы для определения контракта.

#### Scenario: описание метода

- **WHEN** spec описывает метод компонента
- **THEN** описание ДОЛЖНО формулировать контракт, а не перечислять поля реализации
- **BAD**: "ApplicationContext имеет 17 полей: field1, field2, ..., field17"
- **GOOD**: "ApplicationContext предоставляет единый корень сборки общей инфраструктуры runtime"

### Requirement: single source of truth

Нормативные архитектурные правила ДОЛЖНЫ оставаться в `docs/TARGET_ARCHITECTURE.md`. Component-specific contracts ДОЛЖНЫ вестись в `openspec/specs/<domain>/<component>/spec.md`. Implementation descriptions ДОЛЖНЫ оставаться в `docs/`.

#### Scenario: проверка размещения правил и контрактов

- **WHEN** автор готовит описание компонента
- **THEN** нормативные правила ДОЛЖНЫ идти в `docs/TARGET_ARCHITECTURE.md`
- **AND** контракт компонента ДОЛЖЕН идти в `openspec/specs/<domain>/<component>/spec.md`
- **AND** детали реализации ДОЛЖНЫ идти в `docs/`

### Requirement: отсутствие дублирования

Система НЕ ДОЛЖНА создавать второе нормативное описание одного компонента в другом файле OpenSpec.

#### Scenario: обнаружено дублирование

- **WHEN** один и тот же контракт описан в двух разных spec
- **THEN** одно из описаний ДОЛЖНО быть удалено или перемещено в `docs/`

## Negative Requirements

Система НЕ ДОЛЖНА:

- выдавать предположения за контракт (spec должна основываться на анализе кода или явно согласованных решениях)
- создавать spec для каждого `.py` файла (только архитектурные компоненты)
- смешивать нормативные требования с implementation details
- устанавливать статус `complete` без реальной проверки соответствия коду
- описывать желаемую архитектуру вместо фактической (без отдельного change)

## Dependencies

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `openspec/config.yaml` — конфигурация OpenSpec

## Implementation Reference

Основная реализация:

- `openspec/changes/component-specification-system/design.md` — архитектура системы
- `openspec/changes/component-specification-system/proposal.md` — обоснование
- `openspec/changes/component-specification-system/tasks.md` — план работ

Связанные документы:

- `openspec/specs/COMPONENTS.md` — реестр компонентов
- `docs/TARGET_ARCHITECTURE.md` — глобальная архитектура

## Verification

Валидация модели компонента включает:

1. **Структурная проверка**: каждая новая spec следует установленному шаблону.
2. **Языковая проверка**: заголовки разделов на русском языке.
3. **Проверка полноты**: обязательные разделы присутствуют.
4. **Проверка на дублирование**: нет двух spec для одного компонента.
5. **Проверка границ**: boundary явно определён для каждого компонента.

Автоматическая проверка: `tools/validate_component_specs.py`.
