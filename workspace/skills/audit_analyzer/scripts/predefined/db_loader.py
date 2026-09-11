"""Loader: ``public.agent_predefined_scripts`` (DB) → ``ScriptDefinition``.

Единая точка входа для runtime-чтения predefined-скриптов из PostgreSQL.
Резолв таблицы идёт через ``lib.core.skill_config.get_predefined_scripts_table``
(метка ``scripts_registry`` в ``TableRegistry``), а саму строку читаем
через CacheProvider/DuckDB — после того, как ``PgDuckDbSyncService``
опубликовал снимок в ``workspace/data_store/duckdb/cache.duckdb``.

Python ``REGISTRY`` (legacy) удалён в Phase 7 — этот loader теперь
единственный источник ``ScriptDefinition``. Ошибка чтения таблицы
возвращает пустой результат / ``None``, **не** молчаливый fallback.

Не вводит новых абстракций: ``load_script`` / ``load_all`` + dataclass-
конвертер из JSONB-строки PG в ``ScriptDefinition``.
"""

from __future__ import annotations

from typing import Any, Protocol

from workspace.skills.audit_analyzer.scripts.predefined.models import (
    ParamDefinition,
    ScriptDefinition,
)


__all__ = [
    "DBScriptProvider",
    "load_all",
    "load_script",
]


class DBScriptProvider(Protocol):
    """Минимальный интерфейс для чтения скриптов из DuckDB-кэша PG-снимка.

    Совместим с любым ``CacheProvider``: ``DuckDbCacheStore`` и
    ``PostgresDuckDbProvider``. Сейчас единственный путь — ``query_sql``
    поверх снимка ``workspace/data_store/duckdb/cache.duckdb``.
    """

    def query_sql(
        self,
        sql: str,
        params: list[Any] | None = None,
    ) -> dict[str, Any]: ...


def _row_to_script(row: dict[str, Any]) -> ScriptDefinition:
    """PG-row → ``ScriptDefinition``.

    Ожидаемые колонки (см. ``sql/audit_analyzer/create_public_agent_predefined_scripts.sql``):
        name, description, returns, long_description, sql_template,
        parameters (JSONB), max_rows_default.

    JSONB-параметры хранятся как ``{param_name: {type, required, default,
    description, validation}}`` (см. ``scripts/predefined/models.py::ParamDefinition``).
    """
    raw_params = row.get("parameters") or {}
    if isinstance(raw_params, str):
        import json as _json

        raw_params = _json.loads(raw_params)

    params: dict[str, ParamDefinition] = {}
    for pname, pdef in raw_params.items():
        if pdef is None:
            # JSONB содержит «канонический» набор параметров: ключи,
            # которые скрипт не использует, хранятся как ``null``. Пропускаем.
            continue
        params[pname] = ParamDefinition(
            type=pdef.get("type", "exact"),
            required=bool(pdef.get("required", False)),
            default=pdef.get("default"),
            description=pdef.get("description", ""),
            validation=pdef.get("validation"),
        )

    return ScriptDefinition(
        name=row["name"],
        description=row.get("description", ""),
        sql_template=row["sql_template"],
        parameters=params,
        max_rows_default=int(row.get("max_rows_default") or 1000),
        returns=row.get("returns", ""),
        long_description=row.get("long_description", ""),
    )


def _qualified_table(table: str) -> tuple[str, str]:
    """``"schema.table"`` → ``(schema, table)`` для SQL f-string."""
    if "." not in table:
        return ("main", table)
    schema, tbl = table.split(".", 1)
    return (schema, tbl)


def load_all(provider: DBScriptProvider, table: str) -> dict[str, ScriptDefinition]:
    """Загрузить все скрипты из PG-реестра через DuckDB-кэш.

    Args:
        provider: ``CacheProvider`` с открытым DuckDB-кэшем.
        table: ``schema.table`` реестра (из
            ``lib.core.skill_config.get_predefined_scripts_table``).

    Returns:
        ``{name: ScriptDefinition}``. Пустой dict при пустой таблице / ошибке.
    """
    schema, tbl = _qualified_table(table)
    sql = (
        f'SELECT name, description, returns, long_description, '
        f'sql_template, parameters, max_rows_default '
        f'FROM "{schema}"."{tbl}" ORDER BY name'
    )
    try:
        result = provider.query_sql(sql)
    except Exception:
        return {}
    if not isinstance(result, dict) or result.get("status") != "success":
        return {}

    out: dict[str, ScriptDefinition] = {}
    for row in result.get("rows") or []:
        try:
            sd = _row_to_script(row)
        except Exception:
            continue
        out[sd.name] = sd
    return out


def load_script(
    provider: DBScriptProvider, table: str, name: str
) -> ScriptDefinition | None:
    """Загрузить один скрипт по имени. ``None`` если не найден."""
    schema, tbl = _qualified_table(table)
    sql = (
        f'SELECT name, description, returns, long_description, '
        f'sql_template, parameters, max_rows_default '
        f'FROM "{schema}"."{tbl}" WHERE name = ?'
    )
    try:
        result = provider.query_sql(sql, [name])
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("status") != "success":
        return None
    rows = result.get("rows") or []
    if not rows:
        return None
    try:
        return _row_to_script(rows[0])
    except Exception:
        return None