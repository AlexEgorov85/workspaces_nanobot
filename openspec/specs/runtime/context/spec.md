# Runtime Context (Контекст выполнения)

## Purpose

Определение границы между `ApplicationContext` (долгоживущая общая инфраструктура) и состоянием сессии/выполнения. Граница гарантирует, что runtime-инфраструктура не накапливает эфемерные данные и что переходы жизненного цикла детерминированы.

## Responsibility

Runtime Context отвечает за:
- предоставление единого корня сборки runtime-сервисов через ApplicationContext
- изоляцию состояния сессии от общей инфраструктуры
- определение детерминированного жизненного цикла context

## Boundary

### Owns
- сборкой общих runtime-сервисов
- координацией жизненного цикла контекста
- предоставлением доступа к инфраструктурным сервисам

### Does Not Own
- состоянием пользовательской сессии
- сообщениями разговора
- состоянием на один вопрос
- бизнес/domain данными

### May Depend On
- инфраструктурных сервисов (кеш, логирование, БД)
- фабрик компонентов

### Must Not Depend On
- конкретной реализации Skills
- session-specific данных
- конфигурации профиля (profile resolution происходит на уровне config)

## Public Contract

ApplicationContext предоставляет:
- единый корень сборки runtime-сервисов
- доступ к ConfigService, CacheProvider, VectorIndexService
- детерминированный lifecycle (start/stop)
- изоляцию от session state

## Requirements

### Requirement: Единый корень общей инфраструктуры

Система ДОЛЖНА предоставлять `ApplicationContext` как единственную точку сборки runtime-сервисов.

#### Scenario: Сервисы подключаются через ApplicationContext

- **КОГДА** требуется runtime-сервис
- **ТОГДА** он ДОЛЖЕН быть получен через `ApplicationContext` или его документированный аксессор, а не создан ad hoc

### Requirement: Состояние сессии вне ApplicationContext

Система ДОЛЖНА хранить состояние сессии (сообщения, метаданные, per-turn deltas) в `PGSessionManager` или канальном слое, но НЕ в `ApplicationContext`.

#### Scenario: Поиск сессии

- **КОГДА** требуются метаданные сессии
- **ТОГДА** система ДОЛЖНА прочитать их из `PGSessionManager`, а не из атрибутов `ApplicationContext`

### Requirement: Детерминированный жизненный цикл

Система ДОЛЖНА определять детерминированный жизненный цикл `ctx.start()` / `ctx.stop()`, порядок которого НЕ ДОЛЖЕН зависеть от вызывающей стороны или активного профиля.

#### Scenario: Независимый жизненный цикл

- **КОГДА** два вызывающих лица вызывают `ctx.start()` параллельно
- **ТОГДА** порядок жизненного цикла ДОЛЖЕН быть детерминированным и НЕ ДОЛЖЕН различаться между запусками

### Requirement: Совместимость с upstream nanobot

`ApplicationContext.start()` MUST ДОЛЖЕН вызывать `RuntimePatcher.apply_all` после инициализации сервисов и до старта каналов; если `apply_all` оставляет непустой `report.failed`, система MUST ДОЛЖНА логировать warning, но MUST NOT НЕ ДОЛЖНА прерывать старт (каждый `failed`-патч явно помечен `DEPRECATED` и не критичен для прод).

#### Scenario: Апгрейд upstream-nanobot без регрессии

- **WHEN** версия `nanobot-ai` в `requirements.txt` меняется
- **THEN** `pytest tests/contract/` MUST ДОЛЖЕН запускаться первым; если есть падения, они MUST ДОЛЖНЫ быть исправлены или явно помечены `xfail` до merge upgrade-изменения

### Requirement: ContextCompactionService через upstream EventSink

`ContextCompactionService` MUST ДОЛЖЕН использовать upstream `EventSink` для получения событий сжатия контекста (`ContextCompactionEvent`) и MUST NOT НЕ ДОЛЖЕН оборачивать внутренние методы `Consolidator` (которых может не быть в следующих версиях upstream).

