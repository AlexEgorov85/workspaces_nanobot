# Startup Schema Validation

## Purpose

Гарантирует, что gateway не поднимется с битой схемой БД: перед стартом
сервисов выполняется проверка наличия обязательных runtime-таблиц,
имена которых зависят от активного профиля конфигурации. Если хотя
бы одна таблица отсутствует — старт блокируется с диагностическим
сообщением, содержащим полный список недостающих таблиц в формате
`schema.table`.

## Ответственность

- Pre-startup проверка наличия 6 runtime-таблиц в БД.
- Жёсткая блокировка старта (`exit 2` + `stderr`) при отсутствии любой
  из таблиц.
- Резолв списка ожидаемых таблиц ТОЛЬКО из merged SETTINGS
  (`channels.postgres.*` + `logging.db.*`) — без зашитых в код имён.
- Уважение опционального gate `gateway.startup.schema_validation.enabled`.

## Граница

### Owns

- Метод `ApplicationContext._validate_runtime_schema()` —
  вызывается из `start()` сразу после `_start_db_pool()`.
- Класс `lib.services.schema_validation.SchemaValidationService`.
- Исключение `SchemaValidationError` (наследник `ConfigurationError`).
- Тип `MissingTable`.

### Does not own

- Имена runtime-таблиц (живут в `config.EXPECTED_RUNTIME_TABLE_NAMES`
  + `SETTINGS` после `_initialize_settings`).
- Применение миграций (`tools/migrate.py`,
  `tools/apply_test_profile_tables.py`).
- Auto-create таблиц.
- Health-check / runtime-readiness — это отдельный компонент
  (`runtime/runtime-events-subscription` + `data/vector-indexes`).
- Проверка других таблиц (`oarb.*`, skill-таблицы, benchmarks,
  legacy `agent_vector_index_*`) — не входит в объём.

### May depend on

- `config.ConfigurationError` (для boundary с gateway/cli entrypoint).
- `utils.db.fetch` (psycopg2-пул) — адаптер для SELECT.
- `lib.core.project_settings.StartupSchemaValidationSettings` —
  pydantic-валидация `gateway.startup.schema_validation.*`.

### Must not depend on

- Upstream nanobot (никакой логики оттуда).
- Конкретных имён таблиц в коде проверки.
- Сетевых ресурсов вне пула `utils.db`.

## Публичный контракт

- `SchemaValidationService.expected_table_names(settings) -> list[tuple[str, str]]`
  — извлекает 6 ожидаемых имён из merged SETTINGS (порядок и схема
  `public` фиксированы).
- `SchemaValidationService.check_tables(fetch, expected, *, timeout_sec)`
  — выполняет один SELECT к `information_schema.tables`,
  возвращает `list[MissingTable]` (пустой, если всё на месте).
- `SchemaValidationService.validate(settings, *, fetch, timeout_sec)`
  — верхний уровень: ожидаемые → проверка → `raise SchemaValidationError`.
- `ApplicationContext._validate_runtime_schema(self) -> None` —
  приватный метод, вызывается из `start()`.

## Требования

### Requirement: Pre-startup runtime schema gate

Gateway SHALL запускать проверку наличия обязательных runtime-таблиц
ДО подъёма каких-либо сервисов (channels, db_logging, sync, runtime
patches, preload, hooks). Проверка SHALL выполняться ровно один раз
на lifecycle-этапе `ApplicationContext.start()` (или эквивалентном
месте, определённом в design.md) и SHALL быть идемпотентной.

#### Scenario: Все таблицы существуют
- **WHEN** все обязательные runtime-таблицы присутствуют в БД
- **THEN** проверка завершается без побочных эффектов и старт
  продолжается штатно

#### Scenario: Хотя бы одна таблица отсутствует
- **WHEN** одна или несколько обязательных таблиц отсутствуют в БД
- **THEN** старт блокируется, в логе/stderr выводится сообщение
  со списком недостающих таблиц в формате `schema.table`, и
  ApplicationContext переходит в состояние «не запущен» (exit 2
  на уровне entrypoint)

### Requirement: Список обязательных таблиц зависит от профиля

