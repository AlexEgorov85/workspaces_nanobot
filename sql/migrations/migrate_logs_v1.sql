-- Миграция существующей схемы под модель agent_question_runs + стройный agent_gateway_logs.
-- Идемпотентна. Применяется из DbLoggingService._ensure_schema после create_logs_table.sql.
-- Префикс agent_ — таблицы агента (не навыка).

-- 0) Миграция со старых имён (если ещё не сделано в create_logs_table.sql)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'question_runs') THEN
    ALTER TABLE public.question_runs RENAME TO agent_question_runs;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'gateway_logs') THEN
    ALTER TABLE public.gateway_logs RENAME TO agent_gateway_logs;
  END IF;
END $$;

-- 1) Таблица контекста вопроса (одна строка на request_id).
CREATE TABLE IF NOT EXISTS agent_question_runs (
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
    summary           TEXT,
    question          TEXT,
    response          TEXT,
    media             TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_qruns_user
    ON agent_question_runs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_qruns_session
    ON agent_question_runs (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_qruns_parent_request
    ON agent_question_runs (parent_request_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_qruns_agent
    ON agent_question_runs (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_qruns_subagent
    ON agent_question_runs (is_subagent, created_at DESC);

-- колонки question/response/media для уже существующей таблицы
ALTER TABLE agent_question_runs ADD COLUMN IF NOT EXISTS question TEXT;
ALTER TABLE agent_question_runs ADD COLUMN IF NOT EXISTS response TEXT;
ALTER TABLE agent_question_runs ADD COLUMN IF NOT EXISTS media TEXT;

-- 2) agent_gateway_logs: добавить недостающие колонки стройной схемы
--    (request_id — связь с agent_question_runs, name — субъект события).
--    Избыточные колонки, оставшиеся от старой плоской схемы, НЕ дропаем
--    (деструктивно) — они просто остаются пустыми. Новые события пишутся
--    только в стройный набор колонок.
ALTER TABLE agent_gateway_logs ADD COLUMN IF NOT EXISTS request_id VARCHAR(256);
ALTER TABLE agent_gateway_logs ADD COLUMN IF NOT EXISTS name VARCHAR(256);
CREATE INDEX IF NOT EXISTS idx_agent_logs_name
    ON agent_gateway_logs (name, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_agent_logs_request
    ON agent_gateway_logs (request_id, "timestamp" DESC);
