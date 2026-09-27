-- ============================================================================
-- LEGACY — таблица больше НЕ читается кодом.
--
-- Единственный источник конфигурации векторных индексов —
-- ``project.json::gateway.vector.index.indexes`` (читается через
-- ``lib.services.cache_provider_impl.read_vector_index_config``). FAISS
-- собирается в памяти из DuckDB-снапшота
-- ``gateway.vector.index.storage_table`` на лету; persisted-кеша нет
-- (см. ``V003__drop_vector_index_store.sql``).
--
-- Таблица оставлена как legacy-артефакт SQL (и как цель для
-- ``V002__vector_chunk_params.sql``). НЕ применяйте этот DDL на новых
-- инстансах.
--
-- Оригинальный документ ниже — для истории.
-- ============================================================================
-- public.agent_vector_index_config — конфигурация сборки векторных индексов
-- Описывает ЧТО строить: имя индекса, исходная таблица, колонки для
-- content/embedding, колонка-маркер изменений.
-- Не содержит самих векторов — только метаданные сборки.
-- Generic infrastructure: применимо к любому домену с эмбеддингами.
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_vector_index_config (
    index_name      TEXT NOT NULL,
    source_table    TEXT NOT NULL,
    src_table       TEXT NOT NULL,
    pk_column       TEXT NOT NULL DEFAULT 'id',
    content_cols    TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    embedding_cols  JSONB NOT NULL DEFAULT '[]'::JSONB,
    track_column    TEXT NOT NULL DEFAULT 'updated_at',
    chunk_size      INTEGER NOT NULL DEFAULT 500,
    chunk_overlap   INTEGER NOT NULL DEFAULT 80,
    metric          TEXT NOT NULL DEFAULT 'cosine',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (index_name)
)
DISTRIBUTED BY (index_name);

COMMENT ON TABLE  public.agent_vector_index_config IS 'Конфигурация сборки векторных индексов (generic).';
COMMENT ON COLUMN public.agent_vector_index_config.index_name     IS 'PK — уникальное имя индекса (= source в audit_vectors).';
COMMENT ON COLUMN public.agent_vector_index_config.source_table   IS 'Короткое имя для колонки source в audit_vectors. Должно совпадать с index_name.';
COMMENT ON COLUMN public.agent_vector_index_config.src_table      IS 'Исходная таблица (schema.table).';
COMMENT ON COLUMN public.agent_vector_index_config.pk_column      IS 'Колонка первичного ключа в исходной таблице.';
COMMENT ON COLUMN public.agent_vector_index_config.content_cols   IS 'TEXT[] — колонки, попадающие в audit_vectors.content (для отображения).';
COMMENT ON COLUMN public.agent_vector_index_config.embedding_cols IS 'JSONB — какие колонки эмбеддингить и чанковать ли.';
COMMENT ON COLUMN public.agent_vector_index_config.chunk_size     IS 'Размер чанка в символах для этой сборки индекса (часть signature).';
COMMENT ON COLUMN public.agent_vector_index_config.chunk_overlap  IS 'Перекрытие чанков в символах для этой сборки индекса (часть signature).';
COMMENT ON COLUMN public.agent_vector_index_config.metric         IS 'Метрика FAISS: cosine (нормализация L2) | inner_product (без нормализации). Часть signature.';
COMMENT ON COLUMN public.agent_vector_index_config.track_column   IS 'Колонка исходной таблицы для инкрементальных обновлений (обычно updated_at).';
COMMENT ON COLUMN public.agent_vector_index_config.enabled        IS 'False — пропустить индекс при сборке.';
COMMENT ON COLUMN public.agent_vector_index_config.created_at     IS 'Время создания записи конфига.';
COMMENT ON COLUMN public.agent_vector_index_config.updated_at     IS 'Время последнего изменения конфига.';