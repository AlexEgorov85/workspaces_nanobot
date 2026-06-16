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
"""

import decimal
import math
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any


def _sanitize_value(obj: Any) -> Any:
    """
    Рекурсивно привести объект к JSON-совместимому виду.

    Проблема: стандартный json.dumps обрабатывает float('nan')/inf на уровне
    C-коде encoder'а и выводит NaN/Infinity (невалидный JSON), НЕ вызывая
    default. Поэтому мы делаем предварительную обработку.

    Поддерживает:
        datetime / date / time → .isoformat()
        timedelta              → str()
        Decimal                → float или int
        UUID                   → str
        bytes                  → str (utf-8 decode)
        float (nan/inf)        → None
        list / tuple / dict    → рекурсивно
        всё остальное          → str()

    Args:
        obj: Любой объект.

    Returns:
        JSON-совместимое значение (None, bool, int, float, str, list, dict).

    Пример:
        >>> _sanitize_value(datetime(2024, 1, 15, 10, 30))
        '2024-01-15T10:30:00'
        >>> _sanitize_value(float('nan')) is None
        True
        >>> _sanitize_value(decimal.Decimal('1.23'))
        1.23
        >>> _sanitize_value(b'hello')
        'hello'
    """
    if obj is None:
        return None
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (int, str, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_sanitize_value(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _sanitize_value(v) for k, v in obj.items()}
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    try:
        return str(obj)
    except Exception:
        return repr(obj)


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
