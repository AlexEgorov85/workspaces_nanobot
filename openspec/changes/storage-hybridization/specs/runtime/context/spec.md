## ADDED Requirements

### Requirement: Session hot-path state принадлежит upstream SessionManager

Система SHALL хранить горячее состояние сессии (сообщения,
метаданные, per-turn deltas, checkpoints, provider state)
через upstream `nanobot.session.manager.SessionManager`
(JSONL-стор), а НЕ SHALL хранить его в `ApplicationContext`
или в прямом SQL к `agent_session_meta` /
`agent_session_messages`. Система MUST использовать upstream
JSONL как единственный hot-path стор для session data.

#### Scenario: Чтение сессии через upstream API

- **WHEN** требуется сессия по `session_key`
- **THEN** система SHALL прочитать её через
  `SessionManager.get_or_create(session_key)` (upstream
  API, JSONL-стор), а НЕ SHALL выполнять `SELECT` из
  `agent_session_meta` в hot path.

#### Scenario: Запись сессии через upstream API

- **WHEN** завершён turn и требуется сохранить состояние
  сессии
- **THEN** система SHALL вызвать `SessionManager.save(session)`
  (upstream API), а НЕ SHALL выполнять `INSERT` в
  `agent_session_messages` в рамках hot path.

#### Scenario: list_sessions и read_session_metadata через upstream API

- **WHEN** UI или runtime-код запрашивает список сессий или
  метаданные конкретной сессии
- **THEN** система SHALL использовать
  `SessionManager.list_sessions()` /
  `SessionManager.read_session_metadata(...)` (upstream API,
  JSONL-стор), а НЕ SHALL выполнять прямой `SELECT` из
  `agent_session_meta` в hot path.

### Requirement: Session cold-storage mirror в PostgreSQL

PostgreSQL SHALL использоваться как cold-storage mirror для
сессий через отдельный фоновый сервис `SessionColdSyncService`.
Запись в `agent_session_meta` / `agent_session_messages` MUST
идти только через этот sync-сервис, не из hot path операций
upstream `SessionManager`. Hot path SHALL NOT блокироваться
sync-циклом.

#### Scenario: PGSessionManager больше не hot-path writer

- **WHEN** агент работает с сессией через
  `session_manager.get_or_create` / `save`
- **THEN** upstream `SessionManager` (JSONL) SHALL обработать
  операцию.
- **AND** `PGSessionManager` (или его новый наследник)
  SHALL быть переработан так, чтобы его hot-path методы
  (`get_or_create` / `save` / `list_sessions` /
  `read_session_metadata`) выполняли только делегирование
  в `super()` без прямого SQL `INSERT/UPDATE/DELETE`
  в таблицы `agent_session_meta` / `agent_session_messages`.

#### Scenario: SessionColdSyncService асинхронно зеркалит

- **WHEN** `SessionColdSyncService` запускается по расписанию
- **THEN** он SHALL прочитать upstream JSONL-стор и записать
  изменения в PG `agent_session_meta` /
  `agent_session_messages` через `last-write-wins`.
- **AND** hot path операции `SessionManager` SHALL NOT
  блокироваться этим sync-циклом.
