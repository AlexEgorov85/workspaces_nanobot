## Why

Change `error-fallback-messages` (архив `2026-09-27-error-fallback-messages`) ввёл `RuntimePatcher.patch_turn_delivery_fail`, который подменяет upstream-ответ при необработанном исключении. Реализация нарушает собственный контракт capability `runtime/error-fallback` (`openspec/specs/runtime/error-fallback/spec.md`) в **пяти** точках:

1. Пользователь получает **два** сообщения: наш fallback + upstream `"Sorry, I encountered an error."`. Комментарий в `runtime_patcher.py:1721-1722` ошибочно утверждает, что оригинал публикует только `turn_completed` — на деле upstream `fail()` (`turn_delivery.py:337-344`) **всегда** публикует outbound с захардкоженным текстом.
2. В `agent_gateway_logs` не пишутся `exception_type`/`exception_message` — требование №3 спеки их предписывает, но `TurnDelivery.fail(self, *, publish_completion: bool)` (`turn_delivery.py:336`) не получает исключение в сигнатуре, а захват через `sys.exception()`/`exc` не реализован.
3. `session_key` и `user_id` читаются из `lifecycle_message.session_key` / `lifecycle_message.user_id` — **этих полей не существует**. `InboundMessage` (`bus/events.py:25-37`) определяет `sender_id` и `session_key_override`, но не `session_key`/`user_id`. Результат: `session_id=None`, `user_id=None` в БД всегда.
4. `agent_id` не передаётся в `patch_turn_delivery_fail` из `apply_all` (`runtime_patcher.py:573-574` вызывает `self.patch_turn_delivery_fail(settings, db_logging_service)` без `agent_id=...`), хотя task 2.3 заархивированного change это требовал.
5. Существующие тесты `tests/test_runtime_patcher.py::TestPatchTurnDeliveryFail` закрепляют баг — они ожидают **два** outbound'а и используют синхронный `MagicMock` для `bus.publish_outbound` (который в реальности `async`), скрывая ещё и багу с `await`.

## What Changes

- **`RuntimePatcher.patch_turn_delivery_fail._wrap_fail`** (`lib/services/runtime_patcher.py:1612-1727`) переписан:
  - Захват активного исключения через `sys.exception()` при входе в обёртку. Вызов `fail()` происходит изнутри `except Exception` в `loop.py:1480-1482`, поэтому `sys.exception()` возвращает активное исключение.
  - Источник `session_key` — `self.session_key` (атрибут `TurnDelivery`, устанавливается через `TurnDelivery.create(msg, session_key, ...)` в `turn_delivery.py:85-103`). Поле `lifecycle_message.session_key` НЕ используется (его не существует).
  - Идентификатор пользователя — `self.lifecycle_message.sender_id` (поле `bus/events.py:29`); `user_id` НЕ используется (его не существует).
  - **Подавление двойного ответа**: на время вызова оригинального `fail()` атрибут экземпляра `self.bus` подменяется на прокси `_OutboundSilencer`. Прокси наследует поведение реального bus через `__getattr__`, но метод `publish_outbound` возвращает `None` без публикации. После возврата `self.bus` восстанавливается через `try/finally`. Per-instance атрибут → безопасно для конкурентных оборотов.
  - Payload в `LogEvent` дополнен: `exception_type`, `exception_message`, `exception_available`, `sender_id`, `agent_id`. Поля `session_id` / `user_id` / `chat_id` / `channel` / `failure_error_kind` сохранены.
- **`RuntimePatcher.apply_all`** (`lib/services/runtime_patcher.py:573-574`): вызов `patch_turn_delivery_fail` теперь передаёт `agent_id` из `config` (по тому же паттерну, что и `patch_assemble_outbound`).
- **`tests/test_runtime_patcher.py::TestPatchTurnDeliveryFail`** — переписаны под `AsyncMock` для `bus.publish_outbound` и `runtime_event_publisher.turn_completed`. Утверждения: ровно один outbound, `content` НЕ содержит upstream-литерала, `turn_completed` вызван при `publish_completion=True`, payload БД содержит обязательные поля.
- **`docs/architecture/runtime-patcher-inventory.md`** — risk записи `turn_delivery_fail` повышен с `low` до `medium`.
- **`CHANGELOG.md`** — запись в `[Unreleased]` в категории `Fixed`.

## Capabilities

### Modified Capabilities
- `runtime/error-fallback`: требование №1 ужесточается (ровно один outbound, upstream-литерал явно запрещён), требование №3 переформулировано (полный диагностический payload с явным перечислением полей и источников).

## Impact

- `lib/services/runtime_patcher.py` (`_wrap_fail`, `apply_all`).
- `tests/test_runtime_patcher.py` (`TestPatchTurnDeliveryFail`).
- `docs/architecture/runtime-patcher-inventory.md`.
- `CHANGELOG.md`.

**Не задеты (находятся в согласии с контрактом):** `lib/core/project_settings.py::ErrorMessagesSettings`, `tests/test_project_settings.py::TestErrorMessagesSettings`, `project.json` (закомментированный пример), `AGENTS.md`, `docs/TARGET_ARCHITECTURE.md` § «Управление сжатием контекста» — рядом. Архивная спека `runtime/error-fallback/spec.md` правится только через дельту этого change при архивировании.
