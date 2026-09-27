# storage/usage-store Specification

## Purpose
Определяет нормативный контракт использования upstream
`nanobot.llm_usage.store.LLMUsageStore` для записи content-free
metadata-only LLM usage (provider, model, duration_ms,
token counts, finish_reason). Стор — SQLite WAL, инициализируется
в `ApplicationContext` и подключается через observer-pipeline
(`provider.set_llm_call_observer(store.record)` или эквивалентный
hook в nanobot 0.3.5). Контракт фиксирует раздельную
семантику с `DbLoggingService`: `LLMUsageStore` — content-free
per-call metadata, `DbLoggingService` — content-rich audit-trail.

## Requirements

### Requirement: LLMUsageStore подключается через observer-pipeline

`ApplicationContext` MUST инстанциировать
`nanobot.llm_usage.store.LLMUsageStore(path)` (где `path`
указывает на SQLite WAL-файл в `get_runtime_subdir("usage")`
или иной документированный runtime-путь) и MUST подключить
его через `provider_snapshot_loader` callback в nanobot
0.3.5. Это MUST быть реализовано через обёртку
`wrap_provider_snapshot_loader(base_loader, store)`, которая
для каждого загруженного `ProviderSnapshot` вызывает
`snapshot.provider.set_llm_call_observer(store.record)`.
Для `FallbackProvider` дополнительно вызывается
`snapshot.provider.set_fallback_model_observer(...)` (по
образцу `nanobot.cli.gateway_runtime._observe_provider`).

Подключение MUST происходить ДО первого LLM-вызова (через
передачу обёрнутого loader'а в `AgentLoop.from_config(...)`
в `ApplicationContext.start()`).

Обоснование механизма: проект использует кастомный
`gateway.py` и `ApplicationContext`, а не upstream CLI
`nanobot.cli.gateway_runtime`, где observer подключается
автоматически. `AgentLoop.from_config(...)` создаёт
`provider` внутри и не возвращает его наружу, поэтому
единственный стабильный путь подключения —
оборачивание `provider_snapshot_loader` callback'а,
который `AgentLoop` вызывает сам для получения
`ProviderSnapshot`.

#### Scenario: UsageStore записывает LLM call metadata

- **WHEN** LLM provider завершает вызов с metadata
  `{started_at_ms, duration_ms, provider, model, source,
  stream, finish_reason, usage, error_status_code, error_kind}`
- **THEN** observer-pipeline вызывает `store.record(LLMCallRecord(...))`
  и запись попадает в SQLite WAL-файл.
- **AND** `DbLoggingService` НЕ получает отдельную запись
  `event_type="llm_usage"` с тем же payload (это разные
  concerns — LLMUsageStore хранит metadata, DbLoggingService —
  content-rich события, к которым usage не относится).

#### Scenario: UsageStore недоступен — LLM-вызов продолжается

- **WHEN** `LLMUsageStore.record(...)` бросает исключение
  (например, SQLite file locked, disk full)
- **THEN** observer-pipeline MUST проглотить исключение
  и залогировать WARNING через `logger.warning(...)`.
- **AND** LLM-вызов MUST НЕ прерываться (usage tracking —
  observability concern, не business-critical).

#### Scenario: Подключение через wrap_provider_snapshot_loader

- **WHEN** `ApplicationContext` создаёт `AgentLoop` через
  `from_config(...)` и передаёт
  `provider_snapshot_loader=wrap_provider_snapshot_loader(base, store)`
- **THEN** при первом вызове loader'а (внутри `AgentLoop`)
  обёртка загружает `ProviderSnapshot` через `base(...)`
  и ВЫЗЫВАЕТ `snapshot.provider.set_llm_call_observer(store.record)`
  ДО возврата снапшота.
- **AND** если `snapshot.provider` — `FallbackProvider`,
  обёртка дополнительно вызывает
  `snapshot.provider.set_fallback_model_observer(...)`.
- **AND** при последующих вызовах loader'а (lazy reload
  или смена model preset) обёртка повторно подключает
  observer для нового снапшота.

#### Scenario: Проект не использует nanobot.cli.gateway_runtime

