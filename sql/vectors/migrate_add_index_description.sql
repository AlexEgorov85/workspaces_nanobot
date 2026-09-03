-- ============================================================================
-- Миграция: добавить колонку description в agent_vector_index_config
-- и заполнить для 3 дефолтных индексов audit_analyzer.
--
-- После рефакторинга SKILL.md навыка audit_analyzer каталожная секция
-- «Vector indexes» рендерится runtime из auto-populated env-vars
-- SKILL_<NAME>_VECTORS и SKILL_<NAME>_VECTOR_DESCRIPTIONS. Описания берутся
-- из этой колонки.
--
-- Применяется через tools/migrate.py --apply. Идемпотентна (ADD COLUMN
-- IF NOT EXISTS + UPDATE только если description пустая).
--
-- Совместимость: Greenplum 6.5.
-- ============================================================================

ALTER TABLE public.agent_vector_index_config
    ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN public.agent_vector_index_config.description IS
    'Описание индекса для runtime-каталога (рендерится в SKILL.md через '
    'SkillCatalog). Когда использовать, когда НЕ использовать. Источник истины — БД, не SKILL.md.';

UPDATE public.agent_vector_index_config
SET description = CASE index_name
    WHEN 'audits_index' THEN
        'Поиск проверок по смыслу заголовка. Использовать для «проверки по пожарной безопасности», «бухгалтерские ревизии», «проверки в школах». НЕ использовать для агрегаций (COUNT/GROUP BY), фильтров по actual_date/status/audit_type → nl_sql_generate.'
    WHEN 'violations_index' THEN
        'Поиск нарушений по смыслу описания. Использовать для «нарушения, похожие на …», «нарушения про X». НЕ использовать для числовых агрегаций, фильтров по severity/status/deadline, точных кодов (WHERE violation_code = ...) → nl_sql_generate.'
    WHEN 'audit_reports_index' THEN
        'Поиск по отчётам целиком (title + full_text). Использовать для «отчёты с выводами о неэффективности», «отчёты про X». НЕ использовать для точных данных (числа, статусы) или JOIN с другими таблицами → nl_sql_generate.'
END,
updated_at = NOW()
WHERE index_name IN ('audits_index', 'violations_index', 'audit_reports_index')
  AND (description IS NULL OR description = '');
