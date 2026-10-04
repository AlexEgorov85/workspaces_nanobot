-- ============================================================================
-- V012 — test-контур журнала и ключ строки сообщения (change 2026-10-04-db-schema-audit)
-- ============================================================================
--
-- Найдено сверкой живой базы с эталоном, собранным из sql/ (DDL применяется
-- в отдельную базу, схема сравнивается машинно — tools/migrate.py этого не
-- делает). Три расхождения, все — «в базе меньше, чем объявлено в
-- репозитории». Ни одна из них не проявилась падением: ровно поэтому они и
-- дожили незамеченными.
--
-- 1. public.agent_gateway_logs_test без колонок seq и occurred_at.
--    Писатель журнала (lib/services/db_logging_service.py::_flush_batch) вставляет
--    ФИКСИРОВАННЫЙ список колонок, включающий seq и occurred_at, и event_time_columns()
--    отказывается штамповать событие без них. Значит под профилем test не пишется
--    НИ ОДНА строка журнала: весь execute_batch падает на «column "seq" does not
--    exist», а потеря батча не видна в консоли — в этом и суть дефекта. Колонки
--    объявляются nullable намеренно, как и в DDL клона: контракт чтения оборота
--    без ключа порядка должен оставаться представимым (см. шапку
--    sql/logs/create_public_agent_gateway_logs_test.sql).
--
-- 2. На тест-журнале нет индексов idx_agent_gateway_logs_test_seq и
--    idx_agent_gateway_logs_test_occurred_at, которые объявляет его же DDL.
--    Следствие 1 объясняет, почему их не создавал и повторный psql -f: файл
--    строил индексы по колонке, которую объявлял НИЖЕ по тексту, и падал на
--    чистой базе. Порядок исправлен в самом DDL (create_public_agent_gateway_logs_test.sql) —
--    файл не версионированный, отдельной миграции его перестановка не требует.
--
-- 3. public.agent_session_messages без первичного ключа. sql/session/
--    create_public_agent_session_messages.sql объявляет PRIMARY KEY
--    (replica_id, session_key, seq), V010 добавил такой ключ только
--    agent_session_meta (шаг 4), V011 построил уникальный индекс на сообщения,
--    но ограничения на него так и не навесили. Уникальный индекс уже есть и
--    покрывает ровно эти три колонки, поэтому PostgreSQL переиспользует его
--    как индекс первичного ключа — перестроения таблицы не будет. Дубликатов
--    быть не может: они запрещены этим самым индексом, и V011 перед его
--    созданием проверял остачу громко.
--
-- Ничего не удаляется и ни одна существующая строка не переписывается.
--
-- Совместимость: Greenplum 6.5 (ядро PostgreSQL 9.4) и PostgreSQL 13+.
-- ADD COLUMN IF NOT EXISTS (9.6) и CREATE INDEX IF NOT EXISTS (9.5) на 9.4
-- недоступны, поэтому каждая операция идёт через проверку каталога.
-- Идемпотентна: повторное применение — no-op.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Журнал тест-профиля: момент события и ключ порядка.
-- ---------------------------------------------------------------------------
DO $columns$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'agent_gateway_logs_test'
          AND column_name  = 'seq'
    ) THEN
        ALTER TABLE public.agent_gateway_logs_test ADD COLUMN seq BIGINT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'agent_gateway_logs_test'
          AND column_name  = 'occurred_at'
    ) THEN
        ALTER TABLE public.agent_gateway_logs_test ADD COLUMN occurred_at TIMESTAMPTZ;
    END IF;
END
$columns$;

-- ---------------------------------------------------------------------------
-- 2. Индексы под момент события и ключ порядка — те же, что на боевой
--    таблице, иначе тест-контур проверял бы план чтения, которого нет в бою.
-- ---------------------------------------------------------------------------
DO $indexes$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'agent_gateway_logs_test'
          AND indexname  = 'idx_agent_gateway_logs_test_seq'
    ) THEN
        CREATE INDEX idx_agent_gateway_logs_test_seq
            ON public.agent_gateway_logs_test (seq);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'agent_gateway_logs_test'
          AND indexname  = 'idx_agent_gateway_logs_test_occurred_at'
    ) THEN
        CREATE INDEX idx_agent_gateway_logs_test_occurred_at
            ON public.agent_gateway_logs_test (occurred_at DESC);
    END IF;
END
$indexes$;

-- ---------------------------------------------------------------------------
-- 3. Первичный ключ журнала тест-профиля. Проверка «а нет ли уже ключа»:
--    на свежей базе DDL создаёт таблицу с ключом, и навешивать второй нельзя.
-- ---------------------------------------------------------------------------
DO $log_pk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.agent_gateway_logs_test'::regclass
          AND contype  = 'p'
    ) THEN
        ALTER TABLE public.agent_gateway_logs_test
            ADD CONSTRAINT agent_gateway_logs_test_pkey PRIMARY KEY (id);
    END IF;
END
$log_pk$;

-- ---------------------------------------------------------------------------
-- 4. Первичный ключ строки сообщения — то, что объявляет create-DDL и чего
--    не навесили ни V010 (ключ менялся только у метаданных), ни V011 (индексы).
-- ---------------------------------------------------------------------------
DO $messages_pk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.agent_session_messages'::regclass
          AND contype  = 'p'
    ) THEN
        ALTER TABLE public.agent_session_messages
            ADD CONSTRAINT agent_session_messages_pkey
            PRIMARY KEY (replica_id, session_key, seq);
    END IF;
END
$messages_pk$;
