# Design: Component Specification System

## Архитектура системы

```
openspec/
├── changes/
│   └── component-specification-system/
│       ├── proposal.md      # Описание проблемы и цели
│       ├── design.md        # Это файл
│       ├── tasks.md         # План работ
│       └── specs/           # Спецификации этого change
│
├── specs/
│   ├── COMPONENTS.md        # Реестр компонентов
│   │
│   ├── architecture/        # Архитектурные правила
│   │   ├── component-model/ # Модель компонента (этот change)
│   │   └── skill-tool-boundary/
│   │
│   ├── runtime/             # Runtime компоненты
│   ├── configuration/       # Конфигурация
│   ├── channels/            # Каналы коммуникации
│   ├── sessions/            # Управление сессиями
│   ├── data/                # Данные, кеш, векторы
│   ├── observability/       # Логирование, мониторинг
│   ├── infrastructure/      # Инфраструктура
│   ├── interfaces/          # CLI, Gateway, Streamlit
│   ├── security/            # Безопасность
│   ├── skills/              # Навыки
│   └── testing/             # Тестирование, бенчмарки
│
└── config.yaml              # Конфигурация OpenSpec
```

## Что такое компонент?

Компонент — это самостоятельная архитектурная единица, которая имеет хотя бы одно из:

- собственный lifecycle
- публичный контракт
- собственную конфигурацию
- собственное состояние
- отдельную ответственность
- отдельные внешние зависимости
- возможность независимо изменяться
- архитектурный boundary

### Примеры компонентов

```
ApplicationContext    → компонент (lifecycle, assembly root)
ConfigService         → компонент (configuration resolution)
PGSessionManager      → компонент (session state)
MessageBus            → компонент (event routing)
CacheProvider         → компонент (caching contract)
VectorIndexService    → компонент (vector storage)
Skill                 → компонент (domain capability)
```

### Не являются отдельными компонентами

```
_private_helper()     → internal helper
format_name()         → utility function
small DTO             → data structure
internal constant     → configuration value
```

## Шаблон спецификации компонента

Каждая спецификация компонента содержит следующие обязательные разделы:

```markdown
# <Название компонента>

## Назначение (Purpose)

Одно-два предложения о том, зачем существует этот компонент.

## Ответственность (Responsibility)

Что именно этот компонент делает.

## Граница (Boundary)

### Владеет (Owns)
- ...

### Не владеет (Does not own)
- ...

### Может зависеть от (May depend on)
- ...

### Не должен зависеть от (Must not depend on)
- ...

## Публичный контракт (Public Contract)

Что компонент предоставляет вовне.

## Требования (Requirements)

### Требование: <название>

Описание требования.

#### Сценарий: <название сценария>

- **КОГДА** ...
- **ТОГДА** ...

## Запрещённое поведение (Negative Requirements)

Система НЕ ДОЛЖНА:

- ...
- ...

## Зависимости (Dependencies)

Явные зависимости компонента.

## Конфигурация (Configuration)

Конфигурационные параметры, если есть.

## Жизненный цикл (Lifecycle)

Как компонент создаётся, инициализируется, уничтожается (если применимо).

## Состояние (State)

Какое состояние хранит компонент (если применимо).

## Инварианты (Invariants)

Условия, которые всегда истинны для этого компонента.

## Поведение при ошибке (Error Behavior)

Как компонент ведёт себя при ошибках.

## Потребители (Consumers)

Кто использует этот компонент.

## Реализация (Implementation Reference)

Ссылки на код реализации.

## Проверка (Verification)

Как проверяется соответствие спецификации.
```

## Словарь терминов

| Английский | Нормативный русский |
|------------|---------------------|
| Component | Компонент |
| Responsibility | Ответственность |
| Boundary | Граница |
| Contract | Контракт |
| Requirement | Требование |
| Scenario | Сценарий |
| Invariant | Инвариант |
| Dependency | Зависимость |
| Lifecycle | Жизненный цикл |
| State | Состояние |
| Source of truth | Источник истины |
| Failure behavior | Поведение при ошибке |
| Negative requirement | Запрещённое поведение |
| Consumer | Потребитель |
| Provider | Провайдер |
| Configuration | Конфигурация |
| Purpose | Назначение |
| Verification | Проверка |
| Implementation | Реализация |

**Важно:** названия Python-классов, методов, файлов, конфигурационных ключей и API **никогда не переводятся**.

## Правила именования

### Каталог компонента

```
openspec/specs/<domain>/<component-name>/spec.md
```

где:
- `<domain>` — категория компонента (runtime, data, skills, etc.)
- `<component-name>` — kebab-case название компонента

### Примеры

```
runtime/application-context/spec.md
data/cache-provider/spec.md
skills/audit-analyzer/spec.md
```

### Название в spec.md

Заголовок первого уровня должен содержать название компонента:

```markdown
# ApplicationContext
```

или

```markdown
# CacheProvider
```

## Источники истины

### docs/TARGET_ARCHITECTURE.md
Отвечает: **Какой архитектуры мы придерживаемся?**

Содержит глобальные архитектурные правила и принципы.

### openspec/specs/<component>/spec.md
Отвечает: **Какой контракт имеет конкретный компонент?**

Содержит нормативное описание поведения компонента.

### docs/*.md
Отвечает: **Как компонент реализован сейчас?**

Содержит описание текущей реализации, operational details.

### Код
Отвечает: **Как это реализовано непосредственно?**

Содержит фактическую реализацию.

## Статусы спецификаций

| Статус | Когда устанавливается |
|--------|----------------------|
| `missing` | Компонент обнаружен в коде, но spec отсутствует |
| `draft` | Spec создана, требует проверки автором |
| `partial` | Spec заполнена частично, некоторые разделы пустуют |
| `complete` | Spec прошла проверку: все обязательные разделы заполнены, соответствует реализации |
| `deprecated` | Компонент устарел, spec сохраняется для истории |

## Валидация

CI должен проверять:

1. **Существование**: каждая запись в COMPONENTS.md имеет соответствующий spec.md
2. **Полнота**: каждый production-компонент зарегистрирован
3. **Структура**: каждая spec содержит обязательные разделы
4. **Язык**: заголовки разделов на русском языке
5. **Отсутствие дубликатов**: нет двух specs для одного компонента

## Миграция существующих спецификаций

Существующие 5 OpenSpec должны быть:

1. Переведены на русский язык
2. Приведены к новому шаблону
3. Дополнены разделами Boundary, Dependencies, Implementation Reference
4. Проверены на соответствие текущему коду

Список существующих spec:
- `architecture/skill-tool-boundary`
- `configuration/profiles`
- `data/cache`
- `data/vector-indexes`
- `runtime/context`
