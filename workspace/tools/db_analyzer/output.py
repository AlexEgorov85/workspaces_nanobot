"""Форматирование результатов для stdout/JSON."""

from datetime import datetime
from typing import Any


def serialize(obj: Any) -> str:
    """Сериализовать не-JSON-совместимые типы (datetime, asyncpg-типы)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    return str(obj)


def prepare_output(result: dict, mode: str) -> dict:
    """Привести результат к плоскому формату для вывода.

    На выходе: mode, status, rows, columns, row_count, sql/script_name.
    """
    out: dict[str, Any] = {"mode": mode, "status": result.get("status", "error")}
    data = result.get("data", {})

    if "result" in data:
        r = data["result"]
        out["row_count"] = r.get("row_count", 0)
        out["columns"] = r.get("columns", [])
        out["rows"] = r.get("rows", [])
        out["sql"] = data.get("sql", "")
    elif "message" in data:
        out["message"] = data["message"]

    if "script_name" in data:
        out["script_name"] = data["script_name"]
        out["sql"] = data.get("sql", "")

    if "results" in data:
        out["vector_results"] = data["results"]
        out["count"] = len(data["results"])

    return out
