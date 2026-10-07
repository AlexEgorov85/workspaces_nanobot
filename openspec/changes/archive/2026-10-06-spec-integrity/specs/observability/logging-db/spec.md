## Purpose

Дельта закрывает расхождения B1 и B2 из `divergence-register.md`: путь записи
авто-сжатия и имя события в каноне журнала.

Метод `ContextCompactionService.record_external_compaction` удалён — у него не было
ни одного вызывающего в production-коде, а шесть тестовых вызовов и собственный
докстринг утверждали, что он работает. Фактический путь авто-сжатия другой:
`lib/channels/postgres_channel.py:1412` → `CompactionEventSubscriber.feed()`
(`compaction_event_subscriber.py:33`) → `notify_session_compacted()` (`:90`).
Этот путь `_notify` **не зовёт**: у события нет замеров, поэтому пишется
`_record_event_log` напрямую, а `_write_history_notice` добавляется только для
фазы `succeeded` при `notify_in_history=True`.

Имя события канон решил сам, и дельта применяет это решение, а не выдумывает
новое: требование «Единый словарь имён событий» велит писать только канонические
имена из `mcp-platform/libs/enterprise_common/eventing/types.py`, его таблица
соответствия отображает `context_compacted` → `agent.compacted`
(`docs/journal-observability.md:734`), а таблица покрытия этапов оборота называет `agent.compacted`
каноническим именем стадии 9 (`docs/journal-observability.md:310`). Код уже пишет `agent.compacted`
(`lib/services/context_compaction.py:377,396`), словарь платформы его объявляет
(`types.py:32,94`). Расходились только два требования ниже.

Оба требования заменяются целиком, а не модифицируются: их **заголовки содержат
имя удалённого метода или неверное имя события**, и правка тела оставила бы
заголовок, противоречащий собственному содержимому. `MODIFIED` здесь неприменим
и по правилу инструмента: он требует сохранить заголовки сценариев, включая
`record_external_compaction наследует decoupled поведение` — то есть сохранить
имя метода, которого больше нет. Поэтому `REMOVED` + `ADDED` с явным перечислением
снятых сценариев.

## Scope

Owner: `agent`. Реализация: `lib/services/context_compaction.py`,
`docs/ARCHITECTURE.md`. Правка кода — удаление метода и уточнение докстринга;
правка тестов — `tests/test_context_compaction.py`,
`tests/test_unified_event_logging_contract.py`,
`tests/test_compaction_event_subscriber.py`.

Дельта MUST NOT трогать требование «Sync-события через DbLoggingService» и
«Producers не читают logging-DB конфиг»: их снимает незакрытый change
`2026-10-05-logging-db-dead-producers`. Пересечение двух правок одного канона
привело бы к потере одной из них.

## REMOVED Requirements

### Requirement: context_compacted через DbLoggingService

Требование снято целиком. Оно требовало писать в журнал событие
`context_compacted`, которого нет ни в словаре платформы, ни в коде, и описывало
авто-сжатие через `runtime_patcher._wrap_auto_compact_archive` и
`record_external_compaction` — метода, не существующего ни в одном дереве.

Снятые сценарии, перенесённые в новое требование без потери:

- `Ручной /compact пишет context_compacted` → `Ручной /compact пишет
  agent.compacted`
- `Авто compact пишет context_compacted` → `Авто-сжатие пишет agent.compacted`
  (прежние условия сценария невыполнимы: вызывающих метода не существовало)
- `compaction не падает при недоступности сервиса` → перенесён дословно по
  существу; убран дословный текст предупреждения, который коду не соответствовал

### Requirement: notify_in_history не управляет structured event logging

Требование снято целиком из-за сценария, построенного на удалённом методе.
Разделение concerns само по себе верно и целиком переносится в новое требование
с исправленным именем события.

Снятые сценарии:

- `notify_in_history=true — оба side-effect'а` → перенесён, имя события исправлено
- `notify_in_history=false — только structured event` → перенесён, имя события
  исправлено
- `record_external_compaction наследует decoupled поведение` → **премисса
  недействительна**: метода нет. Инвариант (событие пишется даже при выключенной
  записи в историю) перенесён на живой путь под новым именем

## ADDED Requirements

### Requirement: agent.compacted через DbLoggingService

The system SHALL записывать событие `agent.compacted` в `agent_gateway_logs` через
`DbLoggingService.try_log_event(...)` (вызов `svc.log_event(LogEvent(...))`
внутри). Имя `context_compacted` SHALL использоваться только как префикс
`event_id` (`f"context_compacted:{session_key}"`,
`lib/services/context_compaction.py:480`) и SHALL NOT использоваться как
`event_type`.

