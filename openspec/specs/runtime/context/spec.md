# Runtime Context (Контекст выполнения)

## Purpose

Определение границы между `ApplicationContext` (долгоживущая общая инфраструктура) и состоянием сессии/выполнения. Граница гарантирует, что runtime-инфраструктура не накапливает эфемерные данные и что переходы жизненного цикла детерминированы.

## Scope

`agent` — точка сборки сервисов агента
Реализация: `lib/core/application_context.py`

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
- доступ к ConfigService
- детерминированный lifecycle (start/stop)
- изоляцию от session state

## Requirements

### Requirement: Единый корень общей инфраструктуры

Система ДОЛЖНА предоставлять `ApplicationContext` как единственную точку сборки runtime-сервисов.

`ApplicationContext.create()` MUST иметь typed signature **БЕЗ** параметров `profile`, `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls`. Эти параметры НЕ ДОЛЖНЫ быть в `def create(..., *, ...):` typed signature.

**`profile` MUST быть разрешён ДО `ApplicationContext.create()`** через `config._initialize_settings(profile=...)`. `ApplicationContext.create()` MUST NOT принимать `profile` как параметр — профиль — это configuration-resolution concern, не runtime concern. После `_initialize_settings(profile=...)` runtime получает уже resolved `SETTINGS` через `SETTINGS["profile"]`.

Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MAY приниматься через `**kwargs` для обратной совместимости. Если kwarg передан — используется + `warnings.warn(..., DeprecationWarning, stacklevel=2)`. Если не передан — читается из `SETTINGS["gateway"].*`.

CLI и gateway вызывают `ApplicationContext.create(...)` с одинаковой typed signature; различие только в обязательном kwarg `role` и runtime-флагах `storage_override`, `session_override` (последние два — только из CLI).

Deprecated kwargs через `**kwargs` MUST быть удалены в MINOR релизе после раскрытия этого change (см. отдельный change `remove-deprecated-enable-kwargs`). См. `runtime/entrypoints` для полного контракта.

#### Scenario: Сервисы подключаются через ApplicationContext

- **КОГДА** требуется runtime-сервис
- **ТОГДА** он ДОЛЖЕН быть получен через `ApplicationContext` или его документированный аксессор, а не создан ad hoc

#### Scenario: profile resolved ДО ApplicationContext.create()

- **КОГДА** application entrypoint стартует
- **ТОГДА** он MUST вызвать `config._initialize_settings(profile=...)` ПЕРЕД `ApplicationContext.create(...)`
- **И ДОЛЖЕН** полагаться на global `SETTINGS`, опубликованный `_initialize_settings()`
- **И НЕ ДОЛЖЕН** передавать `profile` как параметр в `ApplicationContext.create(...)`
- **AND** `ApplicationContext.create()` MUST потреблять (consume) уже-resolved global `SETTINGS` через `import config as _config; ctx_settings = _config.SETTINGS`
- **AND** `ApplicationContext.create()` MUST NOT resolve profile самостоятельно и MUST NOT принимать `settings` как параметр

#### Scenario: CLI и gateway используют одну typed signature create()

- **КОГДА** `cli_agent.py` и `gateway.py` инициализируют runtime
- **ТОГДА** оба entrypoint'а MUST вызывать `ApplicationContext.create(...)` БЕЗ `profile`-параметра
- **И ДОЛЖНЫ** передавать только `role`, `storage_override`, `session_override` (последние два — только из CLI)
- **И НЕ ДОЛЖНЫ** зависеть от того, передал ли caller `enable_*`-флаги через `**kwargs`

#### Scenario: Утилиты и тесты могут использовать **kwargs для backward compat

- **КОГДА** `tools/build_vectors.py` или тест вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **ТОГДА** система MUST принять этот kwarg, использовать значение и залогировать `DeprecationWarning`
- **И НЕ ДОЛЖНА** падать с `TypeError: unexpected keyword argument`

### Requirement: Состояние сессии вне ApplicationContext

Система ДОЛЖНА хранить состояние сессии (сообщения, метаданные, per-turn deltas) в менеджере сессий (`SessionManager` поверх `SanitizingSessionStore`) или канальном слое, но НЕ в `ApplicationContext`.

#### Scenario: Поиск сессии

- **КОГДА** требуются метаданные сессии
- **ТОГДА** система ДОЛЖНА прочитать их из менеджера сессий, а не из атрибутов `ApplicationContext`

### Requirement: Детерминированный жизненный цикл

Система ДОЛЖНА определять детерминированный жизненный цикл `ctx.start()` / `ctx.stop()`, порядок которого НЕ ДОЛЖЕН зависеть от вызывающей стороны или активного профиля.

#### Scenario: Независимый жизненный цикл

- **КОГДА** два вызывающих лица вызывают `ctx.start()` параллельно
- **ТОГДА** порядок жизненного цикла ДОЛЖЕН быть детерминированным и НЕ ДОЛЖЕН различаться между запусками

### Requirement: Совместимость с upstream nanobot

