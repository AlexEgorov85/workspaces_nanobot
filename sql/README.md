# sql/ — DDL всех таблиц проекта

Все SQL-скрипты собраны в корневом каталоге `sql/`, разбиты по доменам.

**Применяются вручную** через `psql` (или совместимый клиент) — никаких
`ensure_tables()` в коде больше нет.

> **Соглашение об именах:**
>
> - `create_<schema>_<table>.sql` — DDL **одной** таблицы для Greenplum 6.5.
>   `DISTRIBUTED BY (...)`, `pgcrypto` для UUID, `WITHOUT OIDS`, `BIGINT IDENTITY`,
>   без FK (GP 6.5 не поддерживает).
> - `seed_*.sql` — данные (INSERT), без DDL.
>
> **Один файл = одна таблица.** Никаких объединённых `create_*_tables.sql` —
> каждый DDL создаёт ровно одну таблицу и её индексы.

---

## Каталог

```
sql/
├── README.md                                            # этот файл
│
├── session/                                             # PGSessionManager
│   ├── create_public_agent_session_meta.sql             #   public.agent_session_meta
│   └── create_public_agent_session_messages.sql         #   public.agent_session_messages
│
├── channels/                                            # PostgresChannel / Web UI
│   ├── create_public_agent_conversation_messages.sql    #   public.agent_conversation_messages
│   └── seed_messages.sql                                #   тестовые сообщения
│
├── logs/                                                # DbLoggingService
│   ├── create_public_agent_question_runs.sql            #   public.agent_question_runs
│   └── create_public_agent_gateway_logs.sql             #   public.agent_gateway_logs
│
├── benchmarks/                                          # Benchmarks
│   ├── create_public_agent_benchmark_runs.sql           #   public.agent_benchmark_runs
│   └── create_public_agent_benchmark_results.sql        #   public.agent_benchmark_results
│
└── audit_analyzer/                                      # навык audit_analyzer
    ├── create_oarb_audits.sql                           #   oarb.audits          (REFERENCE)
    ├── create_oarb_violations.sql                       #   oarb.violations      (REFERENCE)
    ├── create_oarb_audit_reports.sql                    #   oarb.audit_reports   (REFERENCE)
    ├── create_oarb_report_items.sql                     #   oarb.report_items    (REFERENCE)
    ├── create_oarb_audit_vectors.sql                    #   oarb.audit_vectors
    ├── create_public_agent_predefined_scripts.sql       #   public.agent_predefined_scripts
    ├── create_public_agent_vector_index_config.sql      #   public.agent_vector_index_config
    ├── create_public_agent_vector_index_store.sql       #   public.agent_vector_index_store
    └── seed_default_indexes.sql                         #   3 дефолтных индекса (audits/violations/reports)
```

---

## Порядок применения

### Минимальная установка (CLI-агент)

```bash
psql "$DATABASE_URL" -f sql/session/create_public_agent_session_meta.sql
psql "$DATABASE_URL" -f sql/session/create_public_agent_session_messages.sql
psql "$DATABASE_URL" -f sql/channels/create_public_agent_conversation_messages.sql
```

### Полная установка (gateway + audit_analyzer + benchmarks)

```bash
# 1. Сессии
psql "$DATABASE_URL" -f sql/session/create_public_agent_session_meta.sql
psql "$DATABASE_URL" -f sql/session/create_public_agent_session_messages.sql

# 2. Канал
psql "$DATABASE_URL" -f sql/channels/create_public_agent_conversation_messages.sql

# 3. Журнал событий (DbLoggingService)
psql "$DATABASE_URL" -f sql/logs/create_public_agent_question_runs.sql
psql "$DATABASE_URL" -f sql/logs/create_public_agent_gateway_logs.sql

# 4. Бенчмарки
psql "$DATABASE_URL" -f sql/benchmarks/create_public_agent_benchmark_runs.sql
psql "$DATABASE_URL" -f sql/benchmarks/create_public_agent_benchmark_results.sql

# 5. Домен audit_analyzer — reference таблицы (если нет в существующей БД)
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_audits.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_violations.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_audit_reports.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_report_items.sql

# 6. Домен audit_analyzer — таблицы навыка
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_audit_vectors.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_public_agent_predefined_scripts.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_public_agent_vector_index_config.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_public_agent_vector_index_store.sql

# 7. Дефолтные индексы (3 шт.: audits_index, violations_index, audit_reports_index)
psql "$DATABASE_URL" -f sql/audit_analyzer/seed_default_indexes.sql

# 8. Сборка векторных индексов
python tools/build_vectors.py --full-rebuild
```

---

## Когда добавлять новый DDL

| Ситуация                                                | Куда класть                                              |
|---------------------------------------------------------|----------------------------------------------------------|
| Таблица для новой фичи runtime                          | подкаталог по домену: `sql/<domain>/create_<schema>_<table>.sql` |
| Доменная таблица для навыка                             | `sql/<skill>/create_<schema>_<table>.sql`                |
| Тестовые данные                                         | `sql/<domain>/seed_<table>.sql`                          |

**Один файл = одна таблица.** Все `COMMENT ON TABLE / COLUMN` живут прямо
в файле создания таблицы — отдельный `comments/` каталог больше не нужен.

DDL **не хранится** рядом с кодом компонента (`lib/<component>/sql/`).
Единственная точка правды — корневой `sql/`.

---

## Совместимость

Все скрипты рассчитаны на **Greenplum 6.5** (PostgreSQL 9.4 ядро):

- `DISTRIBUTED BY (...)` для каждой таблицы;
- `pgcrypto` для `gen_random_uuid()` (нет `uuid-ossp` по умолчанию);
- `BIGINT GENERATED BY DEFAULT AS IDENTITY` вместо `SERIAL`;
- Без FK (GP 6.5 не поддерживает referential integrity);
- `CREATE INDEX IF NOT EXISTS` заменён на DO-блок с проверкой `pg_indexes`;
- `WITHOUT OIDS` не пишем явно (GP по умолчанию).

### Если нужен обычный PostgreSQL 13+

Удалите `DISTRIBUTED BY (...)` и замените `gen_random_uuid()` на
`uuid_generate_v4()` из `uuid-ossp`. Всё остальное совместимо.
