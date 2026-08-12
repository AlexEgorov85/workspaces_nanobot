-- gateway_logs — структурированный журнал событий агента.
CREATE TABLE IF NOT EXISTS gateway_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    "timestamp"     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level           VARCHAR(16) NOT NULL,
    event_type      VARCHAR(64) NOT NULL,
    session_id      VARCHAR(256),
    channel         VARCHAR(64),
    actor           VARCHAR(32),
    summary         TEXT,
    payload         JSONB,
    metadata        JSONB,
    CONSTRAINT valid_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR'))
);

CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON gateway_logs ("timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_logs_session ON gateway_logs (session_id, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_logs_event_type ON gateway_logs (event_type, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_logs_level ON gateway_logs (level, "timestamp" DESC);

-- Полезные запросы для проверки:
-- SELECT "timestamp", level, event_type, session_id, summary
-- FROM gateway_logs ORDER BY "timestamp" DESC LIMIT 10;
--
-- SELECT event_type, COUNT(*) FROM gateway_logs
-- WHERE "timestamp" > NOW() - INTERVAL '1 hour'
-- GROUP BY event_type ORDER BY 2 DESC;
--
-- SELECT payload->>'tool' AS tool,
--        AVG((metadata->>'latency_ms')::float) AS avg_ms,
--        COUNT(*) AS calls
-- FROM gateway_logs
-- WHERE event_type = 'tool_result' AND "timestamp" > NOW() - INTERVAL '24 hours'
-- GROUP BY payload->>'tool' ORDER BY avg_ms DESC;
