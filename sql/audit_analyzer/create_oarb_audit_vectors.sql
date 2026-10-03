-- ============================================================================
-- oarb.audit_vectors — векторные эмбеддинги для семантического поиска
-- Распределены по source (имени индекса): все вектора одного индекса на одном
-- сегменте GP, фильтрация без broadcast.
-- embedding: REAL[] (float32 массив). GP 6.5 поддерживает с ограничением ~1GB
-- на массив на сегмент (для 1024-dim = ~1M векторов на сегмент — ОК).
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE TABLE IF NOT EXISTS oarb.audit_vectors (
    id             BIGSERIAL NOT NULL,
    source         TEXT NOT NULL DEFAULT 'audits_index',
    content        TEXT,
    search_text    TEXT,
    "table"        TEXT,
    pk_value       TEXT,
    chunk_index    INTEGER NOT NULL DEFAULT 0,
    chunk_count    INTEGER NOT NULL DEFAULT 1,
    row_data       JSONB,
    embedding      REAL[] NOT NULL,
    content_hash   TEXT,
    max_src_track  TEXT,
    synced_at      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
);

-- Распределение объявлено ограждённым шагом, а не в теле CREATE TABLE:
-- файлы из sql/ применяются и к PostgreSQL 13.22 (тестовый контур), где
-- клаузы DISTRIBUTED в синтаксисе нет. Проверка служебного каталога
-- pg_dist_partition отличает Greenplum, поэтому на PostgreSQL шаг — no-op.
DO $distribution$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'pg_dist_partition'
          AND n.nspname = 'pg_catalog'
    ) THEN
        EXECUTE 'ALTER TABLE oarb.audit_vectors
                 SET DISTRIBUTED BY (source)';
    END IF;
END
$distribution$;

COMMENT ON TABLE  oarb.audit_vectors IS 'Векторные эмбеддинги для семантического поиска audit_analyzer.';
COMMENT ON COLUMN oarb.audit_vectors.id            IS 'PK эмбеддинга (BIGSERIAL).';
COMMENT ON COLUMN oarb.audit_vectors.source        IS 'Имя индекса (= public.agent_vector_index_config.index_name).';
COMMENT ON COLUMN oarb.audit_vectors.content       IS 'Текст для отображения в результатах поиска.';
COMMENT ON COLUMN oarb.audit_vectors.search_text   IS 'Текст, по которому строился эмбеддинг.';
COMMENT ON COLUMN oarb.audit_vectors."table"       IS 'Короткое имя исходной таблицы.';
COMMENT ON COLUMN oarb.audit_vectors.pk_value      IS 'PK исходной строки (TEXT для совместимости с UUID/BIGINT/INTEGER).';
COMMENT ON COLUMN oarb.audit_vectors.chunk_index   IS 'Номер чанка (0-based).';
COMMENT ON COLUMN oarb.audit_vectors.chunk_count   IS 'Общее количество чанков для строки.';
COMMENT ON COLUMN oarb.audit_vectors.row_data      IS 'Полная строка исходных данных (JSONB).';
COMMENT ON COLUMN oarb.audit_vectors.embedding     IS 'Векторный эмбеддинг float32 (REAL[]).';
COMMENT ON COLUMN oarb.audit_vectors.content_hash  IS 'MD5 от search_text — для дедупликации при пересборке.';
COMMENT ON COLUMN oarb.audit_vectors.max_src_track IS 'MAX(track_column) в источнике на момент синхронизации.';
COMMENT ON COLUMN oarb.audit_vectors.synced_at     IS 'Время последней синхронизации.';
COMMENT ON COLUMN oarb.audit_vectors.created_at    IS 'Время создания записи.';
