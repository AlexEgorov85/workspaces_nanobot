# Реестр компонентов (Component Registry)

Единый реестр архитектурных компонентов проекта workspaces_nanobot.

## Статистика

| Категория | Всего | Complete | Partial | Draft | Missing |
|-----------|-------|----------|---------|-------|---------|
| architecture | 2 | 0 | 1 | 0 | 1 |
| runtime | 3 | 0 | 1 | 0 | 2 |
| configuration | 2 | 0 | 1 | 0 | 1 |
| sessions | 1 | 0 | 0 | 0 | 1 |
| data | 3 | 0 | 2 | 0 | 1 |
| channels | 3 | 0 | 0 | 0 | 3 |
| observability | 2 | 0 | 0 | 0 | 2 |
| infrastructure | 4 | 0 | 0 | 0 | 4 |
| security | 1 | 0 | 0 | 0 | 1 |
| interfaces | 3 | 0 | 0 | 0 | 3 |
| skills | 4 | 0 | 0 | 0 | 4 |
| testing | 1 | 0 | 0 | 0 | 1 |
| documentation | 1 | 0 | 0 | 1 | 0 |
| validation | 1 | 0 | 0 | 1 | 0 |
| **Итого** | **31** | **0** | **5** | **2** | **24** |

## Компоненты

### Architecture

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentModel | N/A (мета-спецификация) | `architecture/component-model` | partial |
| SkillToolBoundary | N/A (архитектурное правило) | `architecture/skill-tool-boundary` | partial |

### Runtime

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ApplicationContext | `lib/core/application_context.py:ApplicationContext` | `runtime/context` | partial |
| AgentFactory | `lib/core/agent_factory.py:AgentFactory` | — | missing |
| MessageBus | `lib/core/message_bus.py:MessageBus` | — | missing |

### Configuration

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ConfigService | `lib/services/config_service.py:ConfigService` | — | missing |
| Profiles | `project.json::profiles` (конфигурация) | `configuration/profiles` | partial |

### Sessions

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| PostgresSessionManager | `lib/sessions/postgres_session_manager.py:PostgresSessionManager` | — | missing |

### Data

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| CacheProvider | `lib/services/cache_provider.py:CacheProvider` | — | missing |
| DuckDBSync | `lib/data/duckdb_sync.py:DuckDBSync` | — | missing |
| VectorIndexService | `lib/data/vector_index_service.py:VectorIndexService` | `data/vector-indexes` | partial |

### Channels

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ChannelManager | `lib/channels/channel_manager.py:ChannelManager` | — | missing |
| PostgresChannel | `lib/channels/postgres_channel.py:PostgresChannel` | — | missing |
| RedisChannel | `lib/channels/redis_channel.py:RedisChannel` | — | missing |

### Observability

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| DatabaseLogging | `lib/observability/database_logging.py:DatabaseLoggingService` | — | missing |
| EventLogging | `lib/observability/event_logging.py:EventLoggingService` | — | missing |

### Infrastructure

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| RuntimePatcher | `lib/infrastructure/runtime_patcher.py:RuntimePatcher` | — | missing |
| Hooks | `lib/infrastructure/hooks.py` | — | missing |
| SubprocessManagement | `lib/infrastructure/subprocess_management.py` | — | missing |
| Transcription | `lib/infrastructure/transcription.py` | — | missing |

### Security

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| SqlSafety | `lib/security/sql_safety.py:SqlSafety` | — | missing |

### Interfaces

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| CLI | `cli/main.py` | — | missing |
| Gateway | `gateway/main.py` | — | missing |
| Streamlit | `streamlit_app.py` | — | missing |

### Skills

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| SkillContract | N/A (контракт) | — | missing |
| AuditAnalyzer | `workspace/skills/audit_analyzer/__init__.py:AuditAnalyzerSkill` | — | missing |
| LegalSummarizer | `workspace/skills/legal_summarizer/__init__.py:LegalSummarizerSkill` | — | missing |
| OfficeFiles | `workspace/skills/office_files/__init__.py:OfficeFilesSkill` | — | missing |

### Testing

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| Benchmarks | `benchmarks/` | — | missing |

### Documentation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentRegistry | N/A (мета-спецификация) | `documentation/component-registry` | draft |

### Validation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentSpecValidation | N/A (правила валидации) | `validation/component-spec-validation` | draft |

## Правила ведения реестра

1. **Каждый production-компонент зарегистрирован**: компонент, существующий в коде, должен иметь запись в реестре.

2. **Честные статусы**:
   - `missing` — spec отсутствует
   - `draft` — spec создана, требует проверки
   - `partial` — spec заполнена частично
   - `complete` — spec прошла проверку полноты
   - `deprecated` — компонент устарел

3. **Реестр не является вторым источником истины**: содержит только метаданные, не описание поведения.

4. **Нет дубликатов**: каждый компонент имеет одну запись.

## План заполнения

### Wave 1 (текущий change): Инфраструктура + миграция существующих spec
- [x] component-model
- [x] component-registry
- [x] component-spec-validation
- [ ] skill-tool-boundary (миграция)
- [ ] profiles (миграция)
- [ ] cache (миграция)
- [ ] vector-indexes (миграция)
- [ ] context (миграция)

### Wave 2: Core Runtime
- [ ] application-context
- [ ] agent-factory
- [ ] message-bus
- [ ] config-service
- [ ] postgres-session-manager
- [ ] cache-provider
- [ ] duckdb-sync

### Wave 3: Channels, Observability, Infrastructure
- [ ] channel-manager
- [ ] postgres-channel
- [ ] redis-channel
- [ ] database-logging
- [ ] event-logging
- [ ] runtime-patcher
- [ ] hooks
- [ ] subprocess-management
- [ ] transcription
- [ ] sql-safety
- [ ] cli
- [ ] gateway
- [ ] streamlit

### Wave 4: Skills
- [ ] skill-contract
- [ ] audit-analyzer
- [ ] legal-summarizer
- [ ] office-files
- [ ] benchmarks