Список таблиц, проверяемых на старте, SHALL вычисляться
динамически из активного профиля конфигурации (`--profile`), а не
захардкожен в коде проверки. Источник истины для имён 6 runtime-
таблиц — `config.EXPECTED_RUNTIME_TABLE_NAMES[profile]`
(`config.py:205-222`), который уже используется механизмом
`validate_runtime_isolation`.

#### Scenario: Prod-профиль
- **WHEN** активен профиль `prod`
- **THEN** проверяются имена из
  `EXPECTED_RUNTIME_TABLE_NAMES["prod"]` (6 таблиц:
  `agent_conversation_messages`, `agent_session_meta`,
  `agent_session_messages`, `agent_worker_claims`,
  `agent_gateway_logs`, `agent_question_runs`)

#### Scenario: Test-профиль
- **WHEN** активен профиль `test`
- **THEN** проверяются имена из
  `EXPECTED_RUNTIME_TABLE_NAMES["test"]` (6 таблиц:
  `agent_conversation_messages_test`,
  `agent_session_meta_test`, `agent_session_messages_test`,
  `agent_worker_claims_test`, `agent_gateway_logs_test`,
  `agent_question_runs_test`)

### Requirement: Сообщение об ошибке содержит список недостающих таблиц

Сообщение, выводимое при блокировке старта, SHALL включать:
1. Человекочитаемую причину («не найдены обязательные
   runtime-таблицы»).
2. Полный список недостающих таблиц в формате `schema.table`
   (по одной на строку).
3. Имя активного профиля.
4. Подсказку, что для создания таблиц нужно применить миграции
   (без привязки к конкретным командам — текст нейтральный).

Никакие чувствительные данные (пароли, DSN, секреты) в сообщении
появляться не должны.

#### Scenario: Вывод в лог при missing
- **WHEN** старт блокируется из-за отсутствия таблиц
- **THEN** в `stderr` и в loguru-логгер уровня ERROR выводится
  структурированное сообщение с указанными выше полями

### Requirement: Проверка выполняется через пул соединений БД

Проверка SHALL использовать тот же пул соединений, что и остальные
сервисы (`utils.db` / `get_pool`). При недоступности БД SHALL
выбрасываться отдельная ошибка (`OperationalError` /
`RuntimeError`) — не подменяться «отсутствием таблиц».

#### Scenario: БД недоступна
- **WHEN** пул не может выполнить SELECT за отведённый таймаут
- **THEN** проверка падает с ошибкой недоступности БД, а не
  маскирует её под «отсутствие таблиц»

#### Scenario: БД доступна, таблиц нет
- **WHEN** пул соединений работает, но таблицы отсутствуют
- **THEN** проверка возвращает отличимый результат «отсутствуют
  таблицы X, Y, Z» (а не ошибку соединения)

### Requirement: Изоляция проверки от hot-path

Проверка SHALL выполняться один раз за старт и SHALL NOT
регистрироваться как `RuntimeReadiness`-проверка (не дублирует
уже существующие `postgres` / `duckdb_cache` / `vector_search`).
Результат проверки не должен влиять на readiness-флаг после
успешного старта.

#### Scenario: Успешный старт не оставляет следов проверки в readiness
- **WHEN** проверка прошла успешно
- **THEN** `RuntimeReadiness` не содержит дополнительных компонентов
  с именем `schema_validation` (или эквивалентным)

### Requirement: Отсутствие ложных блокировок при существующих данных

Проверка SHALL различать ситуации «таблица существует» и
«таблица не существует» строго через обращение к
`information_schema.tables` с фильтром `table_type = 'BASE TABLE'`.
Не SHALL использовать эвристики «таблица есть, но пустая» /
«DROP TABLE IF EXISTS».

#### Scenario: Таблица существует, пустая
- **WHEN** таблица существует в каталоге, но не содержит строк
- **THEN** проверка считает её присутствующей и не блокирует
  старт

## Запрещённое поведение

- Захардкоженные имена таблиц в коде проверки.
- Авто-создание недостающих таблиц.
- Маскировка ошибок БД (`OperationalError`, `InterfaceError`)
  под «отсутствие таблиц».
- Регистрация `SchemaValidationService` как `RuntimeReadiness`
  компонента.
