# documentation/component-registry Specification

## Purpose
Этот документ определяет правила ведения единого реестра архитектурных компонентов проекта workspaces_nanobot. Реестр служит индексом для навигации по компонентным спецификациям и отслеживания полноты покрытия системы спецификациями.

## Scope

`agent` — реестр компонентов и его валидатор живут в репозитории агента
Реализация: `tools/validate_component_specs.py`, `openspec/specs/COMPONENTS.md`

## Requirements

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
- `interfaces` — интерфейсы (CLI, Gateway)
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
