-- ============================================================================
-- Реестр предопределённых SQL-скриптов (audit_analyzer)
-- Источник истины для скриптов: публичная таблица predefined_scripts
-- Структура JSONB parameters зеркалит dataclass ParamDefinition,
-- чтобы db_loader.py сводился к ParamDefinition(**pdef).
-- ============================================================================

DROP TABLE IF EXISTS predefined_scripts;

CREATE TABLE IF NOT EXISTS predefined_scripts (
    name                TEXT PRIMARY KEY,
    description         TEXT NOT NULL,
    sql_template        TEXT NOT NULL,
    parameters          JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_rows_default    INTEGER NOT NULL,
    returns             TEXT NOT NULL DEFAULT '',
    long_description    TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE predefined_scripts IS
    'Реестр предопределённых SQL-скриптов навыка audit_analyzer. '
    'Источник истины для режима --mode predefined. '
    'JSONB-колонка parameters повторяет структуру dataclass ParamDefinition: '
    '{param_name: {type, required, default, description, validation}}. '
    'Копируется в DuckDB-кэш через db_additional_tables (config project.json) '
    'и читается в run-time через db_loader.load_registry().';

COMMENT ON COLUMN predefined_scripts.name IS
    'PK — уникальное имя скрипта. Используется в CLI: --script <name>. '
    'Имя должно быть валидным идентификатором (^[a-z][a-z0-9_]*$) — иначе '
    'f-string в CacheProvider.query_sql может сломать SQL.';
COMMENT ON COLUMN predefined_scripts.description IS
    'Краткое описание для меню/подсказок (1-2 строки). Показывается в list_available().';
COMMENT ON COLUMN predefined_scripts.sql_template IS
    'SQL-шаблон с Jinja2-подобными блоками: '
    '{% if param %}...{% endif %} (условные блоки) и :param_name (плейсхолдеры). '
    'При выполнении DynamicQueryBuilder: рендерит условия, подставляет :param → %s, '
    'добавляет LIMIT :max_rows.';
COMMENT ON COLUMN predefined_scripts.parameters IS
    'JSONB: {param_name: ParamDefinition}. ParamDefinition имеет поля: '
    'type (like/exact/limit/number/date/enum/boolean), required, default, '
    'description, validation (опц., для vector-резолва).';
COMMENT ON COLUMN predefined_scripts.max_rows_default IS
    'Лимит строк по умолчанию (добавляется в LIMIT). '
    'Если передан --params с полем type=limit, перекрывает default.';
COMMENT ON COLUMN predefined_scripts.returns IS
    'Что возвращает скрипт (для документации и LLM-промпта в --mode sql).';
COMMENT ON COLUMN predefined_scripts.long_description IS
    'Подробное описание для LLM-промпта: что делает, когда использовать, edge cases.';
COMMENT ON COLUMN predefined_scripts.created_at IS
    'Время создания записи (при первой INSERT).';
COMMENT ON COLUMN predefined_scripts.updated_at IS
    'Время последнего изменения (обновляется триггером predefined_scripts_touch_updated_at).';

CREATE OR REPLACE FUNCTION predefined_scripts_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_predefined_scripts_updated_at ON predefined_scripts;
CREATE TRIGGER trg_predefined_scripts_updated_at
    BEFORE UPDATE ON predefined_scripts
    FOR EACH ROW EXECUTE FUNCTION predefined_scripts_touch_updated_at();

-- ============================================================================
-- INSERT: 6 скриптов
-- parameters — JSONB {param_name: {type, required, default, description, validation}}
-- ============================================================================

INSERT INTO predefined_scripts (name, description, sql_template, parameters, max_rows_default, returns, long_description) VALUES
('analytics_by_year_month',
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
        "type": "number",
        "required": false,
        "description": "Год проверки (например, 2024)"
    }
}$json$::jsonb,
 100,
 'год, месяц, количество проверок, название месяца',
 'Показывает количество аудиторских проверок в разрезе годов и месяцев.'),

('violations_by_type',
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
        "type": "date",
        "required": false,
        "description": "Начальная дата (включительно)"
    },
    "violation_code": {
        "type": "like",
        "required": false,
        "description": "Код/тип нарушения (например, ''финансовые'')",
        "validation": {
            "vector_source": "violations",
            "vector_field": "violation_code",
            "vector_min_score": 0.7,
            "vector_top_k": 3
        }
    }
}$json$::jsonb,
 100,
 'код нарушения, количество нарушений, количество проверок',
 'Группировка нарушений по violation_code с количеством и количеством затронутых проверок.'),

('top_audited_objects',
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
        "type": "like",
        "required": false,
        "description": "Название проверяемого объекта",
        "validation": {
            "vector_source": "audits",
            "vector_field": "auditee_entity",
            "vector_min_score": 0.7,
            "vector_top_k": 3
        }
    },
    "date_from": {
        "type": "date",
        "required": false,
        "description": "Начальная дата (включительно)"
    },
    "limit": {
        "type": "limit",
        "required": false,
        "default": 10,
        "description": "Количество записей в топе"
    }
}$json$::jsonb,
 10,
 'объект, количество проверок, лет покрыто, дата последней проверки',
 'Рейтинг auditee_entity по количеству проведённых проверок.'),

('audit_effectiveness',
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
        "type": "date",
        "required": false,
        "description": "Начальная дата (включительно)"
    },
    "date_to": {
        "type": "date",
        "required": false,
        "description": "Конечная дата (включительно)"
    },
    "min_violations": {
        "type": "number",
        "required": false,
        "description": "Минимальное количество нарушений для фильтрации"
    }
}$json$::jsonb,
 100,
 'ID проверки, название, дата, количество нарушений, уровень серьёзности',
 'Оценка каждой проверки по количеству выявленных нарушений с классификацией severity_level.'),

('audit_dynamics',
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
    "period": {
        "type": "enum",
        "required": false,
        "default": "month",
        "description": "Период группировки: month, quarter, week"
    },
    "date_from": {
        "type": "date",
        "required": false,
        "description": "Начальная дата (включительно)"
    }
}$json$::jsonb,
 100,
 'период, количество проверок, уникальных объектов, нарушений',
 'Динамика проверок с группировкой по месяцам, кварталам или неделям.'),

('audit_types_stats',
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
        "type": "like",
        "required": false,
        "description": "Тип проверки (например, ''финансовый'')",
        "validation": {
            "vector_source": "audits",
            "vector_field": "audit_type",
            "vector_min_score": 0.7,
            "vector_top_k": 3
        }
    },
    "date_from": {
        "type": "date",
        "required": false,
        "description": "Начальная дата (включительно)"
    }
}$json$::jsonb,
 100,
 'тип проверки, количество, объектов, нарушений, средняя серьёзность',
 'Статистика в разрезе audit_type: количество проверок, объектов, нарушений, средняя severity.')
ON CONFLICT (name) DO UPDATE SET
    description = EXCLUDED.description,
    sql_template = EXCLUDED.sql_template,
    parameters = EXCLUDED.parameters,
    max_rows_default = EXCLUDED.max_rows_default,
    returns = EXCLUDED.returns,
    long_description = EXCLUDED.long_description;
