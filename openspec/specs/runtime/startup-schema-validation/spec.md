# Startup Schema Validation

## Purpose

Гарантирует, что gateway не поднимется с битой схемой БД: перед стартом
сервисов выполняется проверка наличия обязательных runtime-таблиц,
имена которых зависят от активного профиля конфигурации. Если хотя
бы одна таблица отсутствует — старт блокируется с диагностическим
сообщением, содержащим полный список недостающих таблиц в формате
`schema.table`.

## Scope

`agent` — pre-startup проверка runtime-таблиц принадлежит агенту; у платформы своя, для её собственных таблиц
Реализация: `lib/services/schema_validation.py`

## Requirements

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
захардкожен в коде проверки. Источник истины для имён 5 runtime-
таблиц — `config.EXPECTED_RUNTIME_TABLE_NAMES[profile]`,
который уже используется механизмом `validate_runtime_isolation`.
`channels.postgres.claims_table` в перечень НЕ входит: таблица
аренды `agent_worker_claims` удалена миграцией
`sql/migrations/V006__drop_agent_worker_claims.sql`.

#### Scenario: Prod-профиль
- **WHEN** активен профиль `prod`
- **THEN** проверяются имена из
  `EXPECTED_RUNTIME_TABLE_NAMES["prod"]` (5 таблиц:
  `agent_conversation_messages`, `agent_session_meta`,
  `agent_session_messages`, `agent_gateway_logs`,
  `agent_question_runs`)

#### Scenario: Test-профиль
- **WHEN** активен профиль `test`
- **THEN** проверяются имена из
  `EXPECTED_RUNTIME_TABLE_NAMES["test"]` (5 таблиц:
  `agent_conversation_messages_test`,
  `agent_session_meta_test`, `agent_session_messages_test`,
  `agent_gateway_logs_test`, `agent_question_runs_test`)

### Requirement: Сообщение об ошибке содержит список недостающих таблиц

Сообщение, выводимое при блокировке старта, SHALL включать:
1. Человекочитаемую причину на русском языке («не найдены
   обязательные runtime-таблицы» и т.п.); английский — только
   для имён собственных (имена таблиц, имя профиля, имена
   config-ключей, имена файлов и CLI-команд).
2. Полный список недостающих таблиц в формате `schema.table`
   (по одной на строку), отсортированный в порядке `_EXPECTED_KEYS`
   (детерминированный вывод).
3. Имя активного профиля.
4. **Actionable-подсказку с конкретной командой**, зависящей
   от профиля:
   - профиль `prod` → `python tools/migrate.py --apply`;
   - профиль `test` → `python tools/apply_test_profile_tables.py`;
   - любой другой профиль → generic-вариант
     («примените миграции для выбранного профиля»).

Никакие чувствительные данные (пароли, DSN, секреты) в сообщении
появляться не должны. Сообщение формируется через `__str__`
исключения и попадает в `stderr` (`gateway.main()` / `cli_agent.main()`)
и в loguru-логгер уровня ERROR.

#### Scenario: Вывод в лог при missing
- **WHEN** старт блокируется из-за отсутствия таблиц
- **THEN** в `stderr` и в loguru-логгер уровня ERROR выводится
  структурированное сообщение с указанными выше полями

#### Scenario: Вывод в лог при missing (профиль prod)
- **WHEN** старт блокируется из-за отсутствия таблиц при профиле `prod`
- **THEN** в `stderr` и в loguru-логгер уровня ERROR выводится
  сообщение на русском, содержащее имя профиля, список
  недостающих таблиц и подсказку
  `python tools/migrate.py --apply`

#### Scenario: Вывод в лог при missing (профиль test)
- **WHEN** старт блокируется из-за отсутствия таблиц при профиле `test`
- **THEN** в `stderr` и в loguru-логгер уровня ERROR выводится
  сообщение на русском, содержащее имя профиля, список
  недостающих таблиц и подсказку
  `python tools/apply_test_profile_tables.py`

#### Scenario: Вывод в лог при missing (неизвестный профиль)
- **WHEN** старт блокируется при профиле, не равном `prod` и не `test`
- **THEN** подсказка — generic-вариант
  («примените миграции для выбранного профиля»), без указания
  конкретной команды

### Requirement: Проверка выполняется через пул соединений БД

Проверка SHALL использовать тот же пул соединений, что и остальные
сервисы (`utils.db` / `get_pool`). Предел времени SHALL выставляться как
`statement_timeout` на соединении воркера и SHALL сниматься после запроса
(в том числе при отказе) — соединение возвращается в пул общим, и незакрытый
предел уехал бы в чужие запросы. По истечении предела SHALL выбрасываться
`SchemaValidationTimeoutError`, а не подменяться «отсутствием таблиц».
Прочие ошибки БД (`OperationalError` и прочие) SHALL пробрасываться как есть:
отмена запроса — подкласс `OperationalError` в PostgreSQL, поэтому ловление
родителя замаскировало бы любую другую ошибку под «не уложился в таймаут».

