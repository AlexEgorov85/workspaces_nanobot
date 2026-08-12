-- Greenplum 6.25: create_session_tables.sql + DISTRIBUTED BY, без FK

CREATE TABLE IF NOT EXISTS public.session_meta (
    session_key       TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_consolidated INT NOT NULL DEFAULT 0,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
)
DISTRIBUTED BY (session_key);

-- Сообщения сессии (append-only по seq)
-- FK убран — GP не поддерживает внешние ключи.
-- Каскадное удаление выполняется в коде (pg_session_manager.py).
CREATE TABLE IF NOT EXISTS public.session_messages (
    id                BIGSERIAL,
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
)
DISTRIBUTED BY (session_key);

CREATE INDEX IF NOT EXISTS idx_session_messages_sk_seq
    ON public.session_messages (session_key, seq);
