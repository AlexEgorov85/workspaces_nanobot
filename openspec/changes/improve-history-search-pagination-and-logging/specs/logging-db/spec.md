## Purpose

Определяет **только те аспекты контракта** сервиса
`DbLoggingService`, которые этот change реально изменяет:
настройка flush-интервала через конфигурацию и публикация
диагностических счётчиков в `get_stats()`. Существующее
поведение (`retention`, `purge`, batching, `question_runs`,
поведение при недоступности БД) этим change **не
переспецифицируется** — оно остаётся как реализовано.

## ADDED Requirements

### Requirement: Настраиваемый flush_interval_sec

The system SHALL принимать параметр `flush_interval_sec`
через конструктор `DbLoggingService` со следующими
инвариантами:

- Диапазон: `0.5 ≤ value ≤ 60.0` (секунды).
- Дефолт: `5.0`.
- Значение SHALL передаваться из resolved `SETTINGS`
  (`logging.db.flush_interval_sec`) через
  `ConfigurationResolver` → `ProjectSettings` →
  `ApplicationContext` → конструктор `DbLoggingService`.
  Сервис НЕ читает `config.json`/`project.json` напрямую.
- **Boundary ошибок валидации** (двухуровневый):
  - На уровне **модели** `LoggingDbSettings` —
    `pydantic.ValidationError` при выходе за диапазон
    (юнит-тест модели ловит именно `ValidationError`).
  - На уровне **resolver / конфигурации** —
    `ConfigurationError` (см. AGENTS.md «Профили
    конфигурации»), если `ConfigurationResolver` /
    `validate_project_settings` не может построить
    `ProjectSettings` (например, опечатка в имени секции,
    битый JSON). Интеграционный тест на пути
    `project.json → ConfigurationResolver → ProjectSettings`
    ловит именно `ConfigurationError`.
  - В обоих случаях — fail-fast, до старта сервиса.
- Поведение worker'а (батчевый flush по `flush_interval_sec`
  или `batch_size`, дедлайн-цикл с таймаутом) SHALL остаться
  как описано в `lib/services/db_logging_service.py:548-606`.

#### Scenario: Дефолтное значение
- **WHEN** в `project.json` отсутствует `logging.db.flush_interval_sec`
- **THEN** валидация `LoggingDbSettings` принимает дефолт `5.0`,
  `DbLoggingService._flush_interval == 5.0`

#### Scenario: Ускоренный flush
- **WHEN** `project.json::logging.db.flush_interval_sec = 1.0`
- **THEN** валидация принимает значение,
  `DbLoggingService._flush_interval == 1.0`,
  события в среднем видны в БД через 1–3 секунды

#### Scenario: Значение вне диапазона — ValidationError на модели
- **WHEN** `LoggingDbSettings(flush_interval_sec=0.1)`
  вызван напрямую (юнит-тест модели)
- **THEN** `pydantic.ValidationError` бросается
  с указанием диапазона, `ApplicationContext` НЕ
  вовлекается

#### Scenario: Битый project.json — ConfigurationError на resolver
- **WHEN** `project.json` содержит невалидный JSON
  или отсутствует обязательная секция, через которую
  валидируется `flush_interval_sec`
- **THEN** `ConfigurationResolver` / `validate_project_settings`
  бросает `ConfigurationError`, `ApplicationContext.start()`
  НЕ создаёт `DbLoggingService` (fail-fast)

#### Scenario: Значение передаётся через resolver chain
- **WHEN** `SETTINGS` сформирован `ConfigurationResolver`
  с `logging.db.flush_interval_sec = 2.0`
- **THEN** runtime-тест проверяет:
  `db_logging_service._flush_interval == 2.0`

### Requirement: Счётчик written_by_type

The system SHALL вести в `get_stats()` счётчик
`written_by_type: dict[str, int]`, где ключ — `event_type`,
значение — количество успешно записанных событий этого
типа.

Инварианты:

- Счётчик инкрементируется **только** после успешного
  `_flush_batch` (то есть когда INSERT в БД прошёл без
  исключения). В `_enqueue` инкремент ЗАПРЕЩЁН —
  это различает «поставлено в очередь» и «реально
  записано».
- При ошибке `_flush_batch` события из батча НЕ учитываются
  в `written_by_type` (они идут в `failed`).
- Срок жизни счётчика = lifetime экземпляра
  `DbLoggingService`. Повторный `start()` после `stop()`
  НЕ сбрасывает счётчик (диагностический сервис не
  теряет историю на restart worker'а).

#### Scenario: Видно, что run_finished пишется
- **WHEN** в течение сессии записано 5 `tool_call`,
  5 `tool_result`, 3 `run_finished`, 0 `subagent_run_finished`
- **THEN** `get_stats()["written_by_type"]` возвращает
  `{"tool_call": 5, "tool_result": 5, "run_finished": 3}`
  без ключа `subagent_run_finished`

#### Scenario: Счётчик не растёт при ошибке flush'а
- **WHEN** `_flush_batch` бросает исключение (например,
  БД недоступна) для батча из 3 `tool_call`
- **THEN** `stats["failed"]` инкрементируется на 3,
  `stats["written_by_type"]["tool_call"]` НЕ изменяется

#### Scenario: Счётчик не сбрасывается при restart
- **WHEN** экземпляр `DbLoggingService` прошёл
  `stop()` (с `written_by_type={"tool_call": 5}`),
  затем `start()` вызван повторно, и записан ещё 1 `tool_call`
- **THEN** `get_stats()["written_by_type"]["tool_call"] == 6`

### Requirement: Метрика oldest_queued_age_sec

The system SHALL публиковать в `get_stats()` поле
`oldest_queued_age_sec: float | None` — возраст самого
старого `LogEvent`, ожидающего записи в
`agent_gateway_logs`, в секундах.

Инварианты:

- Метрика SHALL учитывать только объекты `LogEvent`.
  `_QuestionRunRecord` (отдельная таблица `agent_question_runs`)
  и `_FlushSentinel` (служебный сигнал остановки) MUST NOT
  участвовать в вычислении.
- Если очередь содержит только `_QuestionRunRecord` или
  `_FlushSentinel`, или пуста — SHALL возвращаться `None`.
- Вычисление ленивое (только при вызове `get_stats()`),
  без отдельного потока.
- Для отслеживания возраста `LogEvent` SHALL иметь поле
  `queued_at: float | None`, заполняемое в `_enqueue`
  значением `time.time()`.

#### Scenario: Здоровая очередь
- **WHEN** очередь `LogEvent` пуста
- **THEN** `get_stats()["oldest_queued_age_sec"] is None`

#### Scenario: Задержка flush'а
- **WHEN** `flush_interval_sec=5.0`, событие добавлено в
  очередь 12 секунд назад, но ещё не flush'нуто
- **THEN** `get_stats()["oldest_queued_age_sec"] ≈ 12.0`

#### Scenario: Очередь с _QuestionRunRecord не учитывается
- **WHEN** в очереди есть `_QuestionRunRecord` и пусто
  `LogEvent`
- **THEN** `get_stats()["oldest_queued_age_sec"] is None`

#### Scenario: _FlushSentinel не учитывается
- **WHEN** в очереди только `_FlushSentinel` и нет `LogEvent`
- **THEN** `get_stats()["oldest_queued_age_sec"] is None`
