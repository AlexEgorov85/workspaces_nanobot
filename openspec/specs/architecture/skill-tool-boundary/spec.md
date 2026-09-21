# Skill / Tool Boundary (Граница Skill / Tool)

## Назначение

Определение архитектурной границы между Skills (предметные возможности агента проекта) и Tools (общая инфраструктура). Граница сохраняет предметную логику в Skills и переиспользуемую plumbing-логику в Tools.

## Ответственность

Skill/Tool Boundary отвечает за:
- определение ответственности слоя Skills (предметная логика)
- определение ответственности слоя Tools (общая инфраструктура)
- обеспечение независимой разработки Skills и Tools

## Граница

### Владеет
- разделением: domain logic в Skills, reusable plumbing в Tools
- механизмом tool-call для вызова Tools из Skills
- реестром Skills и Tools через project.json

### Не владеет
- конкретными реализациями Skills
- конкретными реализациями Tools
- бизнес-логикой внутри Skills

### Может зависеть от
- agent runtime (tool-call механизм)
- project.json (реестр skills/tools)

### Не должен зависеть от
- импорта конкретных Tool implementation из Skills
- импорта конкретных Skill logic из Tools

## Публичный контракт

Boundary предоставляет:
- Skills живут в `workspace/skills/<name>/` с предметной логикой
- Tools живут в `workspace/tools/` с общей инфраструктурой
- Skills вызывают Tools через standard tool-call mechanism

## Требования

### Требование: Skill layer содержит предметную логику

Система ДОЛЖНА сохранять project-specific domain logic внутри слоя Skills (`workspace/skills/<name>/`).

#### Сценарий: Skill реализует свои скрипты

- **КОГДА** Skill нуждается в domain logic (например, SQL composition, output formatting, skill-specific orchestration)
- **ТОГДА** эта логика ДОЛЖНА жить в `workspace/skills/<name>/scripts/` и НЕ ДОЛЖНА быть переизобретена как generic Tool

### Требование: Tool layer содержит общую инфраструктуру

Система ДОЛЖНА сохранять generic, reusable infrastructure функциональность в Tool implementations под `workspace/tools/`.

#### Сценарий: Tool переиспользуется across Skills

- **КОГДА** Tool определён под `workspace/tools/`
- **ТОГДА** он ДОЛЖЕН быть usable из любого Skill через agent tool-call interface

### Требование: Независимость

Система ДОЛЖНА сохранять Skills и Tools независимо разрабатываемыми: ни один слой не требует compile-time dependency на другой.

#### Сценарий: Skill потребляет Tool через tool-call

- **КОГДА** Skill нуждается в функциональности, реализованной как Tool
- **ТОГДА** Skill ДОЛЖЕН вызвать Tool через standard tool-call mechanism, а не через direct module import

## Запрещённое поведение

Система НЕ ДОЛЖНА:

- импортировать конкретные Tool implementations изнутри Skill кода (`workspace/skills/<name>/scripts/`, `workspace/skills/<name>/SKILL.md`)
- импортировать конкретную Skill logic изнутри Tool кода (`workspace/tools/<tool>.py`)
- создавать fallback path, который bypass эту границу (нет "legacy Skill import" или "secondary Tool call" механизма)
- создавать второй реестр Skills или Tools вне объявленного в `project.json::skills.*`
- добавлять альтернативный execution path (например, Tool напрямую callable без tool-call interface)

## Зависимости

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `docs/skill-tool-architecture.md` — описание реализации (descriptive)
- `docs/SKILL_AUTHORING.md` — руководство по созданию Skills
- `project.json` — реестр skills/tools

## Конфигурация

Skills и Tools регистрируются в `project.json`:

```json
{
  "skills": {
    "audit_analyzer": { ... },
    "legal_summarizer": { ... }
  },
  "tools": [
    "file_reader",
    "sql_executor"
  ]
}
```

## Жизненный цикл

1. **Регистрация**: Skills и Tools определяются в project.json
2. **Инициализация**: agent runtime загружает registry при старте
3. **Вызов**: Skills вызывают Tools через tool-call mechanism
4. **Обновление**: новые Skills/Tools добавляются через change в project.json

## Состояние

Отсутствует. Boundary является архитектурным правилом, не runtime состоянием.

## Инварианты

- Domain logic всегда в Skills
- Reusable infrastructure всегда в Tools
- Нет cross-layer imports
- Единый реестр через project.json

## Поведение при ошибке

- Попытка прямого импорта → ошибка code review / CI
- Отсутствие tool-call механизма → runtime error

## Потребители

- Авторы Skills — понимание где размещать логику
- Авторы Tools — понимание границ ответственности
- Архитекторы — validation архитектуры

## Реализация

Основная реализация:
- `docs/skill-tool-architecture.md` — описание реализации
- `docs/SKILL_AUTHORING.md` — руководство по authoring

Связанные компоненты:
- `project.json` — реестр skills/tools
- `workspace/skills/` — директория Skills
- `workspace/tools/` — директория Tools

## Проверка

Валидация включает:
1. Проверка отсутствия импортов Tools из Skills (code review, grep)
2. Проверка отсутствия импортов Skills из Tools (code review, grep)
3. Проверка единого реестра в project.json (validation script)
