-- ============================================================================
-- Seed: дефолтные predefined scripts для audit_analyzer
-- ============================================================================
-- Заполняет public.agent_predefined_scripts пятью каноничными скриптами,
-- которые раньше жили в workspace/skills/audit_analyzer/predefined/scripts.py
-- (Python REGISTRY). После миграции DB становится source of truth,
-- Python REGISTRY остаётся как transitional fallback (deprecated).
--
-- Идемпотентно: ON CONFLICT (name) DO UPDATE — повторный запуск обновляет
-- существующие строки и не создаёт дубликатов.
--
-- Применение:
--   psql "$DATABASE_URL" -f sql/audit_analyzer/seed_predefined_scripts.sql
--
-- Зависимости (порядок применения):
--   1. sql/audit_analyzer/create_public_agent_predefined_scripts.sql
--   2. sql/audit_analyzer/seed_default_indexes.sql
--   3. sql/audit_analyzer/seed_predefined_scripts.sql   ← этот файл
--
-- ВАЖНО:
--   * Шестой script ``audit_types_stats`` НЕ сидится здесь — он уже живёт
--     в public.agent_predefined_scripts (см. sql/audit_analyzer/fix_audit_types_stats_avg.sql).
--   * SQL канонизирован с текущим Python REGISTRY (snapshot v2.5.x).
--     Изменения — через новые миграционные файлы, не правкой этого.
-- ========================================================================= */

INSERT INTO public.agent_predefined_scripts
    (name, description, returns, long_description, sql_template, parameters, max_rows_default)
