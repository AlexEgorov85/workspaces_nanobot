-- =====================================================================
-- predefined_scripts_migration.sql — перенос реестра SQL-скриптов v1.4 → v2.0
-- Сгенерировано автоматически: 6 скриптов
-- Совместимо с Greenplum 6.5 / PostgreSQL 9.4+ (без ON CONFLICT)
-- Требует, чтобы public.agent_predefined_scripts уже была создана.
-- Применение:  psql -d <db> -f predefined_scripts_migration.sql
-- =====================================================================

-- удалить старые записи с этими именами (если есть)
DELETE FROM public.agent_predefined_scripts WHERE name IN ('analytics_by_year_month', 'audit_dynamics', 'audit_effectiveness', 'audit_types_stats', 'top_audited_objects', 'violations_by_type');

INSERT INTO public.agent_predefined_scripts (
    name,
    description,
    sql_template,
    parameters,
    max_rows_default,
    returns,
    long_description
) VALUES

(
    'analytics_by_year_month',
    'Аналитика проверок по годам и месяцам',
    $sql$
            SELECT
                EXTRACT(YEAR FROM actual_date) AS audit_year,
                EXTRACT(MONTH FROM actual_date) AS audit_month,
                COUNT(*) AS audit_count,
                TO_CHAR(actual_date, 'Month') AS month_name
            FROM oarb.audits
            WHERE actual_date IS NOT NULL
            {% if year %} AND EXTRACT(YEAR FROM actual_date) = :year {% endif %}
            GROUP BY audit_year, audit_month, TO_CHAR(actual_date, 'Month')
            ORDER BY audit_year DESC, audit_month
        
$sql$,
    $json${
    "year": {
        "description": "Год проверки (например, 2024)",
        "required": false,
        "type": "number"
    }
}$json$::jsonb,
    100,
    'год, месяц, количество проверок, название месяца',
    'Показывает количество аудиторских проверок в разрезе годов и месяцев.'
),

(
    'violations_by_type',
    'Статистика нарушений по типам и категориям',
    $sql$
            SELECT
                v.violation_code,
                COUNT(*) AS violation_count,
                COUNT(DISTINCT v.audit_id) AS affected_audits
            FROM oarb.violations v
            JOIN oarb.audits a ON v.audit_id = a.id
            WHERE a.actual_date IS NOT NULL
            {% if date_from %} AND a.actual_date >= :date_from {% endif %}
            {% if violation_code %} AND v.violation_code ILIKE :violation_code {% endif %}
            GROUP BY v.violation_code
            ORDER BY violation_count DESC
        
$sql$,
    $json${
    "date_from": {
        "description": "Начальная дата (включительно)",
        "required": false,
        "type": "date"
    },
    "violation_code": {
        "description": "Код/тип нарушения (например, 'финансовые')",
        "required": false,
        "type": "like",
        "validation": {
            "vector_field": "violation_code",
            "vector_min_score": 0.7,
            "vector_source": "violations",
            "vector_top_k": 3
        }
    }
}$json$::jsonb,
    100,
    'код нарушения, количество нарушений, количество проверок',
    'Группировка нарушений по violation_code с количеством и количеством затронутых проверок.'
),

(
    'top_audited_objects',
    'Топ проверяемых объектов по количеству проверок',
    $sql$
            SELECT
                a.auditee_entity,
                COUNT(*) AS audit_count,
                COUNT(DISTINCT EXTRACT(YEAR FROM a.actual_date)) AS years_covered,
                MAX(a.actual_date) AS last_audit_date
            FROM oarb.audits a
            WHERE a.actual_date IS NOT NULL
              AND a.auditee_entity IS NOT NULL
            {% if auditee_entity %} AND a.auditee_entity ILIKE :auditee_entity {% endif %}
            {% if date_from %} AND a.actual_date >= :date_from {% endif %}
            GROUP BY a.auditee_entity
            ORDER BY audit_count DESC
        
$sql$,
    $json${
    "auditee_entity": {
        "description": "Название проверяемого объекта",
        "required": false,
        "type": "like",
        "validation": {
            "vector_field": "auditee_entity",
            "vector_min_score": 0.7,
            "vector_source": "audits",
            "vector_top_k": 3
        }
    },
    "date_from": {
        "description": "Начальная дата (включительно)",
        "required": false,
        "type": "date"
    },
    "limit": {
        "default": 10,
        "description": "Количество записей в топе",
        "required": false,
        "type": "limit"
    }
}$json$::jsonb,
    10,
    'объект, количество проверок, лет покрыто, дата последней проверки',
    'Рейтинг auditee_entity по количеству проведённых проверок.'
),

