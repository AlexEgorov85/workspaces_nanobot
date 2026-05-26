-- Таблицы для хранения истории сессий nanobot в PostgreSQL
-- Вместо JSONL-файлов в workspace/sessions/*.jsonl
-- Управляется PGSessionManager (pg_session_manager.py)
-- Создаются автоматически при вызове PGSessionManager.ensure_tables()

-- Мета-информация о сессии
CREATE TABLE IF NOT EXISTS public.session_meta (
    session_key       TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_consolidated INT NOT NULL DEFAULT 0,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Сообщения сессии (append-only по seq)
CREATE TABLE IF NOT EXISTS public.session_messages (
    id                BIGSERIAL PRIMARY KEY,
    session_key       TEXT NOT NULL
                      REFERENCES public.session_meta(session_key) ON DELETE CASCADE,
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

-- Индекс для быстрой загрузки сообщений сессии по порядку
CREATE INDEX IF NOT EXISTS idx_session_messages_sk_seq
    ON public.session_messages (session_key, seq);
