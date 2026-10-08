# Документация проекта

Навигационный индекс для разработчиков. Каждый документ — **самодостаточный
reference** по своей подсистеме; README в корне — это навигационный хаб.

## 📋 Компонентные спецификации (OpenSpec)

Каталог OpenSpec-спецификаций архитектурных компонентов живёт в
[`openspec/specs/`](../openspec/specs/) и дополняет этот `docs/`-каталог.
Реестр — [`openspec/specs/COMPONENTS.md`](../openspec/specs/COMPONENTS.md).

| Категория | Назначение |
|---|---|
| `architecture/component-model` | Модель архитектурного компонента и шаблон spec |
| `architecture/skill-tool-boundary` | Граница Skill / Tool |
| `runtime/context` | `ApplicationContext` и его жизненный цикл |
| `runtime/agent-hooks` | Фреймворковые и workspace-хуки агента |
| `runtime/error-fallback` | Fallback-ответ при internal-ошибке оборота |
| `runtime/runtime-events-subscription` | Подписка на runtime-события turn'а |
| `configuration/profiles` | Профили конфигурации (`prod` / `test`) |
| `data/cache-provider` | `CacheProvider` (SQL-кэш + FAISS) |
| `data/vector-indexes` | Векторные индексы (FAISS, lifecycle, целостность) |
| `logging-db` | Долговечный журнал `agent_gateway_logs` |
| `storage/session-hybridization` | Гибридное хранение сессий (JSONL + PG mirror) |
| `storage/session-recovery` | Восстановление сессий после потери метаданных |
| `storage/usage-store` | LLM usage tracking (`LLMUsageStore`) |
| `tools-history-search` | Tool `history_search` по журналу `agent_gateway_logs` |

Разделение ответственности между OpenSpec, `docs/` и кодом описано в
[`openspec/specs/architecture/component-model/spec.md`](../openspec/specs/architecture/component-model/spec.md)
(краткое резюме — в этом README ниже, в секции «📐 Нормативная архитектура (TARGET)»).

## Каталог

### Архитектура и интеграции

| Документ | Назначение |
|---|---|
| [architecture/nanobot-inventory.md](architecture/nanobot-inventory.md) | Инвентарь всех зависимостей от `nanobot-ai` (GREEN/YELLOW/ORANGE/RED) |
| [architecture/runtime-patcher-inventory.md](architecture/runtime-patcher-inventory.md) | Каталог monkey-patch'ей с target/risk/тестами |
| [architecture/storage-layers.md](architecture/storage-layers.md) | Гибридная модель хранения сессий (upstream JSONL + cold-storage PG mirror); правила использования пула |
| [architecture/usage-tracking.md](architecture/usage-tracking.md) | LLM usage tracking через upstream `LLMUsageStore` (observer-pipeline) + `DbLoggingService` |
| [architecture/decisions/](architecture/decisions/) | ADR-подобные decision-записи по архитектурным изменениям |
| [skill-tool-architecture.md](skill-tool-architecture.md) | Контракт Skill ↔ Tool: что разрешено, что запрещено |
| [skill-tool-inventory.md](skill-tool-inventory.md) | Текущее состояние всех skill/tool и история удалённых |
| [legal_summarizer_question_pipeline.md](legal_summarizer_question_pipeline.md) | Конвейер question-mode навыка `legal_summarizer` |
| [SKILL_AUTHORING.md](SKILL_AUTHORING.md) | **Пошаговый гайд**: как создать свой skill (структура, SKILL.md, регистрация в project.json, runtime API, best practices, anti-patterns, DoD) |
| [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) | **Нормативный контракт**: принципы, invariant'ы, anti-patterns, decision-чеклист (цель, не «as-is») |

### Подсистемы

| Документ | Назначение |
|---|---|
| [table-registry.md](table-registry.md) | Реестр таблиц PG → DuckDB, sync-контроль, track-колонки |

### Операционные руководства

