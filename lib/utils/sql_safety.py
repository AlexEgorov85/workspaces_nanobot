"""SQL safety helpers used by read-only tools and by skills that generate SQL.

Контракт validate_sql/format_schema совместим с ранее существовавшими
функциями в ``workspace/skills/audit_analyzer/scripts/database.py``,
вынесен сюда для переиспользования из ``workspace/tools/duckdb_query_tool.py``
и из skill'ов без cross-import'ов.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["validate_sql", "format_schema"]


_DDL_DML_FIRST_WORDS: frozenset[str] = frozenset({
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "EXECUTE",
    "CALL",
    "MERGE",
    "REPLACE",
})


def validate_sql(sql: str) -> Optional[str]:
    """Проверить SQL на безопасность: только SELECT-подобные, один statement.

    Args:
        sql: исходный SQL (любой регистр).

    Returns:
        None если SQL безопасен, иначе строка с описанием причины отказа.
    """
    stripped = sql.strip().upper()
    if not stripped:
        return "SQL query is empty"

    first_word = stripped.split(maxsplit=1)[0] if stripped else ""
    if first_word in _DDL_DML_FIRST_WORDS:
        return f"DML/DDL statements are not allowed: {first_word}"

    if stripped.count(";") > 1:
        return "Multiple SQL statements are not allowed"

    return None


def format_schema(schema: dict) -> str:
    """Преобразовать схему БД в человекочитаемый формат для LLM-промпта.

    Структура ``schema``::

        {
            "schema": "oarb",
            "tables": {
                "audits": {
                    "comment": "Аудиторские проверки",
                    "columns": {
                        "id": {"type": "integer", "not_null": True, "comment": "ID"},
                        "title": {"type": "varchar(500)", "not_null": False},
                    },
                },
            },
        }
    """
    schema_name = schema.get("schema", "?")
    parts: list[str] = [f"=== Schema: {schema_name} ===", ""]
    for tbl, info in schema.get("tables", {}).items():
        comment = info.get("comment") or ""
        parts.append(f'Table: "{schema_name}".{tbl} — {comment}')
        for col, cinfo in info.get("columns", {}).items():
            nn = " NOT NULL" if cinfo.get("not_null") else ""
            col_comment = cinfo.get("comment") or ""
            if col_comment:
                parts.append(f"  {col}: {cinfo['type']}{nn} — {col_comment}")
            else:
                parts.append(f"  {col}: {cinfo['type']}{nn}")
        parts.append("")
    return "\n".join(parts)