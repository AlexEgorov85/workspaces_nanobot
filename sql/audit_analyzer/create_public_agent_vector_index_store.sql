-- ============================================================================
-- public.agent_vector_index_store — сериализованные FAISS-индексы (binary blob)
-- Одна строка на source (= index_name из agent_vector_index_config).
-- Строится из oarb.audit_vectors инструментами build_vectors.py:
-- все вектора одного source собираются в faiss.IndexFlatIP/IVFFlat,
-- сериализуются в BYTEA. Загружается lib.services.cache_provider_impl
-- при search_vector.
--
-- Распределение: DISTRIBUTED REPLICATED — каждый сегмент GP получает полную
-- копию. Строк мало (по одной на индекс), экономит JOIN с
-- agent_vector_index_config.
--
-- Ограничения GP 6.5:
--   * BYTEA на сегмент: до ~1GB (для индексов <1M×1024 float32 = ~400MB OK).
--   * Для очень больших индексов (>1M векторов) — нужно партиционирование
--     или внешнее хранилище.
-- Совместимость: Greenplum 6.5.
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
COMMENT ON COLUMN public.agent_vector_index_store.source       IS 'PK — имя индекса (= index_name из agent_vector_index_config, = source в audit_vectors).';
COMMENT ON COLUMN public.agent_vector_index_store.index_binary IS 'FAISS-индекс, сериализованный через faiss.serialize_index.';
COMMENT ON COLUMN public.agent_vector_index_store.metadata     IS 'JSONB: связь FAISS-индекса с audit_vectors (pk_value → chunk_index/row_id).';
COMMENT ON COLUMN public.agent_vector_index_store.dimension    IS 'Размерность векторов (для валидации при десериализации).';
COMMENT ON COLUMN public.agent_vector_index_store.vector_count IS 'Количество векторов в индексе.';
COMMENT ON COLUMN public.agent_vector_index_store.updated_at   IS 'Время последней пересборки индекса.';
