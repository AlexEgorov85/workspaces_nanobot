## ADDED Requirements

### Requirement: Личность оборота имеет одного владельца

Личность оборота (`session_id` + `user_id` + `request_id`) MUST храниться в
одном носителе, а читаться — из одного места. Владельцем MUST быть
`TurnIdentityStore` (`lib/services/turn_identity.py`): писателем остаётся
`DbLoggingService.register_request` (он один знает `sender_id` в момент
входа), но и индекс вопроса `session_key → request_id`, и снимок личности
ВХОДА MUST быть **частями одной записи** под одним замком.

Две части обязаны иметь разные сроки жизни, и это MUST быть выражено в
записи, а не в двух словарях:

- снимок ВХОДА (`user_id`, `request_id`, `at_seq`) MUST переживать снятие
  привязки вопроса — финальный ответ оборота приходит после конца оборота, и
  подписать его больше нечем;
- привязка вопроса MUST сниматься отдельным действием владельца, после
  которого `request_id` сессии больше не выдаётся.

Чтение личности оборота из фреймворка (`RequestContext`) MUST иметь одну
реализацию на проект. Все места, которым личность нужна для подписи вызова
(`McpIdentityHook`, `EnterpriseMcpClient`, `RuntimeEventsSubscriber`) и
места, которым нужен `sender_id` для подписи своего события, MUST получать
её из этой реализации, а не читать `RequestContext` сами.

Запись личности MUST быть неизменяемой: подписать чужое событие чужим
отправителем MUST NOT быть возможно по ошибке вызывающего.

#### Scenario: у журнала нет второго носителя личности

- **WHEN** `DbLoggingService` регистрирует вход (`register_request`)
- **THEN** индекс вопроса и снимок входа MUST записываться одной операцией
  в `TurnIdentityStore`
- **AND** `DbLoggingService` MUST NOT содержать собственного словаря или
  структуры с личностью оборота
- **AND** пара «индекс + снимок» MUST NOT требовать сверок на стороне
  читателей для своей атомарности

#### Scenario: чтение личности из фреймворка одно на проект

- **WHEN** личность оборота нужна более чем одному компоненту
- **THEN** все они MUST получать её одной и той же функцией чтения
- **AND** ни один из них MUST NOT читать `RequestContext` напрямую ради
  `session_key` или `sender_id`
- **AND** расхождение имён ключей личности вызова MUST NOT быть возможно:
  набор ключей MUST объявляться рядом со сборкой и кормить её

#### Scenario: снятие привязки не стирает снимок

- **WHEN** привязка вопроса снята в конце оборота
- **THEN** `request_id` сессии MUST перестать выдаваться
- **AND** снимок входа MUST остаться доступным для подписи финального
  ответа, который приходит после этого момента

#### Scenario: личность нельзя подменить чужой

- **WHEN** читатель получил запись личности из хранилища
- **THEN** изменение полей записи MUST отклоняться
- **AND** значение у владельца MUST остаться прежним

## MODIFIED Requirements

### Requirement: agent_question_runs как отдельная aggregate-модель

`DbLoggingService` MUST владеть двумя разными
persistence-моделями (как разные aggregate-контракты
в одном сервисе):

1. **`agent_gateway_logs`** — event timeline
   (immutable-ish журнал structured agent events;
   строки добавляются, не обновляются);
2. **`agent_question_runs`** — request aggregate
   (per-request контекст: `user_id`, `agent_id`,
   `parent_request_id`, `is_subagent`, `status`,
   `summary`, `question`, `media`; обновляется
   через upsert по `request_id`).

`agent_question_runs` НЕ объединяется с
`agent_gateway_logs` в одну таблицу и НЕ
превращается в часть event timeline. Это
**другая persistence-модель**, отвечающая на
другие вопросы:
- event timeline: «что произошло в системе
  в момент X» (для `history_search`, observability);
- request aggregate: «какой вопрос сейчас
  обрабатывается и в каком он статусе» (для
  UI, отображения текущего request, маршрутизации).

`DbLoggingService` является владельцем обоих —
через специализированные методы
`register_request` / `finish_request` для
`agent_question_runs` (через
`_QuestionRunRecord` + `_handle_question_run` +
`_upsert_question_run`) и `log_event(LogEvent(...))`
для `agent_gateway_logs`. **Никаких вторых
writer'ов для обоих таблиц** вне `DbLoggingService`.

Владение записью личности оборота — отдельный вопрос и решается
требованием «Личность оборота имеет одного владельца»: `DbLoggingService`
MUST оставаться писателем, но MUST NOT быть хранилищем.

Запрещено:

- Вводить отдельный «run-store service»,
  «run-tracker», «run-state-manager» или
  аналогичные слои над `agent_question_runs`
  (или под ним).
- Сливать `agent_question_runs` с
  `agent_gateway_logs` в одну таблицу через
  JSONB-поле `request_state` — это другой
  persistence contract.
- Эмитить `agent_question_runs` rows через
  `record_event` / `record_sync_event` /
  `emit_sync_event` (или через прямой SQL) — это
  контрактно разные persistence-модели.
- Вводить в `DbLoggingService` второй носитель
  личности оборота (индекс вопросов, снимок
  входа или их эквивалент) — владелец один.

#### Scenario: agent_question_runs update через DbLoggingService

- **WHEN** agent регистрирует начало нового
  вопроса через `db_logging_service.register_request(...)`
- **THEN** строка SHALL быть вставлена в
  `agent_question_runs` через `DbLoggingService._handle_question_run`,
  NOT через прямой SQL.
- **AND** `agent_question_runs` SHALL остаться
  отдельной таблицей (не объединена с
  `agent_gateway_logs`).

#### Scenario: agent_question_runs row идентифицируется по request_id

- **WHEN** строка в `agent_gateway_logs`
  ссылается на `request_id`
- **THEN** соответствующий row в
  `agent_question_runs` SHALL существовать
  (через привязку `session_key → request_id` в
  `TurnIdentityStore`).
- **AND** обновление статуса
  (`finish_request(...)`) SHALL идти через
  `DbLoggingService` (`_handle_question_run` →
  `_upsert_question_run`), не через прямой SQL.
