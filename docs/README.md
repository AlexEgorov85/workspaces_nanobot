# Документация проекта

Навигационный индекс для разработчиков. Каждый документ — **самодостаточный
reference** по своей подсистеме; README в корне — это навигационный хаб.

## Каталог

### Архитектура и интеграции

| Документ | Назначение |
|---|---|
| [architecture/nanobot-inventory.md](architecture/nanobot-inventory.md) | Инвентарь всех зависимостей от `nanobot-ai` (GREEN/YELLOW/ORANGE/RED) |
| [architecture/runtime-patcher-inventory.md](architecture/runtime-patcher-inventory.md) | Каталог monkey-patch'ей с target/risk/тестами |
| [skill-tool-architecture.md](skill-tool-architecture.md) | Контракт Skill ↔ Tool: что разрешено, что запрещено |
| [skill-tool-inventory.md](skill-tool-inventory.md) | Текущее состояние всех skill/tool и история удалённых |
| [refactor_baseline.md](refactor_baseline.md) | pytest baseline ветки `refactor/skills-tools-cleanup` |

### Подсистемы

| Документ | Назначение |
|---|---|
| [table-registry.md](table-registry.md) | Реестр таблиц PG → DuckDB, sync-контроль |
| [runtime_patches.md](runtime_patches.md) | Облегчённая сводка патчей (устарел, см. architecture/runtime-patcher-inventory.md) |

### Операционные руководства

| Документ | Назначение |
|---|---|
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Диагностический runbook — типовые ошибки и решения |
| [MIGRATION.md](MIGRATION.md) | Сводка изменений между релизами + breaking changes |

### Внешние ссылки

- [README.md](../README.md) — навигационный хаб проекта.
- [DEVELOPMENT.md](../DEVELOPMENT.md) — технический справочник (архитектура
  сервисов, контракты, troubleshooting в глубину).
- [CHANGELOG.md](../CHANGELOG.md) — полная история изменений по Keep a Changelog.
- [TARGET_ARCHITECTURE.md](../TARGET_ARCHITECTURE.md) — целевая архитектура
  проекта.
- [AGENTS.md](../AGENTS.md) — инструкции для opencode-ассистента.
- [sql/README.md](../sql/README.md) — каталог DDL и миграций схемы.

## Конвенция именования

- `*.md` в корне `docs/` — навигационные / операционные документы.
- `docs/architecture/` — каталоги инвентарей (генерируются из кода).
- `docs/*-architecture.md` — архитектурные контракты (skill/tool).
- `docs/*-inventory.md` — инвентаризация компонентов.
- `docs/*-baseline.md` — wip-заметки рефакторингов.

Все ссылки между документами — относительные (`./SKILL.md`, `../README.md`).
