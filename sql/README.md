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
> каждый DDL создаёт ровно одну таблицу с COMMENT-комментариями (без индексов).

---

## Каталог

```
sql/
├── README.md                                            # этот файл
│
├── session/                                             # cold-storage mirror сессий
│   ├── create_public_agent_session_meta.sql             #   public.agent_session_meta
│   ├── create_public_agent_session_meta_test.sql        #   профиль test
│   ├── create_public_agent_session_messages.sql         #   public.agent_session_messages
│   └── create_public_agent_session_messages_test.sql    #   профиль test
│
├── channels/                                            # PostgresChannel / Web UI
│   ├── create_public_agent_conversation_messages.sql    #   public.agent_conversation_messages
│   ├── create_public_agent_conversation_messages_test.sql
│   └── seed_messages.sql                                #   тестовые сообщения
│
├── logs/                                                # DbLoggingService
│   ├── create_public_agent_question_runs.sql            #   public.agent_question_runs
│   ├── create_public_agent_question_runs_test.sql       #   профиль test
│   ├── create_public_agent_gateway_logs.sql             #   public.agent_gateway_logs
│   └── create_public_agent_gateway_logs_test.sql        #   профиль test
│
├── benchmarks/                                          # Benchmarks
│   ├── create_public_agent_benchmark_runs.sql           #   public.agent_benchmark_runs
│   └── create_public_agent_benchmark_results.sql        #   public.agent_benchmark_results
│
├── vectors/                                             # legacy (кодом не читается)
│   ├── create_vector_index_config.sql                   #   public.agent_vector_index_config — LEGACY
│   └── create_vector_index_store.sql                    #   public.agent_vector_index_store — DEPRECATED (V003)
│
├── comments/                                            # массовые COMMENT ON (сгенерировано)
│   └── apply_all_comments.sql                           #   tools/generate_comments_sql.py
│
├── migrations/                                          # версионные миграции схемы
│   ├── schema_migrations.sql                            #   tracking-таблица public.schema_migrations
│   ├── V001__baseline.sql                               #   базовая линия (штамп, без DDL)
│   ├── V002__vector_chunk_params.sql                    #   chunk_size/chunk_overlap/metric в agent_vector_index_config
│   ├── V003__drop_vector_index_store.sql                #   ШАБЛОН: DROP <signature_table> (подставить вручную)
│   ├── V004__agent_gateway_logs_user_id.sql             #   user_id + backfill + индекс в agent_gateway_logs
│   └── V006__drop_agent_worker_claims.sql               #   DROP agent_worker_claims (снят протокол аренды)
│
└── audit_analyzer/                                      # навык audit_analyzer
    ├── create_oarb_audits.sql                           #   oarb.audits          (REFERENCE)
    ├── create_oarb_violations.sql                       #   oarb.violations      (REFERENCE)
    ├── create_oarb_audit_reports.sql                    #   oarb.audit_reports   (REFERENCE)
    ├── create_oarb_report_items.sql                     #   oarb.report_items    (REFERENCE)
    ├── create_oarb_audit_vectors.sql                    #   oarb.audit_vectors (= storage_table)
    ├── create_public_agent_predefined_scripts.sql       #   public.agent_predefined_scripts
    ├── seed_predefined_scripts.sql                      #   наполнение реестра скриптов
    ├── fix_audit_types_stats_avg.sql                    #   фикс типов (stats_avg)
    └── seed_default_indexes.sql                         #   LEGACY-сид agent_vector_index_config
```

---

## Миграции схемы (tools/migrate.py)

Инфраструктурные изменения схемы, начиная с baseline, оформляются
версионными миграциями `sql/migrations/V<N>__<name>.sql` и применяются
runner'ом (psycopg2, DSN: `DATABASE_URL` или `channels.postgres.dsn`):

```bash
python tools/migrate.py --status            # состояние: PENDING/applied/DRIFT!
python tools/migrate.py --dry-run           # показать SQL ожидающих
python tools/migrate.py --apply             # применить ожидающие по порядку (транзакционно)
python tools/migrate.py --apply --target 4  # до V004 включительно
python tools/migrate.py --verify            # сверить checksums применённых с файлами
python tools/migrate.py --baseline          # штамповать существующие версии без выполнения
```

