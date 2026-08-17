-- ============================================================================
-- public.agent_gateway_logs — структурированный журнал событий агента
-- Распределён по request_id для co-located JOIN с agent_question_runs.
-- PK на id не объявлен: в GP ключ распределения должен быть подмножеством PK,
-- а DISTRIBUTED BY (request_id) + PK(id) — конфликт. Уникальность id
-- обеспечивает приложение (UUID генерируется в Python).
-- Управляется: lib/services/db_logging_service.py.
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS public.agent_gateway_logs (
    id           UUID NOT NULL,
    "timestamp"  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level        VARCHAR(16) NOT NULL,
    event_type   VARCHAR(64) NOT NULL,

    request_id   VARCHAR(256),
    session_id   VARCHAR(256),
    channel      VARCHAR(64),
    actor        VARCHAR(32),
    name         VARCHAR(256),

    summary      TEXT,
    payload      JSONB,
    metadata     JSONB,

    CONSTRAINT valid_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR'))
)
DISTRIBUTED BY (request_id);

COMMENT ON TABLE  public.agent_gateway_logs IS 'Структурированный журнал событий агента. Связан с agent_question_runs по request_id.';
COMMENT ON COLUMN public.agent_gateway_logs.id          IS 'PK события (UUID, генерируется в приложении).';
COMMENT ON COLUMN public.agent_gateway_logs."timestamp" IS 'Время события.';
COMMENT ON COLUMN public.agent_gateway_logs.level       IS 'Уровень логирования: DEBUG/INFO/WARN/ERROR.';
COMMENT ON COLUMN public.agent_gateway_logs.event_type  IS 'Тип события (tool_call, agent_run, ...).';
COMMENT ON COLUMN public.agent_gateway_logs.request_id  IS 'FK-логически на agent_question_runs.request_id.';
COMMENT ON COLUMN public.agent_gateway_logs.session_id  IS 'Денормализованный channel:chat_id для удобства.';
COMMENT ON COLUMN public.agent_gateway_logs.channel     IS 'Канал (telegram/cli/etc).';
COMMENT ON COLUMN public.agent_gateway_logs.actor       IS 'Кто инициировал событие (user/agent/system).';
COMMENT ON COLUMN public.agent_gateway_logs.name        IS 'Имя инструмента / задачи / сущности события.';
COMMENT ON COLUMN public.agent_gateway_logs.summary     IS 'Краткое текстовое описание события.';
COMMENT ON COLUMN public.agent_gateway_logs.payload     IS 'JSONB: детальные данные события.';
COMMENT ON COLUMN public.agent_gateway_logs.metadata    IS 'JSONB: дополнительные метаданные.';
