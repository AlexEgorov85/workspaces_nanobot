"""
Форматирование результатов для вывода в stdout (JSON).

Приводит вложенные dict-результаты от режимов к плоскому
единообразному формату для сериализации в JSON.

Выходной JSON всегда содержит:
    - mode: режим работы ("predefined", "sql", "vector")
    - status: "success" | "error"
И дополнительные поля в зависимости от режима:
    - predefined/sql: row_count, columns, rows, sql [, script_name]
    - vector: vector_results, count

JSON-сериализация значений (datetime/Decimal/NaN/bytes) — через
``_sanitize_value`` (тонкая обёртка над ``lib.utils.text_utils.sanitize_value``,
сохранена для back-compat).
"""

from typing import Any

from lib.utils.text_utils import sanitize_value as _sanitize_value  # noqa: F401


def prepare_output(result: dict, mode: str) -> dict:
    """
    Привести вложенный результат режима к плоскому формату для вывода.

    Для predefined и sql:
        {"mode", "status", "row_count", "columns", "rows", "sql"}
        + "script_name" для predefined.

    Для vector:
        {"mode", "status", "vector_results", "count"}

    Args:
        result: dict от run() одного из режимов.
        mode: "predefined" | "sql" | "vector"

    Returns:
        Плоский dict для json.dumps().

    Пример (predefined):
        >>> prepare_output(
        ...   {"status": "success", "data": {
        ...     "script_name": "test", "sql": "SELECT 1",
        ...     "result": {"row_count": 1, "columns": ["x"], "rows": [{"x": 1}]}
        ...   }},
        ...   "predefined"
        ... )
        {'mode': 'predefined', 'status': 'success', 'row_count': 1,
         'columns': ['x'], 'rows': [{'x': 1}], 'script_name': 'test', 'sql': 'SELECT 1'}

    Пример (vector):
        >>> prepare_output(
        ...   {"status": "success", "data": {
        ...     "results": [{"content": "test", "score": 0.9}], "count": 1
        ...   }},
        ...   "vector"
        ... )
        {'mode': 'vector', 'status': 'success', 'vector_results': [...], 'count': 1}
    """
    out: dict[str, Any] = {"mode": mode, "status": result.get("status", "error")}
    data = result.get("data", {})

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
