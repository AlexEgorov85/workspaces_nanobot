"""
Режим: predefined — выполнение готовых SQL-шаблонов по имени скрипта.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from db_loader import set_provider
from predefined import build_sql, get_script_by_name, list_available, resolve_params_with_vector


def run(
    script_name: str,
    db: Any,
    params: dict[str, Any] | None = None,
    index_dir: str = "",
) -> dict:
    """Выполнить предопределённый SQL-скрипт."""
    if not isinstance(script_name, (str,)):
        return {
            "status": "error",
            "data": {"message": f"Ожидалось имя скрипта (str), получено: {type(script_name).__name__}"},
        }
    # Реестр скрптов должен читаться из ТОГО ЖЕ DuckDB-кэша, что и данные.
    set_provider(db)
    script = get_script_by_name(script_name)
    if not script:
        return {
            "status": "error",
            "data": {
                "message": f"Скрипт '{script_name}' не найден. Доступны: {list_available()}",
            },
        }
    merged, unknown = resolve_params_with_vector(script, params, index_dir=index_dir)
    if unknown:
        valid = list(script.parameters.keys())
        return {
            "status": "error",
            "data": {
                "message": (
                    f"Неизвестные параметры: {', '.join(unknown)}. "
                    f"Допустимые параметры для скрипта '{script.name}': {', '.join(valid)}"
                ),
            },
        }
    if params and not merged:
        valid = list(script.parameters.keys())
        return {
            "status": "error",
            "data": {
                "message": (
                    f"Ни один из переданных параметров не подходит для скрипта '{script.name}'. "
                    f"Допустимые параметры: {', '.join(valid)}"
                ),
            },
        }
    for pname, pdef in script.parameters.items():
        is_merged = pname in merged
        if not is_merged:
            if pdef.required:
                return {
                    "status": "error",
                    "data": {
                        "message": f"Обязательный параметр '{pname}' не указан для скрипта '{script.name}'",
                    },
                }
            continue
        val = merged[pname]
        if pdef.type in ("number", "limit"):
            try:
                int(val)
            except (ValueError, TypeError):
                return {
                    "status": "error",
                    "data": {
                        "message": f"Параметр '{pname}' должен быть числом, получено: {val}",
                    },
                }
        elif pdef.type == "boolean" and not isinstance(val, bool):
            return {
                "status": "error",
                "data": {
                    "message": f"Параметр '{pname}' должен быть boolean (true/false), получено: {val}",
                },
            }
    try:
        sql, sql_params = build_sql(script, merged)
    except ValueError as e:
        return {
            "status": "error",
            "data": {"message": f"Ошибка сборки SQL: {e}"},
        }
    result = db.query_sql(sql, sql_params)
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
