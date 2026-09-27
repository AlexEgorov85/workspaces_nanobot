# Error Fallback Messages

## Why

В `nanobot.agent.turn_delivery.TurnDelivery.fail` (upstream, `site-packages/.../turn_delivery.py:336-353`) при любом `Exception` в `AgentLoop._process_message` (`site-packages/.../loop.py:1480`) пользователю уходит хардкод `"Sorry, I encountered an error."`. На русскоязычных деплоях это выглядит как сырой англоязычный ответ, не согласовано с tone-of-voice других ответов и не поддаётся редактированию без форка nanobot. Хотим завести конфигурируемый заготовленный текст в `project.json::gateway.error_messages.internal_error`, который подставляется вместо литерала, а детали исключения пишутся в `agent_gateway_logs` (через `DbLoggingService`) для диагностики — без утечки внутреннего traceback пользователю.

## What Changes

- Добавляется опциональная секция `gateway.error_messages.*` в `project.json` (Pydantic-валидация в `lib/core/project_settings.py`): один строковый ключ `internal_error` (default `"Произошла внутренняя ошибка. Попробуйте позже."`) и bool-флаг `log_to_db` (default `true`).
- В `lib/services/runtime_patcher.py` появляется новый patch `patch_turn_delivery_fail`: подменяет `TurnDelivery.fail` обёрткой, которая читает `error_messages` из SETTINGS, формирует `OutboundMessage` с `content=internal_error` и `metadata._error_kind="internal"`, вызывает оригинальный `fail` (для `turn_completed` event), и при `log_to_db=true` пишет `event_type="turn_failed"` в `agent_gateway_logs` через существующий `DbLoggingService`.
- Patch регистрируется в `RuntimePatcher.apply_all(...)` после `patch_assemble_outbound` и применяется в `ApplicationContext.start()` (вместе со всем набором monkey-patch'ей).
- В `project.json` добавляется закомментированный пример секции `gateway.error_messages` с дефолтами для discoverability.
- Документация: `docs/TARGET_ARCHITECTURE.md` (§ runtime contract) и `AGENTS.md` (секция «Configuration») получают описание `gateway.error_messages.*`.

**Non-breaking:** старая строка `"Sorry, I encountered an error."` заменяется на русский default; формат `OutboundMessage` не меняется; `turn_completed` event по-прежнему публикуется; `agent_gateway_logs` остаётся источником деталей (раньше детали не писались вообще — только `loguru.exception`).

## Capabilities

### New Capabilities

- `runtime/error-fallback`: контракт пользовательского fallback-сообщения при необработанном исключении в `AgentLoop._process_message` — конфигурируемый текст, поведение observability, поведение при недоступности SETTINGS.

### Modified Capabilities

Нет. Существующие спеки (`runtime/context`, `tools-history-search`, `data/cache-provider`, `configuration/profiles`, `architecture/skill-tool-boundary`, `architecture/component-model`, `data/vector-indexes`) описывают смежные, но не пересекающиеся контракты: error-fallback — это новый runtime-механизм, не модификация существующего.

## Impact

- **Код:**
  - `lib/core/project_settings.py` — добавить `ErrorMessagesSettings` + поле в `GatewaySettings`.
  - `lib/services/runtime_patcher.py` — добавить `patch_turn_delivery_fail`, зарегистрировать в `apply_all(...)` и в `_record(...)` (для `runtime-patcher-inventory.md`).
  - `lib/core/application_context.py` — без изменений: patch подключается через общий `RuntimePatcher.apply_all(...)`.
  - `tests/test_runtime_patcher.py` — новый `TestPatchTurnDeliveryFail` (default / custom / missing SETTINGS / no db_logging_service).
  - `tests/test_project_settings.py` — новый `TestErrorMessagesSettings` (валидация `internal_error: str` + `log_to_db: bool` + отсутствие секции = default).
- **Конфиг:** `project.json` — закомментированный пример `gateway.error_messages` (после `gateway.session_cold_sync`).
- **Документация:** `docs/TARGET_ARCHITECTURE.md` (новый пункт §30.x), `AGENTS.md` (Configuration-секция + Project Layout — `runtime_patcher.py`), `docs/architecture/runtime-patcher-inventory.md` (новый патч).
- **БД:** без миграций. Используется существующий `agent_gateway_logs` (event_type — новый литерал `"turn_failed"`).
- **Зависимости:** нет новых внешних пакетов; только stdlib + уже подключённый `loguru`/`pydantic`.
- **Backwards-compat:** отсутствие `gateway.error_messages` в `project.json` → default-текст + `log_to_db=true`. Никаких правок обязательных ключей. Каналы получают `OutboundMessage` той же формы (`channel`/`chat_id`/`content`/`metadata`); UI продолжает работать без изменений (текст `internal_error` просто длиннее литерала).