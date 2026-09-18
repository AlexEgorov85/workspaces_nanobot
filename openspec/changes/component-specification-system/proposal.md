# Component Specification System

## Проблема

Проект имеет значительный объём архитектурной документации, но отсутствует единая система компонентных спецификаций.

Существующие OpenSpec покрывают только несколько архитектурных аспектов:
- `architecture/skill-tool-boundary`
- `configuration/profiles`
- `runtime/context`
- `data/cache`
- `data/vector-indexes`

Описание компонентов распределено между:
- `docs/ARCHITECTURE.md` (описание текущей реализации)
- `docs/TARGET_ARCHITECTURE.md` (нормативные принципы)
- `docs/SKILL_AUTHORING.md` (гайд по созданию skills)
- отдельными OpenSpec-спецификациями
- кодом

Необходимо создать единый русскоязычный каталог компонентных спецификаций без дублирования нормативной и implementation документации.

## Цель

Создать единую систему спецификаций всех значимых компонентов `workspaces_nanobot`, где каждый компонент имеет отдельную машиночитаемую OpenSpec-спеку, описывающую его:

- назначение;
- границы ответственности;
- публичный контракт;
- зависимости;
- жизненный цикл;
- данные;
- конфигурацию;
- ошибки;
- инварианты.

**Важно:** спека не должна описывать каждую функцию и каждую строку кода. Она должна фиксировать архитектурный контракт компонента.

## Разделение ответственности

### OpenSpec (`openspec/specs/`)

Отвечает: **Что компонент обязан делать и какие ограничения существуют?**

- Нормативный контракт компонента
- Архитектурные инварианты
- Запрещённое поведение
- Границы ответственности

### `docs/`

Отвечает: **Как текущая реализация это делает?**

- Детали реализации
- Операционные руководства
- Исторические/WIP заметки
- Implementation reference

### Код

Отвечает: **Как это реализовано непосредственно сейчас?**

- Фактическая реализация
- Внутренние детали
- Алгоритмы

## Что меняется

### Новые артефакты

1. **Модель компонента** (`openspec/specs/architecture/component-model/spec.md`)
   - Определяет обязательный шаблон для всех компонентных спецификаций
   - Устанавливает русский язык как нормативный
   - Определяет словарь терминов
   - Устанавливает правила именования и размещения

2. **Реестр компонентов** (`openspec/specs/COMPONENTS.md`)
   - Индекс всех архитектурных компонентов
   - Отслеживание статуса спецификаций
   - План работ по наполнению

3. **Спецификация системы** (этот документ)
   - Описание проблемы и цели
   - Требования к системе

### Изменение существующих артефактов

Существующие 5 OpenSpec-спецификаций будут приведены к новому шаблону:
- Переведены на русский язык
- Дополнены разделами Boundary, Dependencies, Implementation Reference
- Приведены к единой структуре

### Не меняется

- `docs/TARGET_ARCHITECTURE.md` — остаётся нормативным документом глобальной архитектуры
- `docs/ARCHITECTURE.md` — остаётся описанием текущей реализации
- Исходный код — изменения только в документации

## Требования

### Требование: каждый архитектурный компонент имеет спецификацию

Система ДОЛЖНА поддерживать реестр архитектурных компонентов.

Каждый production-компонент, включённый в реестр, ДОЛЖЕН иметь отдельную OpenSpec specification.

### Требование: спецификация на русском

Все новые и изменяемые компонентные спецификации ДОЛЖНЫ быть написаны на русском языке.

Технические идентификаторы, имена классов, методов, файлов, конфигурационных ключей и API ДОЛЖНЫ сохранять оригинальное написание.

### Требование: единый шаблон

Каждая компонентная спецификация ДОЛЖНА содержать:

- Назначение
- Ответственность
- Границу
- Контракт
- Зависимости
- Конфигурацию
- Жизненный цикл (если применимо)
- Состояние (если применимо)
- Инварианты
- Запрещённое поведение
- Ссылку на реализацию
- Проверку

### Требование: разделение ответственности

Component specification ДОЛЖНА описывать contractual behavior.

Implementation details ДОЛЖНЫ оставаться в `docs/` и source code, если они не необходимы для определения контракта.

### Требование: единственный источник истины

Нормативные архитектурные правила ДОЛЖНЫ оставаться в `docs/TARGET_ARCHITECTURE.md`.