#### Scenario: Подписка на compaction-события

- **WHEN** upstream публикует `ContextCompactionEvent(phase=...)` через `EventSink.emit` → `bus.publish_event` → `OutboundMessage.event` в `bus.outbound`
- **THEN** `postgres_channel` MUST ДОЛЖЕН распознать `isinstance(msg.event, ContextCompactionEvent)` и вызвать `ContextCompactionService._notify(session_key, report_from_event)`; `_notify` MUST ДОЛЖЕН писать history-notice в `agent_conversation_messages` для фазы `succeeded` и event `context_compacted` в `agent_gateway_logs` для всех фаз (`started`, `succeeded`, `failed`, `cancelled`)

#### Scenario: Принудительное сжатие через tool `/compact`

- **WHEN** агент вызывает `compact_context` tool или пользователь подаёт `/compact` slash-команду
- **THEN** система MUST ДОЛЖЕН вызвать `loop.consolidator.compact_idle_session(ctx.key, runtime=runtime, events=delivery.events)` (upstream API через `cmd_compact`); наш `_notify` срабатывает через фильтр `postgres_channel`, MUST NOT НЕ ДОЛЖЕН через `AgentHook.after_run` (хуки не видят `OutboundMessage.event`)

## Forbidden Behavior

Система НЕ ДОЛЖНА:

- хранить per-session данные в `ApplicationContext` (его время жизни превышает время жизни любой отдельной сессии)
- хранить runtime-wide конфигурацию в объекте сессии или сообщения
- создавать параллельный application context (нет `ApplicationContext2`, нет shadow registry, нет override механизма)
- добавлять fallback путь для application-context (нет `try_new` затем `legacy_new`)
- добавлять profile-specific ветки в `ApplicationContext` (согласно `openspec/specs/configuration/profiles/spec.md`, профиль разрешается на этапе конфигурации, бизнес-логика НЕ ДОЛЖНА ветвиться по профилю)

## Dependencies

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `openspec/specs/configuration/profiles/spec.md` — разрешение профилей
- `lib/core/application_context.py:ApplicationContext` — реализация

## Configuration

Отсутствует. Конфигурация разрешается через ConfigService до создания ApplicationContext.

## Lifecycle

1. **Создание**: `ApplicationContext.create()` вызывается один раз при старте системы
2. **Инициализация**: `ctx.start()` инициализирует все сервисы в детерминированном порядке
3. **Использование**: сервисы доступны через accessor'ы контекста
4. **Остановка**: `ctx.stop()` освобождает ресурсы в обратном порядке

## State

ApplicationContext хранит ссылки на:
- ConfigService
- CacheProvider
- VectorIndexService
- другие infrastructure сервисы

НЕ хранит:
- session state
- conversation messages
- per-question state

## Invariants

- ApplicationContext существует в единственном экземпляре
- Session state никогда не хранится в ApplicationContext
- Lifecycle ordering детерминирован независимо от caller
- Profile не влияет на бизнес-логику внутри ApplicationContext

## Error Behavior

- Ошибка инициализации сервиса → `ctx.start()` выбрасывает исключение, система не запускается
- Ошибка остановки сервиса → `ctx.stop()` логирует ошибку, продолжает остановку остальных сервисов

## Consumers

- AgentFactory — создание agent loop
- ChannelManager — инициализация каналов
- CLI/Gateway/Streamlit — точки входа приложения

## Implementation

Основная реализация:
- `lib/core/application_context.py:ApplicationContext`

Связанные компоненты:
- `lib/services/config_service.py:ConfigService`
- `lib/services/cache_provider.py:CacheProvider`
- `lib/data/vector_index_service.py:VectorIndexService`

## Verification

Валидация включает:
1. Проверка отсутствия session state в ApplicationContext (code review)
2. Проверка детерминированности lifecycle (тесты на concurrent start)
3. Проверка отсутствия profile-specific веток в коде ApplicationContext
