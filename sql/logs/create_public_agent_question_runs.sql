-- ============================================================================
-- public.agent_question_runs — контекст вопроса/прогона агента
-- Одна строка на request_id. Не дублируется на каждое событие лога.
-- Стройная схема: полный текст вопроса/ответа здесь, события — в agent_gateway_logs.
-- Управляется: lib/services/db_logging_service.py.
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_question_runs (
    request_id        VARCHAR(256) NOT NULL,
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
    media             TEXT,

    PRIMARY KEY (request_id)
)
DISTRIBUTED BY (request_id);

COMMENT ON TABLE  public.agent_question_runs IS 'Контекст вопроса/прогона: пользователь, агент, статус, вопрос/ответ, summary. Одна строка на request_id.';
COMMENT ON COLUMN public.agent_question_runs.request_id        IS 'PK — ID сообщения, вызвавшего обработку.';
COMMENT ON COLUMN public.agent_question_runs.created_at        IS 'Время регистрации вопроса.';
COMMENT ON COLUMN public.agent_question_runs.updated_at        IS 'Время последнего изменения (status/summary).';
COMMENT ON COLUMN public.agent_question_runs.session_id        IS 'Ключ сессии (channel:chat_id).';
COMMENT ON COLUMN public.agent_question_runs.user_id           IS 'ID пользователя (sender_id).';
COMMENT ON COLUMN public.agent_question_runs.chat_id           IS 'ID чата.';
COMMENT ON COLUMN public.agent_question_runs.channel           IS 'Канал (telegram/cli/etc).';
COMMENT ON COLUMN public.agent_question_runs.agent_id          IS 'Агент, обрабатывающий вопрос.';
COMMENT ON COLUMN public.agent_question_runs.parent_agent_id   IS 'Для подагента — родительский агент.';
COMMENT ON COLUMN public.agent_question_runs.parent_request_id IS 'Для подагента — request_id родительского вопроса.';
COMMENT ON COLUMN public.agent_question_runs.is_subagent       IS 'True, если это подагент.';
COMMENT ON COLUMN public.agent_question_runs.status            IS 'running / finished / error.';
COMMENT ON COLUMN public.agent_question_runs.summary           IS 'Краткое описание: финальный ответ или описание задачи.';
COMMENT ON COLUMN public.agent_question_runs.question          IS 'Полный текст вопроса (без обрезки).';
COMMENT ON COLUMN public.agent_question_runs.response          IS 'Полный текст ответа агента (без обрезки).';
COMMENT ON COLUMN public.agent_question_runs.media             IS 'JSON-список вложений (media).';
