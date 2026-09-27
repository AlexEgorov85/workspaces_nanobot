# Реестр компонентов (Component Registry)

## Purpose

Этот документ определяет правила ведения единого реестра архитектурных компонентов проекта workspaces_nanobot. Реестр служит индексом для навигации по компонентным спецификациям и отслеживания полноты покрытия системы спецификациями.

## ADDED Requirements

### Requirement: каждый production-компонент зарегистрирован

Система ДОЛЖНА поддерживать реестр всех production-компонентов. Каждый компонент, включённый в реестр, ДОЛЖЕН иметь запись со следующими полями:

- Компонент (название)
- Категория (домен)
- Реализация (путь к коду)
- Спецификация (путь к `spec.md`)
- Статус (`missing` / `draft` / `partial` / `complete` / `deprecated`)

#### Scenario: добавление нового компонента

- **WHEN** обнаружен новый архитектурный компонент в коде
- **THEN** он ДОЛЖЕН быть добавлен в `COMPONENTS.md` со статусом `missing`
- **AND** для него ДОЛЖНА быть создана спецификация и статус обновлён

### Requirement: реестр не является вторым источником истины

Реестр ДОЛЖЕН содержать только метаданные (название, категория, реализация, спецификация, статус). Реестр НЕ ДОЛЖЕН содержать описание поведения компонентов.

#### Scenario: обновление описания

- **WHEN** требуется обновить описание поведения компонента
- **THEN** изменения ДОЛЖНЫ быть внесены в соответствующий `spec.md`
- **AND НЕ** в `COMPONENTS.md`

### Requirement: честные статусы

Статус `complete` ДОЛЖЕН устанавливаться только когда:

- все обязательные разделы spec заполнены
- spec соответствует фактическому коду
- boundary явно определён
- есть как минимум одно требование со сценарием
- есть запрещённое поведение

Статус `partial` устанавливается когда:

- spec создана, но некоторые разделы пустуют
- spec требует проверки соответствия коду

Статус `draft` устанавливается когда:

- spec создана автором, но ещё не проверена

Статус `missing` устанавливается когда:

- компонент существует в коде, но spec отсутствует

#### Scenario: проверка статуса complete

- **WHEN** spec помечена как `complete`
- **THEN** она ДОЛЖНА пройти проверку по чек-листу `component-model`
- **AND** иначе статус ДОЛЖЕН быть понижен до `partial` или `draft`

### Requirement: категоризация компонентов

Каждый компонент ДОЛЖЕН быть отнесён к одной из категорий:

- `runtime` — runtime компоненты (`ApplicationContext`, `AgentFactory`, `MessageBus`)
- `configuration` — конфигурация (`ConfigService`, profiles)
- `channels` — каналы коммуникации (`PostgresChannel`, `RedisChannel`)
- `sessions` — управление сессиями (`PostgresSessionManager`)
- `data` — данные, кеш, векторы (`CacheProvider`, `VectorIndexService`)
- `observability` — логирование, мониторинг (`DatabaseLogging`, `EventLogging`)
- `infrastructure` — инфраструктура (`RuntimePatcher`, Hooks, SubprocessManagement)
- `interfaces` — интерфейсы (CLI, Gateway, Streamlit)
- `security` — безопасность (`SqlSafety`)
- `skills` — навыки (`AuditAnalyzer`, `LegalSummarizer`, `OfficeFiles`)
- `testing` — тестирование, бенчмарки (Benchmarks)
- `architecture` — архитектурные правила (`component-model`, `skill-tool-boundary`)
- `documentation` — мета-документация (`component-registry`)
- `validation` — валидация (`component-spec-validation`)

#### Scenario: добавление записи в реестр

- **WHEN** автор добавляет запись о компоненте в `COMPONENTS.md`
- **THEN** категория ДОЛЖНА быть выбрана из закрытого списка выше
- **AND** неизвестная категория ДОЛЖНА считаться ошибкой реестра

## Negative Requirements

Система НЕ ДОЛЖНА:

- смешивать компоненты и архитектурные правила в одном типе записи (использовать категории)
- устанавливать статус `complete` без реальной проверки
- удалять записи о deprecated компонентах (сохранять для истории)
- дублировать записи для одного компонента
- содержать пути к несуществующим файлам

## Dependencies

- `openspec/specs/architecture/component-model` — определение компонента и шаблона
- фактический код проекта — для inventory компонентов

## Implementation Reference

Основная реализация:

- `openspec/specs/COMPONENTS.md` — файл реестра компонентов

Формат записи:

```markdown
| Компонент | Категория | Реализация | Спецификация | Статус |
|-----------|-----------|------------|--------------|--------|
| ApplicationContext | runtime | `lib/core/application_context.py:ApplicationContext` | `runtime/context` | partial |
| CacheProvider | data | `lib/services/cache_provider.py:CacheProvider` | `data/cache-provider` | draft |
| AuditAnalyzer | skills | `workspace/skills/audit_analyzer/__init__.py:AuditAnalyzerSkill` | — | missing |
```

## Verification

Валидация реестра включает:

1. **Проверка существования**: каждая запись с non-missing статусом имеет соответствующий `spec.md`.
2. **Проверка полноты**: каждый production-компонент из кода зарегистрирован.
3. **Проверка путей**: все указанные пути к файлам существуют.
4. **Проверка уникальности**: нет дубликатов компонентов.
5. **Проверка статусов**: статус `complete` подтверждён проверкой spec.

Автоматическая проверка: `tools/validate_component_specs.py`.