ИЗМЕНЕНО нормативное место вызова `RuntimePatcher.apply_all()` —
фактически это composition-фаза `ApplicationContext.create()`, а не
фоновый lifecycle `ApplicationContext.start()`. Также ИЗМЕНЕНО
утверждение про failed-патчи: `required=True` — это metadata для
diagnostics, а НЕ триггер startup-abort. Требование ниже полностью
заменяет ранее существовавшее требование «Совместимость с upstream
nanobot» в `openspec/specs/runtime/context/spec.md`.

`ApplicationContext.create()` MUST вызывать `RuntimePatcher.apply_all`
после инициализации сервисов и до того, как `ApplicationContext`
отдаёт `ctx.agent` внешним потребителям (gateway, CLI).

`ApplicationContext.start()` SHALL NOT вызывать
`RuntimePatcher.apply_all()` или отдельные `patch_*` методы
`RuntimePatcher`, входящие в `apply_all`. Существующий
lifecycle `start()` (template overrides, `_start_db_pool()`,
`_validate_runtime_schema()`, старт `db_logging_service`,
`sync_service`, `session_cold_sync_service`,
`RuntimeEventsSubscriber`) сохраняется без изменения —
эта change **не** рефакторит `start()` (см. Non-Goals в
`design.md`). Граница фиксируется только в части runtime
patches: `start()` их не применяет, ни прямо, ни косвенно.

**Семантика failed-патчей и `PatchSpec.required`:**

`PatchSpec.required: bool` — это metadata для diagnostics
(startup-баннер, `diff_runtime_patches()`, `tools/diagnose_startup.py`),
а **НЕ** триггер прерывания startup. Если `apply_all` оставляет
непустой `report.failed`, система MUST логировать warning со
всеми именами failed-патчей (включая те, у которых
`PatchSpec.required=True`, — для оператора), и MUST NOT
прерывать startup. Это поведение уже реализовано в
`lib/core/application_context.py:352-357` (только
`logger.warning(... %d runtime patch(es) failed ...)`).

Утверждение «каждый failed-патч явно помечен DEPRECATED и не
критичен для прод» из старой версии спеки — НЕВЕРНО: failed-патч
может иметь `required=True` (например, если upstream-метод изменился
и сломалась приватная обёртка), и это должно быть явно видно
оператору через warning-лог, но не должно прерывать startup.

#### Scenario: Апгрейд upstream-nanobot без регрессии

- **WHEN** версия `nanobot-ai` в `requirements.txt` меняется
- **THEN** `pytest tests/contract/` MUST запускаться первым; если
  есть падения, они MUST быть исправлены или явно помечены `xfail`
  до merge upgrade-изменения.

#### Scenario: `apply_all` вызывается ровно один раз и только в `create()`

- **WHEN** `ApplicationContext.create()` завершается успешно
- **THEN** `RuntimePatcher.apply_all` MUST быть вызван ровно один
  раз, и `ctx.agent._assemble_outbound` MUST содержать ровно один
  project wrapper layer.
- **AND** `ApplicationContext.start()` MUST NOT вызывать
  `RuntimePatcher.apply_all` ни прямо, ни через отдельные
  `patch_*` методы `RuntimePatcher`.
- **AND** внешние entrypoint'ы (`cli_agent.py`, `gateway.py`)
  MUST NOT вызывать `RuntimePatcher.apply_all`
  или отдельные `patch_*` методы, входящие в `apply_all`, после
  возврата из `create()`.

#### Scenario: Failed-патч с `required=True` логируется, но не прерывает startup

- **WHEN** `apply_all` оставляет `report.failed` и один из failed-патчей
  имеет `PatchSpec.required=True` (например, `assemble_outbound`
  сломался из-за изменения сигнатуры upstream-метода)
- **THEN** система MUST логировать `logger.warning(...)` с именем
  этого патча и общим списком failed-патчей (для оператора).
- **AND** система MUST NOT выбрасывать исключение, MUST NOT
  прерывать startup, MUST NOT вызывать `sys.exit`.
- **AND** тот факт, что `required=True`-патч fail'нул, остаётся
  видимым через warning-лог и через startup-баннер
  `PatchReport.render(...)`.

#### Scenario: `required=True` НЕ означает startup-abort

- **WHEN** разработчик читает `PatchSpec.required` и пытается
  добавить raise/abort на failed required-патче
- **THEN** тест `tests/test_application_context.py`
  (или эквивалентный) должен явно проверять, что
  `ApplicationContext.create()` НЕ выбрасывает исключение при
  failed `required=True`-патче, и startup продолжается с
  warning-логом.

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
- CLI/Gateway — точки входа приложения

## Implementation

Основная реализация:
- `lib/core/application_context.py:ApplicationContext`

Связанные компоненты:
- `lib/services/config_service.py:ConfigService`

## Verification

Валидация включает:
1. Проверка отсутствия session state в ApplicationContext (code review)
2. Проверка детерминированности lifecycle (тесты на concurrent start)
3. Проверка отсутствия profile-specific веток в коде ApplicationContext
