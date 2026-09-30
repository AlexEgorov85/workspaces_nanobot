## MODIFIED Requirements

### Requirement: Подстановка заготовленного текста при internal-ошибке

> **Reason for MODIFICATION:** Реализация в `runtime_patcher.py:_wrap_fail`
> после отправки собственного `OutboundMessage` вызывала оригинальный
> `TurnDelivery.fail`, который **тоже** публиковал
> `OutboundMessage(content="Sorry, I encountered an error.")`. Пользователь
> получал два ответа. Контракт требовал «заменить», не «дополнить».

WHEN `AgentLoop._process_message` ловит `Exception` (любое исключение, кроме `asyncio.CancelledError`) и зовёт `TurnDelivery.fail`,
THEN система SHALL отправить пользователю **ровно один** `OutboundMessage` с `content` равным `gateway.error_messages.internal_error` из `project.json` (или default-значению `_DEFAULT_INTERNAL_ERROR_TEXT`, если секция отсутствует),
AND система SHALL **не вызывать** upstream `fail()` для публикации его собственного `OutboundMessage` (вместо этого вызов upstream сохраняется только ради `turn_completed` runtime-event, см. требование `Сохранение runtime-event публикации`),
AND ни один `publish_outbound` SHALL NOT содержать `content="Sorry, I encountered an error."`.

#### Scenario: Default-текст при отсутствии project.json-секции

- **WHEN** в `project.json` нет `gateway.error_messages.internal_error`
- **THEN** пользователь получает `OutboundMessage.content = "Произошла внутренняя ошибка. Попробуйте позже."`

#### Scenario: Custom-текст из project.json

- **WHEN** в `project.json` указано `gateway.error_messages.internal_error = "Сервис временно недоступен."`
- **THEN** пользователь получает `OutboundMessage.content = "Сервис временно недоступен."`

#### Scenario: Невалидный тип секции

- **WHEN** в `project.json` `gateway.error_messages.internal_error` имеет тип, отличный от `string` (например, число или массив)
- **THEN** старт gateway/CLI падает с `ConfigurationError` (fail-fast на Pydantic-валидации), runtime-до пользователя ошибка не доходит

#### Scenario: Ровно один outbound и ни одного upstream-литерала

- **WHEN** `TurnDelivery.fail` вызывается из `except Exception`-блока в `_process_message` с настройками `gateway.error_messages` по умолчанию или с custom-текстом
- **THEN** на `bus.publish_outbound` SHALL быть вызван **ровно один раз** с `content` равным configured-тексту
- **AND** ни один вызов `publish_outbound` НЕ ДОЛЖЕН содержать `content="Sorry, I encountered an error."`

#### Scenario: Подмена `self.bus` на прокси на время вызова upstream `fail()`

- **WHEN** обёртка `_wrap_fail` отправляет свой outbound
- **THEN** она SHALL вызвать оригинальный `TurnDelivery.fail(self, publish_completion=...)` для продолжения логики `turn_completed`
- **AND** на время этого вызова `self.bus` SHALL быть подменён на прокси `_OutboundSilencer`, который НЕ публикует outbound (но пропускает все остальные методы bus через `__getattr__`)
- **AND** после возврата оригинального `fail()` атрибут `self.bus` SHALL быть восстановлен в исходное значение через `try/finally`

### Requirement: Поведение observability

> **Reason for MODIFICATION:** Реализация читала `session_key` и `user_id`
> из `lifecycle_message`, но `InboundMessage` (`bus/events.py:25-37`) не имеет
> ни `session_key`, ни `user_id`. Кроме того, `TurnDelivery.fail(self, *,
> publish_completion: bool)` (`turn_delivery.py:336`) не получает объект
> исключения в сигнатуре, а захват через `sys.exception()` не был
> реализован. Результат: `session_id=None`, `user_id=None`,
> `exception_type`/`exception_message` отсутствуют в payload.

WHEN система формирует fallback-ответ,
THEN при `gateway.error_messages.log_to_db=true` (default) система SHALL записать в `agent_gateway_logs` запись `event_type="turn_failed"` с payload, содержащим ВСЕ перечисленные ниже поля, полученные **из авторитетных источников**:
- `session_key` — из атрибута `TurnDelivery.session_key` (установлен через `TurnDelivery.create(msg, session_key, ...)` в `turn_delivery.py:85-103`). НЕ из `lifecycle_message` (такого поля нет).
- `channel` — из `lifecycle_message.channel` (`bus/events.py:28`).
- `chat_id` — из `lifecycle_message.chat_id` (`bus/events.py:30`).
- `sender_id` — из `lifecycle_message.sender_id` (`bus/events.py:29`). НЕ из `lifecycle_message.user_id` (такого поля нет).
- `agent_id` — из `config`, переданный через `RuntimePatcher.apply_all` → `patch_turn_delivery_fail`.
- `exception_type` — имя класса активного исключения, либо `null` если `exception_available=false`.
- `exception_message` — `str(exc)` активного исключения, либо `null`.
- `exception_available` — bool: `True` если в момент вызова `_wrap_fail` есть активное исключение (`sys.exception()` не `None`), `False` иначе.
- `failure_error_kind` — snapshot `self._failure_error_kind` (private upstream-атрибут, опциональный).

AND при `log_to_db=false` система SHALL **не** вызывать `try_log_event`.

#### Scenario: Запись при log_to_db=true

- **WHEN** в `project.json` `gateway.error_messages.log_to_db=true` (или отсутствует — default)
- **THEN** в таблице `agent_gateway_logs` появляется строка с `event_type="turn_failed"` и `payload`, содержащим тип и текст оригинального исключения

#### Scenario: Без записи при log_to_db=false

- **WHEN** в `project.json` `gateway.error_messages.log_to_db=false`
- **THEN** в `agent_gateway_logs` НЕ появляется новая строка для этого `turn_failed`; оригинальное исключение остаётся только в `loguru`

#### Scenario: Отсутствие DbLoggingService

- **WHEN** `DbLoggingService` не зарегистрирован в `ApplicationContext` (например, при `--profile=test` без логирования или в unit-тестах)
- **THEN** fallback-ответ пользователю всё равно отправляется; попытка записи в БД SHALL быть no-op без падения оборота

#### Scenario: Все диагностические поля заполнены из авторитетных источников

- **WHEN** `TurnDelivery.fail` вызывается из `except ValueError("boom")`-блока, `lifecycle_message.sender_id="u-42"`, `TurnDelivery.session_key="s-7"`, `agent_id="agent_main"`
- **THEN** payload `turn_failed` SHALL содержать `exception_type="ValueError"`, `exception_message="boom"`, `exception_available=true`, `sender_id="u-42"`, `session_id="s-7"`, `agent_id="agent_main"`
- **AND** НЕ ДОЛЖЕН быть записан с `session_id=null` или `user_id=null` из-за чтения несуществующих полей `lifecycle_message`

#### Scenario: Отсутствие активного исключения деградирует gracefully

- **WHEN** `_wrap_fail` вызван вне `except`-блока (например, прямо из unit-теста без активного исключения)
- **THEN** payload SHALL содержать `exception_available=false`, `exception_type=null`, `exception_message=null`
- **AND** остальные поля (`session_key`, `sender_id`, `agent_id`, `channel`, `chat_id`) SHALL заполняться как обычно
