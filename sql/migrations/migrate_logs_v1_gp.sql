-- Миграция существующей схемы под модель question_runs + стройный gateway_logs.
-- Greenplum 6.x (база PostgreSQL 9.4): идемпотентность НЕ через
-- CREATE INDEX/ADD COLUMN IF NOT EXISTS (нет в PG 9.4) — через DO-блоки
-- с проверкой каталога (pg_indexes / information_schema.columns).
--
-- Плейсхолдеры (см. create_logs_table_gp.sql): @@SCHEMA@@ / @@TABLE@@ /
-- @@TABLE_DDL@@.

-- 1) Таблица контекста вопроса (одна строка на request_id).
CREATE TABLE IF NOT EXISTS question_runs (
    request_id        VARCHAR(256) PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    session_id        VARCHAR(256),
    user_id           VARCHAR(256),
    chat_id           VARCHAR(256),
    channel           VARCHAR(64),
    agent_id          VARCHAR(256),
    parent_agent_id   VARCHAR(256),
    parent_request_id VARCHAR(256),
    is_subagent       BOOLEAN NOT NULL DEFAULT FALSE,
    status            VARCHAR(32),
    summary           TEXT
) DISTRIBUTED BY (request_id);

DO $gp$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = '@@SCHEMA@@'
                     AND tablename = 'question_runs'
                     AND indexname = 'idx_qruns_user') THEN
        CREATE INDEX idx_qruns_user ON question_runs (user_id, created_at DESC);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = '@@SCHEMA@@'
                     AND tablename = 'question_runs'
                     AND indexname = 'idx_qruns_session') THEN
        CREATE INDEX idx_qruns_session ON question_runs (session_id, created_at DESC);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = '@@SCHEMA@@'
                     AND tablename = 'question_runs'
                     AND indexname = 'idx_qruns_parent_request') THEN
        CREATE INDEX idx_qruns_parent_request ON question_runs (parent_request_id, created_at DESC);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = '@@SCHEMA@@'
                     AND tablename = 'question_runs'
                     AND indexname = 'idx_qruns_agent') THEN
        CREATE INDEX idx_qruns_agent ON question_runs (agent_id, created_at DESC);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = '@@SCHEMA@@'
                     AND tablename = 'question_runs'
                     AND indexname = 'idx_qruns_subagent') THEN
        CREATE INDEX idx_qruns_subagent ON question_runs (is_subagent, created_at DESC);
    END IF;
END
$gp$;

-- 2) gateway_logs: добавить недостающие колонки стройной схемы
--    (request_id — связь с question_runs, name — субъект события).
--    Избыточные колонки старой плоской схемы НЕ дропаем (деструктивно).
DO $gp$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = '@@SCHEMA@@'
                     AND table_name = '@@TABLE@@'
                     AND column_name = 'request_id') THEN
        EXECUTE format('ALTER TABLE %s ADD COLUMN request_id VARCHAR(256)', '@@TABLE_DDL@@');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = '@@SCHEMA@@'
                     AND table_name = '@@TABLE@@'
                     AND column_name = 'name') THEN
        EXECUTE format('ALTER TABLE %s ADD COLUMN name VARCHAR(256)', '@@TABLE_DDL@@');
    END IF;
END
$gp$;

DO $gp$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = '@@SCHEMA@@'
                     AND tablename = '@@TABLE@@'
                     AND indexname = 'idx_logs_name') THEN
        EXECUTE format('CREATE INDEX idx_logs_name ON %s (name, "timestamp" DESC)', '@@TABLE_DDL@@');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = '@@SCHEMA@@'
                     AND tablename = '@@TABLE@@'
                     AND indexname = 'idx_logs_request') THEN
        EXECUTE format('CREATE INDEX idx_logs_request ON %s (request_id, "timestamp" DESC)', '@@TABLE_DDL@@');
    END IF;
END
$gp$;