Контракты конкретных компонентов ДОЛЖНЫ поддерживаться в `openspec/specs/<domain>/<component>/spec.md`.

Описания реализации ДОЛЖНЫ оставаться в `docs/`.

### Требование: отсутствие дублирования

Система НЕ ДОЛЖНА создавать второе нормативное описание одного и того же компонента в другом файле OpenSpec.

### Требование: реестр

Система ДОЛЖНА поддерживать единый реестр компонентов.

## Подход к наполнению

Не нужно создавать все спецификации сразу. Это создаст огромное количество поверхностных документов.

Правильный подход: **сначала создать механизм Component Specification System, затем последовательно наполнить его спецификациями по фактическому коду.**

Этот change — инфраструктура документации + первая волна спецификаций.

### Волна 1: Инфраструктурное ядро

```text
architecture/component-model          ← этот change
architecture/application-boundaries   ← следующий change

runtime/application-context
runtime/agent-factory
runtime/message-bus
runtime/lifecycle

configuration/configuration-resolution
configuration/config-service

sessions/session-storage
sessions/postgres-session-manager

channels/channel-manager
channels/postgres-channel
channels/redis-channel

data/cache-provider
data/duckdb-sync
data/vector-indexes
```

### Волна 2: Observability и Infrastructure

```text
observability/event-logging
observability/database-logging

runtime/runtime-patcher
infrastructure/hooks
infrastructure/subprocess-management
infrastructure/transcription

security/sql-safety
interfaces/cli
interfaces/gateway
interfaces/streamlit
testing/benchmarks
```

### Волна 3: Skills

Только после того, как инфраструктурные boundaries определены:

```text
skills/skill-contract
skills/audit-analyzer
skills/legal-summarizer
skills/office-files
```

Это особенно важно, потому что архитектура Skills/Tools ещё активно меняется. Специфицировать `audit_analyzer` до стабилизации его границы будет означать постоянно переписывать спецификацию.

## Capabilities

### New Capabilities

- `architecture/component-model` — модель архитектурного компонента и шаблон спецификаций
- `documentation/component-registry` — реестр всех компонентов проекта

### Modified Capabilities

- `architecture/skill-tool-boundary` — будет приведена к новому шаблону (перевод на русский, добавление разделов)
- `configuration/profiles` — будет приведена к новому шаблону
- `runtime/context` — будет приведена к новому шаблону
- `data/cache` — будет переименована в `data/cache-provider` и приведена к шаблону
- `data/vector-indexes` — будет приведена к новому шаблону

## Impact

- Affected files:
  - `openspec/specs/architecture/component-model/spec.md` (новая)
  - `openspec/specs/COMPONENTS.md` (новый)
  - `openspec/specs/architecture/skill-tool-boundary/spec.md` (обновление)
  - `openspec/specs/configuration/profiles/spec.md` (обновление)
  - `openspec/specs/runtime/context/spec.md` (обновление)
  - `openspec/specs/data/cache/` → `openspec/specs/data/cache-provider/` (переименование + обновление)
  - `openspec/specs/data/vector-indexes/spec.md` (обновление)
  - `docs/README.md` (добавление ссылки на COMPONENTS.md)
  - `AGENTS.md` (обновление правил работы со спецификациями)

- No source code affected
- No runtime behavior affected
- No configuration affected

## Definition of Done

- [x] Определена модель архитектурного компонента
- [x] Определён единый шаблон component specification
- [x] Зафиксирован русский язык как нормативный язык новых спецификаций
- [x] Создан Component Registry
- [x] Определены правила именования и размещения спецификаций
- [x] Определено разделение: OpenSpec = contract, docs = implementation reference, code = implementation
- [ ] Существующие 5 OpenSpec приведены к новой модели
- [ ] Созданы спецификации первой волны для core/runtime компонентов
- [ ] Для каждой спецификации существует явная ссылка на реализацию
- [ ] Нет второго registry
- [ ] Нет дублирующих нормативных описаний
- [ ] Добавлена автоматическая проверка структуры спецификаций (future CI)
- [ ] `docs/README.md` обновлён
- [ ] `AGENTS.md` обновлён правилами работы со спецификациями
- [ ] OpenSpec change сам прошёл validate/apply/archive workflow
