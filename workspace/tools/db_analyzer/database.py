"""Работа с PostgreSQL: схема, выполнение запросов, валидация SQL."""

from typing import Optional


async def get_schema(db: dict, schema_name: str = "oarb") -> dict:
    """Получить структуру таблиц из information_schema."""
    import asyncpg

    conn = await asyncpg.connect(**db)
    try:
        rows = await conn.fetch(
            """
            SELECT
                c.table_name,
                c.column_name,
                c.data_type,
                pgd.description AS column_comment,
                obj_description(pc.oid) AS table_comment
            FROM information_schema.columns c
            JOIN pg_class pc ON pc.relname = c.table_name
            LEFT JOIN pg_catalog.pg_description pgd
                ON pgd.objsubid = c.ordinal_position
               AND pgd.objoid = pc.oid
            WHERE c.table_schema = $1
            ORDER BY c.table_name, c.ordinal_position
            """,
            schema_name,
        )
        tables: dict = {}
        for row in rows:
            tbl = row["table_name"]
            if tbl not in tables:
                tables[tbl] = {"comment": row["table_comment"], "columns": {}}
            tables[tbl]["columns"][row["column_name"]] = {
                "type": row["data_type"],
                "comment": row["column_comment"],
            }
        return {"schema": schema_name, "tables": tables}
    finally:
        await conn.close()


async def execute_query(db: dict, sql: str, params: Optional[list] = None) -> dict:
    """Выполнить SELECT и вернуть колонки + строки.

    Args:
        db: Параметры подключения (host, port, user, password, database).
        sql: SQL-запрос с $1, $2, ... плейсхолдерами.
        params: Значения для плейсхолдеров (опционально).
    """
    import asyncpg

    conn = await asyncpg.connect(**db)
    try:
        if params:
            rows = await conn.fetch(sql, *params)
        else:
            rows = await conn.fetch(sql)
        if not rows:
            return {"status": "success", "row_count": 0, "columns": [], "rows": []}
        columns = list(rows[0].keys())
        return {
            "status": "success",
            "row_count": len(rows),
            "columns": columns,
            "rows": [dict(r) for r in rows],
        }
    finally:
        await conn.close()


async def execute_explain(db: dict, sql: str) -> dict:
    """Выполнить EXPLAIN (FORMAT JSON) для проверки корректности SQL без выполнения.

    Returns:
        {"valid": True, "plan": ...} или {"valid": False, "error": "..."}
    """
    import asyncpg

    conn = await asyncpg.connect(**db)
    try:
        explain_sql = f"EXPLAIN (FORMAT JSON) {sql}"
        rows = await conn.fetch(explain_sql)
        plan = rows[0][0] if rows else None
        return {"valid": True, "plan": plan}
    except asyncpg.PostgresError as e:
        return {"valid": False, "error": str(e)}
    except Exception as e:
        return {"valid": False, "error": f"EXPLAIN failed: {e}"}
    finally:
        await conn.close()


def validate_sql(sql: str) -> Optional[str]:
    """Запретить DDL/DML и мульти-запросы."""
    stripped = sql.strip().upper()
    if not stripped:
        return "SQL query is empty"
    first_word = stripped.split(maxsplit=1)[0] if stripped else ""
    ddl = {
        "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
        "TRUNCATE", "EXECUTE", "CALL", "MERGE", "REPLACE",
    }
    if first_word in ddl:
        return f"DML/DDL statements are not allowed: {first_word}"
    if stripped.count(";") > 1:
        return "Multiple SQL statements are not allowed"
    return None


def format_schema(schema: dict) -> str:
    """Schema → читаемый текст для промпта LLM."""
    lines: list[str] = []
    for tbl, info in schema.get("tables", {}).items():
        lines.append(f"Table: {tbl} — {info.get('comment') or ''}")
        for col, cinfo in info.get("columns", {}).items():
            lines.append(f"  {col}: {cinfo['type']} — {cinfo.get('comment') or ''}")
        lines.append("")
    return "\n".join(lines)
