## ADDED Requirements

### Requirement: Lifecycle MUST surface logging-hook attachment failures

При подключении опциональных хуков логирования
(`DatabaseLoggingHook`, subagent logging patch) в рамках
`ctx.start()` сервис SHALL явно логировать через
`loguru.logger.warning` любой отказ подключения (например,
`db_logging_service is None` или изменение API nanobot) и
инкрементировать счётчик `runtime_health` компонента
`db_logging`. Тихая потеря событий запрещена.

Это дополняет существующий requirement `Deterministic lifecycle`
(`openspec/specs/runtime/context/spec.md`) — не заменяет его.

#### Scenario: Хук не подключился — видно в логах и health

- **WHEN** `ctx.start()` вызван, но `db_logging_service is None`
  на момент применения патча subagent-логирования
- **THEN** в логе есть warning с указанием причины отказа,
  `runtime_health["db_logging"]` имеет статус `DEGRADED`
  или `NOT_READY`, события `subagent_run_finished` не пишутся,
  но оператор видит причину без отладки
