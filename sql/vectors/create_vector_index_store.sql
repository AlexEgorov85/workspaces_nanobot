-- ============================================================================
-- DEPRECATED — этот DDL больше НЕ применяется.
--
-- После change ``remove-vector-index-store`` (OpenSpec
-- openspec/changes/archive/<YYYY-MM-DD-remove-vector-index-store>/) persisted
-- FAISS-кеш в ``public.agent_vector_index_store`` удалён. FAISS-индекс
-- собирается в памяти из DuckDB-снапшота ``<storage_table>`` на лету
-- (``provider.preload_indexes`` при старте gateway). См. migration
-- V003__drop_vector_index_store.sql.
--
-- Файл сохранён как исторический артефакт (DDL, который раньше создавал
-- эту таблицу) для архивной документации. НЕ запускайте его на новых
-- инстансах; для уже существующих таблиц применяется V003 (DROP TABLE).
--
-- Оригинальный документ ниже — для истории.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_vector_index_store (
    source       TEXT NOT NULL,
    index_binary BYTEA NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::JSONB,
    dimension    INTEGER NOT NULL DEFAULT 1024,
    vector_count INTEGER NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source)
)
DISTRIBUTED REPLICATED;

COMMENT ON TABLE  public.agent_vector_index_store IS 'Сериализованные FAISS-индексы (binary blob + metadata). Одна строка на source.';
COMMENT ON COLUMN public.agent_vector_index_store.source       IS 'PK — имя индекса (= index_name из agent_vector_index_config).';
COMMENT ON COLUMN public.agent_vector_index_store.index_binary IS 'FAISS-индекс, сериализованный через faiss.serialize_index.';
COMMENT ON COLUMN public.agent_vector_index_store.metadata     IS 'JSONB: связь FAISS-индекса с исходной таблицей (pk_value → chunk_index/row_id).';
COMMENT ON COLUMN public.agent_vector_index_store.dimension    IS 'Размерность векторов (для валидации при десериализации).';
COMMENT ON COLUMN public.agent_vector_index_store.vector_count IS 'Количество векторов в индексе.';
COMMENT ON COLUMN public.agent_vector_index_store.updated_at   IS 'Время последней пересборки индекса.';