- Параметризация SQL через f-string или `%` (только
  `%s`-placeholder'ы с tuple-параметрами).

## Зависимости

- `config.ConfigurationError` — базовый класс для
  `SchemaValidationError`.
- `utils.db.fetch` — адаптер для SELECT (psycopg2-пул).
- `lib.core.project_settings.StartupSchemaValidationSettings` —
  pydantic-валидация конфигурации.
- `lib.core.application_context.ApplicationContext` — место вызова
  `_validate_runtime_schema` в `start()`.

## Реализация

- `lib/services/schema_validation.py` — `SchemaValidationService`,
  `MissingTable`, `SchemaValidationError`.
- `lib/core/project_settings.py` —
  `StartupSchemaValidationSettings`, `StartupSettings` (поля
  `gateway.startup.schema_validation.{enabled, timeout_sec}`).
- `lib/core/application_context.py` — приватный метод
  `_validate_runtime_schema` + вызов из `start()`.
- Тесты: `tests/test_schema_validation.py` (20 unit),
  `tests/test_application_context_schema_validation.py` (7 unit),
  `tests/test_gateway_entrypoint_schema_validation.py` (1 boundary).

## Проверка

- `python -m pytest tests/test_schema_validation.py -q` — 20
  unit-тестов.
- `python -m pytest tests/test_application_context_schema_validation.py -q` — 7
  unit-тестов.
- `python -m pytest tests/test_gateway_entrypoint_schema_validation.py -q` — 1
  boundary-тест.
- `openspec.cmd validate startup-schema-validation` — зелёный.

## Конфигурация

| Ключ | Тип | Default | Описание |
|------|-----|---------|----------|
| `gateway.startup.schema_validation.enabled` | bool | `true` | Включить pre-startup проверку схемы |
| `gateway.startup.schema_validation.timeout_sec` | float | `5.0` | Таймаут SELECT к `information_schema.tables` (диапазон `0.1 ≤ value ≤ 60.0`) |

## Жизненный цикл

1. `ApplicationContext.create()` — конфигурирует сервисы, но ещё
   не запускает их.
2. `ApplicationContext.start()`:
   1. `mark_started()`
   2. `apply_template_overrides()`
   3. `_start_db_pool()`
   4. **`_validate_runtime_schema()`** ← здесь
   5. `RuntimeEventsSubscriber.start()`
   6. `db_logging_service.start()`
   7. `sync_service.start()`
3. При пропуске шага 4 или его падении — старт блокируется,
   `ApplicationContext._started = False`, последующие шаги не
   выполняются. `gateway.main()` ловит `ConfigurationError` →
   `exit 2` + `stderr`.

## Состояние

После успешного старта компонент **не имеет состояния**: проверка
одноразовая, idempotent.

## Инварианты

- Любой startup-error (отсутствие таблиц, отсутствие ключей в
  settings, ошибка БД) идёт через `ConfigurationError` →
  `exit 2` + `stderr`.
- Имена 6 runtime-таблиц ВСЕГДА резолвятся из
  `SETTINGS["channels"]["postgres"]` + `SETTINGS["logging"]["db"]`,
  не из кода проверки.
- `SchemaValidationError.missing: list[MissingTable]` отсортирован
  в порядке `_EXPECTED_KEYS` (детерминированный вывод).

## Поведение при ошибке

| Ситуация | Поведение |
|----------|-----------|
| Все 6 таблиц на месте | No-op (старт продолжается) |
| 1+ таблиц отсутствуют | `SchemaValidationError` → `exit 2` |
| Ключ `channels.postgres.*` отсутствует в settings | `_MissingConfigKeys` (наследник `SchemaValidationError`) → `exit 2` |
| Ключ `logging.db.*` отсутствует в settings | `_MissingConfigKeys` → `exit 2` |
| `OperationalError` / `RuntimeError` от пула | Поднимается наверх, **не маскируется** |
| `gateway.startup.schema_validation.enabled = false` | No-op + WARNING в логе |

## Потребители

- `gateway.py:main()` — startup-boundary.
- `cli_agent.py:main()` — startup-boundary.
- `streamlit_app.py` (через ApplicationContext) — startup-boundary.
