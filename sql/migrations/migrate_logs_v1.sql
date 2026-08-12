-- Миграция существующей схемы под модель question_runs + стройный gateway_logs.
-- Идемпотентна. Применяется из DbLoggingService._ensure_schema после create_logs_table.sql.

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
);
CREATE INDEX IF NOT EXISTS idx_qruns_user ON question_runs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qruns_session ON question_runs (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qruns_parent_request ON question_runs (parent_request_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qruns_agent ON question_runs (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qruns_subagent ON question_runs (is_subagent, created_at DESC);

-- 2) gateway_logs: добавить недостающие колонки стройной схемы
--    (request_id — связь с question_runs, name — субъект события).
--    Избыточные колонки, оставшиеся от старой плоской схемы, НЕ дропаем
--    (деструктивно) — они просто остаются пустыми. Новые события пишутся
--    только в стройный набор колонок.
ALTER TABLE gateway_logs ADD COLUMN IF NOT EXISTS request_id VARCHAR(256);
ALTER TABLE gateway_logs ADD COLUMN IF NOT EXISTS name VARCHAR(256);
CREATE INDEX IF NOT EXISTS idx_logs_name ON gateway_logs (name, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_logs_request ON gateway_logs (request_id, "timestamp" DESC);
