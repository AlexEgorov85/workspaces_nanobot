## Purpose

Определяет контракт сервиса `DbLoggingService` — фонового
батчевого writer'а событий агента в PostgreSQL: публичный
API, lifecycle, поведение при недоступности БД, диагностика
и наблюдаемость. Это нормативный источник для всех мест,
где упоминается `DbLoggingService` (раньше был описан только
в docstring `lib/services/db_logging_service.py` и
`docs/ARCHITECTURE.md` без явного контракта).

## ADDED Requirements

### Requirement: Батчевая запись с настраиваемой latency

The system SHALL записывать события в `agent_gateway_logs`
батчами через пул `utils.db`. Параметры SHALL быть
конфигурируемыми через `logging.db` секцию `config.json`:

- `flush_interval_sec` — интервал flush'а в секундах,
  диапазон `0.5 ≤ value ≤ 60.0`, дефолт `5.0`;
- `batch_size` — максимум событий в одном INSERT, дефолт 100;
- `retention_days` — возраст старше которого события удаляются,
  `0` = без авто-удаления;
- `purge_interval_sec` — интервал периодической очистки,
  дефолт `3600.0`;
- `summary_max_chars` — максимум символов `summary`, дефолт 200;
- `queue_maxsize` — ёмкость очереди, дефолт 10000.

Worker-поток SHALL пробуждаться не реже `flush_interval_sec`
и flush'ить батч либо при `len(buffer) >= batch_size`, либо
по достижении дедлайна. Запись SHALL идти через общий пул
`utils.db.run(lambda conn: ...)` — собственный psycopg2-коннект
сервис НЕ держит.

#### Scenario: Дефолтный flush_interval_sec
- **WHEN** в `config.json` отсутствует `logging.db.flush_interval_sec`
- **THEN** сервис использует дефолт `5.0` и событие становится
  видимым в `agent_gateway_logs` через 5–15 секунд после
  `log_event` (зависит от фазы цикла worker'а)

#### Scenario: Ускоренный flush
- **WHEN** `config.json::logging.db.flush_interval_sec = 1.0`
- **THEN** сервис валидирует значение (в диапазоне),
  использует `1.0`, события видны через 1–3 секунды

### Requirement: Поведение при недоступности БД

The system SHALL при отсутствии DSN или при ошибке flush'а
батча выбрасывать события (без записи в JSONL-fallback) и
инкрементировать счётчик `failed` в `get_stats()`. Сервис
SHALL НЕ молча терять события: каждая потерянная запись
должна быть видна в `stats["failed"]` и `stats["last_error"]`.

#### Scenario: БД недоступна на старте
- **WHEN** `start()` вызван, но `self._dsn == ""`
- **THEN** все `log_event(...)` возвращают `True` (событие
  поставлено в очередь), но `_flush_batch` вызывает
  `_drop_batch`, `stats["failed"]` растёт на длину батча,
  `stats["last_error"]` содержит причину

### Requirement: Диагностика — счётчики по типам

The system SHALL вести в `get_stats()` счётчик
`written_by_type: dict[str, int]`, где ключ — `event_type`,
значение — количество успешно записанных событий этого типа
с момента старта сервиса. Счётчик SHALL инкрементироваться
в `_flush_batch` после успешного INSERT (по числу строк в
батче, разнесённому по `event_type`).

#### Scenario: Видно, что run_finished пишется
- **WHEN** в течение сессии записано 5 `tool_call`, 5 `tool_result`,
  0 `run_finished`, 0 `subagent_run_finished`
- **THEN** `get_stats()["written_by_type"]` возвращает
  `{"tool_call": 5, "tool_result": 5}` без ключей `run_finished`
  и `subagent_run_finished`

#### Scenario: Видно, что run_finished теряется
- **WHEN** хук `DatabaseLoggingHook.after_run` вызывается
  3 раза, но в `get_stats()["written_by_type"]["run_finished"]`
  приращение 0
- **THEN** это явный сигнал потери события между
  `hook.after_run → _enqueue → _flush_batch`

### Requirement: Диагностика — возраст очереди

The system SHALL вычислять и публиковать в `get_stats()`
поле `oldest_queued_age_sec: float | None` — возраст самого
старого события в очереди `queue.Queue` в секундах. Если
очередь пуста, SHALL возвращать `None`. Сервис SHALL
вычислять это поле «лениво» (только при вызове `get_stats()`),
без отдельного потока.

#### Scenario: Здоровая очередь
- **WHEN** очередь пуста
- **THEN** `get_stats()["oldest_queued_age_sec"] == None`

#### Scenario: Задержка flush'а
- **WHEN** `flush_interval_sec=5.0`, событие добавлено в
  очередь 12 секунд назад, но ещё не flush'нуто
  (например, БД медленная)
- **THEN** `get_stats()["oldest_queued_age_sec"] ≈ 12.0`,
  это сигнал деградации flush'а

### Requirement: Не проглатывать ошибки подключения хуков

The system SHALL при подключении `DatabaseLoggingHook` и
runtime-patcher'а subagent-логирования к `db_logging_service`
логировать (через `loguru.logger.warning` или аналог) причину,
по которой хук/patcher был отключён, если `db_logging_service`
равен `None` или API nanobot изменилось. Текущее поведение
`runtime_patcher.patch_subagent_logging` (return `(False, reason)`)
MUST дополняться warning-логом на стороне вызывающего
(`ApplicationContext.start()` или эквивалент), чтобы потеря
событий не была «тихой».

#### Scenario: db_logging_service недоступен при старте
- **WHEN** `ApplicationContext.start()` вызывается, но
  `db_logging_service is None` на момент `apply_all`
- **THEN** `loguru.logger.warning("patch_subagent_logging
  skipped: db_logging_service is None")` записывается в лог,
  `runtime_health` помечает компонент `db_logging` как
  `DEGRADED` (если применимо)
