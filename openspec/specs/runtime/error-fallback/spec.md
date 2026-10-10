# runtime/error-fallback Specification

## Purpose
Конфигурируемый заготовленный ответ при необработанном исключении в `AgentLoop._process_message` (upstream-`nanobot`) с записью деталей в долговечный журнал `agent_gateway_logs`. Заменяет захардкоженный `"Sorry, I encountered an error."` в `TurnDelivery.fail` на операторски-редактируемый текст без утечки traceback'а пользователю.

## Scope

`agent` — fallback-ответ на внутреннюю ошибку настраивается агентом
Реализация: `lib/services/turn_delivery_factory.py`

## Requirements

Имя события каноническое: `agent.failed` (`EV_AGENT_FAILED` в `lib/hooks/database_logging_hook.py:50`, запись — `lib/services/turn_delivery_factory.py:145`). Раньше здесь стояло имя `turn_failed`, которого в коде нет.

### Requirement: Подстановка заготовленного текста при internal-ошибке

> **Reason for MODIFICATION:** Реализация в `runtime_patcher.py:_wrap_fail`
> после отправки собственного `OutboundMessage` вызывала оригинальный
> `TurnDelivery.fail`, который **тоже** публиковал
> `OutboundMessage(content="Sorry, I encountered an error.")`. Пользователь
> получал два ответа. Контракт требовал «заменить», не «дополнить».

WHEN `AgentLoop._process_message` ловит `Exception` (любое исключение, кроме `asyncio.CancelledError`) и зовёт `TurnDelivery.fail`,
THEN система SHALL отправить пользователю **ровно один** `OutboundMessage` с `content` равным `gateway.error_messages.internal_error` из `config.json` (или default-значению `_DEFAULT_INTERNAL_ERROR_TEXT`, если секция отсутствует),
AND система SHALL **не вызывать** upstream `fail()` для публикации его собственного `OutboundMessage` (вместо этого вызов upstream сохраняется только ради `turn_completed` runtime-event, см. требование `Сохранение runtime-event публикации`),
AND ни один `publish_outbound` SHALL NOT содержать `content="Sorry, I encountered an error."`.

#### Scenario: Default-текст при отсутствии config.json-секции

- **WHEN** в `config.json` нет `gateway.error_messages.internal_error`
- **THEN** пользователь получает `OutboundMessage.content = "Я не справился с вашим вопросом. Попробуйте, пожалуйста, переформулировать конкретнее — например, уточните ключевую часть или приведите пример."`

#### Scenario: Custom-текст из config.json

- **WHEN** в `config.json` указано `gateway.error_messages.internal_error = "Сервис временно недоступен."`
- **THEN** пользователь получает `OutboundMessage.content = "Сервис временно недоступен."`

#### Scenario: Невалидный тип секции

- **WHEN** в `config.json` `gateway.error_messages.internal_error` имеет тип, отличный от `string` (например, число или массив)
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

### Requirement: Метаданные error_kind в финальном outbound

WHEN система формирует fallback-ответ на необработанное исключение,
THEN `OutboundMessage.metadata._error_kind` SHALL быть равен `"internal"`.

#### Scenario: Маркер в metadata

- **WHEN** пользователь получает fallback-ответ
- **THEN** `outbound.metadata["_error_kind"] == "internal"` (отличимо от обычного ответа и от upstream-литерала `"Sorry, I encountered an error."`)

### Requirement: Поведение observability

> **Reason for MODIFICATION:** Реализация читала `session_key` и `user_id`
> из `lifecycle_message`, но `InboundMessage` (`nanobot/bus/events.py:25-37`) не имеет
> ни `session_key`, ни `user_id`. Кроме того, `TurnDelivery.fail(self, *,
> publish_completion: bool)` (`nanobot/agent/turn_delivery.py:336`) не получает объект
> исключения в сигнатуре, а захват через `sys.exception()` не был
> реализован. Результат: `session_id=None`, `user_id=None`,
> `exception_type`/`exception_message` отсутствуют в payload.

