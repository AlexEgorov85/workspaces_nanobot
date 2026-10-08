# Реестр компонентов (Component Registry)

Единый реестр архитектурных компонентов проекта workspaces_nanobot.

Правила ведения реестра, статусы и формат записей зафиксированы в
[`openspec/specs/architecture/component-model/spec.md`](architecture/component-model/spec.md)
и [`openspec/specs/documentation/component-registry/spec.md`](documentation/component-registry/spec.md).

> **In-flight changes** с компонентной семантикой НЕ добавляют записи
> в этот реестр, пока change не заархивирован. Canonical spec
> создаётся ПОСЛЕ архивации change (см. `ComponentModel` —
> workflow: change → archive → canonical spec → entry в реестре).
> До архивации запись о компоненте существует только в `proposal.md`
> / `design.md` / `tasks.md` самой change.

## Статистика

| Категория | Всего | Complete | Partial | Draft | Missing |
|-----------|-------|----------|---------|-------|---------|
| architecture | 2 | 0 | 2 | 0 | 0 |
| runtime | 7 | 0 | 7 | 0 | 0 |
| configuration | 1 | 0 | 1 | 0 | 0 |
| data | 2 | 0 | 2 | 0 | 0 |
| infrastructure | 1 | 0 | 1 | 0 | 0 |
| storage | 3 | 0 | 3 | 0 | 0 |
| skills | 1 | 0 | 1 | 0 | 0 |
| tools | 1 | 0 | 1 | 0 | 0 |
| logging | 1 | 0 | 1 | 0 | 0 |
| upgrade | 1 | 0 | 1 | 0 | 0 |
| documentation | 1 | 0 | 0 | 1 | 0 |
| validation | 1 | 0 | 0 | 1 | 0 |
| **Итого** | **22** | **0** | **20** | **2** | **0** |

`Complete` не установлен ни у одного компонента: для этого статуса нужны все
обязательные разделы `component-model`, а русский шаблон применён пока к одной
спеке (см. `## Расхождения реестра` ниже).

## Компоненты

### Architecture

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentModel | N/A (мета-спецификация) | [`architecture/component-model`](architecture/component-model/spec.md) | partial |
| SkillToolBoundary | N/A (архитектурное правило) | [`architecture/skill-tool-boundary`](architecture/skill-tool-boundary/spec.md) | partial |

### Runtime

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ApplicationContext | `lib/core/application_context.py:ApplicationContext` | [`runtime/context`](runtime/context/spec.md) | partial |
| RuntimePatcher | `lib/services/runtime_patcher.py:RuntimePatcher` | [`runtime/runtime-patcher`](runtime/runtime-patcher/spec.md) | partial |
| StartupSchemaValidation | `lib/services/schema_validation.py:SchemaValidationService` | [`runtime/startup-schema-validation`](runtime/startup-schema-validation/spec.md) | partial |
| AgentHooks | `lib/hooks/`, `workspace/hooks/` | [`runtime/agent-hooks`](runtime/agent-hooks/spec.md) | partial |
| ErrorFallback | `lib/services/runtime_patcher.py:RuntimePatcher.patch_turn_delivery_fail` | [`runtime/error-fallback`](runtime/error-fallback/spec.md) | partial |
| RuntimeEventsSubscriber | `lib/services/runtime_events_subscriber.py:RuntimeEventsSubscriber` | [`runtime/runtime-events-subscription`](runtime/runtime-events-subscription/spec.md) | partial |
| EntryPoints | `gateway.py`, `cli_agent.py`, `streamlit_app.py` | [`runtime/entrypoints`](runtime/entrypoints/spec.md) | partial |

### Configuration

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| Profiles | `project.json::profiles` (конфигурация) | [`configuration/profiles`](configuration/profiles/spec.md) | partial |

### Data

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| CacheProvider | `lib/services/cache_provider.py:CacheProvider` | [`data/cache-provider`](data/cache-provider/spec.md) | partial |
| VectorIndexBuildService | `lib/services/vector_index_service.py:VectorIndexBuildService` | [`data/vector-indexes`](data/vector-indexes/spec.md) | partial |

