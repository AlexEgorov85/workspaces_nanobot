# Tasks: Component Specification System

## Инфраструктура (этот change)

- [x] Создать структуру каталогов для change
- [x] Написать proposal.md с описанием проблемы и цели
- [x] Написать design.md с архитектурой системы
- [x] Создать component-model spec (модель компонента)
- [x] Создать component-registry spec (правила реестра)
- [x] Создать component-spec-validation spec (валидация)

## Миграция существующих спецификаций

- [ ] Перевести `architecture/skill-tool-boundary` на русский, обновить по шаблону
- [ ] Перевести `configuration/profiles` на русский, обновить по шаблону
- [ ] Обновить `data/cache-provider` (переименование из data/cache)
- [ ] Обновить `data/vector-indexes` по шаблону
- [ ] Обновить `runtime/context` по шаблону

## Критерии готовности этого change

- [x] component-model spec определяет шаблон и правила
- [x] component-registry spec определяет правила ведения реестра
- [x] component-spec-validation spec определяет правила валидации
- [x] Все файлы на русском языке
- [x] Нет дублирования с TARGET_ARCHITECTURE.md
- [x] Нет выдуманных контрактов — только зафиксированные решения
- [x] `.gitignore` не изменён (исправлен blocker)
- [x] Удалены дублирующие spec (data/cache удалён, оставлен data/cache-provider)

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

- [ ] Обновить docs/README.md с описанием системы спецификаций
- [ ] Обновить AGENTS.md правилами работы со спецификациями
- [ ] Добавить validation script для проверки структуры spec
