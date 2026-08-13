-- ============================================================================
-- Таблицы журнала событий агента nanobot.
--
-- Префикс agent_ — таблицы, которыми владеет сам агент (не навык).
-- Это разделяет "системные" таблицы nanobot от "доменных" данных навыков
-- (аудит, бизнес-таблицы) в общей БД.
--
-- Стройная схема: контекст вопроса в agent_question_runs (по request_id),
-- отдельные события в agent_gateway_logs.
-- ============================================================================

-- миграция со старых имён
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
  -- Индексы со старыми именами
  IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_qruns_user') THEN
    ALTER INDEX public.idx_qruns_user RENAME TO idx_agent_qruns_user;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_qruns_session') THEN
    ALTER INDEX public.idx_qruns_session RENAME TO idx_agent_qruns_session;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_qruns_parent_request') THEN
    ALTER INDEX public.idx_qruns_parent_request RENAME TO idx_agent_qruns_parent_request;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_qruns_agent') THEN
    ALTER INDEX public.idx_qruns_agent RENAME TO idx_agent_qruns_agent;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_qruns_subagent') THEN
    ALTER INDEX public.idx_qruns_subagent RENAME TO idx_agent_qruns_subagent;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_logs_timestamp') THEN
    ALTER INDEX public.idx_logs_timestamp RENAME TO idx_agent_logs_timestamp;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_logs_event_type') THEN
    ALTER INDEX public.idx_logs_event_type RENAME TO idx_agent_logs_event_type;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_logs_level') THEN
    ALTER INDEX public.idx_logs_level RENAME TO idx_agent_logs_level;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_logs_session') THEN
    ALTER INDEX public.idx_logs_session RENAME TO idx_agent_logs_session;
  END IF;
END $$;

-- ---- agent_question_runs: контекст вопроса/прогона ----

CREATE TABLE IF NOT EXISTS public.agent_question_runs (
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
    summary           TEXT,           -- финальный ответ или описание задачи (кратко)

    -- Полный текст вопроса и ответа (без обрезки)
    question          TEXT,           -- полный текст сообщения пользователя
    response          TEXT,           -- полный текст ответа агента
    media             TEXT            -- JSON-список вложенных файлов (media)
);

COMMENT ON TABLE public.agent_question_runs IS
    'Контекст вопроса/прогона: пользователь, агент, статус, вопрос/ответ, summary. '
    'Одна строка на request_id. Не дублируется на каждое событие лога. '
    'Полный текст вопроса/ответа в question/response, media — вложения. '
    'Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_question_runs.request_id IS 'PK — ID сообщения, вызвавшего обработку.';
COMMENT ON COLUMN public.agent_question_runs.created_at IS 'Время регистрации вопроса.';
COMMENT ON COLUMN public.agent_question_runs.updated_at IS 'Время последнего изменения (status/summary).';
COMMENT ON COLUMN public.agent_question_runs.session_id IS 'Ключ сессии (channel:chat_id).';
COMMENT ON COLUMN public.agent_question_runs.user_id IS 'ID пользователя (sender_id).';
COMMENT ON COLUMN public.agent_question_runs.chat_id IS 'ID чата.';
COMMENT ON COLUMN public.agent_question_runs.channel IS 'Канал (telegram/cli/etc).';
COMMENT ON COLUMN public.agent_question_runs.agent_id IS 'Агент, обрабатывающий вопрос.';
COMMENT ON COLUMN public.agent_question_runs.parent_agent_id IS 'Для подагента — родительский агент.';
COMMENT ON COLUMN public.agent_question_runs.parent_request_id IS 'Для подагента — request_id родительского вопроса.';
COMMENT ON COLUMN public.agent_question_runs.is_subagent IS 'True, если это подагент.';
COMMENT ON COLUMN public.agent_question_runs.status IS 'running / finished / error.';
COMMENT ON COLUMN public.agent_question_runs.summary IS 'Краткое описание: финальный ответ (обрезанный) или описание задачи.';
COMMENT ON COLUMN public.agent_question_runs.question IS 'Полный текст вопроса (сообщения пользователя), без обрезки.';
COMMENT ON COLUMN public.agent_question_runs.response IS 'Полный текст ответа агента, без обрезки.';
COMMENT ON COLUMN public.agent_question_runs.media IS 'JSON-список вложений (media): пути/URL файлов, приложенных пользователем или агентом.';

