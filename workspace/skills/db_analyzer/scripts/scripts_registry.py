"""
Типизированные модели скриптов (ParamDefinition, ScriptDefinition),
динамический построитель SQL с Jinja2-подобными шаблонами (DynamicQueryBuilder)
и реестр предопределённых скриптов (SCRIPTS_REGISTRY).

SCRIPTS_REGISTRY содержит готовые SQL-шаблоны для аналитики данных
из БД oarb: статистика проверок, нарушений, эффективности и т.д.

Каждый скрипт может иметь параметры с типизацией (like, number, date, ...),
условные блоки {% if param %}...{% endif %} и автоматическое добавление LIMIT.

Пример запуска через CLI:
    audit_analyze --mode predefined --script analytics_by_year_month --params '{"year": 2024}'
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple


# =============================================================================
# ТИПИЗИРОВАННЫЕ МОДЕЛИ
# =============================================================================

@dataclass
class ParamDefinition:
    """
    Определение одного параметра скрипта.

    Атрибуты:
        type: Тип параметра. Определяет, как значение форматируется перед
              подстановкой в SQL.
              - 'like'   → оборачивает в %value% (ILIKE поиск)
              - 'exact'  → точное значение (без изменений)
              - 'limit'  → преобразуется в max_rows для LIMIT
              - 'number' → int(value)
              - 'date'   → строка даты (без изменений)
              - 'enum'   → строка из фиксированного набора
              - 'boolean' → bool(value), используется для {% if %}
        required: True если параметр обязателен.
        default: Значение по умолчанию (если не передан).
        description: Человекочитаемое описание.
        validation: Опциональные правила валидации (напр. для векторного поиска).

    Пример:
        >>> ParamDefinition(type="like", required=False,
        ...                 description="Название объекта")
        ParamDefinition(type='like', required=False, default=None,
                        description='Название объекта', validation=None)

        >>> ParamDefinition(type="number", required=True,
        ...                 description="Год проверки")
        ParamDefinition(type='number', required=True, default=None,
                        description='Год проверки', validation=None)
    """
    type: Literal["like", "exact", "limit", "number", "date", "enum", "boolean"] = "exact"
    required: bool = False
    default: Any = None
    description: str = ""
    validation: Optional[Dict[str, Any]] = None


@dataclass
class ScriptDefinition:
    """
    Полное описание одного предопределённого SQL-скрипта.

    Атрибуты:
        name: Уникальное имя скрипта (используется в --script).
        description: Краткое описание для меню.
        sql_template: SQL-шаблон с :param_name и {% if %}...{% endif %}.
        parameters: Словарь параметров {имя: ParamDefinition}.
        max_rows_default: Лимит строк по умолчанию.
        returns: Что возвращает скрипт (для документации).
        long_description: Подробное описание.

    Пример:
        >>> ScriptDefinition(
        ...     name="test_script",
        ...     description="Тестовый скрипт",
        ...     sql_template="SELECT * FROM oarb.audits WHERE id = :id",
        ...     parameters={"id": ParamDefinition(type="number", required=True)},
        ... )
        ScriptDefinition(name='test_script', ...)
    """
    name: str
    description: str
    sql_template: str
    parameters: Dict[str, ParamDefinition] = field(default_factory=dict)
    max_rows_default: int = 1000
    returns: str = ""
    long_description: str = ""


# =============================================================================
# ДИНАМИЧЕСКИЙ ПОСТРОИТЕЛЬ SQL С ШАБЛОНАМИ
# =============================================================================

class DynamicQueryBuilder:
    """
    Сборка SQL из шаблона: обработка {% if %}, подстановка параметров,
    конвертация :param → %s для psycopg2.

    Pipeline:
        1. Значения по умолчанию для отсутствующих параметров
        2. Форматирование значений по типу (like → %%, limit → max_rows)
        3. Рендеринг {% if param %}...{% endif %} — удаление блоков с пустыми параметрами
        4. Авто-добавление LIMIT :max_rows если не указан
        5. :param_name → %s (psycopg2 style)
    """

    @staticmethod
    def _render_template(sql_template: str, params: Dict[str, Any]) -> str:
        """
        Рендеринг Jinja2-подобных условных блоков.

        Удаляет {% if param_name %}...{% endif %} целиком,
        если param_name отсутствует, None, пустая строка или False.

        Также чистит артефакты: пустые строки, WHERE 1=1 AND → WHERE.

        Args:
            sql_template: SQL с {% if %} блоками.
            params: Значения параметров для проверки условий.

        Returns:
            SQL без условных блоков.

        Пример:
            >>> tmpl = "SELECT * FROM t WHERE 1=1 {% if x %} AND x = :x {% endif %}"
            >>> DynamicQueryBuilder._render_template(tmpl, {"x": 42})
            'SELECT * FROM t WHERE x = :x'

            >>> DynamicQueryBuilder._render_template(tmpl, {})
            'SELECT * FROM t'
        """
        result = sql_template
        pattern = r'\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*endif\s*%\}'

        def replace_if_block(match):
            param_name = match.group(1)
            content = match.group(2)

            if param_name in params and params[param_name] is not None:
                value = params[param_name]
                if isinstance(value, str) and not value.strip():
                    return ""
                if isinstance(value, bool) and value:
                    rendered = re.sub(r'\{%.*?else.*?%\}', '', content, flags=re.DOTALL)
                    return rendered.strip()
                if not isinstance(value, bool):
                    rendered = re.sub(r'\{%.*?else.*?%\}', '', content, flags=re.DOTALL)
                    return rendered.strip()
            return ""

        prev_result = None
        while prev_result != result:
            prev_result = result
            result = re.sub(pattern, replace_if_block, result, flags=re.DOTALL)

        # Удаляем пустые строки
        lines = result.split('\n')
        cleaned_lines = [l.strip() for l in lines if l.strip()]
        result = '\n'.join(cleaned_lines)

        # Чистка WHERE 1=1
        result = re.sub(r'\bWHERE\s+1=1\s+AND\b', 'WHERE', result, flags=re.IGNORECASE)
        result = re.sub(r'\bWHERE\s+1=1\s*$', '', result, flags=re.IGNORECASE)
        result = re.sub(r'\bWHERE\s+AND\b', 'WHERE', result, flags=re.IGNORECASE)

        return result

    @staticmethod
    def _convert_to_positional(sql: str, params: Dict[str, Any]) -> Tuple[str, List[Any]]:
        """
        Конвертация :param_name → %s для psycopg2.

        Не трогает ::type_cast (двойное двоеточие) — используется
        для приведения типов в PostgreSQL.

        Args:
            sql: SQL с :param_name плейсхолдерами.
            params: Словарь значений {имя: значение}.

        Returns:
            (sql_with_percent_s_params, [values_in_order])

        Пример:
            >>> sql = "SELECT * FROM t WHERE x = :x AND y = :y"
            >>> DynamicQueryBuilder._convert_to_positional(sql, {"x": 1, "y": "abc"})
            ('SELECT * FROM t WHERE x = %s AND y = %s', [1, 'abc'])

            >>> sql = "SELECT * FROM t WHERE x = :x AND y = :x"
            >>> DynamicQueryBuilder._convert_to_positional(sql, {"x": 1})
            ('SELECT * FROM t WHERE x = %s AND y = %s', [1, 1])

        Внимание: не трогает ::type_cast (двойное двоеточие):
            >>> sql = "SELECT :x::TEXT"
            >>> DynamicQueryBuilder._convert_to_positional(sql, {"x": 42})
            ('SELECT %s::TEXT', [42])
        """
        matches: list[str] = []

        def _repl(m: re.Match) -> str:
            name = m.group(1)
            matches.append(name)
            return '%s'

        # Negative lookbehind: не заменять ::TEXT, ::INTEGER и т.д.
        positional_sql = re.sub(r'(?<!:):(\w+)', _repl, sql)
        values = [params[name] for name in matches]
        return positional_sql, values

    @classmethod
    def build(
        cls,
        script: ScriptDefinition,
        params: Dict[str, Any]
    ) -> Tuple[str, List[Any]]:
        """
        Полный цикл сборки SQL из шаблона скрипта.

        Pipeline:
            1. Применить значения по умолчанию
            2. Для каждого параметра: форматирование по типу (like → %%, limit → max_rows, etc.)
            3. Рендеринг {% if %} блоков
            4. Авто-добавление LIMIT :max_rows
            5. Конвертация :param → %s

        Args:
            script: ScriptDefinition с sql_template и parameters.
            params: Значения параметров от пользователя.

        Returns:
            (sql_with_percent_s_params, [values_for_execute])

        Raises:
            ValueError: Если обязательный параметр отсутствует.

        Пример:
            >>> from scripts_registry import SCRIPTS_REGISTRY
            >>> script = SCRIPTS_REGISTRY["analytics_by_year_month"]
            >>> DynamicQueryBuilder.build(script, {"year": 2024})
            ('SELECT ... WHERE ... = %s\\nLIMIT %s', [2024, 100])

        Пример с like-параметром:
            >>> script2 = SCRIPTS_REGISTRY["violations_by_type"]
            >>> DynamicQueryBuilder.build(script2, {"violation_code": "финан"})
            ('SELECT ... WHERE ... ILIKE %s\\nLIMIT %s', ['%финан%', 100])
        """
        clean_params: Dict[str, Any] = {}
        final_sql = script.sql_template

        # Шаг 1: значения по умолчанию для отсутствующих параметров
        for pname, pdef in script.parameters.items():
            if pname not in params or params[pname] is None:
                if pdef.default is not None:
                    params[pname] = pdef.default

        # Шаг 2: обработка и форматирование каждого параметра
        for param_name, param_def in script.parameters.items():
            value = params.get(param_name)

            # Пропускаем пустые/None
            if value is None or (isinstance(value, str) and not value.strip()):
                if not param_def.required:
                    continue
                raise ValueError(f"Обязательный параметр '{param_name}' отсутствует")

            # Обработка списков (ANY вместо ILIKE)
            if isinstance(value, list):
                if not value:
                    continue
                final_sql = re.sub(
                    rf'ILIKE\s+:{param_name}\b',
                    f'= ANY(:{param_name})',
                    final_sql,
                    flags=re.IGNORECASE
                )
                clean_params[param_name] = value
                continue

            # Стандартная логика для скаляров
            formatted_value = value

            if param_def.type == "like" and isinstance(value, str):
                if "%" not in value:
                    formatted_value = f"%{value}%"
            elif param_def.type == "limit":
                clean_params["max_rows"] = int(value) if value else script.max_rows_default
                clean_params[param_name] = True  # для {% if limit %}
                continue
            elif param_def.type == "boolean":
                clean_params[param_name] = bool(value)
                continue
            elif param_def.type == "number":
                clean_params[param_name] = int(value) if value else None
                continue
            elif param_def.type == "date":
                clean_params[param_name] = value
                continue

            clean_params[param_name] = formatted_value

        # Шаг 3: рендеринг условных блоков
        final_sql = cls._render_template(final_sql, clean_params)

        # Шаг 4: авто-добавление LIMIT
        if ":max_rows" not in final_sql:
            final_sql += " LIMIT :max_rows"
            clean_params["max_rows"] = clean_params.get("max_rows", script.max_rows_default)

        # Шаг 5: :param → $N
        final_sql, values = cls._convert_to_positional(final_sql, clean_params)

        return final_sql, values


# =============================================================================
# РЕЕСТР ПРЕДОПРЕДЕЛЁННЫХ СКРИПТОВ
# =============================================================================
#
# Каждый скрипт — это ScriptDefinition с SQL-шаблоном, описанием
# и типизированными параметрами. Скрипты выполняются через:
#   audit_analyze --mode predefined --script <имя> --params '{...}'
#
# Доступные скрипты:
#   analytics_by_year_month  — Аналитика проверок по годам и месяцам
#   violations_by_type       — Статистика нарушений по типам
#   top_audited_objects      — Топ проверяемых объектов
#   audit_effectiveness      — Оценка эффективности проверок
#   audit_dynamics           — Динамика проверок по периодам
#   audit_types_stats        — Статистика по типам проверок

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
