"""Режим: predefined — выполнение готовых SQL-шаблонов по имени скрипта."""

from typing import Any, Dict, Optional

from .database import execute_query
from .predefined import build_sql, get_script_by_name, list_available
from .scripts_registry import SCRIPTS_REGISTRY


async def run(script_name: str, db_cfg: dict, params: Optional[Dict[str, Any]] = None) -> dict:
    """Выполнить готовый SQL-шаблон по имени скрипта.

    Args:
        script_name: Имя скрипта из SCRIPTS_REGISTRY.
        db_cfg: Конфигурация подключения к БД.
        params: Значения параметров скрипта (опционально).
    """
    script = get_script_by_name(script_name)
    if not script:
        return {
            "status": "error",
            "data": {
                "message": f"Скрипт '{script_name}' не найден. Доступны: {list_available()}",
            },
        }

    merged = dict(params or {})
    sql, sql_params = build_sql(script, merged)
    result = await execute_query(db_cfg, sql, sql_params)

    return {
        "mode": "predefined",
        "status": result["status"],
        "data": {
            "script_name": script.name,
            "sql": sql,
            "parameters": merged,
            "result": result,
        },
    }
