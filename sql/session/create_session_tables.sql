-- ============================================================================
-- Таблицы для хранения истории сессий nanobot в PostgreSQL
-- Вместо JSONL-файлов в workspace/sessions/*.jsonl
-- Управляется PGSessionManager (pg_session_manager.py)
--
-- Префикс agent_ — таблицы, которыми владеет сам агент (не навык).
-- Это разделяет "системные" таблицы nanobot от "доменных" данных навыков
-- (аудит, бизнес-таблицы) в общей БД.
--
-- Для Greenplum 6.25 используйте create_session_tables_gp.sql
-- (добавляет DISTRIBUTED BY, убирает FK).
-- ============================================================================

-- ---- миграция со старых имён (если есть) ----
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'session_meta') THEN
    ALTER TABLE public.session_meta RENAME TO agent_session_meta;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'session_messages') THEN
    ALTER TABLE public.session_messages RENAME TO agent_session_messages;
  END IF;
  -- Индекс со старым именем
  IF EXISTS (SELECT 1 FROM pg_indexes
             WHERE schemaname = 'public' AND indexname = 'idx_session_messages_sk_seq') THEN
    ALTER INDEX public.idx_session_messages_sk_seq RENAME TO idx_agent_session_messages_sk_seq;
  END IF;
END $$;

-- ---- мета-информация о сессии ----

CREATE TABLE IF NOT EXISTS public.agent_session_meta (
    session_key       TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_consolidated INT NOT NULL DEFAULT 0,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE public.agent_session_meta IS
    'Метаданные сессий nanobot. Заменяет JSONL-файлы в workspace/sessions/. '
    'Управляется PGSessionManager (lib/session/pg_session_manager.py). '
    'Таблица агента (префикс agent_): не путать с доменными таблицами навыков.';
COMMENT ON COLUMN public.agent_session_meta.session_key IS
    'PK — уникальный ключ сессии (например, "telegram:12345").';
COMMENT ON COLUMN public.agent_session_meta.created_at IS
    'Время создания сессии.';
COMMENT ON COLUMN public.agent_session_meta.updated_at IS
    'Время последнего изменения.';
COMMENT ON COLUMN public.agent_session_meta.last_consolidated IS
    'Последний seq, до которого сообщения консолидированы (для оптимизации загрузки).';
COMMENT ON COLUMN public.agent_session_meta.metadata IS
    'Произвольные метаданные сессии (user_id, channel, ...).';

-- ---- сообщения сессии (append-only по seq) ----
-- FK на agent_session_meta убрано — Greenplum 6.25 не поддерживает внешние ключи.
-- Каскадное удаление сообщений выполняется в коде (pg_session_manager.py).
CREATE TABLE IF NOT EXISTS public.agent_session_messages (
    id                BIGSERIAL PRIMARY KEY,
    session_key       TEXT NOT NULL,
    seq               INT NOT NULL,
    role              TEXT NOT NULL,
    content           TEXT,
    msg_timestamp     TEXT,
    tool_calls        JSONB,
    tool_call_id      TEXT,
    name              TEXT,
    reasoning_content TEXT,
    thinking_blocks   JSONB,
    media             JSONB,
    cli_apps          JSONB,
    mcp_presets       JSONB,
    injected_event    TEXT,
    _command          BOOLEAN,
    _channel_delivery BOOLEAN,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.agent_session_messages IS
    'Сообщения чата в рамках сессии (append-only по session_key+seq). '
    'Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_session_messages.id IS 'PK сообщения.';
COMMENT ON COLUMN public.agent_session_messages.session_key IS
    'FK-логически на agent_session_meta.session_key (FK не объявлено для GP).';
COMMENT ON COLUMN public.agent_session_messages.seq IS
    'Порядковый номер сообщения в сессии (0, 1, 2, ...).';
COMMENT ON COLUMN public.agent_session_messages.role IS
    'Роль: user / assistant / system / tool.';
COMMENT ON COLUMN public.agent_session_messages.content IS 'Текст сообщения.';
COMMENT ON COLUMN public.agent_session_messages.msg_timestamp IS
    'Оригинальный timestamp из upstream (text для совместимости с разными форматами).';
COMMENT ON COLUMN public.agent_session_messages.tool_calls IS
    'JSONB: список вызовов инструментов ассистентом.';
COMMENT ON COLUMN public.agent_session_messages.tool_call_id IS
    'ID вызова инструмента (для результата tool-сообщения).';
COMMENT ON COLUMN public.agent_session_messages.name IS
    'Имя tool-функции (для tool-сообщения).';
COMMENT ON COLUMN public.agent_session_messages.reasoning_content IS
    'Цепочка рассуждений модели.';
COMMENT ON COLUMN public.agent_session_messages.thinking_blocks IS
    'JSONB: расширенное reasoning для thinking-моделей.';
COMMENT ON COLUMN public.agent_session_messages.media IS
    'JSONB: вложения (картинки, файлы, ...).';
COMMENT ON COLUMN public.agent_session_messages.cli_apps IS
    'JSONB: список CLI-приложений, доступных в сообщении.';
COMMENT ON COLUMN public.agent_session_messages.mcp_presets IS 'JSONB: MCP-конфигурация.';
COMMENT ON COLUMN public.agent_session_messages.injected_event IS
    'Маркер инжектированного события (webhook/timer).';
COMMENT ON COLUMN public.agent_session_messages._command IS
    'Внутренний флаг: системная команда (не пользователь).';
COMMENT ON COLUMN public.agent_session_messages._channel_delivery IS
    'Внутренний флаг: доставлено в канал.';
COMMENT ON COLUMN public.agent_session_messages.created_at IS 'Время записи в БД.';

-- Индекс для быстрой загрузки сообщений сессии по порядку
CREATE INDEX IF NOT EXISTS idx_agent_session_messages_sk_seq
    ON public.agent_session_messages (session_key, seq);