WHEN система формирует fallback-ответ,
THEN при `gateway.error_messages.log_to_db=true` (default) система SHALL записать в `agent_gateway_logs` запись `event_type="agent.failed"` с payload, содержащим ВСЕ перечисленные ниже поля, полученные **из авторитетных источников**:
- `session_key` — из атрибута `TurnDelivery.session_key` (установлен через `TurnDelivery.create(msg, session_key, ...)` в `nanobot/agent/turn_delivery.py:85-103`). НЕ из `lifecycle_message` (такого поля нет).
- `channel` — из `lifecycle_message.channel` (`nanobot/bus/events.py:28`).
- `chat_id` — из `lifecycle_message.chat_id` (`nanobot/bus/events.py:30`).
- `sender_id` — из `lifecycle_message.sender_id` (`nanobot/bus/events.py:29`). НЕ из `lifecycle_message.user_id` (такого поля нет).
- `agent_id` — из `config`, переданный через `RuntimePatcher.apply_all` → `patch_turn_delivery_fail`.
- `exception_type` — имя класса активного исключения, либо `null` если `exception_available=false`.
- `exception_message` — `str(exc)` активного исключения, либо `null`.
- `exception_available` — bool: `True` если в момент вызова `_wrap_fail` есть активное исключение (`sys.exception()` не `None`), `False` иначе.
- `failure_error_kind` — snapshot `self._failure_error_kind` (private upstream-атрибут, опциональный).

AND при `log_to_db=false` система SHALL **не** вызывать `try_log_event`.

#### Scenario: Запись при log_to_db=true

- **WHEN** в `config.json` `gateway.error_messages.log_to_db=true` (или отсутствует — default)
- **THEN** в таблице `agent_gateway_logs` появляется строка с `event_type="agent.failed"` и `payload`, содержащим тип и текст оригинального исключения

#### Scenario: Без записи при log_to_db=false

- **WHEN** в `config.json` `gateway.error_messages.log_to_db=false`
- **THEN** в `agent_gateway_logs` НЕ появляется новая строка для этого `agent.failed`; оригинальное исключение остаётся только в `loguru`

#### Scenario: Отсутствие DbLoggingService

- **WHEN** `DbLoggingService` не зарегистрирован в `ApplicationContext` (например, при `--profile=test` без логирования или в unit-тестах)
- **THEN** fallback-ответ пользователю всё равно отправляется; попытка записи в БД SHALL быть no-op без падения оборота

#### Scenario: Все диагностические поля заполнены из авторитетных источников

- **WHEN** `TurnDelivery.fail` вызывается из `except ValueError("boom")`-блока, `lifecycle_message.sender_id="u-42"`, `TurnDelivery.session_key="s-7"`, `agent_id="agent_main"`
- **THEN** payload `agent.failed` SHALL содержать `exception_type="ValueError"`, `exception_message="boom"`, `exception_available=true`, `sender_id="u-42"`, `session_id="s-7"`, `agent_id="agent_main"`
- **AND** НЕ ДОЛЖЕН быть записан с `session_id=null` или `user_id=null` из-за чтения несуществующих полей `lifecycle_message`

#### Scenario: Отсутствие активного исключения деградирует gracefully

- **WHEN** `_wrap_fail` вызван вне `except`-блока (например, прямо из unit-теста без активного исключения)
- **THEN** payload SHALL содержать `exception_available=false`, `exception_type=null`, `exception_message=null`
- **AND** остальные поля (`session_key`, `sender_id`, `agent_id`, `channel`, `chat_id`) SHALL заполняться как обычно

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
- **THEN** пользователь НЕ получает fallback-сообщение (ни default, ни custom); ветка `CancelledError` остаётся нетронутой

### Requirement: Отсутствие утечки деталей исключения пользователю

WHEN система формирует fallback-ответ,
THEN `OutboundMessage.content` SHALL содержать ТОЛЬКО текст `gateway.error_messages.internal_error` (без str(exc), traceback, имени функции или module path).

#### Scenario: Только заготовка в content

- **WHEN** исходное исключение — `KeyError("agent_internal_state_xyz")`
- **THEN** пользователь получает default fallback-текст; в `OutboundMessage.content` НЕТ подстроки `"agent_internal_state_xyz"`, `"KeyError"` или пути к исходнику

### Requirement: Не-регрессия публичного контракта OutboundMessage

WHEN система формирует fallback-ответ,
THEN `OutboundMessage` SHALL сохранить все обязательные поля (`channel`, `chat_id`, `content`, `metadata`) и SHALL быть совместим с downstream-каналами (PostgresChannel, ConsoleLoop) без изменений в их обработчиках.

#### Scenario: PostgresChannel не падает на fallback

- **WHEN** fallback-ответ публикуется в `bus.publish_outbound`
- **THEN** PostgresChannel.send корректно обрабатывает его как финальный outbound, переводит задачу в `completed`, удаляет claim — никаких изменений в коде канала не требуется

