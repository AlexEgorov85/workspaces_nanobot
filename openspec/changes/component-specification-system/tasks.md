# Tasks: Component Specification System

## Инфраструктура (этот change)

- [x] Создать структуру каталогов для change
- [x] Написать proposal.md с описанием проблемы и цели
- [x] Написать design.md с архитектурой системы
- [ ] Создать component-model spec (модель компонента)
- [ ] Создать component-registry spec (правила реестра)
- [ ] Создать component-spec-validation spec (валидация)

## Миграция существующих спецификаций

- [ ] Перевести `architecture/skill-tool-boundary` на русский, обновить по шаблону
- [ ] Перевести `configuration/profiles` на русский, обновить по шаблону
- [ ] Перевести `data/cache` на русский, обновить по шаблону (переименовать в cache-provider?)
- [ ] Перевести `data/vector-indexes` на русский, обновить по шаблону
- [ ] Перевести `runtime/context` на русский, обновить по шаблону

## Wave 1: Core Runtime компоненты

- [ ] runtime/application-context
- [ ] runtime/agent-factory
- [ ] runtime/message-bus
- [ ] configuration/config-service
- [ ] sessions/postgres-session-manager
- [ ] data/cache-provider
- [ ] data/vector-indexes (обновить существующую)

## Wave 2: Infrastructure и Interfaces

- [ ] channels/channel-manager
- [ ] channels/postgres-channel
- [ ] channels/redis-channel
- [ ] observability/database-logging
- [ ] observability/event-logging
- [ ] infrastructure/runtime-patcher
- [ ] infrastructure/hooks
- [ ] infrastructure/subprocess-management
- [ ] infrastructure/transcription
- [ ] security/sql-safety
- [ ] interfaces/cli
- [ ] interfaces/gateway
- [ ] interfaces/streamlit

## Wave 3: Skills

- [ ] skills/skill-contract
- [ ] skills/audit-analyzer
- [ ] skills/legal-summarizer
- [ ] skills/office-files

## Интеграция

- [ ] Обновить docs/README.md с описанием системы спецификаций
- [ ] Обновить AGENTS.md правилами работы со спецификациями
- [ ] Добавить validation script для проверки структуры spec
- [ ] Создать COMPONENTS.md с начальным inventory

## Критерии готовности

### Для этого change (инфраструктура):

- [ ] component-model spec определяет шаблон и правила
- [ ] component-registry spec определяет правила ведения реестра
- [ ] component-spec-validation spec определяет правила валидации
- [ ] Все файлы на русском языке
- [ ] Нет дублирования с TARGET_ARCHITECTURE.md
- [ ] Нет выдуманных контрактов — только зафиксированные решения

### Для каждой компонентной spec:

- [ ] Все обязательные разделы заполнены
- [ ] Boundary явно определён (owns, may depend on, must not depend on)
- [ ] Есть как минимум одно требование с сценарием
- [ ] Есть запрещённое поведение (negative requirements)
- [ ] Ссылка на реализацию указана
- [ ] Spec соответствует фактическому коду (не выдумана)
- [ ] Статус установлен честно (missing/draft/partial/complete)
