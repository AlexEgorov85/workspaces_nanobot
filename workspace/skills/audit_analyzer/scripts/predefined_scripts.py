"""
Реестр предопределённых SQL-скриптов (SCRIPTS_REGISTRY).

Каждый скрипт — ScriptDefinition с SQL-шаблоном, описанием
и типизированными параметрами. Модели (ParamDefinition, ScriptDefinition)
и построитель SQL (DynamicQueryBuilder) живут в scripts_registry.py.

Скрипты выполняются через:
    audit_analyze --mode predefined --script <имя> --params '{...}'

Доступные скрипты:
    analytics_by_year_month  — Аналитика проверок по годам и месяцам
    violations_by_type       — Статистика нарушений по типам
    top_audited_objects      — Топ проверяемых объектов
    audit_effectiveness      — Оценка эффективности проверок
    audit_dynamics           — Динамика проверок по периодам
    audit_types_stats        — Статистика по типам проверок
"""

from typing import Dict

from scripts_registry import ParamDefinition, ScriptDefinition

SCRIPTS_REGISTRY: Dict[str, ScriptDefinition] = {
    "analytics_by_year_month": ScriptDefinition(
        name="analytics_by_year_month",
        description="Аналитика проверок по годам и месяцам",
        returns="год, месяц, количество проверок, название месяца",
        long_description="Показывает количество аудиторских проверок в разрезе годов и месяцев.",
        sql_template="""
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
        """,
        parameters={
            "year": ParamDefinition(
                type="number",
                required=False,
                description="Год проверки (например, 2024)",
            ),
        },
        max_rows_default=100,
    ),

    "violations_by_type": ScriptDefinition(
        name="violations_by_type",
        description="Статистика нарушений по типам и категориям",
        returns="код нарушения, количество нарушений, количество проверок",
        long_description="Группировка нарушений по violation_code с количеством и количеством затронутых проверок.",
        sql_template="""
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
        """,
        parameters={
            "date_from": ParamDefinition(
                type="date",
                required=False,
                description="Начальная дата (включительно)",
            ),
            "violation_code": ParamDefinition(
                type="like",
                required=False,
                description="Код/тип нарушения (например, 'финансовые')",
                validation={
                    "vector_source": "violations",
                    "vector_field": "violation_code",
                    "vector_min_score": 0.7,
                    "vector_top_k": 3,
                },
            ),
        },
        max_rows_default=100,
    ),

    "top_audited_objects": ScriptDefinition(
        name="top_audited_objects",
        description="Топ проверяемых объектов по количеству проверок",
        returns="объект, количество проверок, лет покрыто, дата последней проверки",
        long_description="Рейтинг auditee_entity по количеству проведённых проверок.",
        sql_template="""
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
        """,
        parameters={
            "auditee_entity": ParamDefinition(
                type="like",
                required=False,
                description="Название проверяемого объекта",
                validation={
                    "vector_source": "audits",
                    "vector_field": "auditee_entity",
                    "vector_min_score": 0.7,
                    "vector_top_k": 3,
                },
            ),
            "date_from": ParamDefinition(
                type="date",
                required=False,
                description="Начальная дата (включительно)",
            ),
            "limit": ParamDefinition(
                type="limit",
                required=False,
                default=10,
                description="Количество записей в топе",
            ),
        },
        max_rows_default=10,
    ),

    "audit_effectiveness": ScriptDefinition(
        name="audit_effectiveness",
        description="Оценка эффективности проверок",
        returns="ID проверки, название, дата, количество нарушений, уровень серьёзности",
        long_description="Оценка каждой проверки по количеству выявленных нарушений с классификацией severity_level.",
        sql_template="""
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
        """,
        parameters={
            "date_from": ParamDefinition(
                type="date",
                required=False,
                description="Начальная дата (включительно)",
            ),
            "date_to": ParamDefinition(
                type="date",
                required=False,
                description="Конечная дата (включительно)",
            ),
            "min_violations": ParamDefinition(
                type="number",
                required=False,
                description="Минимальное количество нарушений для фильтрации",
            ),
        },
        max_rows_default=100,
    ),

    "audit_dynamics": ScriptDefinition(
        name="audit_dynamics",
        description="Динамика проведения проверок по периодам",
        returns="период, количество проверок, уникальных объектов, нарушений",
        long_description="Динамика проверок с группировкой по месяцам, кварталам или неделям.",
        sql_template="""
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
        """,
        parameters={
            "period": ParamDefinition(
                type="enum",
                required=False,
                default="month",
                description="Период группировки: month, quarter, week",
            ),
            "date_from": ParamDefinition(
                type="date",
                required=False,
                description="Начальная дата (включительно)",
            ),
        },
        max_rows_default=100,
    ),

    "audit_types_stats": ScriptDefinition(
        name="audit_types_stats",
        description="Статистика по типам проведения проверок",
        returns="тип проверки, количество, объектов, нарушений, средняя серьёзность",
        long_description="Статистика в разрезе audit_type: количество проверок, объектов, нарушений, средняя severity.",
        sql_template="""
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
        """,
        parameters={
            "audit_type": ParamDefinition(
                type="like",
                required=False,
                description="Тип проверки (например, 'финансовый')",
                validation={
                    "vector_source": "audits",
                    "vector_field": "audit_type",
                    "vector_min_score": 0.7,
                    "vector_top_k": 3,
                },
            ),
            "date_from": ParamDefinition(
                type="date",
                required=False,
                description="Начальная дата (включительно)",
            ),
        },
        max_rows_default=100,
    ),
}
