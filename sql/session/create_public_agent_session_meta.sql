-- ============================================================================
-- public.agent_session_meta — метаданные сессий nanobot (холодное зеркало)
--
-- Зеркало горячего хранилища: upstream SessionManager (JSONL) — источник
-- истины и единственный писатель в горячем контуре, эта таблица — только
-- холодная копия для multi-instance, наблюдаемости и аварийного восстановления.
-- Пишет её фоновая синхронизация, не hot path: см.
-- openspec/specs/storage/session-hybridization/spec.md.
--
-- Ключ — (replica_id, session_key), а не session_key. Одна и та же сессия на
-- двух репликах — это две строки, а не одна под общим last-write-wins: иначе
-- реплики затирают друг друга, а очистка одной реплики стирает сессии другой.
--
-- Совместимость: PostgreSQL 13.22 (фактическая база). Ранее здесь стояло
-- «Совместимость: Greenplum 6.5» и `DISTRIBUTED BY (session_key)` — объявление
-- было ложным (pg_dist_partition на сервере отсутствует), а локальность,
-- которую оно давало в Greenplum, заменена индексом
-- agent_session_meta_replica_updated_at_idx (см. V011).
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_session_meta (
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

COMMENT ON TABLE  public.agent_session_meta IS 'Холодное зеркало метаданных сессий nanobot. Источник истины — upstream JSONL-стор SessionManager. Пишет фоновая синхронизация, не hot path.';
COMMENT ON COLUMN public.agent_session_meta.replica_id       IS 'Реплика-владелец строки; часть первичного ключа. Пишет операция mirror_session платформы, не вызывающая сторона.';
COMMENT ON COLUMN public.agent_session_meta.session_key      IS 'Ключ сессии (например, "telegram:12345"). Уникален только в пределах реплики.';
COMMENT ON COLUMN public.agent_session_meta.created_at       IS 'Время создания сессии по upstream.';
COMMENT ON COLUMN public.agent_session_meta.updated_at       IS 'Время последнего изменения по upstream. Поле разрешения конфликта, НЕ признак изменения: изменение определяется по source_digest.';
COMMENT ON COLUMN public.agent_session_meta.last_consolidated IS 'Последний seq, до которого сообщения консолидированы.';
COMMENT ON COLUMN public.agent_session_meta.metadata         IS 'Произвольные метаданные сессии (user_id, channel, ...).';
COMMENT ON COLUMN public.agent_session_meta.source_digest   IS 'SHA-256 файла сессии (JSONL) на момент зеркалирования. Признак изменения: upstream не поднимает updated_at при правке metadata, поэтому по updated_at такие правки не видны.';
COMMENT ON COLUMN public.agent_session_meta.missing_cycles  IS 'Сколько циклов подряд сессия была в зеркале, но отсутствовала в списке upstream. Удаление — по достижении порога, а не по первому пропуску.';
COMMENT ON COLUMN public.agent_session_meta.message_count   IS 'Сколько строк сообщений зеркала принадлежит сессии. Расхождение с фактическим числом строк означает разорванную запись и требует принудительной синхронизации.';
COMMENT ON COLUMN public.agent_session_meta.synced_at       IS 'Момент последней записи строки зеркала: отличает «файл давно не менялся» от «зеркалом перестали писать».';
