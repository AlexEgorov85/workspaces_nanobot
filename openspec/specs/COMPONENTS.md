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
| configuration | 1 | 0 | 1 | 0 | 0 |
| data | 4 | 1 | 3 | 0 | 0 |
| documentation | 1 | 0 | 0 | 1 | 0 |
| infrastructure | 1 | 0 | 1 | 0 | 0 |
| interfaces | 2 | 0 | 2 | 0 | 0 |
| observability | 2 | 0 | 2 | 0 | 0 |
| runtime | 13 | 0 | 13 | 0 | 0 |
| sessions | 2 | 0 | 1 | 1 | 0 |
| skills | 1 | 0 | 1 | 0 | 0 |
| testing | 1 | 0 | 1 | 0 | 0 |
| validation | 1 | 0 | 0 | 1 | 0 |
| **Итого** | **31** | **1** | **27** | **3** | **0** |

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
| AgentHooks | `lib/core/agent_factory.py:AgentFactory` | [`runtime/agent-hooks`](runtime/agent-hooks/spec.md) | partial |
| AntiLoopGuard | `lib/hooks/repeat_guard_hook.py:RepeatGuardHook` | [`runtime/anti-loop`](runtime/anti-loop/spec.md) | partial |
| DbQueueClasses | `mcp-platform/libs/enterprise_data/db.py:DBManager` | [`runtime/db-queue-classes`](runtime/db-queue-classes/spec.md) | partial |
| RuntimeEntrypoints | `gateway.py:main` | [`runtime/entrypoints`](runtime/entrypoints/spec.md) | partial |
| ErrorFallback | `lib/services/turn_delivery_factory.py:FallbackTurnDeliveryFactory` | [`runtime/error-fallback`](runtime/error-fallback/spec.md) | partial |
| OperatorConsole | `lib/services/operator_console.py` | [`runtime/operator-console`](runtime/operator-console/spec.md) | partial |
| AgentSettingsBlock | `lib/services/agent_settings.py:AgentSettingsBlock` | [`runtime/platform-settings`](runtime/platform-settings/spec.md) | partial |
| RuntimeEventsSubscriber | `lib/services/runtime_events_subscriber.py:RuntimeEventsSubscriber` | [`runtime/runtime-events-subscription`](runtime/runtime-events-subscription/spec.md) | partial |
| RuntimePatcher | `lib/services/runtime_patcher.py:RuntimePatcher` | [`runtime/runtime-patcher`](runtime/runtime-patcher/spec.md) | partial |
| StartupSchemaValidation | `lib/services/schema_validation.py:SchemaValidationService` | [`runtime/startup-schema-validation`](runtime/startup-schema-validation/spec.md) | partial |
| SessionFiles | `lib/services/session_files.py`, `mcp-platform/servers/enterprise/tools/session_files.py` | [`runtime/session-files`](runtime/session-files/spec.md) | partial |
| CallContract | `mcp-platform/libs/enterprise_common/execution/errors.py` | [`runtime/call-contract`](runtime/call-contract/spec.md) | partial |

### Configuration

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| Profiles | `config.json::profiles` (конфигурация) | [`configuration/profiles`](configuration/profiles/spec.md) | partial |

### Data

> Все компоненты этого раздела принадлежат платформе (`mcp-platform`), не агенту:
> capability `data` владеет снимком, capability `vectors` — индексами, схема
> публикуемых операций — общий контракт платформы. Агентских
> `lib/services/cache_provider.py` и `lib/services/vector_index_service.py`
> в дереве нет (удалены 2026-10-01 вместе с кластером локального снимка).

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| CacheProvider | `mcp-platform/libs/enterprise_data/snapshot/contracts.py:CacheProvider` | [`data/cache-provider`](data/cache-provider/spec.md) | partial |
| VectorIndexBuilder | `mcp-platform/libs/vectors/builder.py:VectorBuilder` | [`data/vector-indexes`](data/vector-indexes/spec.md) | partial |
| VectorIndexOwner | `mcp-platform/libs/vectors/owner.py:VectorIndexOwner` | [`data/vector-indexes`](data/vector-indexes/spec.md) | partial |
| OperationSchema | `mcp-platform/libs/enterprise_common/registry.py:build_input_schema` | [`data/operation-schema`](data/operation-schema/spec.md) | complete |

### Infrastructure

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| TestProfileTables | `tools/apply_test_profile_tables.py:main` | [`infrastructure/test-profile-tables`](infrastructure/test-profile-tables/spec.md) | partial |

### Skills

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| QueryOperation | `mcp-platform/servers/enterprise/tools/query_operation.py:create_tool` | [`skills/legal-summarizer-query`](skills/legal-summarizer-query/spec.md) | partial |

### Testing

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| UnifiedTestContract | `pyproject.toml::[tool.pytest.ini_options]` | [`testing/unified-test-contract`](testing/unified-test-contract/spec.md) | partial |

### Documentation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentRegistry | N/A (мета-спецификация) | [`documentation/component-registry`](documentation/component-registry/spec.md) | draft |

### Validation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentSpecValidation | N/A (правила валидации) | [`validation/component-spec-validation`](validation/component-spec-validation/spec.md) | draft |

### Observability

| Компонент | Реализация | Спецификация | Статус |
|---|---|---|---|
| DbLoggingService | `lib/services/db_logging_service.py`, `lib/services/log_transport.py` | [`observability/logging-db`](observability/logging-db/spec.md) | partial |
| UsageStore | `lib/core/agent_factory.py`, `lib/core/application_context.py` | [`observability/usage-store`](observability/usage-store/spec.md) | partial |

### Sessions

| Компонент | Реализация | Спецификация | Статус |
|---|---|---|---|
| SessionMirror | `lib/gateway/mirror/session_mirror.py`, `lib/gateway/mirror/mirror_poller.py` | [`sessions/session-hybridization`](sessions/session-hybridization/spec.md) | partial |
| SessionRecovery | N/A (содержат один из сюй требований закрыт в дереве) | [`sessions/session-recovery`](sessions/session-recovery/spec.md) | draft |

### Interfaces

| Компонент | Реализация | Спецификация | Статус |
|---|---|---|---|
| HistorySearch | `mcp-platform/servers/enterprise/capabilities/data/tools/history_search.py` | [`interfaces/tools-history-search`](interfaces/tools-history-search/spec.md) | partial |
| UpgradeCompatibility | `lib/services/runtime_patcher.py`, `pyproject.toml` | [`infrastructure/upgrade-compatibility`](infrastructure/upgrade-compatibility/spec.md) | partial |

## План заполнения

### Wave 1 (завершён): Инфраструктура + миграция существующих spec

- [x] component-model
- [x] component-registry
- [x] component-spec-validation
- [x] skill-tool-boundary (миграция на русский + новый шаблон)
- [x] profiles (миграция на русский + новый шаблон)
- [x] cache-provider (миграция из data/cache + новый шаблон)
- [x] vector-indexes (новый шаблон)
- [x] context (новый шаблон)
