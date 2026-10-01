"""Подставные зависимости для тестов библиотеки аудита.

Ни сети, ни настоящего DuckDB, ни реального реестра: вся библиотека
общается наружу только через колбэки (см. ``libs/audit/contracts.py``),
поэтому и проверяется она подставными callable'ами.

Подписные имена методов (``query``/``schema``/``explain``) совпадают с
теми, что capability ``audit`` передаст из ``DataService``: так тест
проверяет не только библиотеку, но и то, что вызывающая сторона сможет
подставить свои методы без адаптера.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "REGISTRY_TABLE",
    "FakeSnapshot",
    "make_registry_row",
    "script_row_violations_by_period",
    "script_row_effectiveness",
]

#: Имя таблицы реестра в тестах. В проде приходит аргументом операции.
REGISTRY_TABLE = "public.agent_predefined_scripts"


def make_registry_row(
    name: str = "script_a",
    *,
    sql_template: str = "SELECT 1 AS one FROM oarb.audits",
    parameters: Any = None,
    description: str = "Тестовый скрипт",
    max_rows_default: Any = 1000,
    returns: str = "one",
    long_description: str = "",
) -> dict[str, Any]:
    """Строка реестра в форме, которую отдаёт снимок."""
    params = parameters
    if params is not None and not isinstance(params, str):
        params = json.dumps(params, ensure_ascii=False)
    return {
        "name": name,
        "description": description,
        "returns": returns,
        "long_description": long_description,
        "sql_template": sql_template,
        "parameters": params if params is not None else "{}",
        "max_rows_default": max_rows_default,
    }


def script_row_violations_by_period() -> dict[str, Any]:
    """Скрипт с двумя обязательными датами (как в агенте)."""
    return make_registry_row(
        name="violations_by_period",
        description="Нарушения за период",
        sql_template=(
            "SELECT v.id, v.violation_code, a.actual_date "
            "FROM oarb.violations v "
            "JOIN oarb.audits a ON a.id = v.audit_id "
            "WHERE a.actual_date IS NOT NULL "
            "AND a.actual_date >= :date_from "
            "AND a.actual_date <= :date_to "
            "ORDER BY a.actual_date DESC, v.id"
        ),
        parameters={
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
        max_rows_default=1000,
    )


def script_row_effectiveness() -> dict[str, Any]:
    """Скрипт с условным блоком и необязательным числовым параметром."""
    return make_registry_row(
        name="audit_effectiveness_summary",
        description="Сводка эффективности",
        sql_template=(
            "SELECT a.id AS audit_id, COUNT(v.id) AS violations_count "
            "FROM oarb.audits a "
            "LEFT JOIN oarb.violations v ON a.id = v.audit_id "
            "WHERE a.actual_date IS NOT NULL "
            "GROUP BY a.id "
            "{% if min_violations %} HAVING COUNT(v.id) >= :min_violations "
            "{% endif %} ORDER BY violations_count DESC"
        ),
        parameters={
            "min_violations": {
                "type": "number",
                "required": False,
                "default": None,
                "description": "Минимальное число нарушений",
            }
        },
        max_rows_default=1000,
    )


class FakeSnapshot:
    """Читатель снимка: реестр в памяти + запись всех вызовов.

    Методы ``query``/``schema``/``explain`` — то, что capability ``audit``
    передаст из ``DataService`` (с подставленными аргументами).
    """

    def __init__(
        self,
        registry_rows: list[dict[str, Any]] | None = None,
        *,
        data_rows: list[dict[str, Any]] | None = None,
        registry_error: Exception | None = None,
        registry_result: Any = None,
        data_result: Any = None,
        schema: dict[str, Any] | None = None,
        explain_valid: bool = True,
        explain_error: str = "",
        schema_error: Exception | None = None,
        registry_match: str = "agent_predefined_scripts",
    ) -> None:
        self.registry_rows = registry_rows if registry_rows is not None else []
        self.data_rows = data_rows if data_rows is not None else []
        self.registry_error = registry_error
        self.registry_result = registry_result
        self.data_result = data_result
        self.schema_error = schema_error
        self.explain_valid = explain_valid
        self.explain_error = explain_error
        self.registry_match = registry_match
        self.schema_payload = schema if schema is not None else default_schema()
        self.calls: list[tuple[str, list[Any] | None]] = []
        self.explain_calls: list[str] = []

    # --- чтение снимка -------------------------------------------------

    def query(self, text: str, params: list[Any] | None = None) -> Any:
        self.calls.append((text, list(params) if params else None))
        if self.registry_match in text:
            return self._registry_answer(params)
        if self.data_result is not None:
            return self.data_result
        return {
            "status": "success",
            "row_count": len(self.data_rows),
            "columns": list(self.data_rows[0]) if self.data_rows else [],
            "rows": self.data_rows,
        }

    def _registry_answer(self, params: list[Any] | None) -> Any:
        if self.registry_error is not None:
            raise self.registry_error
        if self.registry_result is not None:
            return self.registry_result
        rows = self.registry_rows
        if params:
            # Снимок отвечает только на ``WHERE name = ?`` — так же, как БД.
            rows = [row for row in rows if row.get("name") == params[0]]
        else:
            # ``ORDER BY name`` в тексте запроса сортирует на стороне БД.
            rows = sorted(rows, key=lambda row: str(row.get("name") or ""))
        return {
            "status": "success",
            "row_count": len(rows),
            "columns": ["name"],
            "rows": rows,
        }

    # --- схема и EXPLAIN ------------------------------------------------

    def schema(self) -> dict[str, Any]:
        if self.schema_error is not None:
            raise self.schema_error
        return self.schema_payload

    def explain(self, text: str) -> dict[str, Any]:
        self.explain_calls.append(text)
        if self.explain_valid:
            return {"valid": True, "plan": []}
        return {"valid": False, "error": self.explain_error or "syntax error"}

    # --- имена, под которыми capability ждёт владельца снимка -----------
    #
    # Сервис capability ``audit`` обращается к снимку как к ``DataService``, то
    # есть через ``snapshot_query``/``snapshot_schema``. Подписи даны явно, а не
    # через ``__getattr__``, чтобы подмена в тестах capability была обычной
    # подстановкой объекта, а не совпадением имён по счастливой случайности.

    def snapshot_query(self, sql: str, params: list[Any] | None = None) -> Any:
        return self.query(sql, params)

    def snapshot_schema(self, schema_name: str | None = None, table_names: list[str] | None = None) -> Any:
        return self.schema()

    def snapshot_explain(self, sql: str) -> Any:
        """Синтаксическая проверка: EXPLAIN делает владелец снимка.

        Capability ``audit`` ходит за этим швом, а не в ``explain_query``
        напрямую: соединение с снимком у неё нет, и раньше она передавала
        туда вызываемый объект вместо соединения.
        """
        self.explain_calls.append(sql)
        return {"valid": True, "plan": []}

    # --- утверждения для тестов ----------------------------------------

    @property
    def last_text(self) -> str:
        return self.calls[-1][0]

    @property
    def last_params(self) -> list[Any] | None:
        return self.calls[-1][1]

    def data_calls(self) -> list[tuple[str, list[Any] | None]]:
        """Вызовы, не относящиеся к реестру (то есть к данным)."""
        return [call for call in self.calls if self.registry_match not in call[0]]


def default_schema() -> dict[str, Any]:
    """Описание схемы снимка в форме, которую понимает ``format_schema``."""
    return {
        "schema": "oarb",
        "tables": {
            "audits": {
                "comment": "Аудиторские проверки",
                "columns": {
                    "id": {"type": "integer", "not_null": True, "comment": "ID"},
                    "status": {"type": "varchar(50)", "not_null": False, "comment": ""},
                },
            },
            "violations": {
                "comment": "Нарушения",
                "columns": {
                    "id": {"type": "integer", "not_null": True, "comment": "ID"},
                    "violation_code": {
                        "type": "varchar(20)",
                        "not_null": False,
                        "comment": "",
                    },
                },
            },
            "agent_gateway_logs": {
                "comment": "Журнал шлюза — не домен аудита",
                "columns": {"id": {"type": "integer", "not_null": True, "comment": ""}},
            },
        },
    }