Правила:
- каждая применённая версия фиксируется в `public.schema_migrations`
  с SHA256-checksum содержимого; изменение применённого файла = DRIFT
  (ошибка при `--apply`, обход — осознанный `--force`);
- runner выполняет SQL **как есть**, без подстановок плейсхолдеров:
  миграция с шаблоном (`V003__drop_vector_index_store.sql` —
  `DROP TABLE IF EXISTS "<signature_table>";`) применяется как no-op,
  реальное имя таблицы оператор подставляет и выполняет DROP вручную;
- существующая БД: после первой установки выполнить `--baseline`
  (V001 не содержит DDL — только точка отсчёта);
- новые изменения схемы — новый файл `V007__*.sql` и далее; ретроактивно
  менять применённые миграции нельзя. Номера не переиспользуются: в истории
  уже был `V005__create_agent_cache_ownership.sql` (удалён вместе с
  `cache_ownership.py`), и базы, где он применился, хранят `005` в
  `public.schema_migrations`.

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

# 6. Домен audit_analyzer — reference таблицы (если нет в существующей БД)
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_audits.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_violations.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_audit_reports.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_report_items.sql

# 7. Домен audit_analyzer — таблицы навыка
#    (oarb.audit_vectors = storage_table из project.json::gateway.vector.index)
psql "$DATABASE_URL" -f sql/audit_analyzer/create_oarb_audit_vectors.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/create_public_agent_predefined_scripts.sql
psql "$DATABASE_URL" -f sql/audit_analyzer/seed_predefined_scripts.sql

# 8. Сборка векторных индексов (конфиг — только project.json::gateway.vector.index.indexes)
python tools/build_vectors.py --full-rebuild
```

Векторная инфраструктура **не** требует DDL: FAISS собирается в памяти из
DuckDB-снапшота `gateway.vector.index.storage_table`, а декларация индексов
читается из `project.json`. Файлы `sql/vectors/*` — legacy (`agent_vector_index_config`
кодом не читается, `agent_vector_index_store` удалён миграцией V003) и на
новых инстансах не применяются. Аналогично `sql/audit_analyzer/seed_default_indexes.sql`
сидит в legacy-таблицу; актуальные индексы объявлены в `project.json`.

---

## Когда добавлять новый DDL

| Ситуация                                                | Куда класть                                              |
|---------------------------------------------------------|----------------------------------------------------------|
| Таблица для новой фичи runtime                          | подкаталог по домену: `sql/<domain>/create_<schema>_<table>.sql` |
| Доменная таблица для навыка                             | `sql/<skill>/create_<schema>_<table>.sql`                |
| Тестовые данные                                         | `sql/<domain>/seed_<table>.sql`                          |

**Один файл = одна таблица.** `COMMENT ON TABLE / COLUMN` пишутся прямо в
файле создания таблицы; каталог `sql/comments/` содержит только сгенерированный
сводный `apply_all_comments.sql` (генератор — `tools/generate_comments_sql.py`,
применять вручную при необходимости).
Индексы в create-скриптах не создаются — только таблица и комментарии.

DDL **не хранится** рядом с кодом компонента (`lib/<component>/sql/`).
Единственная точка правды — корневой `sql/`.

---

## Совместимость

Все скрипты рассчитаны на **Greenplum 6.5** (PostgreSQL 9.4 ядро):

- `DISTRIBUTED BY (...)` для каждой таблицы;
- `pgcrypto` для `gen_random_uuid()` (нет `uuid-ossp` по умолчанию);
- `BIGINT GENERATED BY DEFAULT AS IDENTITY` вместо `SERIAL`;
- Без FK (GP 6.5 не поддерживает referential integrity);
- Без индексов (только таблица + COMMENT);
- `WITHOUT OIDS` не пишем явно (GP по умолчанию).

### Если нужен обычный PostgreSQL 13+

Удалите `DISTRIBUTED BY (...)` и замените `gen_random_uuid()` на
`uuid_generate_v4()` из `uuid-ossp`. Всё остальное совместимо.
