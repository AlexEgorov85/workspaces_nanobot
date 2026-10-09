## Context

Текущий путь startup в `gateway.py:main()` уже использует
`ConfigurationError` boundary (см. `gateway.py:546-579`): любые
ошибки конфигурации и startup-этапов ловятся единым `except
ConfigurationError` и превращаются в `sys.stderr.write + return 2`.

В `lib/core/application_context.py:370-460` `ApplicationContext.start()`
сейчас делает (по порядку): `mark_started()`, template overrides,
`_start_db_pool()`, `RuntimeEventsSubscriber.start()`,
`db_logging_service.start()`, sync services, sessions.

Если 6 обязательных runtime-таблиц (`channels.postgres.*` +
`logging.db.*`) отсутствуют в БД, эти сервисы стартуют, а падают уже
на первой INSERT/SELECT в runtime-таблицы — с невнятным
`UndefinedTableError` в логе канала и потерей событий.

Канонический источник имён runtime-таблиц уже есть:
`config.EXPECTED_RUNTIME_TABLE_NAMES[profile]`
(`config.py:205-222`) и соответствующие ключи в `SETTINGS` после
`_initialize_settings`. Это и есть «runtime-истина» — то, что
проверяет `validate_runtime_isolation` (`config.py:345-377`).

Дополнительные ограничения/конвенции (из AGENTS.md):

- `ConfigurationError` — единственная boundary-ошибка startup.
- Имена таблиц **не должны быть зашиты** в коде проверки —
  единственный источник `config.EXPECTED_RUNTIME_TABLE_NAMES` +
  резолв из `SETTINGS`.
- `gateway.py:153-204` — блок установки sync callbacks; должен
  выполняться ДО `ctx.start()`, чтобы первый `initial_load` уже
  видел новые callbacks. Проверка схемы должна идти **после**
  `ctx.start()` (нужен пул) или в самом начале `start()`.
- `cli_agent.py:258` тоже ловит `ConfigurationError` — change
  автоматически покроет CLI, если `SchemaValidationError` —
  наследник `ConfigurationError`.

## Goals / Non-Goals

**Goals:**

- Перед подъёмом каналов/db_logging/sync сервисов проверять, что все
  6 runtime-таблиц из `SETTINGS` существуют в БД.
- Hard-fail с понятным сообщением при missing, exit 2 на уровне
  `gateway.main()` / `cli_agent.main()`.
- Имена таблиц резолвятся **только** из
  `SETTINGS["channels"]["postgres"]` и
  `SETTINGS["logging"]["db"]` — никаких литералов в коде проверки.
- Полная совместимость с обоими профилями (`prod` / `test`) без
  условных веток «if profile == 'test'».
- Сохранить текущий API `ApplicationContext.start()` для существующих
  вызовов — только расширить поведение.

**Non-Goals:**

- Авто-создание недостающих таблиц (auto-migrate) — это ответственность
  `tools/migrate.py` / `tools/apply_test_profile_tables.py`.
- Проверка других таблиц (`oarb.*`, skill-таблицы, benchmarks,
  legacy `agent_vector_index_*`) — не входит в объём change.
- Изменение формата конфига или `EXPECTED_RUNTIME_TABLE_NAMES`.
- Изменение `validate_runtime_isolation` или `validate_profile_overlay`.
- Введение нового типа ошибки верхнего уровня (используем
  наследника `ConfigurationError`).
- Изменение пула воркеров / health-check / readiness-компонентов.

## Decisions

### Decision 1: `SchemaValidationError(ConfigurationError)` — наследник существующего boundary

`SchemaValidationError` объявляется как
`class SchemaValidationError(ConfigurationError)` в
`lib/services/schema_validation.py`. Это автоматически покрывает
`gateway.py:main()` (`gateway.py:574-578`) и `cli_agent.py:main()`
(`cli_agent.py:258, 273`) без изменений — единый
`except ConfigurationError → return 2 / stderr` продолжает работать.

**Альтернатива:** объявить новый класс-исключение верхнего уровня
(например, `StartupError`). Отвергнуто — потребует изменений в
обоих entrypoint, нарушает принцип «один boundary в startup».

### Decision 2: Источник имён таблиц — `SETTINGS`, не литералы

Сервис `SchemaValidationService` принимает `settings: dict` и
извлекает 6 имён по путям:

```text
channels.postgres.{table_name, messages_table, meta_table, claims_table}
logging.db.{table_name, question_runs_table}
```

Это **те же 6 ключей**, которые использует runtime (PostgresChannel,
PGSessionManager, DbLoggingService) и которые проходят через
`validate_runtime_isolation` (`config.py:345-377`). Поэтому
drift между проверкой и runtime невозможен — мы проверяем
ровно то, что попытается использовать runtime.

**Альтернативы:**

- (a) Хардкод 6 имён прямо в `SchemaValidationService` — нарушает
  требование «не зашиты в коде», ломается при изменении имён.
- (b) Использовать `EXPECTED_RUNTIME_TABLE_NAMES[profile]`
  напрямую — но это «канонический ожидаемый список», а не то,
  что реально лежит в SETTINGS; validate_runtime_isolation
  уже гарантирует совпадение.
- (c) Сканировать DDL-файлы — не runtime-источник.

### Decision 3: Один SQL-запрос к `information_schema.tables`

