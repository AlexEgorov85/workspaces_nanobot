-- V002__vector_chunk_params — параметры сборки в реестре индексов.
--
-- Добавляет в public.agent_vector_index_config колонки chunk_size / chunk_overlap /
-- metric, отсутствовавшие в исходном DDL. Идемпотентно для многократного применения.
--
-- Назначение: signature индекса (compute_index_signature) учитывает эти параметры;
-- без них смена chunk-настройки при пересборке не помечает индекс как STALE.

ALTER TABLE public.agent_vector_index_config
    ADD COLUMN IF NOT EXISTS chunk_size     INTEGER NOT NULL DEFAULT 500,
    ADD COLUMN IF NOT EXISTS chunk_overlap  INTEGER NOT NULL DEFAULT 80,
    ADD COLUMN IF NOT EXISTS metric         TEXT    NOT NULL DEFAULT 'cosine';

COMMENT ON COLUMN public.agent_vector_index_config.chunk_size     IS 'Размер чанка в символах для этой сборки индекса (часть signature).';
COMMENT ON COLUMN public.agent_vector_index_config.chunk_overlap  IS 'Перекрытие чанков в символах для этой сборки индекса (часть signature).';
COMMENT ON COLUMN public.agent_vector_index_config.metric         IS 'Метрика FAISS: cosine (нормализация L2) | inner_product (без нормализации). Часть signature.';