- **WHEN** разработчик читает спеку и ищет, где происходит
  автоматическое подключение `record_llm_call`
- **THEN** в коде проекта (`lib/`, `workspace/`) НЕТ
  вызовов `nanobot.cli.gateway_runtime._observe_provider`
  или прямых вызовов `set_llm_call_observer` на
  `provider`-объекте (потому что `AgentLoop` не возвращает
  `provider` наружу).
- **AND** подключение происходит через
  `wrap_provider_snapshot_loader` в `ApplicationContext`,
  как описано в предыдущем сценарии.
- **AND** контрактный тест `tests/contract/test_llm_observer_api.py`
  фиксирует `LLMCallObserver` type alias и fail-open
  семантику `record_llm_call` upstream'а.

#### Scenario: Fail-soft при ошибке attach observer

- **WHEN** `wrap_provider_snapshot_loader` вызывает
  `set_llm_call_observer(store.record)` и метод бросает
  исключение (теоретический случай; на практике upstream
  просто присваивает поле)
- **THEN** обёртка логирует WARNING через `loguru`:
  `"LLMUsageStore observer failed to attach: {exc}"`
- **AND** возвращает `ProviderSnapshot` БЕЗ observer —
  агент продолжает работать, теряется только telemetry.
- **AND** метрика `llm_observer_attached=0` экспортируется
  в `RuntimeHealth.get_stats()` (см.
  `lib/services/runtime_health.py`).
- **AND** `ApplicationContext.start()` НЕ падает с
  необработанным исключением.

#### Scenario: FallbackProvider observer fan-out

- **WHEN** `snapshot.provider` — `FallbackProvider`
- **THEN** `FallbackProvider.set_llm_call_observer(observer)`
  пробрасывает observer в `self._primary` (через
  `super().set_llm_call_observer(observer)` +
  `self._primary.set_llm_call_observer(observer)`).
- **AND** при активации secondary (`self._provider_factory(fallback)`
  внутри fallback-flow) `fallback_provider.set_llm_call_observer(
  self._llm_call_observer)` вызывается явно (подтверждено
  через интроспекцию `nanobot/providers/fallback_provider.py`).
- **AND** это значит, что **observer корректно propagate
  на active secondary в момент фейловера**, не только
  на момент attach.
- **AND** контрактный тест `tests/contract/test_llm_observer_api.py
  ::test_fallback_provider_propagates_observer` проверяет
  оба случая:
  - `provider._primary.observer == store.record`
    после attach;
  - `secondary.observer == store.record` после создания
    secondary через `provider._provider_factory(...)`.

### Requirement: Content-free контракт LLMCallRecord

`LLMUsageStore` MUST использоваться строго для content-free
metadata-only записей согласно upstream-контракту
(`LLMCallRecord` имеет поля `started_at_ms`, `duration_ms`,
`provider`, `model`, `source`, `stream`, `finish_reason`,
`usage`, `error_status_code`, `error_kind` — НЕ
`messages`, `response_text`, `reasoning_content`,
`tool_payloads`). Запрос на расширение `LLMCallRecord`
новыми content-полями ЗАПРЕЩЁН в рамках этого change —
это нарушение upstream-контракта.

#### Scenario: LLMCallRecord не содержит content

- **WHEN** observer-pipeline формирует `LLMCallRecord`
  из результата LLM-вызова
- **THEN** поля `started_at_ms`, `duration_ms`, `provider`,
  `model`, `source`, `stream`, `finish_reason`, `usage`
  MUST присутствовать.
- **AND** поля `prompt_messages`, `response_text`,
  `reasoning_content`, `tool_payloads` MUST NOT
  присутствовать (эти данные хранятся в `Session.messages`).

#### Scenario: usage_payload использует upstream API

- **WHEN** WebUI запрашивает usage charts
  (`store.usage_payload(days=N)`)
- **THEN** данные читаются через upstream API
  `LLMUsageStore.usage_payload(...)` /
  `recent_calls(...)`.
- **AND** НЕ выполняется прямой SQL `SELECT` в SQLite-файл
  usage-стора из нашего кода (только upstream API).

### Requirement: Single-writer per layer в usage tracking

