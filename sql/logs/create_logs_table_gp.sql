-- Greenplum 6.x (база PostgreSQL 9.4) vs PostgreSQL 13 — что менять:
--   * CREATE TABLE IF NOT EXISTS      — поддержан (PG 9.1+)        → ок;
--   * CREATE INDEX IF NOT EXISTS      — НЕ поддержан (PG 9.5+)     → DO-блоки
--                                      с проверкой pg_indexes;
--   * ALTER TABLE ADD COLUMN IF NOT EXISTS — НЕ поддержан (PG 9.6+)
--                                      → DO-блоки с проверкой
--                                      information_schema.columns;
--   * INSERT ... ON CONFLICT DO UPDATE — НЕ поддержан (только GP 7)
--                                      → апсерт делается в Python
--                                      (UPDATE + INSERT WHERE NOT EXISTS);
--   * gen_random_uuid()                — только через pgcrypto;
--                                      id всегда приходит из приложения;
--   * DISTRIBUTED BY                   — задаём явно для co-located join'ов
--                                      по request_id.
--
-- Плейсхолдеры, подставляются в DbLoggingService._ensure_schema:
--   @@SCHEMA@@      → имя схемы (напр. public)
--   @@TABLE@@       → имя таблицы логов (напр. gateway_logs)
--   @@TABLE_DDL@@   → schema-qualified имя таблицы логов ("schema"."table")

CREATE TABLE IF NOT EXISTS question_runs (
    request_id        VARCHAR(256) PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Идентификаторы
    session_id        VARCHAR(256),   -- channel:chat_id
    user_id           VARCHAR(256),   -- пользователь (sender_id)
    chat_id           VARCHAR(256),
    channel           VARCHAR(64),

    -- Идентификация агента
    agent_id          VARCHAR(256),   -- агент, обрабатывающий вопрос
    parent_agent_id   VARCHAR(256),   -- для подагента -> родительский агент
    parent_request_id VARCHAR(256),   -- для подагента -> вопрос-родитель
    is_subagent       BOOLEAN NOT NULL DEFAULT FALSE,

    -- Краткое описание/статус (обновляется по мере прогона)
    status            VARCHAR(32),    -- running / finished / error
    summary           TEXT            -- финальный ответ или описание задачи
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


-- gateway_logs — структурированный журнал событий агента.
-- Внимание, Greenplum: PRIMARY KEY на id + DISTRIBUTED BY (request_id)
-- несовместимы (ключ распределения должен быть подмножеством PK), поэтому
-- у таблицы нет PRIMARY KEY — уникальность id обеспечивает приложение
-- (UUID генерируется в Python). Распределение по request_id делает
-- JOIN с question_runs co-located.
CREATE TABLE IF NOT EXISTS @@TABLE_DDL@@ (
    id              UUID NOT NULL,
    "timestamp"    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level          VARCHAR(16) NOT NULL,
    event_type     VARCHAR(64) NOT NULL,

    request_id     VARCHAR(256),   -- FK -> question_runs.request_id (без жёсткого FK)
    session_id     VARCHAR(256),   -- channel:chat_id (денормализовано для удобства)
    channel        VARCHAR(64),
    actor          VARCHAR(32),
    name           VARCHAR(256),   -- субъект события (имя инструмента/задачи/...)

    summary        TEXT,
    payload        JSONB,
    metadata       JSONB,

    CONSTRAINT valid_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR'))
) DISTRIBUTED BY (request_id);

-- Индексы gateway_logs создаются в migrate_logs_v1_gp.sql (после возможного
-- ALTER ADD COLUMN для уже существующей таблицы).

-- Полезные запросы (см. create_logs_table.sql) — идентичны:
-- JOIN по request_id co-located, т.к. обе таблицы распределены по request_id.