| Документ | Назначение |
|---|---|
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Диагностический runbook — типовые ошибки и решения |
| [MIGRATION.md](MIGRATION.md) | Сводка изменений между релизами + breaking changes |
| [PROFILES.md](PROFILES.md) | Профили конфигурации (prod / test): обязательный `--profile`, hard-fail валидация, миграция деплоев |
| [RELEASE.md](RELEASE.md) | Release-процесс: версионирование, ветвление, теги, GitHub Release |

### Разработка

| Документ | Назначение |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | `ApplicationContext`, поток инициализации, `lib/services`, `MessageExchange`, LLM-клиент, утилиты, дерево проекта |
| [DATABASE.md](DATABASE.md) | Единый пул соединений, универсальный слой данных, конфигурация навыка, DDL, границы P0 |
| [VECTOR_INDEXES.md](VECTOR_INDEXES.md) | Векторная подсистема: `project.json` → `storage_table` → DuckDB-снапшот → in-memory FAISS, `tools/build_vectors.py`, edge-cases |
| [INTERNAL_API.md](INTERNAL_API.md) | `tools.exec`, кастомные `workspace/tools/*.py`, CLI-режимы, `tools/`, добавление настроек |
| [TESTING.md](TESTING.md) | Запуск тестов, контрактные тесты nanobot API, live e2e |
| [spec-code-drift-audit.md](spec-code-drift-audit.md) | Срез соответствия «спека ↔ код ↔ документация» (2026-10-08): P0/P1/P2 расхождения, состояние валидаторов и реестра |

### Архив

[`_archive/`](_archive/) — исторические process/baseline/audit-артефакты:
проектные планы рефакторингов, инвентаризации OpenSpec-миграции, baseline'ы
`legal_summarizer`, черновики анализа `history_search` / event logging.
**Не актуальная документация** — на состояние кода не ссылаться; история
изменений проекта живёт в [`CHANGELOG.md`](../CHANGELOG.md).

### Внешние ссылки

- [README.md](../README.md) — навигационный хаб проекта для пользователя.
- [CHANGELOG.md](../CHANGELOG.md) — полная история изменений по Keep a Changelog.
- [AGENTS.md](../AGENTS.md) — инструкции для opencode-ассистента.
- [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) — нормативный архитектурный контракт.
- [sql/README.md](../sql/README.md) — каталог DDL и миграций схемы.

## Конвенция именования

- `*.md` в корне `docs/` — навигационные / операционные документы.
- `docs/architecture/` — каталоги инвентарей (генерируются из кода) и `decisions/`.
- `docs/*-architecture.md` — архитектурные контракты (skill/tool).
- `docs/*-inventory.md` — инвентаризация компонентов.
- `docs/_archive/` — исторические process/baseline/audit-заметки (не актуальны).

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
> - [`openspec/specs/`](../openspec/specs/) — *контракты компонентов* (component-level normative specs;
>   нормативные спеки пишутся по-русски, но по факту английский шаблон сохранён у 21 спеки
>   из 22 — русский применён только к `runtime/startup-schema-validation`):
>   назначение, граница, требования, запрещённое поведение, зависимости, реализация,
>   проверка. Шаблон и правила — [`architecture/component-model`](../openspec/specs/architecture/component-model/spec.md);
>   реестр — [`COMPONENTS.md`](../openspec/specs/COMPONENTS.md); структурная проверка —
>   `python tools/validate_component_specs.py` (**сейчас падает**: 21 спека не переведена
>   на русский шаблон, см. раздел «Расхождения реестра»).
> - Где документы пересекаются по теме — детали реализации только в `docs/*`, правила только в `TARGET_ARCHITECTURE.md`,
>   контракт компонента — только в соответствующей `openspec/specs/<domain>/<component>/spec.md`.

## 🚀 Быстрый старт для разработчика

1. Установите зависимости: `pip install -r requirements.txt`
2. Запустите тесты без БД: `pytest tests/ -q`
3. Запустите gateway с профилем (без `--profile` — `ConfigurationError` + `exit 2`)
   или CLI (фиксированный профиль `test`, флаг не принимается):
   `python gateway.py --profile=prod` или `python cli_agent.py -P`
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