#### Scenario: БД недоступна
- **WHEN** пул не может выполнить SELECT за отведённый таймаут
- **THEN** проверка падает с `SchemaValidationTimeoutError`, а не
  маскирует её под «отсутствие таблиц»

#### Scenario: Превышение предела не оставляет соединение сломанным
- **WHEN** SELECT отменён сервером по истечении `statement_timeout`
- **THEN** прерванная транзакция откатывается, предел снимается, и то же
  соединение годится для следующего владельца пула

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

### Requirement: Сообщение об ошибке для отсутствующих ключей конфига

`_MissingConfigKeys` (наследник `SchemaValidationError`,
выбрасывается, когда в settings отсутствуют ожидаемые ключи)
SHALL формировать сообщение на русском, содержащее:

1. Указание причины («не найдены обязательные ключи конфигурации»).
2. Список недостающих ключей в формате `channels.postgres.*` /
   `logging.db.*` (по одному на строку).
3. Имя активного профиля.
4. Подсказку с указанием конкретной секции `config.json`
   (`channels.postgres.*` / `logging.db.*`), где ключ должен быть
   определён.

Никакие чувствительные данные (пароли, DSN, секреты) в сообщении
появляться не должны. Сообщение формируется через `__str__`
исключения и попадает в `stderr` и в loguru-логгер уровня ERROR.

#### Scenario: Сообщение при missing config keys
- **WHEN** в settings отсутствуют ожидаемые ключи
- **THEN** `_MissingConfigKeys.__str__` возвращает сообщение
  на русском, содержащее имя профиля, список недостающих ключей
  и подсказку про секцию `config.json`

## Responsibility

- Pre-startup проверка наличия 5 runtime-таблиц в БД.
- Жёсткая блокировка старта (`exit 2` + `stderr`) при отсутствии любой
  из таблиц.
- Резолв списка ожидаемых таблиц ТОЛЬКО из merged SETTINGS
  (`channels.postgres.*` + `logging.db.*`) — без зашитых в код имён.
- Уважение опционального gate `gateway.startup.schema_validation.enabled`.

## Boundary

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

## Public Contract

- `SchemaValidationService.expected_table_names(settings) -> list[tuple[str, str]]`
  — извлекает 5 ожидаемых имён из merged SETTINGS (порядок и схема
  `public` фиксированы).
- `SchemaValidationService.check_tables(fetch, expected, *, timeout_sec)`
  — выполняет один SELECT к `information_schema.tables`,
  возвращает `list[MissingTable]` (пустой, если всё на месте).
- `SchemaValidationService.validate(settings, *, fetch, timeout_sec)`
  — верхний уровень: ожидаемые → проверка → `raise SchemaValidationError`.
- `utils.db.fetch_with_timeout(sql, *args, *, timeout_sec)` — адаптер,
  которым `ApplicationContext` подменяет «голый» `fetch`. Механизм
  предела живёт здесь, а не в сервисе проверки: соединение принадлежит
  пулу, и держать открытый доступ к соединениям в потребителе нечего.
  Плоский `utils.db.fetch` тоже годен — он просто ничего не ограничивает.
- `SchemaValidationTimeoutError(ConfigurationError)` — отказ по превышении
  предела. Намеренно **не** подкласс `SchemaValidationError`: таймаут не
  означает «нет таблиц», и отправлять оператора применять миграции там,
  где нужен DBA, — враньё.
- `ApplicationContext._validate_runtime_schema(self) -> None` —
  приватный метод, вызывается из `start()`.

## Inputs

- `SETTINGS` — merged-конфигурация агента (сырой `dict` или `_LazySettings`
  proxy; последний разворачивается через `_unwrap_settings`). Из неё берутся
  пять ожидаемых имён по ключам `channels.postgres.{table_name,
  messages_table, meta_table}` и `logging.db.{table_name,
  question_runs_table}`, плюс `profile` для текста отказа. Литералов имён в
  коде проверки нет: переименование таблицы меняет конфигурацию, а не код;
- `fetch` — адаптер с сигнатурой `(sql, *params) -> list[dict]`, обычно
  `utils.db.fetch_with_timeout`. Предел времени реализует адаптер, потому что
  соединение принадлежит пулу, а не этому модулю;
- `timeout_sec` — предел, который попадает в текст отказа: решение читает
  сообщение и видит конкретную цифру, а не «неизвестный таймаут»;
- флаг включения и сам предел — из конфигурации (см. `## Configuration`),
  то есть тоже через `SETTINGS`.

Схема проверки в коде зафиксирована как `public`: имя таблицы приходит из
конфигурации, имя схемы — нет.

