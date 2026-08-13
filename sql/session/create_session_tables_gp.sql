-- Greenplum 6.25: create_session_tables.sql + DISTRIBUTED BY, без FK
-- Префикс agent_ — таблицы агента (не навыка).

-- миграция со старых имён
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
  IF EXISTS (SELECT 1 FROM pg_indexes
             WHERE schemaname = 'public' AND indexname = 'idx_session_messages_sk_seq') THEN
    ALTER INDEX public.idx_session_messages_sk_seq RENAME TO idx_agent_session_messages_sk_seq;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.agent_session_meta (
    session_key       TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_consolidated INT NOT NULL DEFAULT 0,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
)
DISTRIBUTED BY (session_key);

CREATE TABLE IF NOT EXISTS public.agent_session_messages (
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

-- В GP нет IF NOT EXISTS для CREATE INDEX, поэтому DO-блок
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND indexname = 'idx_agent_session_messages_sk_seq'
  ) THEN
    CREATE INDEX idx_agent_session_messages_sk_seq
        ON public.agent_session_messages (session_key, seq);
  END IF;
END $$;
