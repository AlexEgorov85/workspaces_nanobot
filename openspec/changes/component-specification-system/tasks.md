# Tasks: Component Specification System

## Инфраструктура (этот change)

- [x] Создать структуру каталогов для change
- [x] Написать proposal.md с описанием проблемы и цели
- [x] Написать design.md с архитектурой системы
- [x] Создать component-model spec (модель компонента)
- [x] Создать component-registry spec (правила реестра)
- [x] Создать component-spec-validation spec (валидация)

## Миграция существующих спецификаций

- [x] Перевести `architecture/skill-tool-boundary` на русский, обновить по шаблону
- [x] Перевести `configuration/profiles` на русский, обновить по шаблону
- [x] Обновить `data/cache-provider` (переименование из data/cache)
- [x] Обновить `data/vector-indexes` по шаблону
- [x] Обновить `runtime/context` по шаблону

## Критерии готовности этого change

- [x] component-model spec определяет шаблон и правила
- [x] component-registry spec определяет правила ведения реестра
- [x] component-spec-validation spec определяет правила валидации
- [x] Все файлы на русском языке
- [x] Нет дублирования с TARGET_ARCHITECTURE.md
- [x] Нет выдуманных контрактов — только зафиксированные решения
- [x] `.gitignore` не изменён (исправлен blocker)
- [x] Удалены дублирующие spec (data/cache удалён, оставлен data/cache-provider)
- [x] 5 мигрированных spec проходят `tools/validate_component_specs.py`
- [x] 3 delta-спека в `openspec/changes/.../specs/` приведены к OpenSpec-формату и проходят `openspec validate`
- [x] `COMPONENTS.md` обновлён (статусы после миграции: 6 partial / 2 draft / 0 missing)
- [x] `docs/README.md` дополнен разделом о компонентных спецификациях
- [x] `AGENTS.md` дополнен секцией Component Specification System
- [x] Добавлен `tools/validate_component_specs.py` (автоматическая валидация структуры spec)

## Следующие шаги (отдельные changes)

### Wave 2: Core Runtime компоненты

- runtime/application-context (обновление существующей spec)
- runtime/agent-factory
- runtime/message-bus
- configuration/config-service
- sessions/postgres-session-manager
- data/cache-provider (обновление)
- data/duckdb-sync

### Wave 3: Infrastructure и Interfaces

- channels/channel-manager
- channels/postgres-channel
- channels/redis-channel
- observability/database-logging
- observability/event-logging
- infrastructure/runtime-patcher
- infrastructure/hooks
- infrastructure/subprocess-management
- infrastructure/transcription
- security/sql-safety
- interfaces/cli
- interfaces/gateway
- interfaces/streamlit

### Wave 4: Skills

- skills/skill-contract
- skills/audit-analyzer
- skills/legal-summarizer
- skills/office-files
- testing/benchmarks

## Интеграция

- [x] Обновить docs/README.md с описанием системы спецификаций
- [x] Обновить AGENTS.md правилами работы со спецификациями
- [x] Добавить validation script для проверки структуры spec
