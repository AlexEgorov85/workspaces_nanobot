-- ============================================================================
-- public.agent_session_meta_test — test-клон public.agent_session_meta
--
-- Структурный клон боевой таблицы зеркала сессий под профилем test. Имя таблицы
-- хранит платформа (mcp-platform/platform.json → data.session_meta_table);
-- агент его не знает.
--
-- Совместимость: PostgreSQL 13.22 (фактическая база). Ранее здесь стояло
-- «Совместимость: Greenplum 6.5» — объявление ложное.
--
-- Этот файл живёт ТОЛЬКО для psql-ручного применения.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_session_meta_test (
    replica_id       TEXT NOT NULL,
    session_key      TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_consolidated INT NOT NULL DEFAULT 0,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_digest    TEXT,
    missing_cycles   INT NOT NULL DEFAULT 0,
    message_count    INT,
    synced_at        TIMESTAMPTZ,
    PRIMARY KEY (replica_id, session_key)
);

-- Ключ распределения — составной первичный ключ, как в боевом файле.
-- Ограждён проверкой pg_dist_partition: файлы из sql/ применяются и к
-- PostgreSQL 13.22, где SET DISTRIBUTED BY не существует.
DO $distribution$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'pg_dist_partition'
          AND n.nspname = 'pg_catalog'
    ) THEN
        EXECUTE 'ALTER TABLE public.agent_session_meta_test
                 SET DISTRIBUTED BY (replica_id, session_key)';
    END IF;
END
$distribution$;

COMMENT ON TABLE  public.agent_session_meta_test IS 'Test-профиль: холодное зеркало метаданных сессий. Структурный клон public.agent_session_meta.';
COMMENT ON COLUMN public.agent_session_meta_test.replica_id       IS 'Реплика-владелец строки; часть первичного ключа.';
COMMENT ON COLUMN public.agent_session_meta_test.session_key      IS 'Ключ сессии. Уникален только в пределах реплики.';
COMMENT ON COLUMN public.agent_session_meta_test.created_at       IS 'Время создания сессии по upstream.';
COMMENT ON COLUMN public.agent_session_meta_test.updated_at       IS 'Время последнего изменения по upstream. Поле разрешения конфликта, не признак изменения.';
COMMENT ON COLUMN public.agent_session_meta_test.last_consolidated IS 'Последний seq, до которого сообщения консолидированы.';
COMMENT ON COLUMN public.agent_session_meta_test.metadata         IS 'Произвольные метаданные сессии (user_id, channel, ...).';
COMMENT ON COLUMN public.agent_session_meta_test.source_digest   IS 'SHA-256 файла сессии (JSONL) на момент зеркалирования. Признак изменения.';
COMMENT ON COLUMN public.agent_session_meta_test.missing_cycles  IS 'Сколько циклов подряд сессия отсутствует в списке upstream. Удаление — по порогу, не по первому пропуску.';
COMMENT ON COLUMN public.agent_session_meta_test.message_count   IS 'Сколько строк сообщений зеркала принадлежит сессии.';
COMMENT ON COLUMN public.agent_session_meta_test.synced_at       IS 'Момент последней записи строки зеркала.';
