# Runtime Context (Контекст выполнения)

## Назначение

Определение границы между `ApplicationContext` (долгоживущая общая инфраструктура) и состоянием сессии/выполнения. Граница гарантирует, что runtime-инфраструктура не накапливает эфемерные данные и что переходы жизненного цикла детерминированы.

## Ответственность

Runtime Context отвечает за:
- предоставление единого корня сборки runtime-сервисов через ApplicationContext
- изоляцию состояния сессии от общей инфраструктуры
- определение детерминированного жизненного цикла context

## Граница

### Владеет
- сборкой общих runtime-сервисов
- координацией жизненного цикла контекста
- предоставлением доступа к инфраструктурным сервисам

### Не владеет
- состоянием пользовательской сессии
- сообщениями разговора
- состоянием на один вопрос
- бизнес/domain данными

### Может зависеть от
- инфраструктурных сервисов (кеш, логирование, БД)
- фабрик компонентов

### Не должен зависеть от
- конкретной реализации Skills
- session-specific данных
- конфигурации профиля (profile resolution происходит на уровне config)

## Публичный контракт

ApplicationContext предоставляет:
- единый корень сборки runtime-сервисов
- доступ к ConfigService, CacheProvider, VectorIndexService
- детерминированный lifecycle (start/stop)
- изоляцию от session state

## Требования

### Требование: Единый корень общей инфраструктуры

Система ДОЛЖНА предоставлять `ApplicationContext` как единственную точку сборки runtime-сервисов.

#### Сценарий: Сервисы подключаются через ApplicationContext

- **КОГДА** требуется runtime-сервис
- **ТОГДА** он ДОЛЖЕН быть получен через `ApplicationContext` или его документированный аксессор, а не создан ad hoc

### Требование: Состояние сессии вне ApplicationContext

Система ДОЛЖНА хранить состояние сессии (сообщения, метаданные, per-turn deltas) в `PGSessionManager` или канальном слое, но НЕ в `ApplicationContext`.

#### Сценарий: Поиск сессии

- **КОГДА** требуются метаданные сессии
- **ТОГДА** система ДОЛЖНА прочитать их из `PGSessionManager`, а не из атрибутов `ApplicationContext`

### Требование: Детерминированный жизненный цикл

Система ДОЛЖНА определять детерминированный жизненный цикл `ctx.start()` / `ctx.stop()`, порядок которого НЕ ДОЛЖЕН зависеть от вызывающей стороны или активного профиля.

#### Сценарий: Независимый жизненный цикл

- **КОГДА** два вызывающих лица вызывают `ctx.start()` параллельно
- **ТОГДА** порядок жизненного цикла ДОЛЖЕН быть детерминированным и НЕ ДОЛЖЕН различаться между запусками

## Запрещённое поведение

Система НЕ ДОЛЖНА:

- хранить per-session данные в `ApplicationContext` (его время жизни превышает время жизни любой отдельной сессии)
- хранить runtime-wide конфигурацию в объекте сессии или сообщения
- создавать параллельный application context (нет `ApplicationContext2`, нет shadow registry, нет override механизма)
- добавлять fallback путь для application-context (нет `try_new` затем `legacy_new`)
- добавлять profile-specific ветки в `ApplicationContext` (согласно `openspec/specs/configuration/profiles/spec.md`, профиль разрешается на этапе конфигурации, бизнес-логика НЕ ДОЛЖНА ветвиться по профилю)

## Зависимости

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `openspec/specs/configuration/profiles/spec.md` — разрешение профилей
- `lib/core/application_context.py:ApplicationContext` — реализация

## Конфигурация

Отсутствует. Конфигурация разрешается через ConfigService до создания ApplicationContext.

## Жизненный цикл

1. **Создание**: `ApplicationContext.create()` вызывается один раз при старте системы
2. **Инициализация**: `ctx.start()` инициализирует все сервисы в детерминированном порядке
3. **Использование**: сервисы доступны через accessor'ы контекста
4. **Остановка**: `ctx.stop()` освобождает ресурсы в обратном порядке

## Состояние

ApplicationContext хранит ссылки на:
- ConfigService
- CacheProvider
- VectorIndexService
- другие infrastructure сервисы

НЕ хранит:
- session state
- conversation messages
- per-question state

## Инварианты

- ApplicationContext существует в единственном экземпляре
- Session state никогда не хранится в ApplicationContext
- Lifecycle ordering детерминирован независимо от caller
- Profile не влияет на бизнес-логику внутри ApplicationContext

## Поведение при ошибке

- Ошибка инициализации сервиса → `ctx.start()` выбрасывает исключение, система не запускается
- Ошибка остановки сервиса → `ctx.stop()` логирует ошибку, продолжает остановку остальных сервисов

## Потребители

- AgentFactory — создание agent loop
- ChannelManager — инициализация каналов
- CLI/Gateway/Streamlit — точки входа приложения

## Реализация

Основная реализация:
- `lib/core/application_context.py:ApplicationContext`

Связанные компоненты:
- `lib/services/config_service.py:ConfigService`
- `lib/services/cache_provider.py:CacheProvider`
- `lib/data/vector_index_service.py:VectorIndexService`

## Проверка

Валидация включает:
1. Проверка отсутствия session state в ApplicationContext (code review)
2. Проверка детерминированности lifecycle (тесты на concurrent start)
3. Проверка отсутствия profile-specific веток в коде ApplicationContext
