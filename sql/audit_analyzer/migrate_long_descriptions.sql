-- ============================================================================
-- Миграция: заполнить long_description для 5 predefined scripts.
--
-- До рефакторинга SKILL.md навыка audit_analyzer содержал секцию
-- «Подробное описание» с edge-case'ами и правилами «когда использовать /
-- когда НЕ использовать». Эти знания должны жить в runtime-БД, чтобы
-- SKILL.md мог быть очищен от hardcoded каталога и рендериться runtime'ом.
--
-- После миграции `agent_predefined_scripts.long_description` содержит
-- текст, который SkillCatalog подмешивает в rendered SKILL.md через
-- {{SCRIPTS_CATALOG}} (см. docs/ARCHITECTURE.md «Skill catalog rendering»).
--
-- Совместимость: Greenplum 6.5. Используется UPDATE ... CASE для идемпотентности.
-- ============================================================================

UPDATE public.agent_predefined_scripts
SET long_description = CASE name
    WHEN 'audit_status_summary' THEN
        'Агрегация проверок по статусу (Завершена / В работе / Запланирована). Использовать для вопросов «сколько аудитов по статусам», «распределение проверок по состоянию». НЕ использовать, когда нужны подробности по конкретным проверкам или фильтры по датам/типу → nl_sql_generate.'
    WHEN 'top_violations_by_type' THEN
        'Топ кодов нарушений. Использовать для «самые частые нарушения», «топ кодов». НЕ использовать, когда нужны нарушения по конкретному коду (WHERE violation_code = ...) или фильтры по severity/status → nl_sql_generate.'
    WHEN 'violations_by_period' THEN
        'Нарушения в заданный период. Использовать для «нарушения за 2024», «что выявлено в Q1». НЕ использовать, когда период не указан/неочевиден или нужны фильтры по severity/status → nl_sql_generate. Параметры: date_from, date_to — обязательные ISO-даты (YYYY-MM-DD).'
    WHEN 'audits_by_period' THEN
        'Аудиторские проверки в заданный период. Использовать для «проверки за 2024», «что проверяли в Q2». НЕ использовать, когда период не указан или нужны фильтры по status/audit_type → nl_sql_generate. Параметры: date_from, date_to — обязательные.'
    WHEN 'audit_effectiveness_summary' THEN
        'Сводка эффективности: проверки × нарушения × severity. Использовать для «какие проверки самые проблемные», «уровень серьёзности нарушений». НЕ использовать, когда нужны JOIN с другими таблицами или детализация по auditee_entity → nl_sql_generate.'
END,
updated_at = NOW()
WHERE name IN (
    'audit_status_summary', 'top_violations_by_type',
    'violations_by_period', 'audits_by_period',
    'audit_effectiveness_summary'
)
  AND (long_description IS NULL OR long_description = '');
