-- ============================================================================
-- public.agent_session_meta — метаданные сессий nanobot
-- Заменяет JSONL-файлы в workspace/sessions/.
-- Управляется: lib/session/pg_session_manager.py (PGSessionManager).
-- Совместимость: Greenplum 6.5 (PostgreSQL 9.4 ядро).
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_session_meta (
    session_key       TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_consolidated INT NOT NULL DEFAULT 0,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
)
DISTRIBUTED BY (session_key);

COMMENT ON TABLE  public.agent_session_meta IS 'Метаданные сессий nanobot. Заменяет JSONL-файлы в workspace/sessions/. Управляется PGSessionManager.';
COMMENT ON COLUMN public.agent_session_meta.session_key       IS 'PK — уникальный ключ сессии (например, "telegram:12345").';
COMMENT ON COLUMN public.agent_session_meta.created_at        IS 'Время создания сессии.';
COMMENT ON COLUMN public.agent_session_meta.updated_at        IS 'Время последнего изменения.';
COMMENT ON COLUMN public.agent_session_meta.last_consolidated IS 'Последний seq, до которого сообщения консолидированы.';
COMMENT ON COLUMN public.agent_session_meta.metadata          IS 'Произвольные метаданные сессии (user_id, channel, ...).';
