# Design: Component Specification System

## Архитектура системы

```text
openspec/
├── config.yaml
│
├── specs/
│   ├── COMPONENTS.md                    ← реестр всех компонентов
│   │
│   ├── architecture/
│   │   ├── component-model/             ← модель компонента (этот change)
│   │   │   └── spec.md
│   │   ├── application-boundaries/      ← следующий change
│   │   ├── skill-tool-boundary/         ← существующая, требует обновления
│   │   └── dependency-rules/
│   │
│   ├── runtime/
│   │   ├── application-context/         ← существующая, требует обновления
│   │   ├── agent-factory/
│   │   ├── message-bus/
│   │   ├── lifecycle/
│   │   └── ...
│   │
│   ├── configuration/
│   │   ├── configuration-resolution/
│   │   ├── profiles/                    ← существующая, требует обновления
│   │   └── config-service/
│   │
│   ├── data/
│   │   ├── cache-provider/              ← переименовать из cache/
│   │   ├── vector-indexes/              ← существующая, требует обновления
│   │   └── ...
│   │
│   └── ...
│
└── changes/
    └── component-specification-system/  ← этот change
        ├── proposal.md
        ├── design.md
        ├── tasks.md
        └── specs/
            ├── architecture/component-model/
            ├── documentation/component-registry/
            └── validation/component-spec-validation/
```

## Модель компонента

Каждая компонентная спецификация следует единому шаблону:

```markdown
# <Название компонента>

## Назначение

## Ответственность

## Граница

## Публичный контракт

## Входы

## Выходы

## Состояние

## Зависимости

## Конфигурация

## Жизненный цикл

## Владение данными

## Поведение при ошибке

## Инварианты

## Запрещённое поведение

## Потребители

## Реализация

## Проверка
```

Полное описание шаблона — в `openspec/specs/architecture/component-model/spec.md`.

## Реестр компонентов

Реестр (`COMPONENTS.md`) содержит таблицу:

| Компонент | Категория | Реализация | Спецификация | Статус |
|-----------|-----------|------------|--------------|--------|

Статусы:
- `missing` — компонент существует в коде, но спецификация отсутствует
- `draft` — спецификация создана, но неполная
- `partial` — спецификация содержит основные разделы, но требует доработки
- `complete` — спецификация полная и соответствует шаблону
- `deprecated` — компонент устарел и будет удалён

## Правила

### Что является компонентом

Компонент — это самостоятельная архитектурная единица, которая имеет хотя бы одно из:

- собственный жизненный цикл;
- публичный контракт;
- собственную конфигурацию;
- собственное состояние;
- отдельную ответственность;
- отдельные внешние зависимости;
- возможность независимо изменяться;
- архитектурную границу.

### Язык спецификаций

**Все новые и изменяемые компонентные спецификации проекта ДОЛЖНЫ быть написаны на русском языке.**

Технические идентификаторы сохраняют оригинальное написание (ApplicationContext, CacheProvider, MessageBus и т.д.).

### Разделение ответственности

| Документ | Отвечает |
|----------|----------|
| `openspec/specs/<component>/spec.md` | Что компонент обязан делать, какие ограничения существуют |
| `docs/ARCHITECTURE.md` и другие `docs/*` | Как текущая реализация это делает |
| Код | Как это реализовано непосредственно сейчас |

### Отсутствие дублирования

Один компонент — одна спецификация. Запрещено создавать несколько спецификаций для одного компонента или дублировать нормативные описания в разных файлах.

## Валидация

Будущая CI-проверка `validate_component_specs` должна проверять:

1. **Существование спецификации**: каждая запись в `COMPONENTS.md` имеет соответствующий файл `spec.md`
2. **Обязательные секции**: каждая спецификация содержит минимальный набор разделов
3. **Язык**: запрещены английские заголовки нормативных секций
4. **Отсутствие дублирования**: один компонент — одна спецификация

## План наполнения

### Волна 1: Инфраструктурное ядро

- architecture/component-model (этот change)
- architecture/application-boundaries (следующий change)
- runtime/application-context (обновить существующую)
- runtime/agent-factory (новая)
- runtime/message-bus (новая)
- configuration/config-service (новая)
- sessions/postgres-session-manager (новая)
- channels/postgres-channel (новая)
- channels/redis-channel (новая)
- data/cache-provider (обновить существующую)
- data/vector-indexes (обновить существующую)

### Волна 2: Observability и Infrastructure

- observability/database-logging (новая)
- observability/event-logging (новая)
- infrastructure/runtime-patcher (новая)
- infrastructure/hooks (новая)
- infrastructure/subprocess-management (новая)
- security/sql-safety (новая)

### Волна 3: Interfaces и Testing

- interfaces/cli (новая)
- interfaces/gateway (новая)
- testing/benchmarks (новая)

### Волна 4: Skills

- skills/skill-contract (новая)
- skills/audit-analyzer (новая)
- skills/legal-summarizer (новая)

## Миграция существующих спецификаций

| Существующая спецификация | Действие |
|---------------------------|----------|
| `architecture/skill-tool-boundary` | Перевод на русский, добавление разделов Boundary, Dependencies, Implementation Reference |
| `configuration/profiles` | Перевод на русский, добавление разделов |
| `runtime/context` | Перевод на русский, добавление разделов |
| `data/cache` | Переименовать в `data/cache-provider`, перевод на русский, добавление разделов |
| `data/vector-indexes` | Перевод на русский, добавление разделов |

## Связь с TARGET_ARCHITECTURE.md

`docs/TARGET_ARCHITECTURE.md` остаётся нормативным документом глобальной архитектуры. Он отвечает на вопрос «Какой архитектуры мы придерживаемся?».

Component specifications отвечают на вопрос «Какой контракт имеет конкретный компонент?».

Это разделение предотвращает дублирование и рассинхронизацию.