(
    'audit_effectiveness',
    'Оценка эффективности проверок',
    $sql$
            SELECT
                a.id AS audit_id,
                a.title AS audit_title,
                a.actual_date,
                COUNT(v.id) AS violations_count,
                COUNT(DISTINCT v.violation_code) AS violation_types_count,
                CASE
                    WHEN COUNT(v.id) = 0 THEN 'Без нарушений'
                    WHEN COUNT(v.id) <= 3 THEN 'Допустимые нарушения'
                    WHEN COUNT(v.id) <= 10 THEN 'Серьезные нарушения'
                    ELSE 'Критические нарушения'
                END AS severity_level
            FROM oarb.audits a
            LEFT JOIN oarb.violations v ON a.id = v.audit_id
            WHERE a.actual_date IS NOT NULL
            {% if date_from %} AND a.actual_date >= :date_from {% endif %}
            {% if date_to %} AND a.actual_date <= :date_to {% endif %}
            GROUP BY a.id, a.title, a.actual_date
            {% if min_violations %} HAVING COUNT(v.id) >= :min_violations {% endif %}
            ORDER BY violations_count DESC, a.actual_date DESC
        
$sql$,
    $json${
    "date_from": {
        "description": "Начальная дата (включительно)",
        "required": false,
        "type": "date"
    },
    "date_to": {
        "description": "Конечная дата (включительно)",
        "required": false,
        "type": "date"
    },
    "min_violations": {
        "description": "Минимальное количество нарушений для фильтрации",
        "required": false,
        "type": "number"
    }
}$json$::jsonb,
    100,
    'ID проверки, название, дата, количество нарушений, уровень серьёзности',
    'Оценка каждой проверки по количеству выявленных нарушений с классификацией severity_level.'
),

(
    'audit_dynamics',
    'Динамика проведения проверок по периодам',
    $sql$
            SELECT
                CASE
                    WHEN :period = 'quarter' THEN
                        EXTRACT(YEAR FROM actual_date) || '-Q' || EXTRACT(QUARTER FROM actual_date)
                    WHEN :period = 'week' THEN
                        EXTRACT(YEAR FROM actual_date) || '-W' || EXTRACT(WEEK FROM actual_date)
                    ELSE
                        EXTRACT(YEAR FROM actual_date) || '-' || LPAD(EXTRACT(MONTH FROM actual_date)::TEXT, 2, '0')
                END AS period,
                COUNT(*) AS audit_count,
                COUNT(DISTINCT a.auditee_entity) AS unique_objects,
                COUNT(v.id) AS total_violations
            FROM oarb.audits a
            LEFT JOIN oarb.violations v ON a.id = v.audit_id
            WHERE a.actual_date IS NOT NULL
            {% if date_from %} AND a.actual_date >= :date_from {% endif %}
            GROUP BY period
            ORDER BY period DESC
        
$sql$,
    $json${
    "date_from": {
        "description": "Начальная дата (включительно)",
        "required": false,
        "type": "date"
    },
    "period": {
        "default": "month",
        "description": "Период группировки: month, quarter, week",
        "required": false,
        "type": "enum"
    }
}$json$::jsonb,
    100,
    'период, количество проверок, уникальных объектов, нарушений',
    'Динамика проверок с группировкой по месяцам, кварталам или неделям.'
),

(
    'audit_types_stats',
    'Статистика по типам проведения проверок',
    $sql$
            SELECT
                a.audit_type,
                COUNT(*) AS audit_count,
                COUNT(DISTINCT a.auditee_entity) AS unique_objects,
                COUNT(v.id) AS total_violations,
                AVG(v.severity) AS avg_severity,
                MAX(a.actual_date) AS last_audit_date
            FROM oarb.audits a
            LEFT JOIN oarb.violations v ON a.id = v.audit_id
            WHERE a.actual_date IS NOT NULL
              AND a.audit_type IS NOT NULL
            {% if audit_type %} AND a.audit_type ILIKE :audit_type {% endif %}
            {% if date_from %} AND a.actual_date >= :date_from {% endif %}
            GROUP BY a.audit_type
            ORDER BY audit_count DESC
        
$sql$,
    $json${
    "audit_type": {
        "description": "Тип проверки (например, 'финансовый')",
        "required": false,
        "type": "like",
        "validation": {
            "vector_field": "audit_type",
            "vector_min_score": 0.7,
            "vector_source": "audits",
            "vector_top_k": 3
        }
    },
    "date_from": {
        "description": "Начальная дата (включительно)",
        "required": false,
        "type": "date"
    }
}$json$::jsonb,
    100,
    'тип проверки, количество, объектов, нарушений, средняя серьёзность',
    'Статистика в разрезе audit_type: количество проверок, объектов, нарушений, средняя severity.'
)
;
