"""
Режим: predefined — выполнение готовых SQL-шаблонов по имени скрипта.

Pipeline:
    1. Найти ScriptDefinition по имени в реестре (БД → DuckDB-кэш через db_loader)
    2. Отфильтровать параметры (resolve_params — алиасы + валидация)
    3. Собрать SQL через DynamicQueryBuilder (рендер + форматирование)
    4. Выполнить запрос через query_sql
    5. Вернуть результат с SQL, параметрами и данными

Пример запуска через CLI:
    audit_analyze --mode predefined --script analytics_by_year_month
    audit_analyze --mode predefined --script violations_by_type --params '{"date_from": "2024-01-01"}'
    audit_analyze --mode predefined --script top_audited_objects --params '{"limit": 5, "audited_object": "ВУЗ"}'
"""

from typing import Any, Dict, Optional

from database import QueryBackend
from db_loader import set_provider
from predefined import build_sql, get_script_by_name, list_available, resolve_params_with_vector


def run(
    script_name: str,
    db: QueryBackend,
    params: Optional[Dict[str, Any]] = None,
    index_dir: str = "",
) -> dict:
    """
    Выполнить предопределённый SQL-скрипт.

    Если передан index_dir, параметры с vector_source резолвятся
    через семантический поиск по FAISS (Ollama embedding + FAISS index).

    Args:
        script_name: Имя скрипта (ключ в реестре public.agent_predefined_scripts).
        db: Бэкенд запросов (PostgreSQL напрямую или DuckDB-кэш).
        params: Параметры скрипта (опционально).
        index_dir: Путь к директории с FAISS-индексами (опционально).

    Returns:
        dict с полями:
            mode: "predefined"
            status: "success" | "error"
            data:
                script_name: имя скрипта
                sql: сгенерированный SQL
                parameters: использованные параметры
                result: результат query_sql (columns, rows, row_count)
    """
    # Реестр скриптов должен читаться из ТОГО ЖЕ DuckDB-кэша, что и данные,
    # иначе будут два разных CacheProvider на одном файле (race + двойной open).
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
        if pname not in merged:
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
            "data": {
                "message": f"Ошибка сборки SQL: {e}",
            },
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
