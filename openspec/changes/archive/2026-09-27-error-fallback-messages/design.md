## Context

В upstream-`nanobot` (`nanobot.agent.turn_delivery.TurnDelivery.fail`, файл `site-packages/.../turn_delivery.py:336-353`) при любом `Exception` в `AgentLoop._process_message` (`site-packages/.../loop.py:1480`) пользователю уходит `OutboundMessage(content="Sorry, I encountered an error.", ...)`. Этот литерал:

- **Не локализован** (русскоязычный деплой показывает английский текст);
- **Не конфигурируем** — нет ключа в `project.json`, нет ENV, нет API;
- **Не observability-aware** — `_process_message` пишет `loguru.exception(...)`, но детали (тип/сообщение исключения) не попадают в `agent_gateway_logs`, поэтому оператор видит только `loguru`-логи;
- **Не отличим от нормального ответа** — ни `OutboundMessage.metadata._error_kind`, ни что-либо ещё.

`TurnDelivery` создаётся per-turn внутри upstream-кода; monkey-patch метода инстанса `del.fail` невозможен (метод на инстансе создаётся в момент `TurnDelivery.__init__` или прямо в теле `complete`/`fail`?). Нужно проверить, патчится ли класс или инстанс.

## Goals / Non-Goals

**Goals:**

