-- Таблица для хранения сериализованных FAISS-индексов.
-- Совместимо с PostgreSQL 13+ и Greenplum 6+.
-- На GP без DISTRIBUTED BY — hash-распределение по первой колонке.
CREATE TABLE IF NOT EXISTS oarb.vector_index_store (
    source       TEXT PRIMARY KEY,
    index_binary BYTEA NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    dimension    INT NOT NULL DEFAULT 1024,
    vector_count INT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Таблица для хранения векторных эмбеддингов.
-- Совместимо с PostgreSQL 13+ и Greenplum 6+.
-- Поддерживает чанкование: один документ → несколько векторов-чанков
--
-- Использование:
--  1. Выполнить этот SQL на GP:
--     psql -d <dbname> -f sql/create_audit_vectors_table_gp.sql
--  2. Собрать/обновить векторы из исходных таблиц:
--     python tools/build_vectors.py --full-rebuild

CREATE TABLE IF NOT EXISTS oarb.audit_vectors (
    id SERIAL,
    source TEXT NOT NULL DEFAULT 'audits_index',
    content TEXT,
    search_text TEXT,
    "table" TEXT,
    pk_value INTEGER,
    chunk_index INT NOT NULL DEFAULT 0,
    chunk_count INT NOT NULL DEFAULT 1,
    row_data JSONB,
    embedding REAL[] NOT NULL,
    content_hash TEXT,
    max_src_track TEXT,
    synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Индекс для быстрой фильтрации по source + pk_value
CREATE INDEX IF NOT EXISTS idx_audit_vectors_source ON oarb.audit_vectors (source);
CREATE INDEX IF NOT EXISTS idx_audit_vectors_pk ON oarb.audit_vectors (source, pk_value);

COMMENT ON TABLE oarb.audit_vectors IS 'Векторные эмбеддинги для семантического поиска audit_analyzer';

COMMENT ON COLUMN oarb.audit_vectors.source IS 'Имя индекса (соответствует имени .faiss файла, например audits_index)';
COMMENT ON COLUMN oarb.audit_vectors.content IS 'Текст для отображения в результатах поиска';
COMMENT ON COLUMN oarb.audit_vectors.search_text IS 'Текст по которому строился эмбеддинг (может отличаться от content)';
COMMENT ON COLUMN oarb.audit_vectors.embedding IS 'Векторный эмбеддинг float32 размерности 1024';
COMMENT ON COLUMN oarb.audit_vectors.row_data IS 'Полная строка исходных данных (JSONB)';
COMMENT ON COLUMN oarb.audit_vectors.chunk_index IS 'Номер чанка (0-based), если документ разбит на несколько векторов';
COMMENT ON COLUMN oarb.audit_vectors.chunk_count IS 'Общее количество чанков для данного документа';
COMMENT ON COLUMN oarb.audit_vectors.content_hash IS 'MD5 от search_text — для обнаружения изменений без переэмбеддинга';
COMMENT ON COLUMN oarb.audit_vectors.synced_at IS 'Время последней синхронизации с исходной таблицей';
