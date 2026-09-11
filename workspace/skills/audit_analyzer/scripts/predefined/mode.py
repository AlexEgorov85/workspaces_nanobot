"""``predefined.run()`` — выполнение predefined SQL-скрипта через generic core.

Канонический pipeline:

    public.agent_predefined_scripts         (PostgreSQL, source of truth)
        ↓ seed_predefined_scripts.sql
    DuckDB-PG snapshot (cache.duckdb)        (см. PgDuckDbSyncService)
        ↓ db_loader.load_script / load_all
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
  * Скрипты читаются только из ``public.agent_predefined_scripts`` (DB-source).
    Python ``REGISTRY`` удалён; fallback отсутствует.
  * ``DuckDBService`` — generic ``lib.services.DuckDBService`` (или
    интерфейс ``CacheProvider``); никакого домен-знания здесь нет.
  * ``run()`` не делает HTTP/LLM вызовов.
"""

from __future__ import annotations

from typing import Any, Protocol

from workspace.skills.audit_analyzer.scripts.predefined.builder import DynamicQueryBuilder
from workspace.skills.audit_analyzer.scripts.predefined.db_loader import (
    DBScriptProvider,
    load_all,
    load_script,
)
from workspace.skills.audit_analyzer.scripts.predefined.validator import (
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


def list_available(db: DBScriptProvider, predefined_table: str) -> str:
    """Список имён скриптов через запятую (для CLI/error messages)."""
    return ", ".join(load_all(db, predefined_table).keys())


def list_scripts(
    db: DBScriptProvider, predefined_table: str
) -> list[dict[str, str]]:
    """Метаданные всех скриптов для UI/CLI/документации."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "parameters": ", ".join(s.parameters.keys()),
        }
        for s in load_all(db, predefined_table).values()
    ]


def _resolve_script(
    script_name: str,
    db: DuckDBServiceProtocol | DBScriptProvider,
    predefined_table: str,
):
    """DB-only lookup.

    Скрипт читается только из ``public.agent_predefined_scripts``. Никакого
    fallback на Python ``REGISTRY`` (удалён в Phase 7).
    """
    return load_script(db, predefined_table, script_name)


def run(
    script_name: str,
    db: DuckDBServiceProtocol,
    params: dict[str, Any] | None = None,
    *,
    predefined_table: str | None = None,
) -> dict[str, Any]:
    """Выполнить predefined SQL-скрипт.

    Args:
        script_name: имя скрипта (DB-only lookup, см. ``db_loader``).
        db: generic DuckDB-сервис (``DuckDbCacheStore`` или
            stub с тем же интерфейсом в тестах).
        params: пользовательские параметры запроса.
        predefined_table: обязательный ``schema.table`` PG-реестра
            (например, ``public.agent_predefined_scripts``).

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

    if not predefined_table:
        return {
            "status": "error",
            "data": {
                "message": (
                    "predefined_table обязателен: скрипты читаются только "
                    "из PostgreSQL (public.agent_predefined_scripts). "
                    "Передайте predefined_table= явно."
                ),
                "error_type": "missing_predefined_table",
            },
        }

    script = _resolve_script(script_name, db, predefined_table)
    if script is None:
        return {
            "status": "error",
            "data": {
                "message": (
                    f"Скрипт '{script_name}' не найден. "
                    f"Доступны: {list_available(db, predefined_table)}"
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