`LLMUsageStore` MUST быть единственным writer'ом
metadata-only LLM usage. `DbLoggingService` НЕ ДОЛЖЕН
принимать на себя функции usage-стора и наоборот.
Разделение concerns:

- `LLMUsageStore` — per-call metadata для UI-графиков и
  cost-tracking (SQLite WAL, content-free).
- `DbLoggingService` — content-rich audit-trail для
  `history_search` и observability (`agent_gateway_logs` в PG,
  content-rich с payload).

Параллельная запись в оба стора ЗАПРЕЩЕНА для одного и того
же `event_type="llm_usage"` — это должно быть либо metadata
в `LLMUsageStore` (предпочтительно), либо content-rich в
`DbLoggingService`, не оба одновременно.

#### Scenario: нет двойной записи llm_usage

- **WHEN** LLM provider завершает вызов
- **THEN** upstream observer-pipeline записывает
  `LLMCallRecord` в `LLMUsageStore`.
- **AND** `DbLoggingService.log_event(LogEvent(event_type="llm_usage", ...))`
  НЕ вызывается параллельно — content-rich version того же
  вызова уже доступна через `Session.messages` upstream.

#### Scenario: DbLoggingService не эмитит llm_usage

- **WHEN** `DbLoggingService` пишет structured event
- **THEN** `event_type="llm_usage"` MUST NOT использоваться
  для content-free metadata-only записей.
- **AND** если нужно записать content-rich вариант
  (например, tool-call с metadata о LLM-вызове) —
  используется `event_type="tool_call"` / `event_type="llm_call"`
  с полным payload согласно спеке `logging-db`.

### Requirement: Контрактные тесты на LLMUsageStore API

Использование upstream `LLMUsageStore` MUST быть покрыто
контрактными тестами, фиксирующими публичный API nanobot
0.3.5: конструктор `(path: Path)`, методы `record(LLMCallRecord)`,
`record_many(iterable)`, `recent_calls(limit)`,
`usage_payload(days, timezone_name, now)`,
`count()` и `close()`. Тесты MUST запускаться первыми при
апгрейде upstream и MUST падать, если upstream меняет
совместимый API.

#### Scenario: LLMUsageStore конструктор принимает Path

- **WHEN** контрактный тест проверяет
  `inspect.signature(LLMUsageStore.__init__)`
- **THEN** сигнатура MUST быть `(self, path: Path) -> None`.
- **AND** тест MUST падать при изменении сигнатуры.

#### Scenario: LLMUsageStore.record принимает LLMCallRecord

- **WHEN** контрактный тест проверяет
  `inspect.signature(LLMUsageStore.record)`
- **THEN** параметр MUST называться `call` и иметь тип
  `LLMCallRecord`.
- **AND** `LLMCallRecord` MUST быть `@dataclass(frozen=True, slots=True)`
  с полями согласно upstream-контракту.

### Requirement: Конфигурация UsageStore

Путь к SQLite-файлу `LLMUsageStore` MUST быть
конфигурируемым через `project.json::gateway.usage_store.sqlite_path`
(опционально, дефолт — `<get_runtime_subdir("usage")>/usage.db`).
Никаких других ключей конфигурации для `LLMUsageStore` в
рамках этого change: размер retention, WAL-mode,
synchronous-уровень управляются upstream (на момент 0.3.5 —
WAL включён, `MAX_CALLS_RETAINED` и `MAX_DAYS_RETAINED`
hardcoded в `nanobot.llm_usage.store`).

#### Scenario: дефолтный путь UsageStore

- **WHEN** `ApplicationContext.start()` создаёт
  `LLMUsageStore` и `gateway.usage_store.sqlite_path`
  не задан
- **THEN** используется путь
  `get_runtime_subdir("usage") / "usage.db"`.
- **AND** родительский каталог создаётся через `ensure_dir`.

#### Scenario: явный путь UsageStore

- **WHEN** `gateway.usage_store.sqlite_path` задан в
  `project.json` (например, `"~/.cache/nanobot/usage.db"`)
- **THEN** `LLMUsageStore` создаётся по этому пути.
- **AND** путь MUST расширяться через `Path(...).expanduser()`
  для поддержки `~`.
