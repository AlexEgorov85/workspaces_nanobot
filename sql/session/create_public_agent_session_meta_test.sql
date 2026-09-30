-- ============================================================================
-- public.agent_session_meta_test — test-клон public.agent_session_meta
-- Заменяет JSONL-файлы тестовой сессии в workspace/sessions/ под профилем test.
-- Управляется: lib/session/pg_session_manager.py (PGSessionManager).
-- Совместимость: Greenplum 6.5 (PostgreSQL 9.4 ядро).
--
-- Этот файл живёт ТОЛЬКО для psql-ручного применения; для версионированного
-- применения через runner — V005__test_profile_tables.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_session_meta_test (
    session_key       TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_consolidated INT NOT NULL DEFAULT 0,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE  public.agent_session_meta_test IS 'Test-профиль: метаданные сессий nanobot. Заменяет JSONL-файлы в workspace/sessions/ под профилем test. Управляется PGSessionManager.';
COMMENT ON COLUMN public.agent_session_meta_test.session_key       IS 'PK — уникальный ключ сессии (например, "telegram:12345").';
COMMENT ON COLUMN public.agent_session_meta_test.created_at        IS 'Время создания сессии.';
COMMENT ON COLUMN public.agent_session_meta_test.updated_at        IS 'Время последнего изменения.';
COMMENT ON COLUMN public.agent_session_meta_test.last_consolidated IS 'Последний seq, до которого сообщения консолидированы.';
COMMENT ON COLUMN public.agent_session_meta_test.metadata          IS 'Произвольные метаданные сессии (user_id, channel, ...).';
