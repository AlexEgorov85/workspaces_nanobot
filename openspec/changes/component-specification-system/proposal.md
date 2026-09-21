# Component Specification System

## Проблема

Проект имеет значительный объём архитектурной документации, но отсутствует единая система компонентных спецификаций.

Существующие OpenSpec покрывают только несколько архитектурных аспектов:
- `architecture/skill-tool-boundary`
- `configuration/profiles`
- `data/cache`
- `data/vector-indexes`
- `runtime/context`

Описание компонентов распределено между `docs/`, `README`, `SKILL.md` и кодом.

Необходимо создать единый русскоязычный каталог компонентных спецификаций без дублирования нормативной и implementation документации.

## Цель

Создать инфраструктуру для системы спецификаций всех значимых компонентов `workspaces_nanobot`, где каждый компонент имеет отдельную машиночитаемую OpenSpec-спеку, описывающую его назначение, границы ответственности, публичный контракт, зависимости, lifecycle, данные, конфигурацию, ошибки и инварианты.

**Важно:** спека не должна описывать каждую функцию и каждую строку кода. Она должна фиксировать архитектурный контракт компонента.

## Разделение ответственности

### OpenSpec (`openspec/specs/`)
Отвечает: **Что компонент обязан делать и какие ограничения существуют?**

### docs/
Отвечает: **Как текущая реализация это делает?**

### Код
Отвечает: **Как это реализовано непосредственно сейчас?**

```
OpenSpec (контракт)
    ↓
Component (implementation)
    ↓
Code
    ↓
docs/ (operational/reference details)
```

## Нормативные правила

1. **Русский язык**: Все новые и изменяемые component specifications пишутся на русском языке. Технические идентификаторы сохраняют оригинальное написание.

2. **Единый шаблон**: Каждая spec содержит обязательные разделы (см. component-model).

3. **Single Source of Truth**: 
   - Нормативные архитектурные правила остаются в `docs/TARGET_ARCHITECTURE.md`
   - Контракты компонентов — в `openspec/specs/<domain>/<component>/spec.md`
   - Описание реализации — в `docs/`

4. **Отсутствие дублирования**: Система не создаёт второе нормативное описание одного компонента в другом файле OpenSpec.

5. **Реестр**: Система поддерживает единый component registry (`COMPONENTS.md`).

6. **Валидация**: CI проверяет полноту реестра, существование спецификаций, наличие обязательных секций и отсутствие дубликатов.

## Статусы компонентов

| Статус | Описание |
|--------|----------|
| `missing` | Компонент существует в коде, но спецификация отсутствует |
| `draft` | Спецификация создана, но требует проверки |
| `partial` | Спецификация заполнена частично, требует дополнения |
| `complete` | Спецификация прошла проверку полноты и подтверждена относительно реализации |
| `deprecated` | Компонент устарел и будет удалён |

## План работ

### Wave 1: Инфраструктура + Core Runtime
- architecture/component-model
- documentation/component-registry
- validation/component-spec-validation
- runtime/application-context
- runtime/agent-factory
- runtime/message-bus
- configuration/config-service
- data/cache-provider
- data/vector-indexes
- sessions/postgres-session-manager

### Wave 2: Channels, Observability, Infrastructure
- channels/*
- observability/*
- infrastructure/*
- security/sql-safety
- interfaces/*

### Wave 3: Skills
- skills/skill-contract
- skills/audit-analyzer
- skills/legal-summarizer
- skills/office-files

## Definition of Done для этого change

- [x] Определена модель архитектурного компонента
- [x] Определён единый шаблон component specification
- [x] Зафиксирован русский язык как нормативный язык новых спецификаций
- [x] Создан Component Registry (начальный inventory)
- [x] Определены правила именования и размещения спецификаций
- [x] Определено разделение: OpenSpec = contract, docs = implementation reference, code = implementation
- [x] Существующие 5 OpenSpec приведены к новой модели (переведены на русский, обновлены)
- [x] Созданы спецификации первой волны для core/runtime компонентов
- [x] Для каждой спецификации существует явная ссылка на реализацию
- [x] Нет второго registry
- [x] Нет дублирующих нормативных описаний
- [x] Добавлена автоматическая проверка структуры спецификаций
- [x] docs/README.md обновлён
- [x] AGENTS.md обновлён правилами работы со спецификациями
