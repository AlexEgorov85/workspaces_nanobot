"""Форматирование результатов для вывода в stdout (JSON).

Приводит вложенные dict-результаты от режимов к плоскому
единообразному формату для сериализации в JSON.

Выходной JSON всегда содержит:
    - mode: режим работы ("predefined", "generated_sql", "vector")
    - status: "success" | "error"
И дополнительные поля в зависимости от режима:
    - predefined/generated_sql: row_count, columns, rows, sql [, script_name]
    - vector: vector_results, count

JSON-сериализация значений (datetime/Decimal/NaN/bytes) — через
``sanitize_value`` из ``lib.utils.text_utils``.
"""

from __future__ import annotations

from typing import Any

from lib.utils.text_utils import sanitize_value


__all__ = ["prepare_output", "sanitize_output"]


def prepare_output(result: dict, mode: str) -> dict:
    """Привести вложенный результат режима к плоскому формату для вывода.

    Для predefined и sql:
        {"mode", "status", "row_count", "columns", "rows", "sql"}
        + "script_name" для predefined.

    Для vector:
        {"mode", "status", "vector_results", "count"}

    Args:
        result: dict от run() одного из режимов.
        mode: "predefined" | "generated_sql" | "vector"

    Returns:
        Плоский dict для json.dumps().
    """
    out: dict[str, Any] = {"mode": mode, "status": result.get("status", "error")}
    data = result.get("data", {})

    # generated_sql: явный отказ LLM от генерации (``<NO_MATCH>`` —
    # запрос нельзя выполнить на доступных таблицах). Это success,
    # не error; row_count=0 и rows=[] говорят «данных нет», а
    # no_match=true объясняет, почему.
    if data.get("no_match"):
        out["no_match"] = True
        out["row_count"] = 0
        out["columns"] = []
        out["rows"] = []
        out["sql"] = ""
        out["message"] = (
            "Запрос нельзя выполнить на доступных таблицах (LLM явно "
            "отказался подставлять похожие). Уточните запрос или "
            "используйте другой источник данных."
        )
        return out

    if "result" in data:
        r = data["result"]
        out["row_count"] = r.get("row_count", 0)
        out["columns"] = r.get("columns", [])
        out["rows"] = r.get("rows", [])
        out["sql"] = data.get("sql", "")
        if r.get("status") == "error" and "error" in r:
            out["message"] = r["error"]
    elif "message" in data:
        out["message"] = data["message"]

    if "script_name" in data:
        out["script_name"] = data["script_name"]
        out["sql"] = data.get("sql", "")

    if "results" in data:
        out["vector_results"] = data["results"]
        out["count"] = len(data["results"])

    return out


def sanitize_output(out: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивно санировать значения в плоском dict для JSON."""
    return sanitize_value(out)
