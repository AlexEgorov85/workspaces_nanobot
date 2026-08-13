-- ============================================================================
--  Миграция: приведение имён таблиц агента к единому пространству.
--  Предыдущие рефакторинги ввели префикс agent_ для системных таблиц (сессии,
--  логи, бенчмарки). Здесь распространяем префикс на оставшиеся таблицы и
--  переносим векторные таблицы из схемы oarb в public.
--
--  PostgreSQL 13+
-- ============================================================================
--
--  Объекты:
--    public.predefined_scripts                → public.agent_predefined_scripts   ( rename, сохранение данных )
--    oarb.vector_index_config                → public.agent_vector_index_config   ( перенос + rename )
--    oarb.vector_index_store                 → public.agent_vector_index_store    ( перенос + rename )
--    public.conversation_messages            → public.agent_conversation_messages ( rename, сохранение данных )
--
--  Не затрагиваются: oarb.audit_vectors, oarb.audits, oarb.violations,
--  oarb.audit_reports, oarb.report_items (доменные таблицы навыка):
--  векторный источник и домен остаются в схеме oarb.
-- ============================================================================

BEGIN;

-- ---------- 1) predefined_scripts → agent_predefined_scripts ----------
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'predefined_scripts') THEN
    ALTER TABLE public.predefined_scripts RENAME TO agent_predefined_scripts;
  END IF;
END $$;

-- ---------- 2) conversation_messages → agent_conversation_messages ----------
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'conversation_messages') THEN
    ALTER TABLE public.conversation_messages RENAME TO agent_conversation_messages;
  END IF;
END $$;

-- ---------- 3) oarb.vector_index_config → public.agent_vector_index_config ----------
CREATE TABLE IF NOT EXISTS public.agent_vector_index_config (LIKE oarb.vector_index_config INCLUDING ALL);
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'agent_vector_index_config')
     AND EXISTS (SELECT 1 FROM information_schema.tables
                 WHERE table_schema = 'oarb' AND table_name = 'vector_index_config') THEN
    INSERT INTO public.agent_vector_index_config
        (index_name, source_table, src_table, pk_column, content_cols,
         embedding_cols, track_column, enabled, created_at, updated_at)
    SELECT index_name, source_table, src_table, pk_column, content_cols,
           embedding_cols, track_column, enabled, created_at, updated_at
      FROM oarb.vector_index_config
    ON CONFLICT (index_name) DO UPDATE SET
        source_table   = EXCLUDED.source_table,
        src_table      = EXCLUDED.src_table,
        pk_column      = EXCLUDED.pk_column,
        content_cols   = EXCLUDED.content_cols,
        embedding_cols = EXCLUDED.embedding_cols,
        track_column   = EXCLUDED.track_column,
        enabled        = EXCLUDED.enabled,
        updated_at     = EXCLUDED.updated_at;
    DROP TABLE IF EXISTS oarb.vector_index_config CASCADE;
  END IF;
END $$;

-- ---------- 4) oarb.vector_index_store → public.agent_vector_index_store ----------
CREATE TABLE IF NOT EXISTS public.agent_vector_index_store (LIKE oarb.vector_index_store INCLUDING ALL);
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'agent_vector_index_store')
     AND EXISTS (SELECT 1 FROM information_schema.tables
                 WHERE table_schema = 'oarb' AND table_name = 'vector_index_store') THEN
    INSERT INTO public.agent_vector_index_store
        (source, index_binary, metadata, dimension, vector_count, updated_at)
    SELECT source, index_binary, metadata, dimension, vector_count, updated_at
      FROM oarb.vector_index_store
    ON CONFLICT (source) DO UPDATE SET
        index_binary = EXCLUDED.index_binary,
        metadata     = EXCLUDED.metadata,
        dimension    = EXCLUDED.dimension,
        vector_count = EXCLUDED.vector_count,
        updated_at   = EXCLUDED.updated_at;
    DROP TABLE IF EXISTS oarb.vector_index_store CASCADE;
  END IF;
END $$;

COMMIT;