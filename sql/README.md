# sql/ — DDL всех таблиц проекта

Все SQL-скрипты собраны в одном корневом каталоге с подкаталогами по доменам.
**Применяются вручную** через `psql` (или совместимый клиент) — никаких
`ensure_tables()` в коде больше нет.

> **Соглашение об именах:**
>
> - `create_*.sql` — DDL для новой установки (CREATE TABLE / INDEX IF NOT EXISTS).
> - `create_*_gp.sql` — Greenplum-вариант: то же + `DISTRIBUTED BY (...)`,
>   `pgcrypto` вместо `uuid-ossp`, `WITHOUT OIDS`, `bigint` без GENERATED.
> - `migrate_*.sql` — инкрементальные миграции для уже существующих установок.

---

## Каталог

```
sql/
├── README.md                                  # этот файл
├── session/                                   # PGSessionManager
│   ├── create_session_tables.sql              #   PostgreSQL 9.4+
│   └── create_session_tables_gp.sql           #   Greenplum 6.25
├── channels/                                  # PostgresChannel
│   └── seed_messages.sql                      #   тестовые данные (14 user + 4 assistant)
├── logs/                                      # DbLoggingService
│   ├── create_logs_table.sql                  #   PostgreSQL 9.4+
│   └── create_logs_table_gp.sql               #   Greenplum 6.25
├── audit_analyzer/                            # audit_analyzer + cache_provider
│   ├── create_audit_source_tables_gp.sql      #   REFERENCE DDL oarb.* (GP 6.5)
│   ├── create_audit_source_tables.sql         #   PG 13+ вариант (без DISTRIBUTED BY)
│   ├── create_audit_vectors_table_gp.sql      #   oarb.audit_vectors + agent_vector_index_store (GP 6.5)
│   ├── create_audit_vectors_table.sql         #   PG 13+ вариант
│   ├── create_agent_vector_index_config_gp.sql      #   public.agent_vector_index_config (GP 6.5)
│   ├── create_agent_vector_index_config.sql         #   PG 13+ вариант
│   └── seed_default_indexes.sql               #   3 дефолтных индекса (audits/violations/reports)
├── benchmarks/                                # Benchmarks
│   ├── create_benchmark_tables.sql            #   PostgreSQL 9.4+
│   └── create_benchmark_tables_gp.sql         #   Greenplum 6.25
└── migrations/                                # инкрементальные миграции
    ├── migrate_logs_v1.sql                    #   изменения gateway_logs (PG)
    ├── migrate_logs_v1_gp.sql                 #   GP-вариант
    ├── migrate_vectors_v2.sql                 #   vectors + config v2 (PG 13+)
    ├── migrate_vectors_v2_gp.sql              #   GP 6.5 вариант
    └── migrate_agent_table_names_v1.sql       #   agent_-префикс для таблиц агента (PG 13+)
```

---

## Порядок применения

### Минимальная установка (CLI-агент)

```bash
# 1. Сессии (PGSessionManager)
psql "$DATABASE_URL" -f sql/session/create_session_tables.sql
# или для Greenplum:
psql "$DATABASE_URL" -f sql/session/create_session_tables_gp.sql

# 2. Таблица канала создаётся автоматически (PostgresChannel)
#    Тестовые данные — опционально:
psql "$DATABASE_URL" -f sql/channels/seed_messages.sql
```

### Полная установка (gateway + audit_analyzer + benchmarks)

```bash
# 1. Сессии
psql "$DATABASE_URL" -f sql/session/create_session_tables_gp.sql

# 2. Канал — создаётся автоматически

# 3. Журнал событий (DbLoggingService)
psql "$DATABASE_URL" -f sql/logs/create_logs_table_gp.sql
# Миграция с предыдущей версии (если есть таблица):
psql "$DATABASE_URL" -f sql/migrations/migrate_logs_v1_gp.sql

# 4. Домен audit_analyzer (если используется навык)
psql "$DATABASE_URL" -f sql/audit_analyzer/create_audit_source_tables_gp.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_audit_vectors_table_gp.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_agent_vector_index_config_gp.sql

# 5. Дефолтные индексы (3 шт.: audits_index, violations_index, audit_reports_index)
psql "$DATABASE_URL" -f sql/audit_analyzer/seed_default_indexes.sql

# 6. Бенчмарки (если запускаете тесты)
psql "$DATABASE_URL" -f sql/benchmarks/create_benchmark_tables_gp.sql

# 7. Сборка векторных индексов (если используется audit_analyzer)
python tools/build_vectors.py --full-rebuild
```