VALUES
    (
        'audit_status_summary',
        'Сводка по статусам аудитов',
        'статус, количество аудитов',
        $ldesc1$Агрегация проверок по статусу (Завершена / В работе / Запланирована). Использовать для вопросов «сколько аудитов по статусам», «распределение проверок по состоянию». НЕ использовать, когда нужны подробности по конкретным проверкам или фильтры по датам/типу — свободный SQL.$ldesc1$,
        $sql1$SELECT status, COUNT(*) AS cnt
FROM oarb.audits
WHERE status IS NOT NULL
GROUP BY status
ORDER BY status$sql1$,
        '{}'::jsonb,
        100
    ),
    (
        'top_violations_by_type',
        'Топ кодов нарушений',
        'код нарушения, количество нарушений',
        $ldesc2$Топ кодов нарушений. Использовать для «самые частые нарушения», «топ кодов». НЕ использовать, когда нужны нарушения по конкретному коду или фильтры по severity/status — свободный SQL.$ldesc2$,
        $sql2$SELECT violation_code, COUNT(*) AS cnt
FROM oarb.violations
WHERE violation_code IS NOT NULL
GROUP BY violation_code
ORDER BY cnt DESC, violation_code$sql2$,
        '{}'::jsonb,
        100
    ),
    (
        'violations_by_period',
        'Нарушения за период',
        'id нарушения, код, описание, дата проверки',
        $ldesc3$Нарушения в заданный период. Использовать для «нарушения за 2024», «что выявлено в Q1». НЕ использовать, когда период не указан/неочевиден или нужны фильтры по severity/status — свободный SQL. Параметры: date_from, date_to — обязательные ISO-даты (YYYY-MM-DD).$ldesc3$,
        $sql3$SELECT v.id, v.violation_code, v.description, a.actual_date
FROM oarb.violations v
JOIN oarb.audits a ON a.id = v.audit_id
WHERE a.actual_date IS NOT NULL
AND a.actual_date >= :date_from
AND a.actual_date <= :date_to
ORDER BY a.actual_date DESC, v.id$sql3$,
        $params3${"date_from": {"type": "date", "required": true, "default": null, "description": "Начальная дата (включительно, YYYY-MM-DD)"}, "date_to": {"type": "date", "required": true, "default": null, "description": "Конечная дата (включительно, YYYY-MM-DD)"}}$params3$::jsonb,
        1000
    ),
    (
        'audits_by_period',
        'Аудиторские проверки за период',
        'id, название, тип, фактическая дата, статус проверки',
        $ldesc4$Аудиторские проверки в заданный период (по actual_date). Использовать для «проверки за 2024», «что проверяли в Q2». НЕ использовать, когда период не указан или нужны фильтры по status/audit_type — свободный SQL. Параметры: date_from, date_to — обязательные.$ldesc4$,
        $sql4$SELECT id, title, audit_type, actual_date, status
FROM oarb.audits
WHERE actual_date IS NOT NULL
AND actual_date >= :date_from
AND actual_date <= :date_to
ORDER BY actual_date DESC, id$sql4$,
        $params4${"date_from": {"type": "date", "required": true, "default": null, "description": "Начальная дата (включительно, YYYY-MM-DD)"}, "date_to": {"type": "date", "required": true, "default": null, "description": "Конечная дата (включительно, YYYY-MM-DD)"}}$params4$::jsonb,
        1000
    ),
    (
        'audit_effectiveness_summary',
        'Сводка эффективности: проверки × нарушения × severity',
        'id, название, дата проверки, число нарушений, уровень серьёзности',
        $ldesc5$Сводка эффективности: проверки × нарушения × severity. Использовать для «какие проверки самые проблемные», «уровень серьёзности нарушений». НЕ использовать, когда нужны JOIN с другими таблицами или детализация по auditee_entity — свободный SQL.

Если в результате одна проверка содержит >50% всех нарушений — это признак битого сида (нарушения не распределены по проверкам). В таком случае отчёт бесполезен; используйте свободный SQL с проверкой распределения по ``audit_id``.

Baseline-контракт: severity_level синтезируется по количеству нарушений (см. workspace/skills/audit_analyzer/tests/test_audit_analyzer_predefined.py::TestPredefinedAuditEffectivenessSummary::test_execute_real). Замена на реальный v.severity — отдельная задача (см. CHANGELOG).$ldesc5$,
        $sql5$SELECT
  a.id AS audit_id,
  a.title AS audit_title,
  a.actual_date,
  COUNT(v.id) AS violations_count,
  CASE
    WHEN COUNT(v.id) = 0 THEN 'Без нарушений'
    WHEN COUNT(v.id) <= 3 THEN 'Допустимые нарушения'
    WHEN COUNT(v.id) <= 10 THEN 'Серьёзные нарушения'
    ELSE 'Критические нарушения'
  END AS severity_level
FROM oarb.audits a
LEFT JOIN oarb.violations v ON a.id = v.audit_id
WHERE a.actual_date IS NOT NULL
GROUP BY a.id, a.title, a.actual_date
{% if min_violations %} HAVING COUNT(v.id) >= :min_violations {% endif %}
 ORDER BY violations_count DESC, a.actual_date DESC$sql5$,
        $params5${"min_violations": {"type": "number", "required": false, "default": null, "description": "Минимальное число нарушений для включения проверки в отчёт. Полезно, чтобы исключить «Без нарушений»-строки и сосредоточиться на проблемных проверках (рекомендуется 1+)."}}$params5$::jsonb,
        1000
    )
ON CONFLICT (name) DO UPDATE SET
    description      = EXCLUDED.description,
    returns          = EXCLUDED.returns,
    long_description = EXCLUDED.long_description,
    sql_template     = EXCLUDED.sql_template,
    parameters       = EXCLUDED.parameters,
    max_rows_default = EXCLUDED.max_rows_default,
    updated_at       = NOW();

-- Проверка: скриптов должно быть 5 (audit_types_stats — отдельный, см. fix_audit_types_stats_avg.sql).
SELECT COUNT(*) AS scripts_seeded
FROM public.agent_predefined_scripts
WHERE name IN (
    'audit_status_summary',
    'top_violations_by_type',
    'violations_by_period',
    'audits_by_period',
    'audit_effectiveness_summary'
);