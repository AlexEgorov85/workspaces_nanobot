"""Каталог из 5 predefined SQL-скриптов навыка ``audit_analyzer``.

Источник SQL — каталог из SKILL.md (``references/predefined_scripts.md``)
эталона ``66449e06``: только описание и сигнатура, без хранения в PG.
Семантика и бизнес-смысл SQL восстановлены из:

* ``workspace/skills/audit_analyzer/references/predefined_scripts.md``
  (эталон ``66449e06`` — каталог и подробные описания);
* ``tests/test_audit_analyzer_behavior.py::TestAuditAnalyerPredefinedScripts``
  (fixture SQL для ``audit_status_summary``, ``top_violations_by_type``,
  ``violations_by_period``, ``audits_by_period``, ``audit_effectiveness_summary``).

Этих 5 скриптов было достаточно для всех production-cases; добавление
новых — без миграции PG (см. ``Step 9`` плана миграции).

IMPORTANT: SQL использует ``:param`` плейсхолдеры; см.
``DynamicQueryBuilder.build`` для конвертации в позиционные ``?``.
"""

from __future__ import annotations

from workspace.skills.audit_analyzer.predefined.models import (
    ParamDefinition,
    ScriptDefinition,
)


__all__ = ["REGISTRY", "list_scripts", "get_script"]