## Responsibility

Замена захардкоженного текста отказа на настраиваемый: один ответ
пользователю при необработанном исключении в `AgentLoop._process_message` и
запись деталей в долговечный журнал. Владелец —
`lib/services/turn_delivery_factory.py`; механизм подключения — публичная
точка расширения nanobot `turn_delivery_factory`, а не патч
(`lib/core/agent_factory.py:259-264`).

## Boundary

- **Внутри:** текст ответа, признаки `_error_kind`/`_final_turn`, одна
  публикация outbound, запись `agent.failed`, runtime-событие `turn_completed`.
- **Снаружи:** обработка исключения — `AgentLoop`; маршрутизация сообщений —
  `TurnDeliveryFactory` upstream; схема и запись в журнал — `observability/logging-db`.

## Public Contract

- `build_turn_delivery_factory(bus, *, settings=None, db_logging_service=None, agent_id=None) -> FallbackTurnDeliveryFactory | None`
  (`lib/services/turn_delivery_factory.py:225-231`).
- `FallbackTurnDeliveryFactory(bus, *, internal_error=..., log_to_db=..., db_logging_service=None, agent_id=None)`
  (`:185-193`) — наследует `TurnDeliveryFactory`.
- `FallbackTurnDelivery.fail(*, publish_completion)` (`:75`) — переопределение
  upstream-метода.
- `DEFAULT_INTERNAL_ERROR_TEXT` (`:53-57`), `DEFAULT_LOG_TO_DB = True` (`:58`).
- Отдельного доменного типа ошибки нет: подсистема живёт на пути успеха
  отказа, а не добавляет ветку в обработку.

## Inputs

- `settings` — merged `SETTINGS`; читаются `gateway.error_messages.internal_error`
  и `gateway.error_messages.log_to_db` через `_get`
  (`:253-265`). `settings=None` — дефолтный текст и `log_to_db=True`.
- `bus` — **обязательно тот же объект**, что у `AgentLoop`: тот проверяет
  `factory.bus is bus` (`:194-196`).
- `db_logging_service` — `DbLoggingService` или `None` (запись пропускается);
  `agent_id` — для колонки в журнале.
- Активное исключение — `sys.exception()` внутри `fail()`
  (`:126-130`).

## Outputs

- Ровно один `OutboundMessage` с `content = self._fallback_text` и
  metadata `{_error_kind: "internal", _final_turn: True}` (`:81-113`).
- При `publish_completion` — runtime-событие `turn_completed` с
  `outcome="failed"`, `failure_kind="internal"` (`:91-100`).
- Событие журнала `event_type="agent.failed"`, `level="ERROR"`,
  `actor=None`, `summary=<failure_error_kind>`, payload `{kind, failure_error_kind,
  agent_id, sender_id, chat_id, exception_type, exception_message,
  exception_available}`, metadata `{fallback_text_len, publish_completion}`
  (`:141-173`).
- Строка WARNING при сбое публикации или записи (`:116`, `:175`).

## State

Долговременного состояния нет. Экземпляр `FallbackTurnDelivery` несёт на
себе поля класса `_fallback_text`, `_log_to_db`, `_db_logging_service`,
`_agent_id` (`:70-73`), проставленные фабрикой в `_adopt` (`:216-222`).

## Dependencies

- `nanobot.agent.turn_delivery.TurnDelivery`, `TurnDeliveryFactory` —
  базовые классы; недоступность переводит флаг `UPSTREAM_AVAILABLE`
  (`:34`, `:246`);
- `nanobot.bus.events.OutboundMessage` — лениво, внутри публикации (`:104`);
- `lib.services.db_logging_service.LogEvent`, `try_log_event` — лениво
  (`:124`), producer `"turn_delivery_factory"`;
- `lib/core/agent_factory.py:144`, `:264-271` — единственный вызывающий.

## Configuration

`config.json` → `gateway.error_messages` (`lib/core/project_settings.py:104-131`):

- `internal_error` — текст вместо upstream-строки; модель допускает
  `None` и отдаёт дефолт;
- `log_to_db` — писать ли `agent.failed` в журнал; по умолчанию `True`.

Валидация значения в коде: не строка или строка из одних пробелов →
`DEFAULT_INTERNAL_ERROR_TEXT` (`lib/services/turn_delivery_factory.py:256-259`),
иначе пользователь получил бы сообщение, читающееся как «агент молчит».
Не-`bool` в `log_to_db` → `DEFAULT_LOG_TO_DB` (`:264-265`).

