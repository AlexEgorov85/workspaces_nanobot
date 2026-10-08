> **Состояние на момент архивации (2026-10-08).** Чекбоксы ниже не
> проставлены: работа выполнена и заведена в канон напрямую —
> `lib/services/schema_validation.py`, `StartupSchemaValidationSettings`
> в `lib/core/project_settings.py`, `AGENTS.md`, `COMPONENTS.md`,
> CHANGELOG и 37 тестов в трёх файлах (`test_schema_validation.py`,
> `test_application_context_schema_validation.py`,
> `test_gateway_entrypoint_schema_validation.py`, все зелёные).
> Протух только `tasks.md`.
>
> Change закрыт с `--skip-specs`: канон
> `openspec/specs/runtime/startup-schema-validation/spec.md` оказался
> **новее дельты** — в нём `utils.db` вместо `workspace.utils.db`,
> `OperationalError` вместо `DatabaseUnavailableError` и фильтр
> `table_type = 'BASE TABLE'`, которых в дельте нет. Перенос дельты
> откатил бы более точный канон, поэтому спеки не применялись; сама
> спека в каноне уже присутствует.

## 1. Сервис и типы

- [ ] 1.1 Создать `lib/services/schema_validation.py` с типами
  `MissingTable`, исключением `SchemaValidationError` (наследник
  `config.ConfigurationError`) и классом `SchemaValidationService`;
  сервис принимает `settings: dict` + опциональный `timeout_sec`
  и через один `SELECT` к `information_schema.tables` возвращает
  список недостающих таблиц либо `None`. Verify: файл импортируется
  без побочных эффектов; `SchemaValidationError.__mro__` содержит
  `ConfigurationError`.

- [ ] 1.2 Реализовать `SchemaValidationService.check_tables(conn,
  expected, *, timeout_sec) -> list[MissingTable]` и `.validate(...)`
  с выбросом `SchemaValidationError` при non-empty результате.
  Verify: unit-тест в `tests/test_schema_validation.py` на mock-conn
  проверяет контракт (все 6 на месте → нет missing; одна отсутствует →
  ровно она в списке; БД таймаут — отдельный класс ошибки).

## 2. Pydantic-настройка

- [ ] 2.1 Расширить `lib/core/project_settings.py` — добавить
  `StartupSchemaValidationSettings` (`enabled: bool = True`,
  `timeout_sec: float = 5.0` с `0.1 ≤ value ≤ 60.0`) и
  вложить его как `gateway.startup.schema_validation`. Verify:
  `validate_project_settings({...})` принимает значения по умолчанию
  и кастомные; вне диапазона — `pydantic.ValidationError`.

## 3. Интеграция в ApplicationContext

- [ ] 3.1 В `lib/core/application_context.py` добавить приватный
  метод `_validate_runtime_schema()`, который собирает 6 ожидаемых
  имён из `self.settings["channels"]["postgres"]` и
  `self.settings["logging"]["db"]` и вызывает
  `SchemaValidationService.validate(...)`. Verify: ручной smoke
  `python -c "from lib.core.application_context import
  ApplicationContext; ..."` (или существующий test-pattern) с
  удалённой одной таблицей падает с `SchemaValidationError`,
  список содержит ровно её.

- [ ] 3.2 В `ApplicationContext.start()` вызвать
  `_validate_runtime_schema()` сразу после `_start_db_pool()`
  и до `RuntimeEventsSubscriber.start()`. Verify: unit/integration
  тест `test_application_context_schema_validation.py::test_missing_blocks_start`
  — отсутствие таблицы блокирует старт; присутствие — не
  блокирует; idempotency `_started=True` не выставляется.

- [ ] 3.3 Уважение gate `gateway.startup.schema_validation.enabled`:
  при `False` метод пропускается без побочных эффектов и пишет
  WARNING в лог. Verify: тест с `enabled=False` проходит даже при
  отсутствии таблиц; с `enabled=True` — блокируется.

## 4. Тесты

- [ ] 4.1 `tests/test_schema_validation.py`: unit-тесты на сервис —
  (a) все таблицы на месте; (b) одна отсутствует; (c) пул
  возвращает OperationalError; (d) таймаут; (e) что имена не
  зашиты — тест передаёт кастомный settings с произвольными именами
  и проверяет, что они попадают в SQL `ANY(%s)` как параметры, а не
  как f-string (через `caplog`/`mocker`).

- [ ] 4.2 `tests/test_application_context.py`: добавить
  `TestStartupSchemaValidation` — с реальным test-профилем и
  реальной БД: (a) старт блокируется при отсутствии таблицы;
  (b) старт проходит при всех таблицах; (c) gate-off пропускает
  старт. Использовать паттерн, аналогичный существующему
  `test_application_context_logging.py` (см. `tests/test_application_context_logging.py:150-151`
  для примера settings-override).

- [ ] 4.3 `tests/test_gateway_entrypoint.py` (или аналогичный): тест
  на то, что `SchemaValidationError` от `ApplicationContext.create()`
  превращается `gateway.main()` в `sys.stderr.write("FATAL: ...")`
  и `return 2`. Verify: assert `capsys.readouterr().err` содержит
  список недостающих таблиц и exit-code == 2.

## 5. Документация и реестр

- [ ] 5.1 Добавить запись в `openspec/specs/COMPONENTS.md` для
  нового компонента `runtime/startup-schema-validation` со статусом
  `partial` (после полной верификации — `complete` по критериям
  `documentation/component-registry`).

- [ ] 5.2 Создать отдельную спеку
  `openspec/specs/runtime/startup-schema-validation/spec.md`
  (после архивации change), нормативный контракт — по шаблону
  `architecture/component-model`.

- [ ] 5.3 Обновить `AGENTS.md`:
  - секция «Project Layout» — добавить
    `lib/services/schema_validation.py` в список;
  - секция «Configuration» — добавить описание
    `gateway.startup.schema_validation.{enabled,timeout_sec}`.

- [ ] 5.4 Обновить `CHANGELOG.md` — добавить записи в текущий блок
  `[Unreleased]` (категории `Added` для нового сервиса и
  `Changed` для поведения gateway на старте).

## 6. Валидация перед merge

- [ ] 6.1 `openspec.cmd validate startup-schema-validation` —
  зелёный. Verify: exit code 0, нет ERROR/WARN.

- [ ] 6.2 `python -m pytest tests/test_schema_validation.py
  tests/test_application_context.py -q` — все тесты зелёные
  (часть требует test-профиля и PostgreSQL; те, что не требуют
  БД, должны проходить локально).

- [ ] 6.3 Smoke-проверка руками: запустить `cli_agent.py
  --profile=test` без применённых test-таблиц — увидеть
  `SchemaValidationError` с понятным списком недостающих
  таблиц и exit 2; применить `tools/apply_test_profile_tables.py`
  и повторить — успешный старт.
