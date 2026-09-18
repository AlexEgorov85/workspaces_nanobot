# Реестр компонентов

Этот документ содержит индекс всех архитектурных компонентов проекта `workspaces_nanobot`.

**Назначение:** предоставить единый каталог компонентов для отслеживания полноты спецификаций.

**Важно:** этот документ не содержит описания поведения компонентов. Описание поведения находится в соответствующих спецификациях `openspec/specs/<domain>/<component>/spec.md`.

## Компоненты

| Компонент | Категория | Реализация | Спецификация | Статус |
|-----------|-----------|------------|--------------|--------|
| ApplicationContext | Runtime | `lib/core/application_context.py` | `runtime/context` | complete |
| AgentFactory | Runtime | `lib/core/agent_factory.py` | `runtime/agent-factory` | complete |
| MessageBus | Runtime | `lib/channels/message_exchange.py` | `runtime/message-bus` | complete |
| ConfigService | Configuration | `lib/services/config_service.py` | `configuration/config-service` | complete |
| PGSessionManager | Sessions | `lib/session/pg_session_manager.py` | `sessions/postgres-session-manager` | complete |
| PostgresChannel | Channels | `lib/channels/postgres_channel.py` | `channels/postgres-channel` | complete |
| RedisChannel | Channels | `lib/channels/redis_channel.py` | `channels/redis-channel` | complete |
| ChannelManager | Channels | `lib/core/channel_manager.py` | `channels/channel-manager` | complete |
| CacheProvider | Data | `lib/services/cache_provider.py` | `data/cache-provider` | complete |
| VectorIndexService | Data | `lib/services/vector_index_service.py` | `data/vector-indexes` | complete |
| DuckDBSync | Data | `lib/data/duckdb_sync.py` | `data/duckdb-sync` | complete |
| DbLoggingService | Observability | `lib/services/db_logging_service.py` | `observability/database-logging` | complete |
| EventLoggingService | Observability | `lib/observability/event_logging.py` | `observability/event-logging` | complete |
| RuntimePatcher | Infrastructure | `lib/services/runtime_patcher.py` | `infrastructure/runtime-patcher` | complete |
| HookSystem | Infrastructure | `lib/hooks/hooks.py` | `infrastructure/hooks` | complete |
| SubprocessManager | Infrastructure | `lib/services/subprocess_manager.py` | `infrastructure/subprocess-management` | complete |
| TranscriptionService | Infrastructure | `lib/services/transcription_service.py` | `infrastructure/transcription` | complete |
| SqlSafety | Security | `lib/utils/sql_safety.py` | `security/sql-safety` | complete |
| Skill (contract) | Skills | `docs/SKILL_AUTHORING.md` | `skills/skill-contract` | complete |
| audit_analyzer | Skills | `workspace/skills/audit_analyzer/` | `skills/audit-analyzer` | complete |
| legal_summarizer | Skills | `workspace/skills/legal_summarizer/` | `skills/legal-summarizer` | complete |
| office_files | Skills | `workspace/skills/office_files/` | `skills/office-files` | complete |
| ComponentModel | Architecture | N/A (мета-спецификация) | `architecture/component-model` | complete |
| SkillToolBoundary | Architecture | N/A (архитектурное правило) | `architecture/skill-tool-boundary` | complete |
| ConfigurationProfiles | Configuration | `lib/core/project_settings.py` | `configuration/profiles` | complete |
| Gateway | Interfaces | `gateway.py` | `interfaces/gateway` | complete |
| StreamlitApp | Interfaces | `streamlit_app.py` | `interfaces/streamlit` | complete |
| CLI | Interfaces | `lib/cli/` | `interfaces/cli` | complete |
| BenchmarkSuite | Testing | `benchmarks/` | `testing/benchmarks` | complete |

## Статусы спецификаций

- `missing` — компонент существует в коде, но спецификация отсутствует.
- `draft` — спецификация создана, но неполная.
- `partial` — спецификация содержит основные разделы, но требует доработки (приведения к новому шаблону).
- `complete` — спецификация полная и соответствует шаблону `architecture/component-model`.
- `deprecated` — компонент устарел и будет удалён.

## Правила

1. **Каждый production-компонент ДОЛЖЕН иметь запись в реестре.**
2. **Реестр НЕ содержит описание поведения** — только метаданные.
3. **Реестр не является вторым источником истины** — он ссылается на спецификации.
4. **Статус обновляется при изменении спецификации.**

## Итоги

Все основные компоненты системы имеют спецификации:

- **Всего компонентов:** 29
- **Спецификации complete:** 29
- **Спецификации partial:** 0
- **Спецификации missing:** 0

### По категориям:

| Категория | Количество | Статус |
|-----------|------------|--------|
| Runtime | 3 | ✅ complete |
| Configuration | 2 | ✅ complete |
| Sessions | 1 | ✅ complete |
| Channels | 3 | ✅ complete |
| Data | 3 | ✅ complete |
| Observability | 2 | ✅ complete |
| Infrastructure | 4 | ✅ complete |
| Security | 1 | ✅ complete |
| Skills | 4 | ✅ complete |
| Architecture | 2 | ✅ complete |
| Interfaces | 3 | ✅ complete |
| Testing | 1 | ✅ complete |