Никаких прямых `INSERT` из `ContextCompactionService` SHALL NOT происходить.

#### Scenario: Ручной /compact пишет agent.compacted

- **WHEN** `ContextCompactionService.compact()` (slash, CLI `/compact`, tool
  `compact_context`) завершился с `archived_msgs > 0`
- **THEN** `DbLoggingService` SHALL поставить `LogEvent` с
  `event_type="agent.compacted"`, `actor="system"`, `name="consolidator"` и
  payload, содержащим `mode` / `archived_msgs` / `kept_msgs` / `tokens_before` /
  `tokens_after` / `summary` / `raw_dump`

#### Scenario: Авто-сжатие пишет agent.compacted

- **WHEN** канал (`postgres_channel` либо `redis_channel`) отдал `OutboundMessage`
  с `event` типа `ContextCompactionEvent`, и `CompactionEventSubscriber.feed()`
  обработал его
- **THEN** `notify_session_compacted(session_key, phase, compaction_id)` SHALL
  записать `agent.compacted` через `_record_event_log` для **любой** фазы
- **AND** `_write_history_notice` SHALL быть добавлен только для
  `phase="succeeded"` и только при `notify_in_history=True`
- **AND** замеров в этом пути SHALL NOT быть: `tokens_before`, `tokens_after`,
  `archived_msgs` остаются `0`, потому что событие несёт только `compaction_id`
  и `phase`
- **AND** метод `ContextCompactionService.record_external_compaction` SHALL NOT
  существовать: вызывающих у него не было, а авто-сжатие обслуживает
  `notify_session_compacted`

#### Scenario: compaction не падает при недоступности сервиса

- **WHEN** `db_logging_service is None` ИЛИ `db_logging_service.is_running() == False`
- **AND WHEN** `ContextCompactionService.compact(...)` завершил сжатие успешно
- **THEN** `compact(...)` SHALL вернуть успешный отчёт (`ok=True`,
  `archived_msgs > 0`)
- **AND** `DbLoggingService.try_log_event(...)` SHALL обеспечивать no-op for
  business (событие не записано, compaction продолжается)
- **AND** `logger.warning(...)` SHALL быть вызван ровно один раз на уровне
  **WARNING** (НЕ DEBUG, НЕ INFO, НЕ ERROR); требуемый состав сообщения задан
  требованием «try_log_event contract» и здесь не дублируется
- **AND** прямой `INSERT INTO "<schema>"."<table>"` SHALL NOT быть выполнен

### Requirement: notify_in_history не управляет structured event logging, а agent.compacted пишется всегда

The system SHALL разделять два concerns: (a) UI-уведомление о сжатии в
`agent_conversation_messages` (заметка видна в чате); (b) observability-trail в
`agent_gateway_logs` (событие `agent.compacted` доступно через
`data.history_search`).

Настройка `gateway.compact.notify_in_history` SHALL управлять **только** concern
(a). Событие `agent.compacted` SHALL записываться **всегда**, пока
`gateway.compact.enabled=True`, независимо от `notify_in_history`.

#### Scenario: notify_in_history=true — оба side-effect'а

- **WHEN** `gateway.compact.notify_in_history=true`
- **AND WHEN** compaction завершился с `archived_msgs > 0`
- **THEN** `_write_history_notice` SHALL быть вызван и SHALL записать строку в
  `agent_conversation_messages` (`metadata.kind="context_compact"`)
- **AND** `agent.compacted` SHALL быть поставлен в очередь журнала

#### Scenario: notify_in_history=false — только structured event

- **WHEN** `gateway.compact.notify_in_history=false`
- **AND WHEN** compaction завершился с `archived_msgs > 0`
- **THEN** `_write_history_notice` SHALL NOT быть вызван (никакой записи в
  `agent_conversation_messages`)
- **AND** `agent.compacted` SHALL всё равно быть записан в очередь журнала
- **AND** `data.history_search(event_type="agent.compacted",
  session_scope="current")` SHALL находить событие для recovery после compaction

#### Scenario: авто-сжатие наследует decoupled поведение

- **WHEN** `CompactionEventSubscriber.feed()` обработал `ContextCompactionEvent`
  с `phase="succeeded"` при `notify_in_history=false`
- **THEN** `_record_event_log` SHALL быть вызван **даже** при выключенной записи в
  историю, а `_write_history_notice` SHALL NOT быть вызван
- **AND** инвариант SHALL проверяться на живом пути `notify_session_compacted`
  (`tests/test_compaction_event_subscriber.py::
  TestNotifySessionCompactedPublicAPI::test_notify_in_history_false_skips_history_notice`),
  а не на удалённом методе