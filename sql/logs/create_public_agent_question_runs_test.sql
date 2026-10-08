-- ============================================================================
-- public.agent_question_runs_test — test-клон public.agent_question_runs
-- Контекст вопроса/прогона агента под профилем test.
-- Одна строка на request_id. Не дублируется на каждое событие лога.
-- Управляется: lib/services/db_logging_service.py.
-- Совместимость: Greenplum 6.5.
--
-- Этот файл живёт ТОЛЬКО для psql-ручного применения; штатное применение
-- всех шести test-таблиц — `python tools/apply_test_profile_tables.py`
-- (миграции схемы test-профиля в репозитории нет).
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_question_runs_test (
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
);

COMMENT ON TABLE  public.agent_question_runs_test IS 'Test-профиль: контекст вопроса/прогона. Структурный клон public.agent_question_runs; используется под профилем test.';
COMMENT ON COLUMN public.agent_question_runs_test.request_id        IS 'PK — ID сообщения, вызвавшего обработку.';
COMMENT ON COLUMN public.agent_question_runs_test.created_at        IS 'Время регистрации вопроса.';
COMMENT ON COLUMN public.agent_question_runs_test.updated_at        IS 'Время последнего изменения (status/summary).';
COMMENT ON COLUMN public.agent_question_runs_test.session_id        IS 'Ключ сессии (channel:chat_id).';
COMMENT ON COLUMN public.agent_question_runs_test.user_id           IS 'ID пользователя (sender_id).';
COMMENT ON COLUMN public.agent_question_runs_test.chat_id           IS 'ID чата.';
COMMENT ON COLUMN public.agent_question_runs_test.channel           IS 'Канал (telegram/cli/etc).';
COMMENT ON COLUMN public.agent_question_runs_test.agent_id          IS 'Агент, обрабатывающий вопрос.';
COMMENT ON COLUMN public.agent_question_runs_test.parent_agent_id   IS 'Для подагента — родительский агент.';
COMMENT ON COLUMN public.agent_question_runs_test.parent_request_id IS 'Для подагента — request_id родительского вопроса.';
COMMENT ON COLUMN public.agent_question_runs_test.is_subagent       IS 'True, если это подагент.';
COMMENT ON COLUMN public.agent_question_runs_test.status            IS 'running / finished / error.';
COMMENT ON COLUMN public.agent_question_runs_test.summary           IS 'Краткое описание: финальный ответ или описание задачи.';
COMMENT ON COLUMN public.agent_question_runs_test.question          IS 'Полный текст вопроса (без обрезки).';
COMMENT ON COLUMN public.agent_question_runs_test.response          IS 'Полный текст ответа агента (без обрезки).';
COMMENT ON COLUMN public.agent_question_runs_test.media             IS 'JSON-список вложений (media).';