### Documentation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentRegistry | N/A (мета-спецификация) | [`documentation/component-registry`](documentation/component-registry/spec.md) | draft |

### Validation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentSpecValidation | N/A (правила валидации) | [`validation/component-spec-validation`](validation/component-spec-validation/spec.md) | draft |

### Infrastructure

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| TestProfileTables | `tools/apply_test_profile_tables.py` | [`infrastructure/test-profile-tables`](infrastructure/test-profile-tables/spec.md) | partial |

### Storage

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| SessionHybridization | `lib/session/pg_session_manager.py:PGSessionManager` | [`storage/session-hybridization`](storage/session-hybridization/spec.md) | partial |
| SessionRecovery | **реализации нет** (см. «Расхождения реестра») | [`storage/session-recovery`](storage/session-recovery/spec.md) | partial |
| UsageStore | `lib/services/llm_usage_store_factory.py` | [`storage/usage-store`](storage/usage-store/spec.md) | partial |

### Skills

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| LegalSummarizerQuery | `workspace/tools/legal_summarizer_query.py` | [`skills/legal-summarizer-query`](skills/legal-summarizer-query/spec.md) | partial |

### Tools

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| HistorySearch | `workspace/tools/history_search_tool.py` | [`tools-history-search`](tools-history-search/spec.md) | partial |

### Logging

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| LoggingDb | `lib/services/db_logging_service.py:DbLoggingService` | [`logging-db`](logging-db/spec.md) | partial |

### Upgrade

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| UpgradeCompatibility | `tests/contract/` | [`upgrade-compatibility`](upgrade-compatibility/spec.md) | partial |

## Расхождения реестра

Зафиксированы 2026-10-08 по результатам сверки спек, кода и документации
(`docs/spec-code-drift-audit.md`). Реестр не скрывает их, а перечисляет явно.

- **`storage/session-recovery` — спека без реализации.** Ни `SessionRecoveryService`,
  ни `tools/recover_stale_sessions.py` (упомянутого в `spec.md:90`) в проекте нет.
  Статус `partial` формально не покрывает такой случай (в `component-registry`
  такого статуса нет: `missing` означает обратное — код есть, спеки нет), поэтому
  расхождение вынесено сюда явно.
- **Русский шаблон `component-model` применён к 1 спеке из 22** —
  `runtime/startup-schema-validation`. Остальные используют английские разделы
  (`## Purpose`, `## Requirements`), поэтому `validate_component_specs.py`
  отклоняет их структурно.
- **`openspec validate --specs --strict`: 14 passed, 8 failed** — падения уровня
  WARNING «requirement should contain SHALL or MUST» на русскоязычных требованиях.

## План заполнения

### Wave 1 (структура реестра завершена; миграция шаблона — нет)

Оригинальная запись утверждала `[x]` для миграции «на русский + новый шаблон»
у `skill-tool-boundary` и `profiles`. Проверка 2026-10-08: у обеих спек
заголовки английские (`## Purpose`, `## Responsibility`, `## Boundary`),
русского шаблона в них нет. Отметки приведены в соответствие с фактом.

- [x] component-model — мета-спека заведена
- [x] component-registry — мета-спека заведена
- [x] component-spec-validation — мета-спека заведена
- [~] skill-tool-boundary — спека есть, шаблон не мигрирован
- [~] profiles — спека есть, шаблон не мигрирован
- [~] cache-provider — спека переименована из `data/cache`, шаблон не мигрирован
- [~] vector-indexes — спека есть, шаблон не мигрирован
- [~] context — спека есть, шаблон не мигрирован
- [x] реестр покрывает все 22 спеки каталога `openspec/specs/`

`[~]` — работа по регистрации спеки сделана, шаблон `component-model` не применён.
Доведение до `[x]` требует миграции разделов, а не только отметки.
