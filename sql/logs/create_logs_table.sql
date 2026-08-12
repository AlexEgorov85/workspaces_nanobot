-- question_runs — контекст вопроса/прогона (одна строка на request_id).
-- Хранит то, что относится к вопросу целиком, а не к отдельному событию:
-- пользователь, агент, подагент-родитель, состояние. Не дублируется
-- на каждое событие лога (см. gateway_logs.request_id).
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
);

CREATE INDEX IF NOT EXISTS idx_qruns_user ON question_runs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qruns_session ON question_runs (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qruns_parent_request ON question_runs (parent_request_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qruns_agent ON question_runs (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qruns_subagent ON question_runs (is_subagent, created_at DESC);


-- gateway_logs — структурированный журнал событий агента.
-- Стройная: контекст вопроса живёт в question_runs (по request_id),
-- здесь — только то, что относится к конкретному событию.
CREATE TABLE IF NOT EXISTS gateway_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    "timestamp"    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level          VARCHAR(16) NOT NULL,
    event_type     VARCHAR(64) NOT NULL,

    request_id     VARCHAR(256),   -- FK -> question_runs.request_id (логично, но без жесткого FK)
    session_id     VARCHAR(256),   -- channel:chat_id (денормализовано для удобства)
    channel        VARCHAR(64),
    actor          VARCHAR(32),
    name           VARCHAR(256),   -- субъект события (имя инструмента/задачи/...)

    summary        TEXT,
    payload        JSONB,
    metadata       JSONB,

    CONSTRAINT valid_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR'))
);

CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON gateway_logs ("timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_logs_event_type ON gateway_logs (event_type, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_logs_level ON gateway_logs (level, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_logs_session ON gateway_logs (session_id, "timestamp" DESC);
-- Индексы по request_id/name — в migrate_logs_v1.sql (после ALTER ADD COLUMN),
-- т.к. для уже существующей таблицы их нельзя создать до добавления колонок.

-- Полезные запросы:
-- -- Логи одного вопроса
-- SELECT l.*, r.user_id, r.agent_id, r.is_subagent
-- FROM gateway_logs l
-- JOIN question_runs r ON r.request_id = l.request_id
-- WHERE l.request_id = '<message_id>'
-- ORDER BY l."timestamp";
--
-- -- Все вопросы пользователя
-- SELECT * FROM question_runs WHERE user_id = '<user>' ORDER BY created_at DESC;
--
-- -- Вопросы, где запускался подагент
-- SELECT * FROM question_runs
-- WHERE request_id IN (
--     SELECT DISTINCT parent_request_id FROM question_runs
--     WHERE is_subagent AND parent_request_id IS NOT NULL
-- ) ORDER BY created_at DESC;
--
-- -- Подагент какого агента
-- SELECT parent_agent_id, parent_request_id FROM question_runs
-- WHERE is_subagent AND request_id = '<subagent_request_id>';
--
-- -- Вопросы, обработанные конкретным агентом
-- SELECT * FROM question_runs WHERE agent_id = '<agent>' ORDER BY created_at DESC;