## Outputs

- `None` при успехе: проверка ничего не возвращает, а не «список таблиц»,
  потому что вызывающей стороне нужно только «можно начинать»;
- `list[tuple[str, str]]` из `expected_table_names()` — ожидаемые пары
  `(schema, table_name)`, детерминированно в порядке `_EXPECTED_KEYS`;
- `list[MissingTable]` из `check_tables()` — недостающие таблицы в том же
  порядке; пустой список означает «всё на месте»;
- `SchemaValidationError.missing` и `.profile` — на отказе оператор получает
  и перечень, и контур;
- запись об отказе в лог: `profile=` и список отсутствующих полных имён.

## State

После успешного старта компонент **не имеет состояния**: проверка
одноразовая, idempotent.

## Dependencies

- `config.ConfigurationError` — базовый класс для
  `SchemaValidationError`.
- `utils.db.fetch` — адаптер для SELECT (psycopg2-пул).
- `lib.core.project_settings.StartupSchemaValidationSettings` —
  pydantic-валидация конфигурации.
- `lib.core.application_context.ApplicationContext` — место вызова
  `_validate_runtime_schema` в `start()`.

## Configuration

| Ключ | Тип | Default | Описание |
|------|-----|---------|----------|
| `gateway.startup.schema_validation.enabled` | bool | `true` | Включить pre-startup проверку схемы |
| `gateway.startup.schema_validation.timeout_sec` | float | `5.0` | Таймаут SELECT к `information_schema.tables` (диапазон `0.1 ≤ value ≤ 60.0`) |

## Lifecycle

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

## Data Ownership

Владеет:

- **всем состоянием проверки в рамках одного вызова**: списком ожидаемых
  пар и списком недостающих. Они живут внутри вызова и наружу не отдаются
  как хранилище — компонент stateless (см. `## State`);
- правом на единственный SELECT к `information_schema.tables`: он читает
  каталог, а не данные таблиц, и никаких строк из пользовательских таблиц не
  выбирает.

Не владеет:

- соединением: `fetch` — адаптер поверх общего пула `utils.db`, и сам
  сервис соединение не открывает и не держит;
- именами runtime-таблиц: они принадлежат конфигурации
  (`channels.postgres.*`, `logging.db.*`), а проверка их только читает и
  сверяет с каталогом;
- содержимым и схемой таблиц, миграциями и DDL: проверка только спрашивает,
  есть ли таблица; создавать и менять её — не её работа;
- фактом «база в порядке» вообще: успешная проверка означает лишь, что пять
  имён найдены на момент вызова, и никакой гарантии дальше это не даёт.

## Error Behavior

| Ситуация | Поведение |
|----------|-----------|
| Все 5 таблиц на месте | No-op (старт продолжается) |
| 1+ таблиц отсутствуют | `SchemaValidationError` → `exit 2` |
| Ключ `channels.postgres.*` отсутствует в settings | `_MissingConfigKeys` (наследник `SchemaValidationError`) → `exit 2` |
| Ключ `logging.db.*` отсутствует в settings | `_MissingConfigKeys` → `exit 2` |
| `OperationalError` / `RuntimeError` от пула | Поднимается наверх, **не маскируется** |
| `gateway.startup.schema_validation.enabled = false` | No-op + WARNING в логе |

## Invariants

- Любой startup-error (отсутствие таблиц, отсутствие ключей в
  settings, ошибка БД) идёт через `ConfigurationError` →
  `exit 2` + `stderr`.
- Имена 5 runtime-таблиц ВСЕГДА резолвятся из
  `SETTINGS["channels"]["postgres"]` + `SETTINGS["logging"]["db"]`,
  не из кода проверки.
- `SchemaValidationError.missing: list[MissingTable]` отсортирован
  в порядке `_EXPECTED_KEYS` (детерминированный вывод).

## Forbidden Behavior

- Захардкоженные имена таблиц в коде проверки.
- Авто-создание недостающих таблиц.
- Маскировка ошибок БД (`OperationalError`, `InterfaceError`)
  под «отсутствие таблиц».
- Регистрация `SchemaValidationService` как `RuntimeReadiness`
  компонента.
- Параметризация SQL через f-string или `%` (только
  `%s`-placeholder'ы с tuple-параметрами).

## Consumers

- `gateway.py:main()` — startup-boundary.
- `cli_agent.py:main()` — startup-boundary.
## Implementation

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

## Verification

- `python -m pytest tests/test_schema_validation.py -q` — 20
  unit-тестов.
- `python -m pytest tests/test_application_context_schema_validation.py -q` — 7
  unit-тестов.
- `python -m pytest tests/test_gateway_entrypoint_schema_validation.py -q` — 1
  boundary-тест.
- `openspec.cmd validate startup-schema-validation` — зелёный.