### Обновление с v1.x → v2.0.0 (agent_-префикс)

Если вы на v1.x — добавьте шаг миграции `agent_`-префикса:

```bash
# Переименовывает/переносит под единый agent_-префикс с сохранением данных:
#   public.session_meta             → public.agent_session_meta
#   public.session_messages         → public.agent_session_messages
#   public.predefined_scripts       → public.agent_predefined_scripts
#   public.conversation_messages    → public.agent_conversation_messages
#   oarb.vector_index_config        → public.agent_vector_index_config
#   oarb.vector_index_store         → public.agent_vector_index_store
# (доменные таблицы навыка oarb.audits/violations/... не затрагиваются)
psql "$DATABASE_URL" -f sql/migrations/migrate_agent_table_names_v1.sql
```

### Обновление существующей установки

Добавление новой фичи = добавление нового DDL-файла в соответствующий подкаталог.
Миграции для уже существующих таблиц — в `sql/migrations/` (с суффиксом версии).

Если ваша установка на старой версии `gateway_logs` — примените
`sql/migrations/migrate_logs_v1_gp.sql`. Если версия v2.0.0 — таблица уже есть.

---

## Совместимость

| Каталог | PG 13+ | GP 6.5 |
|---------|---------|---------|
| `session/` | ✓ | ✓ |
| `channels/` (seed) | ✓ | ✓ |
| `logs/` | ✓ | ✓ |
| `audit_analyzer/` | ✓ (`*.sql` без суффикса `_gp`) | ✓ (`*_gp.sql` с DISTRIBUTED BY) |
| `benchmarks/` | ✓ | ✓ |
| `migrations/` | ✓ (`*.sql` без суффикса) | ✓ (`*_gp.sql`) |

**Выбор варианта DDL:**

- **PostgreSQL 13+** — используйте файлы без суффикса `_gp`:
  - `sql/audit_analyzer/create_audit_source_tables.sql`
  - `sql/audit_analyzer/create_audit_vectors_table.sql`
  - `sql/audit_analyzer/create_agent_vector_index_config.sql`
  - `sql/migrations/migrate_vectors_v2.sql`

- **Greenplum 6.5+** — используйте файлы с суффиксом `_gp`:
  - `sql/audit_analyzer/create_audit_source_tables_gp.sql`
  - `sql/audit_analyzer/create_audit_vectors_table_gp.sql`
  - `sql/audit_analyzer/create_agent_vector_index_config_gp.sql`
  - `sql/migrations/migrate_vectors_v2_gp.sql`

`seed_default_indexes.sql` — общий для обеих СУБД (только данные).

---

## Когда добавлять новый DDL

| Ситуация | Куда класть |
|----------|-------------|
| Таблицы для новой фичи runtime (сессии, логи, бенчмарки) | подкаталог по домену: `sql/<domain>/` |
| Доменные таблицы для навыка | `sql/<skill>/` (например, `sql/audit_analyzer/`) |
| Миграция для уже существующей таблицы | `sql/migrations/migrate_<table>_v<N>.sql` (+ `_gp`-вариант) |
| Тестовые данные | `sql/<domain>/seed_<table>.sql` |

DDL **не хранится** рядом с кодом компонента (`lib/<component>/sql/`).
Единственная точка правды — корневой `sql/`. Это упрощает обзор и порядок
применения.

---

## Когда удалять

При удалении компонента:
1. Удалите все DDL-файлы компонента в `sql/`.
2. Если был раздел в этом README — удалите его.
3. Если был миграционный скрипт — добавьте `migrate_<table>_drop_v<N>.sql`
   для уже существующих установок (или опишите в CHANGELOG).