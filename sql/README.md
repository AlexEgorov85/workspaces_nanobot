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
│   ├── create_audit_source_tables_gp.sql      #   REFERENCE DDL oarb.*
│   ├── create_audit_vectors_table_gp.sql      #   oarb.audit_vectors + vector_index_store
│   ├── create_vector_index_config_gp.sql      #   oarb.vector_index_config
│   └── seed_default_indexes.sql               #   3 дефолтных индекса (audits/violations/reports)
├── benchmarks/                                # Benchmarks
│   ├── create_benchmark_tables.sql            #   PostgreSQL 9.4+
│   └── create_benchmark_tables_gp.sql         #   Greenplum 6.25
└── migrations/                                # инкрементальные миграции
    ├── migrate_logs_v1.sql                    #   изменения gateway_logs
    └── migrate_logs_v1_gp.sql                 #   GP-вариант
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
psql "$DATABASE_URL" -f sql/audit_analyzer/create_vector_index_config_gp.sql

# 5. Дефолтные индексы (3 шт.: audits_index, violations_index, audit_reports_index)
psql "$DATABASE_URL" -f sql/audit_analyzer/seed_default_indexes.sql

# 6. Бенчмарки (если запускаете тесты)
psql "$DATABASE_URL" -f sql/benchmarks/create_benchmark_tables_gp.sql

# 7. Сборка векторных индексов (если используется audit_analyzer)
python tools/build_vectors.py --full-rebuild
```

### Обновление существующей установки

Добавление новой фичи = добавление нового DDL-файла в соответствующий подкаталог.
Миграции для уже существующих таблиц — в `sql/migrations/` (с суффиксом версии).

Если ваша установка на старой версии `gateway_logs` — примените
`sql/migrations/migrate_logs_v1_gp.sql`. Если версия v2.0.0 — таблица уже есть.

---

## Совместимость

| Каталог | PG 9.4+ | GP 6.25 |
|---------|---------|---------|
| `session/` | ✓ | ✓ |
| `channels/` (seed) | ✓ | ✓ |
| `logs/` | ✓ | ✓ |
| `audit_analyzer/` | нет (только GP) | ✓ |
| `benchmarks/` | ✓ | ✓ |
| `migrations/` | ✓ | ✓ |

`audit_analyzer/` поставляется только в GP-варианте — схема домена и
векторные индексы используют `DISTRIBUTED BY`, специфичный для Greenplum.
Если у вас обычный PostgreSQL — адаптируйте `DISTRIBUTED BY` под
`PARTITION BY` / обычные индексы вручную.

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