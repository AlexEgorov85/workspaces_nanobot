"""Фикстуры для тестов ``audit_analyzer``.

Создают in-memory DuckDB с двумя схемами:

* ``oarb`` — доменные таблицы (audits, violations).
* ``public`` — инфраструктурная таблица ``agent_predefined_scripts``.

Это позволяет тестам полный pipeline ``predefined.run()`` против DB-source:
скрипты читаются из ``public.agent_predefined_scripts``, данные — из
``oarb.*``. Имитирует production-runtime, где DuckDB-кэш — снимок
PostgreSQL через ``PgDuckDbSyncService``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest


# ---------------------------------------------------------------------------
# Repo-root resolution
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    """Подняться от ``start`` вверх до корня репозитория.

    Ищем ``workspace/skills/audit_analyzer/SKILL.md`` вверх по дереву.
    Делаем максимум 8 уровней вверх. Используется для абсолютного пути
    к skill'у без зависимости от cwd pytest'а.
    """
    cur = start.resolve()
    for _ in range(8):
        if (cur / "workspace" / "skills" / "audit_analyzer" / "SKILL.md").is_file():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise RuntimeError(
        f"Cannot find repo root from {start}: workspace/skills/audit_analyzer/SKILL.md not found"
    )


REPO_ROOT = _find_repo_root(Path(__file__).parent)
SKILL_DIR = REPO_ROOT / "workspace" / "skills" / "audit_analyzer"
SKILL_MD = SKILL_DIR / "SKILL.md"
CLI_PATH = SKILL_DIR / "scripts" / "cli.py"


# ---------------------------------------------------------------------------
# Reference SQL (must match sql/audit_analyzer/seed_predefined_scripts.sql).
# Держим тут как Python-литералы, чтобы preload работал без зависимости от
# seed-файла. Если seed меняется — синхронизировать.
# ---------------------------------------------------------------------------


_REGISTRY_SEED: list[tuple[str, str, str, str, str, str, int]] = [
    (
        "audit_status_summary",
        "Сводка по статусам аудитов",
        "статус, количество аудитов",
        (
            "Агрегация проверок по статусу (Завершена / В работе / "
            "Запланирована). Использовать для вопросов «сколько аудитов по "
            "статусам», «распределение проверок по состоянию»."
        ),
        (
            "SELECT status, COUNT(*) AS cnt "
            "FROM oarb.audits WHERE status IS NOT NULL "
            "GROUP BY status ORDER BY status"
        ),
        json.dumps({}, ensure_ascii=False),
        100,
    ),
    (
        "top_violations_by_type",
        "Топ кодов нарушений",
        "код нарушения, количество нарушений",
        (
            "Топ кодов нарушений. Использовать для «самые частые нарушения», "
            "«топ кодов»."
        ),
        (
            "SELECT violation_code, COUNT(*) AS cnt "
            "FROM oarb.violations WHERE violation_code IS NOT NULL "
            "GROUP BY violation_code ORDER BY cnt DESC, violation_code"
        ),
        json.dumps({}, ensure_ascii=False),
        100,
    ),
    (
        "violations_by_period",
        "Нарушения за период",
        "id нарушения, код, описание, дата проверки",
        (
            "Нарушения в заданный период. Параметры: date_from, date_to — "
            "обязательные ISO-даты (YYYY-MM-DD)."
        ),
        (
            "SELECT v.id, v.violation_code, v.description, a.actual_date "
            "FROM oarb.violations v "
            "JOIN oarb.audits a ON a.id = v.audit_id "
            "WHERE a.actual_date IS NOT NULL "
            "AND a.actual_date >= :date_from "
            "AND a.actual_date <= :date_to "
            "ORDER BY a.actual_date DESC, v.id"
        ),
        json.dumps(
            {
                "date_from": {
                    "type": "date",
                    "required": True,
                    "default": None,
                    "description": "Начальная дата (включительно, YYYY-MM-DD)",
                },
                "date_to": {
                    "type": "date",
                    "required": True,
                    "default": None,
                    "description": "Конечная дата (включительно, YYYY-MM-DD)",
                },
            },
            ensure_ascii=False,
        ),
        1000,
    ),
    (
        "audits_by_period",
        "Аудиторские проверки за период",
        "id, название, тип, фактическая дата, статус проверки",
        (
            "Аудиторские проверки в заданный период (по actual_date). "
            "Параметры: date_from, date_to — обязательные."
        ),
        (
            "SELECT id, title, audit_type, actual_date, status "
            "FROM oarb.audits "
            "WHERE actual_date IS NOT NULL "
            "AND actual_date >= :date_from "
            "AND actual_date <= :date_to "
            "ORDER BY actual_date DESC, id"
        ),
        json.dumps(
            {
                "date_from": {
                    "type": "date",
                    "required": True,
                    "default": None,
                    "description": "Начальная дата (включительно, YYYY-MM-DD)",
                },
                "date_to": {
                    "type": "date",
                    "required": True,
                    "default": None,
                    "description": "Конечная дата (включительно, YYYY-MM-DD)",
                },
            },
            ensure_ascii=False,
        ),
        1000,
    ),
    (
        "audit_effectiveness_summary",
        "Сводка эффективности: проверки × нарушения × severity",
        "id, название, дата проверки, число нарушений, уровень серьёзности",
        (
            "Сводка эффективности: проверки × нарушения × severity. "
            "Параметр: min_violations (опц., число; 0 = включить «Без нарушений»)."
        ),
        (
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
            "{% if min_violations %} HAVING COUNT(v.id) >= :min_violations "
            "{% endif %} ORDER BY violations_count DESC, a.actual_date DESC"
        ),
        json.dumps(
            {
                "min_violations": {
                    "type": "number",
                    "required": False,
                    "default": None,
                    "description": "Минимальное число нарушений для отчёта.",
                }
            },
            ensure_ascii=False,
        ),
        1000,
    ),
]


# ---------------------------------------------------------------------------
# Domain seed (audits + violations) — minimal reproducible fixture.
# ---------------------------------------------------------------------------


_AUDIT_ROWS = [
    (1, "Fire safety", "planned", None, "Запланирована"),
    (2, "Financial audit", "planned", "2024-05-01", "Завершена"),
    (3, "Compliance review", "extra", "2024-08-15", "Завершена"),
    (4, "School check", "extra", "2025-02-10", "В работе"),
]


_VIOLATION_ROWS = [
    (10, 2, "F-1", "Fire exit blocked", "high"),
    (11, 2, "F-2", "No extinguisher", "medium"),
    (12, 3, "D-1", "Missing signature", "low"),
]


def _make_db() -> duckdb.DuckDBPyConnection:
    """Создать in-memory DuckDB с доменными таблицами + реестром скриптов.

    Структура идентична production: ``oarb.audits`` + ``oarb.violations``
    + ``public.agent_predefined_scripts`` (5 скриптов из REGISTRY_SEED).
    """
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS oarb")
    conn.execute("CREATE SCHEMA IF NOT EXISTS public")

    conn.execute(
        "CREATE TABLE oarb.audits ("
        "id INTEGER, title VARCHAR, audit_type VARCHAR, "
        "actual_date DATE, status VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO oarb.audits VALUES (?, ?, ?, ?, ?)",
        _AUDIT_ROWS,
    )

    conn.execute(
        "CREATE TABLE oarb.violations ("
        "id INTEGER, audit_id INTEGER, violation_code VARCHAR, "
        "description VARCHAR, severity VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO oarb.violations VALUES (?, ?, ?, ?, ?)",
        _VIOLATION_ROWS,
    )

    conn.execute(
        'CREATE TABLE public.agent_predefined_scripts ('
        "name VARCHAR, description VARCHAR, returns VARCHAR, "
        "long_description VARCHAR, sql_template VARCHAR, "
        "parameters VARCHAR, max_rows_default INTEGER)"
    )
    conn.executemany(
        "INSERT INTO public.agent_predefined_scripts "
        "(name, description, returns, long_description, "
        "sql_template, parameters, max_rows_default) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        _REGISTRY_SEED,
    )

    return conn


class _DBService:
    """Адаптер in-memory DuckDB к ``DuckDBServiceProtocol``."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def query_sql(
        self, sql: str, params: list[Any] | None = None
    ) -> dict[str, Any]:
        try:
            if params:
                result = self._conn.execute(sql, list(params))
            else:
                result = self._conn.execute(sql)
            columns = [c[0] for c in result.description] if result.description else []
            rows = [
                dict(zip(columns, r, strict=False))
                for r in result.fetchall()
            ]
            return {
                "status": "success",
                "row_count": len(rows),
                "columns": columns,
                "rows": rows,
            }
        except Exception as exc:
            return {
                "status": "error",
                "row_count": 0,
                "columns": [],
                "rows": [],
                "error": str(exc),
            }


@pytest.fixture
def db_service():
    """In-memory DuckDB с доменными таблицами + реестром скриптов.

    Заменяет старый fixture из ``test_audit_analyzer_predefined.py``.
    Теперь скрипты читаются из ``public.agent_predefined_scripts``,
    а не из Python ``REGISTRY``.
    """
    return _DBService(_make_db())


@pytest.fixture
def db_connection():
    """Сырой DuckDB-connection для тестов, которым нужен прямой доступ."""
    return _make_db()