## Lifecycle

1. `AgentFactory.create` вызывает `build_turn_delivery_factory(bus, settings=…, db_logging_service=…, agent_id=…)`
   (`lib/core/agent_factory.py:264-269`).
2. При `UPSTREAM_AVAILABLE is False` возвращается `None`, и `AgentLoop`
   собирает фабрику сам — пользователь увидит upstream-текст, как и до
   переноса (`lib/services/turn_delivery_factory.py:241-251`).
3. Иначе `kwargs["turn_delivery_factory"]` передаётся в
   `AgentLoop.from_config` (`lib/core/agent_factory.py:270-271`, `:279`).
4. `create`/`unrouted` вызываются через `super()`, и подменяется только
   класс уже собранного экземпляра (`lib/services/turn_delivery_factory.py:213-217`).
5. При исключении в `_process_message` вызывается `fail(*, publish_completion=…)`.

## Data Ownership

Ответ пользователю — не данные подсистемы, а текст настройки. Детали
исключения уходят в журнал и в payload; traceback пользователю не
передаётся. В `metadata` публикации попадает только
`fallback_text_len` — длина, а не сам текст настройки
(`lib/services/turn_delivery_factory.py:166`).

## Error Behavior

- Сбой публикации fallback не роняет оборот: исключение гасится с
  WARNING (`:106-116`).
- Сбой записи в журнал fail-open: обёрнуто в `try/except` с WARNING
  (`:174-175`).
- Вне активного `except` (юнит-тест) `sys.exception()` вернёт `None`, и это
  отражено флагом `exception_available`, а не выдуманным текстом (`:126-130`,
  `:163`).
- Недоступность upstream-модуля — WARNING и `None`, а не падение старта
  (`:246-251`).

## Invariants

- Ровно одна публикация outbound на отказ: `super().fail()` **не**
  вызывается — он опубликовал бы второй, upstream-текст (`:78-79`).
- `factory.bus is bus`: идентичность шины сохраняется, потому что объект
  тот же, что пришёл в конструктор (`:194-196`).
- Класс подменяется у уже собранного экземпляра, поэтому конструктор
  `FallbackTurnDelivery` не переопределяется, а поля проставляются после
  подмены (`:61-67`, `:216-222`).
- Маршрутизация остаётся upstream: `create`/`unrouted` вызываются через
  `super()` (`:209-214`).
- Путь один — публичная точка расширения, а не патч: забытый при сборке
  путь показал бы пользователю upstream-литерал
  (`lib/core/agent_factory.py:259-263`).

## Forbidden Behavior

- Вызывать `super().fail()` из `FallbackTurnDelivery.fail` — пользователь
  получит два ответа (`lib/services/turn_delivery_factory.py:78-79`).
- Публиковать `content="Sorry, I encountered an error."` из этого пути.
- Передавать в `OutboundMessage` текст traceback'а или иные внутренние
  детали: пользователю отдаётся текст настройки без деталей.
- Подставлять `fallback` при `publish_completion=False` иначе, чем одна
  публикация: оборот должен закрыться этой публикацией.
- Пробельный `internal_error` считать валидным текстом (`:256-259`).
- Добавлять параллельный патч-путь отказа: это возвращает двойную
  публикацию, ради устранения которой подсистема и вынесена в фабрику
  (`:15-19`, комментарий модуля).

## Consumers

- `lib/core/agent_factory.py:264-271` — единственный вызывающий; результат
  идёт в `AgentLoop.from_config` (`:279`).
- `nanobot.agent.loop.AgentLoop` — потребитель фабрики: сам создаёт
  `TurnDelivery` и зовёт `fail` при исключении.
- Оператор — по событию `agent.failed` в `agent_gateway_logs` и по
  сообщению в чате.

## Implementation

Существующие на диске пути:

- `lib/services/turn_delivery_factory.py` — вся подсистема: дефолты, класс
  delivery, фабрика, сборка из конфигурации;
- `lib/core/agent_factory.py` — подключение фабрики к `AgentLoop`;
- `lib/core/project_settings.py` — модель `ErrorMessagesSettings`;
- `lib/services/db_logging_service.py` — `LogEvent` и `try_log_event`.

## Verification

- `tests/test_turn_delivery_factory.py` — текст ответа, отсутствие второго
  `super().fail()`, запись `agent.failed`, fail-open публикации и записи.
