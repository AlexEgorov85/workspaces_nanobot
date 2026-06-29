-- Таблица конфигурации векторных индексов (PostgreSQL 13+ / Greenplum 6+).
-- Альтернатива секции "vector_indexes" в config.json —
-- позволяет управлять настройками индексации через SQL.
--
-- Использование:
--  1. Выполнить SQL
--  2. Вставить настройки индексов:
--     INSERT INTO oarb.vector_index_config (...) VALUES (...);
--  3. Запустить сборку:
--     python build_vectors.py

CREATE TABLE IF NOT EXISTS oarb.vector_index_config (
    index_name      TEXT PRIMARY KEY,
    source_table    TEXT NOT NULL,
    src_table       TEXT NOT NULL,
    pk_column       TEXT NOT NULL DEFAULT 'id',
    content_cols    TEXT[] NOT NULL,
    embedding_cols  JSONB NOT NULL DEFAULT '[]'::jsonb,
    track_column    TEXT NOT NULL DEFAULT 'updated_at',
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE oarb.vector_index_config IS 'Конфигурация сборки векторных индексов';
COMMENT ON COLUMN oarb.vector_index_config.index_name IS 'Уникальное имя индекса (например audits_index)';
COMMENT ON COLUMN oarb.vector_index_config.source_table IS 'Короткое имя для колонки source в audit_vectors';
COMMENT ON COLUMN oarb.vector_index_config.src_table IS 'Исходная таблица (schema.table)';
COMMENT ON COLUMN oarb.vector_index_config.pk_column IS 'Колонка первичного ключа';
COMMENT ON COLUMN oarb.vector_index_config.content_cols IS 'Колонки для content (текст для отображения)';
COMMENT ON COLUMN oarb.vector_index_config.embedding_cols IS 'Колонки для эмбеддинга — массив объектов:
  ["колонка"] — простая колонка
  [{"column":"колонка","chunk":true,"chunk_size":500,"chunk_overlap":80}] — с чанкованием';
COMMENT ON COLUMN oarb.vector_index_config.track_column IS 'Колонка для ORDER BY при инкрементальной загрузке';
COMMENT ON COLUMN oarb.vector_index_config.enabled IS 'Индекс активен';
