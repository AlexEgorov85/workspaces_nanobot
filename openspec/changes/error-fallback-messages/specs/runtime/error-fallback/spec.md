## Purpose

Конфигурируемый заготовленный ответ при необработанном исключении в `AgentLoop._process_message` (upstream-`nanobot`) с записью деталей в долговечный журнал `agent_gateway_logs`. Заменяет захардкоженный `"Sorry, I encountered an error."` в `TurnDelivery.fail` на операторски-редактируемый текст без утечки traceback'а пользователю.

## ADDED Requirements

### Requirement: Подстановка заготовленного текста при internal-ошибке

WHEN `AgentLoop._process_message` ловит `Exception` (любое исключение, кроме `asyncio.CancelledError`) и зовёт `TurnDelivery.fail`,
THEN система SHALL отправить пользователю `OutboundMessage.content` равный `gateway.error_messages.internal_error` из `project.json` (или default-значению, если секция отсутствует).

#### Scenario: Default-текст при отсутствии project.json-секции

- **WHEN** в `project.json` нет `gateway.error_messages.internal_error`
- **THEN** пользователь получает `OutboundMessage.content = "Произошла внутренняя ошибка. Попробуйте позже."`

#### Scenario: Custom-текст из project.json

- **WHEN** в `project.json` указано `gateway.error_messages.internal_error = "Сервис временно недоступен."`
- **THEN** пользователь получает `OutboundMessage.content = "Сервис временно недоступен."`

#### Scenario: Невалидный тип секции

- **WHEN** в `project.json` `gateway.error_messages.internal_error` имеет тип, отличный от `string` (например, число или массив)
- **THEN** старт gateway/CLI падает с `ConfigurationError` (fail-fast на Pydantic-валидации), runtime-до пользователя ошибка не доходит

### Requirement: Метаданные error_kind в финальном outbound

WHEN система формирует fallback-ответ на необработанное исключение,
THEN `OutboundMessage.metadata._error_kind` SHALL быть равен `"internal"`.

#### Scenario: Маркер в metadata

- **WHEN** пользователь получает fallback-ответ
- **THEN** `outbound.metadata["_error_kind"] == "internal"` (отличимо от обычного ответа и от upstream-литерала `"Sorry, I encountered an error."`)

### Requirement: Поведение observability

WHEN система формирует fallback-ответ,
THEN при `gateway.error_messages.log_to_db=true` (default) система SHALL записать в `agent_gateway_logs` запись `event_type="turn_failed"` с payload `{session_key, channel, chat_id, exception_type, exception_message, agent_id}`,
AND при `log_to_db=false` SHALL не писать запись.

#### Scenario: Запись при log_to_db=true

- **WHEN** в `project.json` `gateway.error_messages.log_to_db=true` (или отсутствует — default)
- **THEN** в таблице `agent_gateway_logs` появляется строка с `event_type="turn_failed"` и `payload`, содержащим тип и текст оригинального исключения

#### Scenario: Без записи при log_to_db=false

- **WHEN** в `project.json` `gateway.error_messages.log_to_db=false`
- **THEN** в `agent_gateway_logs` НЕ появляется новая строка для этого `turn_failed`; оригинальное исключение остаётся только в `loguru`

#### Scenario: Отсутствие DbLoggingService

- **WHEN** `DbLoggingService` не зарегистрирован в `ApplicationContext` (например, при `--profile=test` без логирования или в unit-тестах)
- **THEN** fallback-ответ пользователю всё равно отправляется; попытка записи в БД SHALL быть no-op без падения оборота

### Requirement: Сохранение runtime-event публикации

WHEN система формирует fallback-ответ,
THEN система SHALL опубликовать `turn_completed` с `outcome="failed"` и `failure_kind="internal"` (как это делает upstream `TurnDelivery.fail`).

#### Scenario: Postgres-channel корректно финализирует слот

- **WHEN** задача в worker_pool завершилась необработанным исключением и пользователь получил fallback-ответ
- **THEN** PostgresChannel.send получает финальный outbound и помечает задачу как `completed` (а не `processing`), reclaim не срабатывает

### Requirement: Не-применимость для CancelledError

WHEN `AgentLoop._process_message` ловит `asyncio.CancelledError` (shutdown/cleanup path),
THEN поведение SHALL остаться как в upstream: `CancelledError` обрабатывается отдельной веткой (`delivery.abort_stream()` + `restore_runtime_checkpoint`) и НЕ проходит через fallback-обёртку.

#### Scenario: Graceful shutdown без fallback-сообщения

- **WHEN** оператор посылает SIGTERM и активный оборот прерывается
- **THEN** пользователь НЕ получает `"Произошла внутренняя ошибка..."`; ветка `CancelledError` остаётся нетронутой

### Requirement: Отсутствие утечки деталей исключения пользователю

WHEN система формирует fallback-ответ,
THEN `OutboundMessage.content` SHALL содержать ТОЛЬКО текст `gateway.error_messages.internal_error` (без str(exc), traceback, имени функции или module path).

#### Scenario: Только заготовка в content

- **WHEN** исходное исключение — `KeyError("agent_internal_state_xyz")`
- **THEN** пользователь получает `"Произошла внутренняя ошибка. Попробуйте позже."`; в `OutboundMessage.content` НЕТ подстроки `"agent_internal_state_xyz"`, `"KeyError"` или пути к исходнику

### Requirement: Не-регрессия публичного контракта OutboundMessage

WHEN система формирует fallback-ответ,
THEN `OutboundMessage` SHALL сохранить все обязательные поля (`channel`, `chat_id`, `content`, `metadata`) и SHALL быть совместим с downstream-каналами (PostgresChannel, RedisChannel, ConsoleLoop, Streamlit) без изменений в их обработчиках.

#### Scenario: PostgresChannel не падает на fallback

- **WHEN** fallback-ответ публикуется в `bus.publish_outbound`
- **THEN** PostgresChannel.send корректно обрабатывает его как финальный outbound, переводит задачу в `completed`, удаляет claim — никаких изменений в коде канала не требуется