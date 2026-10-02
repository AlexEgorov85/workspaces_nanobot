# LLM usage tracking: upstream LLMUsageStore + DbLoggingService

Этот документ описывает модель LLM-usage tracking после
`storage-hybridization` (см. OpenSpec change
`storage-hybridization`, спека
`openspec/specs/storage/usage-store/spec.md`).

## Архитектурная диаграмма

```
┌──────────────────────────────────────────────────────────────┐
│                        LLM Provider                           │
│                                                               │
│   provider.chat(...) → LLMCallRecord metadata-only            │
│   (provider, model, source, duration_ms, tokens, finish_reason) │
└─────────────────────────────┬────────────────────────────────┘
                              │ observer-pipeline
                              │ set_llm_call_observer(store.record)
                              ▼
┌──────────────────────────────────────────────────────────────┐
│   nanobot.llm_usage.store.LLMUsageStore (upstream)             │
│   (хранилище создаёт библиотека: get_llm_usage_store)         │
│                                                               │
│   SQLite WAL:                                                 │
│   <get_runtime_subdir("usage")>/usage.db                       │
│   или явный путь (gateway.usage_store.sqlite_path)            │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│   DbLoggingService (lib/services/db_logging_service.py)       │
│   — content-rich structured events                              │
│                                                               │
│   agent_gateway_logs (PG):                                    │
│     tool_call / tool_result / inbound / outbound              │
│     context_compacted / sync_* / run_finished / llm_call      │
│     (НЕ llm_usage — см. «Запрет параллельной записи»)         │
└──────────────────────────────────────────────────────────────┘
```

## Разделение concerns

Два независимых стора для LLM usage:

| Стор | Тип данных | Назначение | Retention |
|---|---|---|---|
| `LLMUsageStore` | Content-free metadata-only | UI-графики, cost-tracking | Upstream (WAL, hardcoded в nanobot 0.3.5) |
| `DbLoggingService` → `agent_gateway_logs` | Content-rich structured events | `history_search`, audit-trail | `logging.db.retention_days` (configurable) |

**Параллельная запись в оба стора ЗАПРЕЩЕНА** для одного и того
же события. `LLMUsageStore` — единственный writer для
metadata-only LLM usage; `DbLoggingService` пишет только
content-rich события (tool_call, tool_result, inbound/outbound,
context_compacted, sync_*).

Если нужна content-rich версия LLM-вызова (например, tool-call с
метаданными LLM-вызова) — используется `event_type="tool_call"`
с полным payload, **НЕ** `event_type="llm_usage"`.

## Подключение observer-pipeline

`ApplicationContext.start()` создаёт `LLMUsageStore` и оборачивает
`provider_snapshot_loader`:

```python
agent = AgentLoop.from_config(
    config,
    bus,
    provider_snapshot_loader=wrap_provider_snapshot_loader(
        base_loader=config.build_provider_snapshot,
        store=self.usage_store,
        bus=self.bus,
    ),
    ...
)
```

При каждом вызове loader'а (внутри `AgentLoop`):

1. Загружается `ProviderSnapshot` через `base_loader(...)`.
2. Вызывается `snapshot.provider.set_llm_call_observer(store.record)`.
3. Если `snapshot.provider` — `FallbackProvider`,
   дополнительно вызывается `set_fallback_model_observer(...)`.
4. Возвращается snapshot.

**Fail-soft**: если `set_llm_call_observer` бросает исключение,
обёртка логирует WARNING через `loguru` и возвращает snapshot
без observer. Агент продолжает работу, теряется только telemetry.

## Контракт `LLMCallRecord` (content-free)

Поля upstream-контракта (см. спеку `storage/usage-store`):

```
LLMCallRecord (frozen dataclass):
  started_at_ms: int
  duration_ms: int
  provider: str
  model: str
  source: LLMUsageSource    # ∈ {"user", "api", "cron", "dream", "system"}
  stream: bool
  finish_reason: str
  usage: LLMUsage | None
  error_status_code: int | None
  error_kind: str | None
```

