-- ============================================================================
--  Seed: дефолтные векторные индексы audit_analyzer
-- ============================================================================
--  Заполняет public.agent_vector_index_config тремя индексами на основе реальной
--  схемы источников:
--
--    audits_index       → oarb.audits         (composite из 4 колонок)
--    violations_index   → oarb.violations     (description с чанкованием)
--    audit_reports_index→ oarb.audit_reports  (full_text с чанкованием)
--
--  Идемпотентно: ON CONFLICT DO UPDATE — повторный запуск обновляет настройки
--  существующих индексов и добавляет недостающие.
--
--  Применение:
--    psql "$DATABASE_URL" -f sql/audit_analyzer/seed_default_indexes.sql
--
--  После: python tools/build_vectors.py --full-rebuild
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────
--  audits_index — композитный эмбеддинг аудитов
--  В oarb.audits нет полнотекстовых колонок, поэтому собираем search_text
--  из title + audit_type + auditee_entity + status с метками колонок.
-- ─────────────────────────────────────────────────────────────────────
INSERT INTO public.agent_vector_index_config
    (index_name, source_table, src_table, pk_column,
     content_cols, embedding_cols, track_column, enabled)
VALUES (
    'audits_index',
    'audits',
    'oarb.audits',
    'id',
    ARRAY['title', 'audit_type', 'auditee_entity', 'status']::TEXT[],
    '["title", "audit_type", "auditee_entity", "status"]'::JSONB,
    'updated_at',
    true
)
ON CONFLICT (index_name) DO UPDATE SET
    source_table   = EXCLUDED.source_table,
    src_table      = EXCLUDED.src_table,
    pk_column      = EXCLUDED.pk_column,
    content_cols   = EXCLUDED.content_cols,
    embedding_cols = EXCLUDED.embedding_cols,
    track_column   = EXCLUDED.track_column,
    enabled        = EXCLUDED.enabled,
    updated_at     = NOW();

-- ─────────────────────────────────────────────────────────────────────
--  violations_index — нарушения с чанкованием
--  description может быть длинным → разбиваем на чанки по 500 символов
--  с перекрытием 80. content = полное описание + рекомендация.
-- ─────────────────────────────────────────────────────────────────────
INSERT INTO public.agent_vector_index_config
    (index_name, source_table, src_table, pk_column,
     content_cols, embedding_cols, track_column, enabled)
VALUES (
    'violations_index',
    'violations',
    'oarb.violations',
    'id',
    ARRAY['description', 'recommendation', 'violation_code', 'severity']::TEXT[],
    '[
        {"column": "description", "chunk": true, "chunk_size": 500, "chunk_overlap": 80},
        "violation_code"
    ]'::JSONB,
    'updated_at',
    true
)
ON CONFLICT (index_name) DO UPDATE SET
    source_table   = EXCLUDED.source_table,
    src_table      = EXCLUDED.src_table,
    pk_column      = EXCLUDED.pk_column,
    content_cols   = EXCLUDED.content_cols,
    embedding_cols = EXCLUDED.embedding_cols,
    track_column   = EXCLUDED.track_column,
    enabled        = EXCLUDED.enabled,
    updated_at     = NOW();

-- ─────────────────────────────────────────────────────────────────────
--  audit_reports_index — отчёты с чанкованием
--  full_text — основной текст отчёта, может быть очень длинным.
--  content = full_text + title + report_number.
-- ─────────────────────────────────────────────────────────────────────
INSERT INTO public.agent_vector_index_config
    (index_name, source_table, src_table, pk_column,
     content_cols, embedding_cols, track_column, enabled)
VALUES (
    'audit_reports_index',
    'audit_reports',
    'oarb.audit_reports',
    'id',
    ARRAY['full_text', 'title', 'report_number', 'report_date']::TEXT[],
    '[
        {"column": "full_text", "chunk": true, "chunk_size": 500, "chunk_overlap": 80},
        "title"
    ]'::JSONB,
    'updated_at',
    true
)
ON CONFLICT (index_name) DO UPDATE SET
    source_table   = EXCLUDED.source_table,
    src_table      = EXCLUDED.src_table,
    pk_column      = EXCLUDED.pk_column,
    content_cols   = EXCLUDED.content_cols,
    embedding_cols = EXCLUDED.embedding_cols,
    track_column   = EXCLUDED.track_column,
    enabled        = EXCLUDED.enabled,
    updated_at     = NOW();

-- Проверка: что вставлено
SELECT index_name, source_table, enabled, jsonb_array_length(embedding_cols) AS emb_cols_count
FROM public.agent_vector_index_config
ORDER BY index_name;