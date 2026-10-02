# Skill / Tool Boundary (Граница Skill / Tool)

## Purpose

Определение архитектурной границы между Skills (предметные возможности агента проекта) и Tools (самостоятельные agent-facing capability).

Граница сохраняет предметную логику и доменную оркестрацию в Skills, а в Tools — только те capability, которые агент выбирает и вызывает **самостоятельно**.

Skills и Tools — **параллельные потребители** общей runtime-инфраструктуры, а не два уровня одной цепочки. Ни один слой не является подсистемой другого.

## Scope

`agent` — правило о слоях навыков и инструментов репозитория агента
Реализация: `workspace/skills/`, `workspace/tools/`, `lib/services/project_tool_loader.py`

## Responsibility

Skill/Tool Boundary отвечает за:
- определение ответственности слоя Skills (предметная логика, доменная оркестрация, исполняемый код)
- определение ответственности слоя Tools (самостоятельные agent-facing capability)
- критерий, по которому capability признаётся agent-facing (и, следовательно, Tool'ом), а не остаётся внутренней операцией Skill
- отделение shared runtime infrastructure (`lib/services`, `lib/core`, `lib/utils`) от обоих слоёв
- обеспечение независимой разработки Skills и Tools

## Boundary

### Owns
- разделением: domain logic и доменная оркестрация в Skills, самостоятельные agent-facing capability в Tools
- критерием agent-facing capability
- правилом «Skill и Tool обращаются к инфраструктуре напрямую, минуя друг друга»
- реестром Skills и Tools

### Does Not Own
- конкретными реализациями Skills
- конкретными реализациями Tools
- бизнес-логикой внутри Skills
- содержимым shared runtime infrastructure (`lib/services`, `lib/core`, `lib/utils`)

### May Depend On
- agent runtime (выбор и вызов Tool'а агентом)
- config.json (конфигурация skills и tools)
- shared runtime infrastructure (`lib/services`, `lib/core`, `lib/utils`)

### Must Not Depend On
- импорта конкретных Tool implementation из Skills
- импорта конкретных Skill logic из Tools
- вызова Tool из Skill (в т.ч. через tool-call mechanism) как способа получить capability
- доступа к shared infrastructure только через Tool

## Public Contract

Boundary предоставляет:
- Skills живут в `workspace/skills/<name>/` с предметной логикой, доменной оркестрацией и исполняемым кодом
- Tools живут в `workspace/tools/` и представляют самостоятельные agent-facing capability
- Skill и Tool — **параллельные** потребители shared runtime infrastructure:

```text
              AgentRuntime
               /         \
              v           v
           Skill         Tool
              \           /
               \         /
                v       v
        shared runtime infrastructure
              (lib/services, lib/core, lib/utils)
```

- Skill **не вызывает** Tool; Tool выбирает и вызывает агент
- shared infrastructure — **не** Tool-слой: наличие callable-функции в `lib/` не делает её Tool'ом

## Requirements

### Requirement: Skill layer содержит предметную логику

Система ДОЛЖНА сохранять project-specific domain logic внутри слоя Skills (`workspace/skills/<name>/`).

#### Scenario: Skill реализует свои скрипты

- **КОГДА** Skill нуждается в domain logic (например, SQL composition, output formatting, skill-specific orchestration)
- **ТОГДА** эта логика ДОЛЖНА жить в `workspace/skills/<name>/scripts/` и НЕ ДОЛЖНА быть переизобретена как generic Tool

#### Scenario: Skill обращается к инфраструктуре напрямую

- **КОГДА** Skill нуждается в возможности, уже предоставляемой shared runtime infrastructure (SQL-кэш, векторный поиск, LLM-клиент, валидация SQL, реестр ресурсов)
- **ТОГДА** Skill ДОЛЖЕН использовать существующий runtime/application interface напрямую
- **И** Skill MUST NOT искать или вызывать Tool для этого

### Requirement: Tool layer содержит самостоятельные agent-facing capability

Система ДОЛЖНА размещать в `workspace/tools/` только те capability, которые агент выбирает и вызывает самостоятельно, независимо от выбранного домена.

#### Scenario: Tool переиспользуется across Skills

- **КОГДА** Tool определён под `workspace/tools/`
- **ТОГДА** он ДОЛЖЕН быть usable в любом домене без domain-routing и без знания конкретных Skills

### Requirement: Agent-facing capability — критерий Tool'а

Tool'ом MAY становиться только capability, удовлетворяющая всем условиям:

- агент выбирает и вызывает её **самостоятельно**, как отдельный шаг своего плана;
- её полезность не зависит от выбранного домена;
- она не является внутренним шагом уже существующего доменного workflow.

#### Scenario: Внутренняя операция доменного workflow

- **КОГДА** capability является детерминированным внутренним шагом доменного Skill (например, выполнение заранее определённого SQL-скрипта домена, NL→SQL для конкретной схемы, семантический поиск по доменным индексам)
- **ТОГДА** она ДОЛЖНА оставаться внутренней операцией Skill
- **И** Agent-facing Tool для неё MUST NOT создаваться, если агент не должен выбирать её самостоятельно

#### Scenario: Generic, но не agent-facing

- **КОГДА** capability объективно generic, но обслуживает внутренний шаг доменного workflow и не нужна агенту как отдельное действие
- **ТОГДА** она ДОЛЖНА оставаться в shared infrastructure или в scripts Skill
- **И** generic-природа capability MUST NOT служить достаточным основанием для создания Tool'а

### Requirement: Shared infrastructure не входит в Tool-слой

Реализации, используемые и Skills, и Tools (SQL-кэш, векторный поиск, валидация SQL, LLM-клиент, реестр ресурсов), ДОЛЖНЫ жить в shared runtime infrastructure (`lib/services`, `lib/core`, `lib/utils`) и MUST NOT считаться частью Tool-слоя.

#### Scenario: Callable capability в lib/

- **КОГДА** в `lib/services` или `lib/core` появляется callable-функция
- **ТОГДА** она ДОЛЖНА оставаться инфраструктурой
- **И** её наличие MUST NOT служить основанием заводить одноимённый Agent-facing Tool

### Requirement: Независимость

Система ДОЛЖНА сохранять Skills и Tools независимо разрабатываемыми: ни один слой не требует compile-time или runtime dependency на другой.

#### Scenario: Skill нуждается в capability, доступной как Tool

- **КОГДА** Skill нуждается в функциональности, реализованной как Tool
- **ТОГДА** Skill ДОЛЖЕН использовать существующий runtime/application interface напрямую
- **И** Skill MUST NOT вызывать Tool и MUST NOT импортировать `workspace.tools.*`

## Forbidden Behavior

Система НЕ ДОЛЖНА:

- импортировать конкретные Tool implementations изнутри Skill кода (`workspace/skills/<name>/scripts/`, `workspace/skills/<name>/SKILL.md`)
- импортировать конкретную Skill logic изнутри Tool кода (`workspace/tools/<tool>.py`)
- объявлять Skill → Tool зависимостью (в т.ч. «Skill вызывает Tool через standard tool-call mechanism»)
- превращать любую callable-функцию в Tool только на основании её generic-природы или того, что она «уже реализована»
- считать shared runtime infrastructure (`lib/services`, `lib/core`, `lib/utils`) частью Tool-слоя
- создавать fallback path, который bypass эту границу (нет «legacy Skill import» или «secondary Tool call» механизма)
- создавать второй реестр Skills или Tools вне объявленного в `config.json::skills.*`
- добавлять альтернативный execution path (например, Tool напрямую callable без tool-call interface)

## Dependencies

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `docs/skill-tool-architecture.md` — описание реализации (descriptive)
- `docs/SKILL_AUTHORING.md` — руководство по созданию Skills
- `openspec/specs/data/cache-provider/spec.md` — контракт shared SQL-кэша/FAISS
- `config.json` — конфигурация skills и tools

## Configuration

Skills регистрируются декларативно в `config.json::skills.<name>` (валидация — `SkillSettings`, `extra="forbid"`):

```json
{
  "skills": {
    "audit_analyzer": { ... },
    "legal_summarizer": { ... }
  }
}
```

Tools **не** перечисляются в реестре `config.json`: они обнаруживаются
автоматически по `workspace/tools/*.py` (`lib/services/project_tool_loader.py::register_project_tools`).
Их секции конфигурации читаются из `ctx._settings_ref.tools.<config_key>`
(исторические секции — `gateway.<config_key>`), например
`gateway.compact.*` для `compact_context`.

Правило: перечисление capability в `config.json` конфигурирует Skill,
а не регистрирует Tool.

## Lifecycle

1. **Регистрация**: Skills объявляются в `config.json::skills.*`; Tools обнаруживаются по `workspace/tools/*.py`
2. **Инициализация**: agent runtime загружает оба набора при старте
3. **Вызов**: агент выбирает capability; Tool вызывается агентом, Skill работает через свои scripts и runtime interfaces — Skill не вызывает Tool
4. **Обновление**: новые Skills/Tools добавляются через change в `config.json` / `workspace/tools/`

## State

Отсутствует. Boundary является архитектурным правилом, не runtime состоянием.

## Invariants

- Domain logic и доменная оркестрация всегда в Skills
- Tool-слой содержит только самостоятельные agent-facing capability
- Shared infrastructure не является Tool-слоем
- Нет cross-layer imports и cross-layer вызовов
- Единый реестр Skills; Tools — auto-discovery

## Error Behavior

- Попытка прямого импорта или вызова Tool из Skill → архитектурная регрессия (code review / CI)
- Создание Tool'а под внутреннюю операцию доменного workflow → архитектурная регрессия (code review / CI)
- Отсутствие runtime interface, нужного Skill'у → runtime error

## Consumers

- Авторы Skills — понимание где размещать логику
- Авторы Tools — понимание границ ответственности и критерия agent-facing capability
- Архитекторы — validation архитектуры

## Implementation

Основная реализация:
- `docs/skill-tool-architecture.md` — описание реализации
- `docs/SKILL_AUTHORING.md` — руководство по authoring

Связанные компоненты:
- `config.json` — конфигурация skills
- `workspace/skills/` — директория Skills
- `workspace/tools/` — директория Tools (auto-discovery)
- `lib/services/project_tool_loader.py` — регистрация project tools

## Verification

Валидация включает:
1. Проверка отсутствия импортов Tools из Skills (code review, grep)
2. Проверка отсутствия импортов Skills из Tools (code review, grep)
3. Проверка отсутствия вызова Tool из Skill (в т.ч. через tool-call) — `tests/test_skill_tool_independence.py`
4. Проверка domain-free описаний и кода Tools — `tests/test_architecture_tool_domain_free.py`
5. Code review по критерию agent-facing capability при добавлении нового Tool'а