Проверка выполняется одним запросом:

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name = ANY(%s)
```

с параметром — список из 6 ожидаемых имён. Разница между
полученным и ожидаемым — `missing`.

**Альтернативы:**

- (a) По одной проверке на таблицу через `to_regclass('public.<name>')`
  — проще код, но 6 round-trip'ов к БД.
- (b) `SELECT to_regclass(%s)` для каждой таблицы — то же, но без
  `information_schema`.
- (c) Попытка `SELECT 1 FROM <table> LIMIT 0` — может давать ложные
  срабатывания на RLS/привилегии.

Выбран `information_schema` — стандарт ANSI, совместим с
Greenplum 6.5 и Postgres, минимальный round-trip cost.

### Decision 4: Проверка идёт внутри `ApplicationContext.start()`

Точка вызова — `lib/core/application_context.py:_validate_runtime_schema()`
(новый приватный метод), вызывается **сразу после** `_start_db_pool()`
и **до** `RuntimeEventsSubscriber.start()`. Так:

1. Пул уже работает — есть соединение для запроса.
2. Другие сервисы ещё не стартовали — ранее сломанный INSERT в
   `agent_gateway_logs` / `agent_conversation_messages` не произойдёт.
3. Идемпотентность: тело метода — одна транзакция с `SELECT`.

**Альтернатива:** вызывать из `gateway.py` напрямую. Отвергнуто —
проверка относится к lifecycle ApplicationContext и должна быть
доступна любому entrypoint (cli/gateway/streamlit).

### Decision 5: Опциональный gate `gateway.startup.schema_validation.enabled`

Добавляется секция (опциональная, дефолт `True`):

```text
gateway.startup.schema_validation.enabled: bool = True
gateway.startup.schema_validation.timeout_sec: float = 5.0
```

Реализовано через Pydantic `StartupSchemaValidationSettings`
в `lib/core/project_settings.py` (расширение `GatewaySettings`).

**Зачем:** для аварийного отключения проверки (например, при
разработке нового профиля) без правки кода. По умолчанию —
включено.

**Альтернатива:** всегда включено, без gate. Отвергнуто — symmetry
с `gateway.cache.local_path` / `gateway.compact.enabled` /
`gateway.error_messages.log_to_db`: всё опционально с разумным
дефолтом.

### Decision 6: Сообщение об ошибке — структурированное + plain-text

`SchemaValidationError` имеет `.missing: list[MissingTable]` и
`.profile: str`. При выбросе формируется сообщение:

```text
Schema validation failed for profile='prod':
missing runtime tables:
  - public.agent_conversation_messages
  - public.agent_gateway_logs
hint: apply migrations before starting the gateway
```

Печатается через `logger.error(...)` И попадает в
`SchemaValidationError.__str__` для `sys.stderr.write` в
`gateway.main()`.

Никаких DSN, паролей, секретов. Только schema.table.

### Decision 7: Проверка НЕ регистрируется в `RuntimeReadiness`

`SchemaValidationService` НЕ вызывается как `RuntimeReadiness`
check (в отличие от `postgres` / `duckdb_cache` / `vector_search`).
Причина: проверка pre-startup, и после успешного старта она
больше не нужна — runtime сам диагностирует проблемы через
ready/duckdb_cache. Это соответствует требованию
«не дублировать readiness».

## Risks / Trade-offs

- **[Risk]** При добавлении новой runtime-таблицы в конфиг — нужно
  не забыть, что она теперь обязательна на старте.
  → **Mitigation:** тест `test_schema_validation.py::test_missing_one_blocks_start`
  фиксирует контракт «1 из 6 отсутствует → блокировка». Дополнительно —
  комментарий в `PROFILE_OWNED_RUNTIME_KEYS` (`config.py:196-203`) о том,
  что эти ключи участвуют в startup-валидации.

- **[Risk]** `information_schema.tables` может быть медленным на
  очень больших базах (тысячи таблиц).
  → **Mitigation:** запрос фильтрован `WHERE table_schema = 'public'
  AND table_name = ANY(...)` — индекс `pg_class.relname` обеспечит
  O(log N) lookup. На реальной нагрузке (десятки таблиц в `public`)
  проблем нет.

- **[Risk]** При первой установке на чистую БД (без миграций)
  gateway не стартует — это поведение **намеренное**, но может
  удивить оператора, который привык «всё поднимается».
  → **Mitigation:** (1) сообщение в ошибке содержит подсказку
  «apply migrations before starting the gateway»; (2) обновить
  `README.md` / `docs/QUICKSTART.md` (если есть) — секция
  «первый запуск»; (3) при `gateway.startup.schema_validation.enabled=false`
  можно временно пропустить проверку.

- **[Risk]** Между проверкой схемы и реальной INSERT может
  вклиниться `DROP TABLE` (маловероятно, но возможно в
  параллельной миграции).
  → **Mitigation:** не пытаемся закрыть TOCTOU окончательно —
  это уже существующий риск для DbLoggingService и каналов;
  проверка только ловит **bootstrap**-ситуацию «таблица вообще
  не создана», а не «таблица только что удалена».

- **[Trade-off]** Дополнительный round-trip к БД на старте.
  Принимаемо: один `SELECT` к `information_schema`, ~1-5 ms на
  локальной БД, не блокирует startup существенно.

## Migration Plan

Эта change **не требует миграции БД** — только кода. Деплой:

1. Merge change в `master`.
2. Поднять новую версию gateway.
3. Если таблицы созданы (`tools/migrate.py --apply` +
   `tools/apply_test_profile_tables.py` для test) — старт пройдёт
   штатно, никаких действий не нужно.
4. Если таблицы НЕ созданы — gateway откажется стартовать с
   понятным списком недостающих таблиц; оператор применяет
   миграции и пробует снова.

**Rollback:** revert merge commit; если revert невозможен
(долгий downtime) — `gateway.startup.schema_validation.enabled=false`
через env-override (через `.secrets.env` или прямое изменение
`project.json`). После rollback'а — применить миграции и вернуть
флаг в `true`.

## Open Questions

Нет. Все решения по объёму приняты в proposal-фазе
(только 6 runtime-таблиц, hard-fail, прозрачный источник).
