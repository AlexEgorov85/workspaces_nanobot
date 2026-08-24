-- ============================================================================
-- public.agent_vector_index_config — конфигурация сборки векторных индексов
-- Описывает ЧТО строить: имя индекса, исходная таблица, колонки для
-- content/embedding, колонка-маркер изменений.
-- Не содержит самих векторов — только метаданные сборки.
-- Используется: tools/build_vectors.py, lib/services/cache_provider_impl.py.
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
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (index_name)
)
DISTRIBUTED BY (index_name);

COMMENT ON TABLE  public.agent_vector_index_config IS 'Конфигурация сборки векторных индексов (generic).';
COMMENT ON COLUMN public.agent_vector_index_config.index_name     IS 'PK — уникальное имя индекса (= source в audit_vectors, = source в agent_vector_index_store).';
COMMENT ON COLUMN public.agent_vector_index_config.source_table   IS 'Короткое имя для колонки source в audit_vectors. Должно совпадать с index_name.';
COMMENT ON COLUMN public.agent_vector_index_config.src_table      IS 'Исходная таблица (schema.table).';
COMMENT ON COLUMN public.agent_vector_index_config.pk_column      IS 'Колонка первичного ключа в исходной таблице.';
COMMENT ON COLUMN public.agent_vector_index_config.content_cols   IS 'TEXT[] — колонки, попадающие в audit_vectors.content (для отображения).';
COMMENT ON COLUMN public.agent_vector_index_config.embedding_cols IS 'JSONB — какие колонки эмбеддингить и чанковать ли.';
COMMENT ON COLUMN public.agent_vector_index_config.track_column   IS 'Колонка исходной таблицы для инкрементальных обновлений (обычно updated_at).';
COMMENT ON COLUMN public.agent_vector_index_config.enabled        IS 'False — пропустить индекс при сборке.';
COMMENT ON COLUMN public.agent_vector_index_config.created_at     IS 'Время создания записи конфига.';
COMMENT ON COLUMN public.agent_vector_index_config.updated_at     IS 'Время последнего изменения конфига.';