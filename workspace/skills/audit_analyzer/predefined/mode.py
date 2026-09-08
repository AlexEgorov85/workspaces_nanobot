"""``predefined.run()`` — выполнение predefined SQL-скрипта через generic core.

Pipeline (соответствует ``predefined_mode.run()`` эталона ``a606fe0``):

    ScriptDefinition
        ↓ resolve/merge
    ParameterValidator
        ↓ validate
    DynamicQueryBuilder
        ↓ SQL + params
    CacheProvider.query_sql
        ↓
    result (rows — список dict с ключами-именами колонок)

Critical rules:
  * Skill НЕ обращается к PG-таблице ``public.agent_predefined_scripts``
    (Step 10 плана миграции — реестр хранится внутри skill'а).
  * ``DuckDBService`` — generic ``lib.services.DuckDBService`` (или
    интерфейс ``CacheProvider``); никакого домен-знания здесь нет.
  * ``run()`` не делает HTTP/LLM вызовов.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from workspace.skills.audit_analyzer.predefined.builder import DynamicQueryBuilder
from workspace.skills.audit_analyzer.predefined.scripts import REGISTRY, get_script
from workspace.skills.audit_analyzer.predefined.validator import (
    ParameterValidator,
)


__all__ = [
    "run",
    "list_available",
    "list_scripts",
    "DuckDBServiceProtocol",
]


class DuckDBServiceProtocol(Protocol):
    """Минимальный интерфейс, нужный режиму predefined.

    Совместим с любым ``CacheProvider`` (см. ``lib.services.cache_provider``):
    ``DuckDbCacheStore`` и ``PostgresDuckDbProvider``. SQL-безопасность
    контролируется tool'ом ``duckdb_query`` через ``validate_sql`` (см.
    ``lib.utils.sql_safety``); здесь — только выполнение.
    """

    def query_sql(
        self,
        sql: str,
        params: list[Any] | None = None,
    ) -> dict[str, Any]: ...


def list_available() -> str:
    """Список имён скриптов через запятую (для CLI/error messages)."""
    return ", ".join(REGISTRY.keys())


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


def run(
    script_name: str,
    db: DuckDBServiceProtocol,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Выполнить predefined SQL-скрипт.

    Args:
        script_name: имя скрипта из каталога (см. ``REGISTRY``).
        db: generic DuckDB-сервис (``DuckDbCacheStore`` или
            stub с тем же интерфейсом в тестах).
        params: пользовательские параметры запроса.

    Returns:
        ``{"mode": "predefined", "status": "success" | "error", ...}``.
        При успехе — ``data.script_name``, ``data.sql``, ``data.parameters``,
        ``data.result`` (результат ``query_sql``: ``row_count``/``columns``/
        ``rows``, где ``rows`` — список dict с ключами-именами колонок).
        При ошибке — ``data.message``.
    """
    if not isinstance(script_name, str) or not script_name.strip():
        return {
            "status": "error",
            "data": {
                "message": (
                    f"Ожидалось имя скрипта (str), получено: "
                    f"{type(script_name).__name__}"
                ),
            },
        }

    script = get_script(script_name)
    if script is None:
        return {
            "status": "error",
            "data": {
                "message": (
                    f"Скрипт '{script_name}' не найден. "
                    f"Доступны: {list_available()}"
                ),
            },
        }

    merged, error = ParameterValidator.validate(script, params)
    if error:
        return {"status": "error", "data": {"message": error}}

    try:
        sql, sql_params = DynamicQueryBuilder.build(script, merged)
    except Exception as exc:
        return {
            "status": "error",
            "data": {"message": f"Ошибка сборки SQL: {exc}"},
        }

    try:
        result = db.query_sql(sql, sql_params)
    except Exception as exc:
        return {
            "status": "error",
            "data": {"message": f"Ошибка выполнения SQL: {exc}"},
        }

    # ``query_sql`` возвращает ``{status, row_count, columns, rows, error?}``.
    error_msg = result.get("error") if isinstance(result, dict) else None
    if error_msg or result.get("status") == "error":
        return {
            "status": "error",
            "data": {
                "message": error_msg or "SQL execution error",
                "script_name": script.name,
                "sql": sql,
            },
        }

    return {
        "mode": "predefined",
        "status": "success",
        "data": {
            "script_name": script.name,
            "sql": sql,
            "parameters": merged,
            "result": result,
        },
    }