CREATE INDEX IF NOT EXISTS idx_agent_qruns_user
    ON public.agent_question_runs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_qruns_session
    ON public.agent_question_runs (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_qruns_parent_request
    ON public.agent_question_runs (parent_request_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_qruns_agent
    ON public.agent_question_runs (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_qruns_subagent
    ON public.agent_question_runs (is_subagent, created_at DESC);

-- колонки question/response/media для уже существующей таблицы (идемпотентно)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'agent_question_runs'
                     AND column_name = 'question') THEN
        ALTER TABLE public.agent_question_runs ADD COLUMN question TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'agent_question_runs'
                     AND column_name = 'response') THEN
        ALTER TABLE public.agent_question_runs ADD COLUMN response TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'agent_question_runs'
                     AND column_name = 'media') THEN
        ALTER TABLE public.agent_question_runs ADD COLUMN media TEXT;
    END IF;
END $$;

-- ---- agent_gateway_logs: структурированный журнал событий ----

CREATE TABLE IF NOT EXISTS public.agent_gateway_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    "timestamp"    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level          VARCHAR(16) NOT NULL,
    event_type     VARCHAR(64) NOT NULL,

    request_id     VARCHAR(256),   -- FK -> agent_question_runs.request_id (логично, но без жесткого FK)
    session_id     VARCHAR(256),   -- channel:chat_id (денормализовано для удобства)
    channel        VARCHAR(64),
    actor          VARCHAR(32),
    name           VARCHAR(256),   -- субъект события (имя инструмента/задачи/...)

    summary        TEXT,
    payload        JSONB,
    metadata       JSONB,

    CONSTRAINT valid_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR'))
);

COMMENT ON TABLE public.agent_gateway_logs IS
    'Структурированный журнал событий агента. '
    'Стройный: контекст вопроса в agent_question_runs (по request_id), '
    'здесь — только то, что относится к конкретному событию. '
    'Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_gateway_logs.id IS 'PK события (UUID).';
COMMENT ON COLUMN public.agent_gateway_logs."timestamp" IS 'Время события.';
COMMENT ON COLUMN public.agent_gateway_logs.level IS 'Уровень логирования: DEBUG/INFO/WARN/ERROR.';
COMMENT ON COLUMN public.agent_gateway_logs.event_type IS 'Тип события (tool_call, agent_run, ...).';
COMMENT ON COLUMN public.agent_gateway_logs.request_id IS 'FK-логически на agent_question_runs.request_id.';
COMMENT ON COLUMN public.agent_gateway_logs.session_id IS 'Денормализованный channel:chat_id для удобства.';
COMMENT ON COLUMN public.agent_gateway_logs.channel IS 'Канал (telegram/cli/etc).';
COMMENT ON COLUMN public.agent_gateway_logs.actor IS 'Кто инициировал событие (user/agent/system).';
COMMENT ON COLUMN public.agent_gateway_logs.name IS 'Имя инструмента / задачи / сущности события.';
COMMENT ON COLUMN public.agent_gateway_logs.summary IS 'Краткое текстовое описание события.';
COMMENT ON COLUMN public.agent_gateway_logs.payload IS 'JSONB: детальные данные события.';
COMMENT ON COLUMN public.agent_gateway_logs.metadata IS 'JSONB: дополнительные метаданные (request_id, ...).';

CREATE INDEX IF NOT EXISTS idx_agent_logs_timestamp
    ON public.agent_gateway_logs ("timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_agent_logs_event_type
    ON public.agent_gateway_logs (event_type, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_agent_logs_level
    ON public.agent_gateway_logs (level, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_agent_logs_session
    ON public.agent_gateway_logs (session_id, "timestamp" DESC);
-- Индексы по request_id/name — в migrate_logs_v1.sql (после ALTER ADD COLUMN),
-- т.к. для уже существующей таблицы их нельзя создать до добавления колонок.

-- Полезные запросы:
-- -- Логи одного вопроса
-- SELECT l.*, r.user_id, r.agent_id, r.is_subagent
-- FROM agent_gateway_logs l
-- JOIN agent_question_runs r ON r.request_id = l.request_id
-- WHERE l.request_id = '<message_id>'
-- ORDER BY l."timestamp";
--
-- -- Все вопросы пользователя
-- SELECT * FROM agent_question_runs WHERE user_id = '<user>' ORDER BY created_at DESC;
--
-- -- Вопросы, где запускался подагент
-- SELECT * FROM agent_question_runs
-- WHERE request_id IN (
--     SELECT DISTINCT parent_request_id FROM agent_question_runs
--     WHERE is_subagent AND parent_request_id IS NOT NULL
-- ) ORDER BY created_at DESC;
--
-- -- Подагент какого агента
-- SELECT parent_agent_id, parent_request_id FROM agent_question_runs
-- WHERE is_subagent AND request_id = '<subagent_request_id>';
--
-- -- Вопросы, обработанные конкретным агентом
-- SELECT * FROM agent_question_runs WHERE agent_id = '<agent>' ORDER BY created_at DESC;
