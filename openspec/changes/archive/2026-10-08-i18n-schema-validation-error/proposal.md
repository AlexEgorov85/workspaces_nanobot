## Why

`SchemaValidationError` блокирует старт gateway/CLI при отсутствии
runtime-таблиц в БД. Сообщение сейчас полностью на английском
("Schema validation failed for profile='…': missing runtime tables:
… hint: apply migrations before starting the gateway") — это
противоречит основному языку проекта (commit-сообщения, спеки,
CHANGELOG и комментарии — на русском), а подсказка слишком общая
("apply migrations"), и оператор не знает, какую именно команду
запустить. Хотим, чтобы сообщение читалось оператором без
перевода и сразу указывало на нужное действие.

## What Changes

- Сообщение `SchemaValidationError` (вывод в stderr при блокировке
  старта) переводится на русский, имена таблиц и профиля
  остаются латиницей как имена собственные.
- Подсказка становится actionable: для профиля `prod` —
  `python tools/migrate.py --apply`, для `test` —
  `python tools/apply_test_profile_tables.py`, для неизвестных
  профилей — generic-вариант ("примените миграции для выбранного
  профиля").
- `_MissingConfigKeys` тоже переводится на русский по той же схеме.
- Существующие тесты обновляются под новые подстроки сообщения.
- Поля исключения (`.missing`, `.missing_config_keys`, `.profile`)
  и публичный API `SchemaValidationService` **не меняются** —
  меняется только текст `__str__`.

## Capabilities

### New Capabilities
- (нет)

### Modified Capabilities
- `runtime/startup-schema-validation`: меняется Requirement
  «Сообщение об ошибке содержит список недостающих таблиц» —
  язык и формат подсказки (neutral hint → actionable per-profile
  hint). Контракт структурных полей и failure-mode остаётся.

## Impact

- `lib/services/schema_validation.py` — текст в `_build_message` и
  `_build_config_message`, добавление helper'а для выбора команды
  по профилю.
- `tests/test_schema_validation.py` — замена английских подстрок
  на русские в `test_message_contains_missing_and_profile` и
  `test_lazy_settings_missing_keys_reports_correctly`.
- `openspec/specs/runtime/startup-schema-validation/spec.md` —
  MODIFIED Requirements.
- Никаких изменений в `config.py`, `lib/core/application_context.py`,
  runtime-patch'ах, миграциях.
- Без новых зависимостей.