- Перехватить `TurnDelivery.fail` ровно в одной точке runtime (через `RuntimePatcher`, как и остальные monkey-patch'и к nanobot).
- Дать оператору возможность задать `gateway.error_messages.internal_error` в `project.json` без перезапуска/форка nanobot.
- Писать детали (`exception_type`, `exception_message`, `session_key`, `channel`, `chat_id`) в `agent_gateway_logs` через существующий `DbLoggingService` (fail-open при отсутствии сервиса).
- Не нарушать публичный контракт `OutboundMessage` (форма/метаданные сохраняются; `turn_completed` event по-прежнему публикуется).
- Сохранить `asyncio.CancelledError` path нетронутым (graceful shutdown).

**Non-Goals:**

- Per-channel тексты (один общий — минимальный объём).
- Детальный текст исключения в финальном ответе (`include_details=false` жёстко зашит; см. proposal.md «Как быть с детальным текстом»).
- Какие-либо изменения upstream-`nanobot` (форки, monkey-patch метода инстанса `_process_message` — слишком хрупко, перехват `TurnDelivery.fail` достаточно).
- Изменение `complete()` для `response is None + cli` (пустой outbound в CLI уже корректен — см. proposal.md «Что НЕ делаем»).
- Новые обязательные ключи `project.json` (вся секция опциональна, default работает «из коробки»).

## Decisions

### D1. Точка перехвата — класс `TurnDelivery`, не инстанс

**Решение:** патчить `TurnDelivery.fail` как unbound-метод класса (через `TurnDelivery.fail = _wrap`).

**Альтернативы, отвергнутые:**

- *Патч инстанса* (`delivery.fail = wrapper`): создаёт per-turn утечку ссылок и проблемы с GC; не работает, если `TurnDelivery` создаёт новый инстанс в `complete()` (нужно проверить, но безопаснее — на классе).
- *Патч `_process_message` целиком*: слишком грубо — изменяет hot-path обработки каждого сообщения, легко сломать `asyncio.CancelledError`-ветку.
- *Подписка на `TurnFailedEvent`*: такого события нет в upstream; пришлось бы перехватывать `_process_message`.

**Проверка перед имплементацией:** нужно убедиться (через `read`), что `TurnDelivery.fail` — обычный метод, который вызывается как `self.fail(...)` (по upstream-коду в `_process_message` — да, строка `await delivery.fail(...)`). Патч класса полностью подменяет диспетчеризацию для всех будущих инстансов.

### D2. Чтение конфига — через `settings.gateway.error_messages`, фоллбек — константа

**Решение:** `RuntimePatcher.patch_turn_delivery_fail(settings, ...)` принимает `settings` параметром (как другие patch'и в этом файле). Резолв:

1. Берём `settings.gateway.error_messages` через `get_path` (`lib.utils.node_access.get_path`) с default-значением;
2. `internal_error` → default `"Произошла внутренняя ошибка. Попробуйте позже."`;
3. `log_to_db` → default `True`.

Если `settings is None` (юнит-тест без `ApplicationContext`) — используем ту же захардкоженную константу из `_DEFAULT_INTERNAL_ERROR_TEXT` в начале `patch_turn_delivery_fail`. **Fail-open:** отсутствие настроек ≠ отказ от патча.

### D3. Запись в БД — через `try_log_event` (defensive)

**Решение:** используем `lib.services.db_logging_service.try_log_event(svc, log_event, producer="runtime_patcher", event_type="turn_failed")` вместо прямого `svc.log_event`. Это:

- даёт fail-open при `svc is None` или `not svc.is_running()` (WARNING в `loguru`, событие теряется, оборот НЕ падает);
- нормализует payload через `_json_safe` (уже внутри `LogEvent`/`log_event`);
- переиспользует существующий contract (см. `lib.services.db_logging_service.py:34`).

Payload `LogEvent`:

```python
LogEvent(
    event_type="turn_failed",
    level="ERROR",
    session_id=<session_key>,
    channel=<channel>,
    summary=f"{exception_type}: {exception_message[:120]}",
    payload={
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        "agent_id": <agent_id>,
        "kind": "internal",
    },
)
```

**Альтернативы:** прямой `svc.log_event(...)` без defensive обёртки — отвергнуто: при сбое DB-pool'а оборот агента упадёт и fallback-сообщение тоже не уйдёт.

### D4. Маркер в metadata — `_error_kind="internal"`

**Решение:** в формируемом `OutboundMessage` ставим `metadata["_error_kind"] = "internal"`. Это позволяет downstream-каналам (CLI typewriter, Streamlit `st.error(...)`) при желании стилизовать отрисовку, не ломая существующие потребители.

**Почему `_`-префикс:** уже используется для служебных ключей (`_tool_audit`, `_stop_reason`, `_final_turn`); не конфликтует с пользовательскими данными.

### D5. Структура новой секции

```jsonc
{
  "gateway": {
    "error_messages": {
      // "internal_error": "Произошла внутренняя ошибка. Попробуйте позже.",
      // "log_to_db": true
    }
  }
}
```

**Почему минимум:** только `internal_error` и `log_to_db` — этого достаточно для текущей задачи. Per-channel/per-language/per-error-kind тексты — отдельные change'ы при реальной потребности.

### D6. Расположение кода

- `lib/core/project_settings.py` — `ErrorMessagesSettings` (`_StrictOptional`) + поле `error_messages: ErrorMessagesSettings | None = None` в `GatewaySettings`.
- `lib/services/runtime_patcher.py`:
  - новый метод `patch_turn_delivery_fail(settings, db_logging_service, agent_id=None)` рядом с `patch_assemble_outbound`;
  - регистрация в `apply_all(...)`: `self._record(report, "turn_delivery_fail", self.patch_turn_delivery_fail(settings, db_logging_service, agent_id=None))` — **после** `assemble_outbound`, **до** `subagent_logging` (логически относится к outbound-патчам).
- `lib/core/application_context.py` — без правок: `RuntimePatcher.apply_all(...)` уже принимает `db_logging_service`; `agent_id` доступен через `ctx.config.agent_id` (нужно проверить путь) или дополнительный kwarg.

### D7. Тесты — изоляция от upstream через unittest.mock

**Решение:** тесты используют `unittest.mock.patch` на `TurnDelivery` (или мокают сам модуль `nanobot.agent.turn_delivery` через `sys.modules`), чтобы не требовать живой инстанс `TurnDelivery`. Это позволяет тестам быть быстрыми и не зависеть от того, как именно upstream создаёт инстансы.

Сценарии тестов:

1. `test_patch_turn_delivery_fail_default_text` — без `settings`, без `db_logging_service` → `outbound.content == _DEFAULT_INTERNAL_ERROR_TEXT`, `metadata["_error_kind"] == "internal"`, исключение не упало.
2. `test_patch_turn_delivery_fail_custom_text` — `settings.gateway.error_messages.internal_error = "..."` → `outbound.content == "..."`.
3. `test_patch_turn_delivery_fail_writes_to_db` — мок `DbLoggingService`; проверяем `try_log_event` вызван с `event_type="turn_failed"` и правильным payload.
4. `test_patch_turn_delivery_fail_log_to_db_false` — `log_to_db=false` → `try_log_event` НЕ вызван.
5. `test_patch_turn_delivery_fail_no_db_service` — `db_logging_service=None` → fallback-сообщение всё равно ушло, исключение не упало (fail-open).
6. `test_patch_turn_delivery_fail_cancelled_error_passthrough` — `asyncio.CancelledError` идёт в upstream (патч не вмешивается в CancelledError-ветку `_process_message`, т.к. перехват именно на `fail`, а CancelledError его не вызывает).
7. `test_project_settings_error_messages_default` — секция отсутствует → `ProjectSettings(...).gateway.error_messages is None`.
8. `test_project_settings_error_messages_validation` — `internal_error: int` → `ConfigurationError` на старте.

## Risks / Trade-offs

- **[R1] Изменение сигнатуры/поведения upstream `TurnDelivery.fail` при обновлении nanobot** → Mitig: патч в try/except (`return False, "..."` при `AttributeError`); на апгрейде тест `test_patch_turn_delivery_fail_*` сразу покажет расхождение.
- **[R2] Параллельные инстансы `TurnDelivery` в одном процессе** (теоретически — per-session) → Mitig: патч класса, не инстанса; один патч покрывает все. Тест на concurrent-сценарий необязателен (см. [Open Questions](#open-questions)).
- **[R3] `db_logging_service.try_log_event` падает по таймауту PG-пула** → Mitig: `try_log_event` оборачивает в свой try/except и возвращает `False` без raise; оборот не падает.
- **[R4] Оператор случайно ставит `internal_error: ""` (пустая строка)** → Mitig: Pydantic-валидация не запрещает пустую строку, но runtime всё равно отправит её (валидное поведение — оператор сам решит, что показать). Не добавляем отдельный валидатор, чтобы не размывать контракт.
- **[R5] Детали исключения содержат PII или большие тексты** → Mitig: `exception_message[:120]` в `summary`; полный текст — в `payload.exception_message` (без truncation). Это приемлемо: `agent_gateway_logs` уже хранит другие события с payload, retention управляется `logging.db.retention_days`.

## Migration Plan

Миграция **не требуется**:

- Существующие `project.json` без `gateway.error_messages` → работают как раньше, только получают русский default вместо английского литерала.
- `agent_gateway_logs` принимает любые `event_type` (нет enum-ограничения) — `"turn_failed"` просто новое значение существующего столбца.
- Каналы (`PostgresChannel`, `RedisChannel`, `ConsoleLoop`, `Streamlit`) не меняются — форма `OutboundMessage` идентична.

Откат (если нужно): revert коммита → `RuntimePatcher.apply_all` просто не зовёт `patch_turn_delivery_fail` → upstream `"Sorry, I encountered an error."` возвращается.

## Open Questions

Нет. Все решения, которые могли бы поменять спеку или task breakdown, разрешены с пользователем (default-текст, формат details, формат error_kind, источник записи ошибки, per-channel тексты, fallback при отсутствии SETTINGS).