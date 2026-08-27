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
| [SKILL_AUTHORING.md](SKILL_AUTHORING.md) | **Пошаговый гайд**: как создать свой skill (структура, SKILL.md, регистрация в project.json, runtime API, best practices, anti-patterns, DoD) |
| [refactor_baseline.md](refactor_baseline.md) | pytest baseline ветки `refactor/skills-tools-cleanup` |
| [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) | **Нормативный контракт**: принципы, invariant'ы, anti-patterns, decision-чеклист (цель, не «as-is») |

### Подсистемы

| Документ | Назначение |
|---|---|
| [table-registry.md](table-registry.md) | Реестр таблиц PG → DuckDB, sync-контроль |
| [architecture/runtime-patcher-inventory.md](architecture/runtime-patcher-inventory.md) | Каталог monkey-patch'ей с target/risk/тестами (полная сводка) |

### Операционные руководства

| Документ | Назначение |
|---|---|
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Диагностический runbook — типовые ошибки и решения |
| [MIGRATION.md](MIGRATION.md) | Сводка изменений между релизами + breaking changes |

### Разработка

| Документ | Назначение |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | `ApplicationContext`, поток инициализации, `lib/services`, `MessageExchange`, LLM-клиент, утилиты, дерево проекта |
| [DATABASE.md](DATABASE.md) | Единый пул соединений, универсальный слой данных, конфигурация навыка, DDL, границы P0 |
| [VECTOR_INDEXES.md](VECTOR_INDEXES.md) | FAISS, Ollama, `tools/build_vectors.py`, lifecycle кеша, edge-cases |
| [INTERNAL_API.md](INTERNAL_API.md) | `tools.exec`, кастомные `workspace/tools/*.py`, CLI-режимы, `tools/`, добавление настроек |
| [TESTING.md](TESTING.md) | Запуск тестов, контрактные тесты nanobot API, live e2e |

### Внешние ссылки

- [README.md](../README.md) — навигационный хаб проекта для пользователя.
- [CHANGELOG.md](../CHANGELOG.md) — полная история изменений по Keep a Changelog.
- [AGENTS.md](../AGENTS.md) — инструкции для opencode-ассистента.
- [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) — нормативный архитектурный контракт.
- [sql/README.md](../sql/README.md) — каталог DDL и миграций схемы.

## Конвенция именования

- `*.md` в корне `docs/` — навигационные / операционные документы.
- `docs/architecture/` — каталоги инвентарей (генерируются из кода).
- `docs/*-architecture.md` — архитектурные контракты (skill/tool).
- `docs/*-inventory.md` — инвентаризация компонентов.
- `docs/*-baseline.md` — wip-заметки рефакторингов.

Все ссылки между документами — относительные (`./SKILL.md`, `../README.md`).

## 📐 Нормативная архитектура (TARGET)

[`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md) — **архитектурный контракт**: принципы,
invariant'ы, anti-patterns, decision-чеклист и правила зависимостей. Это «как должно быть»,
а **не** описание текущей реализации. Каждое существенное изменение сверяется с ним
(см. разделы §30–§31).

> **Разделение ответственности, чтобы не дублировать:**
> - `TARGET_ARCHITECTURE.md` — *норма* (правила, цель, contract). Не содержит описания «as-is».
> - `ARCHITECTURE.md`, `DATABASE.md`, `INTERNAL_API.md` (и этот каталог) — *текущая реализация*
>   (что и как работает сейчас). Ссылаются на `TARGET_ARCHITECTURE.md §N` за правилами.
> - Где документы пересекаются по теме — детали реализации только в `docs/*`, правила только в `TARGET_ARCHITECTURE.md`.

## 🚀 Быстрый старт для разработчика

1. Установите зависимости: `pip install -r requirements.txt`
2. Запустите тесты без БД: `pytest tests/ -q`
3. Запустите gateway / CLI и проверьте прогон: `python gateway.py` или `python cli_agent.py -P`
4. Перед коммитом убедитесь, что проверки документации (CI `docs-lint`) проходят.

Хотите написать **свой навык** (skill)? Начните с
[`SKILL_AUTHORING.md`](SKILL_AUTHORING.md) — там пошаговый гайд, best practices,
anti-patterns и Definition of Done.

## 📐 Конвенции правки документации

- Любое изменение поведения, API или конфигурации сопровождается правкой
  соответствующего файла в `docs/` **в том же изменении**.
- Перекрёстные ссылки между документами — относительные (`docs/ARCHITECTURE.md`),
  внутри одного файла — якоря (`#структура-проекта`).
- Этот индекс держите компактным; детали — в файлах подсистем.
