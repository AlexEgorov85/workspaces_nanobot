# infrastructure/test-profile-tables Specification

## Purpose
Нормативный контракт DDL для 5 runtime-таблиц, на которые переключает профиль `test`: наличие, структура, источник определения и способ применения. Capability фиксирует, что test-таблицы — это структурные клоны prod-собратьев, не отдельная схема; живут в той же схеме `public` и применяются через `tools/apply_test_profile_tables.py` или прямой `psql -f`.

## Scope

`agent` — DDL тестового контура и его runner — в агентском репозитории; платформа только повторяет объявленные имена
Реализация: `sql/*/create_public_*_test.sql`, `tools/apply_test_profile_tables.py`

## Requirements

### Requirement: Пять test-таблиц структурно эквивалентны prod-собратьям

DDL в `sql/<domain>/create_public_agent_*_test.sql` SHALL повторять prod-DDL с точностью до: имя таблицы (добавлен суффикс `_test`), `COMMENT ON TABLE / COLUMN` (упоминает «test-профиль»), колонки и их типы, индексы (включая `agent_gateway_logs_test_user_id_timestamp_idx` в `agent_gateway_logs_test`). Расхождения в колонках, типах или ограничениях относительно prod-версии ЗАПРЕЩЕНЫ (допустимы лишь семантически эквивалентные различия в дефолтах UUID-функций и именах sequences — см. `sql/README.md`).

#### Scenario: Все пять create-скриптов существуют
- **КОГДА** проверяется каталог `sql/`
- **ТОГДА** должны существовать ровно эти 5 файлов: `sql/channels/create_public_agent_conversation_messages_test.sql`, `sql/session/create_public_agent_session_meta_test.sql`, `sql/session/create_public_agent_session_messages_test.sql`, `sql/logs/create_public_agent_gateway_logs_test.sql`, `sql/logs/create_public_agent_question_runs_test.sql`

#### Scenario: В БД присутствуют test-таблицы с тем же числом колонок, что и prod
- **КОГДА** на БД применён `tools/apply_test_profile_tables.py`
- **ТОГДА** запрос `information_schema.columns` для каждой из 5 test-таблиц возвращает столько же строк, сколько для prod-аналога (с точностью до семантически эквивалентных различий)

### Requirement: Применение идемпотентно

`tools/apply_test_profile_tables.py` SHALL применять все 5 DDL-скриптов в одной транзакции, идемпотентно (через `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`). Повторный запуск SHALL быть no-op без ошибок. Поддерживается DSN из переменной окружения `DATABASE_URL`.

#### Scenario: Повторный запуск без изменений
- **КОГДА** runner запущен повторно на БД, где все 5 таблиц уже созданы
- **ТОГДА** runner завершается с exit 0 и печатает `ALL_OK` для каждого скрипта

### Requirement: В runtime-коде запрещено автоматическое создание test-таблиц

Runtime-код `lib/`, `workspace/`, `gateway.py`, `cli_agent.py` SHALL NOT содержать `ensure_tables()` / `CREATE TABLE IF NOT EXISTS` для runtime-таблиц профиля. DDL живёт ТОЛЬКО в `sql/<domain>/` и применяется через `tools/apply_test_profile_tables.py` или `psql -f`.

#### Scenario: Поиск ensure_tables не находит test-DDL в runtime
- **КОГДА** выполняется `rg -i "ensure_tables|create table if not exists" lib/ workspace/ tools/ gateway.py cli_agent.py`
- **ТОГДА** совпадений SHALL не быть (DDL — только в `sql/`)

### Requirement: Документация покрывает test-таблицы

`sql/README.md` SHALL содержать раздел «Test-профиль» с перечнем 5 DDL-файлов и командой применения. `docs/PROFILES.md` SHALL ссылаться на DDL и runner как на источник test-таблиц.

#### Scenario: README содержит раздел Test-профиль
- **КОГДА** разработчик открывает `sql/README.md`
- **ТОГДА** в разделе «Test-профиль» перечислены 5 create-скриптов и упомянута команда `python tools/apply_test_profile_tables.py`