**Запрещено** расширять `LLMCallRecord` content-полями
(`prompt_messages`, `response_text`, `reasoning_content`,
`tool_payloads`). Эти данные хранятся в `Session.messages`
upstream JSONL.

## Конфигурация UsageStore

`gateway.usage_store.*`:

| Ключ | Тип | Дефолт | Описание |
|---|---|---|---|
| `enabled` | bool | `true` | Включён ли observer. `false` → graceful degradation |
| `sqlite_path` | str \| null | `<get_runtime_subdir("usage")>/usage.db` | Путь к SQLite-файлу |

`Path.expanduser()` поддерживается для `~`.

Если `enabled=false` или секция отсутствует — `_make_usage_store`
возвращает `None`, обёртка `wrap_provider_snapshot_loader` ничего
не делает (см. `tests/test_storage_hybridization_lifecycle.py`).

## Fail-open семантика

`LLMUsageStore.record(...)` обёрнут в
`nanobot.llm_usage.record_llm_call`, который ловит исключения и
логирует WARNING. **LLM-вызов НЕ прерывается** при сбое SQLite
(file locked, disk full и т.п.). Проверяется в
`tests/contract/test_llm_observer_api.py::test_record_llm_call_is_fail_open`.

## Lifecycle

`ApplicationContext.stop()` вызывает `usage_store.close()` после
`shutdown_all()` сервисов и перед `_stop_db_pool()`. Никаких
race conditions — общий пул `utils.db` живёт до `_stop_db_pool`.

## Архитектурный инвариант

`DbLoggingService` НЕ пишет `event_type="llm_usage"`. Проверяется
через `tests/test_storage_hybridization.py::TestNoDbLoggingServiceLLMUsage`:

```python
for path in _iter_python_files(_LIB_ROOT):
    if path in whitelist: continue
    for lineno, line in enumerate(src.splitlines(), start=1):
        if _LLM_USAGE_PATTERN.search(line):
            offenders.append((path, lineno, line))
```

Whitelist — `AgentFactory._wrap_provider_snapshot_loader` (обёртка живёт в
`lib/core/agent_factory.py` инлайном; отдельного модуля observer'а у агента нет).

## Тесты

- `tests/contract/test_usage_store_api.py` — контракт на
  `LLMUsageStore` (конструктор, `record`, `record_many`,
  `recent_calls`, `usage_payload`, `count`, `close`).
- `tests/contract/test_llm_observer_api.py` — контракт на
  observer-pipeline (`set_llm_call_observer`, type alias
  `LLMCallObserver`, fail-open `record_llm_call`, wrap).
- `tests/test_storage_hybridization_lifecycle.py` — mock-smoke
  `wrap_provider_snapshot_loader` (observer attach, None-store).
- `tests/test_storage_hybridization_factory.py` — mock-smoke
  `_make_usage_store` (включая `enabled=False`).
- `tests/test_storage_hybridization.py::TestNoDbLoggingServiceLLMUsage`
  — гард против параллельной записи.

## Файлы

- `lib/services/llm_usage_store_factory.py`, `lib/services/llm_observer.py` —
  **удалены**: хранилище создаёт библиотека
  (`nanobot.llm_usage.get_llm_usage_store()`), а подписка observer'а свёрнута в
  `AgentFactory._wrap_provider_snapshot_loader`.
- `lib/core/application_context.py` — регистрация.
- `lib/core/agent_factory.py` — пробрасывает `usage_store`
  в `AgentLoop.from_config(provider_snapshot_loader=...)`.
- `lib/core/project_settings.py` — `UsageStoreSettings`.
- `lib/services/runtime_health.py` — `RuntimeHealth.get_stats()`
  для базового liveness; метрики usage — через
  `LLMUsageStore.count()` / `recent_calls()`.