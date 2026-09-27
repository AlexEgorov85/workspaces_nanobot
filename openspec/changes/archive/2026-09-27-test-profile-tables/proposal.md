## Why

Профиль `test` (см. `profiles/test.jsonc` и `docs/PROFILES.md`) переключает 6 runtime-таблиц агента на суффикс `_test`, но в `sql/` нет DDL, который бы их создавал. В результате запуск `python gateway.py --profile=test` (и `cli_agent.py --profile=test`) падает на первом же обращении к БД с ошибкой `relation "public.agent_*_test" does not exist` — это видно в реальных `webui/*.jsonl` (см. `agent_gateway_logs_test` ошибки `history_search` от 2026-09-17). Без test-таблиц профиль `test` неработоспособен как самостоятельная runtime-среда.

## What Changes

- Добавить 6 DDL-скриптов в `sql/{channels,session,workers,logs}/create_public_agent_*_test.sql` — структурные клоны prod-собратьев (те же колонки, типы, индексы, `COMMENT ON`), с суффиксом `_test` в имени таблицы.
- Добавить runner `tools/apply_test_profile_tables.py`, который читает DSN из `DATABASE_URL`, сплитит каждый create-скрипт на отдельные statement'ы и применяет через psycopg2 (DDL идемпотентен через `IF NOT EXISTS`).
- Дополнить спеку `configuration/profiles` требованием: для каждого из 6 runtime-имён профиля ДОЛЖНА существовать соответствующая test-таблица в БД, DDL которой живёт в `sql/<domain>/create_public_agent_*_test.sql`.
- Обновить `sql/README.md` (раздел «Test-профиль» с перечнем DDL и командой применения) и `docs/PROFILES.md` (§ «Что меняется в runtime» — ссылка на DDL).

## Capabilities

### New Capabilities
- `infrastructure/test-profile-tables`: нормативный контракт DDL для 6 runtime-таблиц профиля test (наличие, источник, идемпотентность, канал применения).

### Modified Capabilities
- `configuration/profiles`: добавить требование, что для каждого из 6 runtime-имён, разрешённых профилем, ДОЛЖНА существовать таблица с соответствующим суффиксом.

## Impact

- **Код:** новый файл `tools/apply_test_profile_tables.py` (~70 строк, только DDL-применение). Никаких правок в `lib/`, `workspace/`, runtime-сервисах.
- **API:** нет.
- **Зависимости:** `psycopg2` (уже есть).
- **БД:** 6 новых таблиц в `public` + индекс `agent_gateway_logs_test_user_id_timestamp_idx`. Применяется через `python tools/apply_test_profile_tables.py` (или прямой `psql -f`).
- **Деploy:** одноразовое выполнение для каждой БД, где поднимается профиль `test`. Идемпотентно.
- **Документация:** `sql/README.md` (новый раздел «Test-профиль»), `docs/PROFILES.md` (§ «Что меняется в runtime»), `openspec/specs/configuration/profiles/spec.md` (MODIFIED Requirements).
