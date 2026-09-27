# Skill / Tool Boundary (Граница Skill / Tool)

## Purpose

Определение архитектурной границы между Skills (предметные возможности агента проекта) и Tools (общая инфраструктура). Граница сохраняет предметную логику в Skills и переиспользуемую plumbing-логику в Tools.

## Responsibility

Skill/Tool Boundary отвечает за:
- определение ответственности слоя Skills (предметная логика)
- определение ответственности слоя Tools (общая инфраструктура)
- обеспечение независимой разработки Skills и Tools

## Boundary

### Owns
- разделением: domain logic в Skills, reusable plumbing в Tools
- механизмом tool-call для вызова Tools из Skills
- реестром Skills и Tools через project.json

### Does Not Own
- конкретными реализациями Skills
- конкретными реализациями Tools
- бизнес-логикой внутри Skills

### May Depend On
- agent runtime (tool-call механизм)
- project.json (реестр skills/tools)

### Must Not Depend On
- импорта конкретных Tool implementation из Skills
- импорта конкретных Skill logic из Tools

## Public Contract

Boundary предоставляет:
- Skills живут в `workspace/skills/<name>/` с предметной логикой
- Tools живут в `workspace/tools/` с общей инфраструктурой
- Skills вызывают Tools через standard tool-call mechanism

## Requirements

### Requirement: Skill layer содержит предметную логику

Система ДОЛЖНА сохранять project-specific domain logic внутри слоя Skills (`workspace/skills/<name>/`).

#### Scenario: Skill реализует свои скрипты

- **КОГДА** Skill нуждается в domain logic (например, SQL composition, output formatting, skill-specific orchestration)
- **ТОГДА** эта логика ДОЛЖНА жить в `workspace/skills/<name>/scripts/` и НЕ ДОЛЖНА быть переизобретена как generic Tool

### Requirement: Tool layer содержит общую инфраструктуру

Система ДОЛЖНА сохранять generic, reusable infrastructure функциональность в Tool implementations под `workspace/tools/`.

#### Scenario: Tool переиспользуется across Skills

- **КОГДА** Tool определён под `workspace/tools/`
- **ТОГДА** он ДОЛЖЕН быть usable из любого Skill через agent tool-call interface

### Requirement: Независимость

Система ДОЛЖНА сохранять Skills и Tools независимо разрабатываемыми: ни один слой не требует compile-time dependency на другой.

#### Scenario: Skill потребляет Tool через tool-call

- **КОГДА** Skill нуждается в функциональности, реализованной как Tool
- **ТОГДА** Skill ДОЛЖЕН вызвать Tool через standard tool-call mechanism, а не через direct module import

## Forbidden Behavior

Система НЕ ДОЛЖНА:

- импортировать конкретные Tool implementations изнутри Skill кода (`workspace/skills/<name>/scripts/`, `workspace/skills/<name>/SKILL.md`)
- импортировать конкретную Skill logic изнутри Tool кода (`workspace/tools/<tool>.py`)
- создавать fallback path, который bypass эту границу (нет "legacy Skill import" или "secondary Tool call" механизма)
- создавать второй реестр Skills или Tools вне объявленного в `project.json::skills.*`
- добавлять альтернативный execution path (например, Tool напрямую callable без tool-call interface)

## Dependencies

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `docs/skill-tool-architecture.md` — описание реализации (descriptive)
- `docs/SKILL_AUTHORING.md` — руководство по созданию Skills
- `project.json` — реестр skills/tools

## Configuration

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

## Lifecycle

1. **Регистрация**: Skills и Tools определяются в project.json
2. **Инициализация**: agent runtime загружает registry при старте
3. **Вызов**: Skills вызывают Tools через tool-call mechanism
4. **Обновление**: новые Skills/Tools добавляются через change в project.json

## State

Отсутствует. Boundary является архитектурным правилом, не runtime состоянием.

## Invariants

- Domain logic всегда в Skills
- Reusable infrastructure всегда в Tools
- Нет cross-layer imports
- Единый реестр через project.json

## Error Behavior

- Попытка прямого импорта → ошибка code review / CI
- Отсутствие tool-call механизма → runtime error

## Consumers

- Авторы Skills — понимание где размещать логику
- Авторы Tools — понимание границ ответственности
- Архитекторы — validation архитектуры

## Implementation

Основная реализация:
- `docs/skill-tool-architecture.md` — описание реализации
- `docs/SKILL_AUTHORING.md` — руководство по authoring

Связанные компоненты:
- `project.json` — реестр skills/tools
- `workspace/skills/` — директория Skills
- `workspace/tools/` — директория Tools

## Verification

Валидация включает:
1. Проверка отсутствия импортов Tools из Skills (code review, grep)
2. Проверка отсутствия импортов Skills из Tools (code review, grep)
3. Проверка единого реестра в project.json (validation script)
