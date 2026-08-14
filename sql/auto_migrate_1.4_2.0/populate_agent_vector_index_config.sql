-- =====================================================================
-- populate_agent_vector_index_config.sql
-- Заполнение public.agent_vector_index_config начальными данными v2.0
-- (аналогично тому, что в БД примере).
--
-- Совместимо с Greenplum 6.5 / PostgreSQL 9.4+ (без ON CONFLICT).
-- Применение:
--   psql -d <db> -f populate_agent_vector_index_config.sql
-- =====================================================================

-- 1) удалить существующие с теми же именами (idempotent)
DELETE FROM public.agent_vector_index_config
WHERE index_name IN ('audits_index', 'violations_index', 'audit_reports_index');

-- 2) вставить
INSERT INTO public.agent_vector_index_config (
    index_name, source_table, src_table, pk_column,
    content_cols, embedding_cols, track_column, enabled,
    created_at, updated_at
) VALUES
(
    'audits_index',
    'audits',
    'oarb.audits',
    'id',
    ARRAY['title', 'audit_type', 'auditee_entity', 'status']::TEXT[],
    '["title", "audit_type", "auditee_entity", "status"]'::JSONB,
    'updated_at',
    TRUE,
    NOW(),
    NOW()
),
(
    'violations_index',
    'violations',
    'oarb.violations',
    'id',
    ARRAY['description', 'recommendation', 'violation_code', 'severity']::TEXT[],
    '[{"chunk": true, "column": "description", "chunk_size": 500, "chunk_overlap": 80}, "violation_code"]'::JSONB,
    'updated_at',
    TRUE,
    NOW(),
    NOW()
),
(
    'audit_reports_index',
    'audit_reports',
    'oarb.audit_reports',
    'id',
    ARRAY['full_text', 'title', 'report_number', 'report_date']::TEXT[],
    '[{"chunk": true, "column": "full_text", "chunk_size": 500, "chunk_overlap": 80}, "title"]'::JSONB,
    'updated_at',
    TRUE,
    NOW(),
    NOW()
);