REGISTRY: dict[str, ScriptDefinition] = {
    "audit_status_summary": ScriptDefinition(
        name="audit_status_summary",
        description="Сводка по статусам аудитов",
        returns="статус, количество аудитов",
        long_description=(
            "Агрегация проверок по статусу (Завершена / В работе / "
            "Запланирована). Использовать для вопросов «сколько аудитов по "
            "статусам», «распределение проверок по состоянию». "
            "НЕ использовать, когда нужны подробности по конкретным "
            "проверкам или фильтры по датам/типу — свободный SQL."
        ),
        sql_template=(
            "SELECT status, COUNT(*) AS cnt "
            "FROM oarb.audits "
            "WHERE status IS NOT NULL "
            "GROUP BY status "
            "ORDER BY status"
        ),
        parameters={},
        max_rows_default=100,
    ),

    "top_violations_by_type": ScriptDefinition(
        name="top_violations_by_type",
        description="Топ кодов нарушений",
        returns="код нарушения, количество нарушений",
        long_description=(
            "Топ кодов нарушений. Использовать для «самые частые "
            "нарушения», «топ кодов». НЕ использовать, когда нужны "
            "нарушения по конкретному коду или фильтры по "
            "severity/status — свободный SQL."
        ),
        sql_template=(
            "SELECT violation_code, COUNT(*) AS cnt "
            "FROM oarb.violations "
            "WHERE violation_code IS NOT NULL "
            "GROUP BY violation_code "
            "ORDER BY cnt DESC, violation_code"
        ),
        parameters={},
        max_rows_default=100,
    ),

    "violations_by_period": ScriptDefinition(
        name="violations_by_period",
        description="Нарушения за период",
        returns="id нарушения, код, описание, дата проверки",
        long_description=(
            "Нарушения в заданный период. Использовать для "
            "«нарушения за 2024», «что выявлено в Q1». "
            "НЕ использовать, когда период не указан/неочевиден или нужны "
            "фильтры по severity/status — свободный SQL. "
            "Параметры: date_from, date_to — обязательные ISO-даты (YYYY-MM-DD)."
        ),
        sql_template=(
            "SELECT v.id, v.violation_code, v.description, a.actual_date "
            "FROM oarb.violations v "
            "JOIN oarb.audits a ON a.id = v.audit_id "
            "WHERE a.actual_date IS NOT NULL "
            "AND a.actual_date >= :date_from "
            "AND a.actual_date <= :date_to "
            "ORDER BY a.actual_date DESC, v.id"
        ),
        parameters={
            "date_from": ParamDefinition(
                type="date",
                required=True,
                description="Начальная дата (включительно, YYYY-MM-DD)",
            ),
            "date_to": ParamDefinition(
                type="date",
                required=True,
                description="Конечная дата (включительно, YYYY-MM-DD)",
            ),
        },
        max_rows_default=1000,
    ),

    "audits_by_period": ScriptDefinition(
        name="audits_by_period",
        description="Аудиторские проверки за период",
        returns="id, название, тип, фактическая дата, статус проверки",
        long_description=(
            "Аудиторские проверки в заданный период (по actual_date). "
            "Использовать для «проверки за 2024», «что проверяли в Q2». "
            "НЕ использовать, когда период не указан или нужны фильтры "
            "по status/audit_type — свободный SQL. "
            "Параметры: date_from, date_to — обязательные."
        ),
        sql_template=(
            "SELECT id, title, audit_type, actual_date, status "
            "FROM oarb.audits "
            "WHERE actual_date IS NOT NULL "
            "AND actual_date >= :date_from "
            "AND actual_date <= :date_to "
            "ORDER BY actual_date DESC, id"
        ),
        parameters={
            "date_from": ParamDefinition(
                type="date",
                required=True,
                description="Начальная дата (включительно, YYYY-MM-DD)",
            ),
            "date_to": ParamDefinition(
                type="date",
                required=True,
                description="Конечная дата (включительно, YYYY-MM-DD)",
            ),
        },
        max_rows_default=1000,
    ),

    "audit_effectiveness_summary": ScriptDefinition(
        name="audit_effectiveness_summary",
        description=(
            "Сводка эффективности: проверки × нарушения × severity"
        ),
        returns=(
            "id, название, дата проверки, число нарушений, "
            "уровень серьёзности"
        ),
        long_description=(
            "Сводка эффективности: проверки × нарушения × severity. "
            "Использовать для «какие проверки самые проблемные», "
            "«уровень серьёзности нарушений». НЕ использовать, когда "
            "нужны JOIN с другими таблицами или детализация по "
            "auditee_entity — свободный SQL.\n\n"
            "Если в результате одна проверка содержит >50% всех "
            "нарушений — это признак битого сида (нарушения не "
            "распределены по проверкам). В таком случае отчёт "
            "бесполезен; используйте свободный SQL с проверкой "
            "распределения по ``audit_id``."
        ),
        sql_template=(
            "SELECT "
            "  a.id AS audit_id, "
            "  a.title AS audit_title, "
            "  a.actual_date, "
            "  COUNT(v.id) AS violations_count, "
            "  CASE "
            "    WHEN COUNT(v.id) = 0 THEN 'Без нарушений' "
            "    WHEN COUNT(v.id) <= 3 THEN 'Допустимые нарушения' "
            "    WHEN COUNT(v.id) <= 10 THEN 'Серьёзные нарушения' "
            "    ELSE 'Критические нарушения' "
            "  END AS severity_level "
            "FROM oarb.audits a "
            "LEFT JOIN oarb.violations v ON a.id = v.audit_id "
            "WHERE a.actual_date IS NOT NULL "
            "GROUP BY a.id, a.title, a.actual_date "
            "{% if min_violations %} HAVING COUNT(v.id) >= :min_violations {% endif %}"
            " ORDER BY violations_count DESC, a.actual_date DESC"
        ),
        parameters={
            "min_violations": ParamDefinition(
                type="number",
                required=False,
                default=None,
                description=(
                    "Минимальное число нарушений для включения проверки в отчёт. "
                    "Полезно, чтобы исключить «Без нарушений»-строки и "
                    "сосредоточиться на проблемных проверках (рекомендуется 1+)."
                ),
            ),
        },
        max_rows_default=1000,
    ),
}


def list_scripts() -> list[dict[str, str]]:
    """Метаданные всех скриптов для UI/CLI/документации."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "parameters": ", ".join(s.parameters.keys()),
        }
        for s in REGISTRY.values()
    ]


def get_script(name: str) -> ScriptDefinition | None:
    """Получить ``ScriptDefinition`` по имени или ``None`` если не найден."""
    return REGISTRY.get(name)
