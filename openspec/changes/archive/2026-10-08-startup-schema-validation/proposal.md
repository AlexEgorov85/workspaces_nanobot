## Why

Сейчас gateway может успешно стартовать с битой схемой БД: если профильные
runtime-таблицы (`agent_conversation_messages`, `agent_gateway_logs` и др.)
ещё не созданы в БД, сервисы типа `DbLoggingService`, `PostgresChannel`,
`PGSessionManager` поднимаются, но первая же попытка INSERT/SELECT в
runtime-таблицы падает с `UndefinedTableError` (Greenplum / Postgres),
что приводит к шумным 500-м ошибкам в каналах, потере событий и
невозможности быстро локализовать причину. Нужно, чтобы gateway проверял
наличие обязательных runtime-таблиц на старте и **не поднимался**, если
чего-то не хватает, выводя оператору понятный список недостающих таблиц.
Имена таблиц при этом **не должны быть зашиты в коде** — они должны
резолвиться из уже существующих источников истины (конфиг профиля +
TableRegistry), чтобы при изменении имён не пришлось править код
проверки.

## What Changes

- Добавить новый шаг startup-gate в `ApplicationContext.start()` (или
  сразу после `create()` — место определено в design.md), который
  перед подъёмом любых сервисов проверяет наличие 6 обязательных
  runtime-таблиц в БД.
- Список обязательных таблиц собирается прозрачно:
  - 6 имён из `config.EXPECTED_RUNTIME_TABLE_NAMES[profile]`
    (это уже канонический источник для `channels.postgres.*` +
    `logging.db.*`).
- При обнаружении отсутствующих таблиц — выброс
  `SchemaValidationError` (наследник `ConfigurationError`) с полным
  списком недостающих таблиц в формате `schema.table`, остановка
  ApplicationContext без `start()`, печать в stderr/лог и exit 1
  на уровне gateway.py.
- Добавить отдельный сервис/хелпер `SchemaValidationService`
  (расположение — `lib/services/`) — единая точка проверки и
  сообщения об ошибке, чтобы её можно было вызвать как из
  `ApplicationContext`, так и standalone (например, из утилиты).
- Никаких изменений в SQL/DDL, никаких изменений в формате
  конфига, никаких изменений в `validate_profile_overlay` /
  `validate_runtime_isolation` — change использует их результат,
  не модифицирует.

## Capabilities

### New Capabilities

- `runtime/startup-schema-validation`: описывает контракт
  startup-gate, который перед подъёмом сервисов проверяет наличие
  обязательных runtime-таблиц, зависящих от активного профиля, и
  блокирует старт gateway с понятным сообщением при их отсутствии.

### Modified Capabilities

- Нет. Контракты существующих capabilities (`configuration/profiles`,
  `runtime/context`, `runtime/error-fallback`, `data/cache-provider`)
  не меняются: новый шаг использует их публичные артефакты, не
  изменяет их требования.

## Impact

- Код:
  - новый модуль `lib/services/schema_validation.py`
    (`SchemaValidationService`, `SchemaValidationError`),
  - новый тест-модуль `tests/test_schema_validation.py`,
  - изменения в `lib/core/application_context.py`
    (вызов проверки перед `start()` или в начале `start()`),
  - изменения в `gateway.py` (catch `SchemaValidationError` →
    печать + exit 1),
  - обновление `lib/services/__init__.py` (если экспорт
    публичных символов через `__init__.py`),
  - регистрация новой capability в `openspec/specs/COMPONENTS.md`
    после реализации.
- API:
  - публичный экспорт `SchemaValidationService` и
    `SchemaValidationError` из `lib.services.schema_validation`.
- Конфигурация: без изменений (используем существующие ключи).
- SQL/DDL: без изменений.
- Операторы: при развёртывании без выполненных миграций
  (`tools/migrate.py --apply` / `tools/apply_test_profile_tables.py`)
  увидят чёткий список недостающих таблиц и не получат работающий,
  но сломанный gateway.
