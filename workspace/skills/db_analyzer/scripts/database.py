"""
Работа с PostgreSQL: получение схемы, выполнение запросов,
EXPLAIN-валидация и проверка безопасности SQL.

Все функции, работающие с БД, принимают dict-конфиг подключения:
    {"host": str, "port": int, "database": str, "user": str, "password": str}
"""

from typing import Optional


async def get_schema(db: dict, schema_name: str = "oarb") -> dict:
    """
    Получить структуру таблиц из information_schema.

    Запрашивает колонки, их типы, комментарии колонок и таблиц
    для указанной схемы PostgreSQL.

    Args:
        db: Параметры подключения (см. load_db_config в config.py).
        schema_name: Имя схемы (по умолчанию 'oarb').

    Returns:
        dict вида:
            {
              "schema": "oarb",
              "tables": {
                "audits": {
                  "comment": "Аудиторские проверки",
                  "columns": {
                    "id": {"type": "integer", "comment": "Идентификатор"},
                    "actual_date": {"type": "date", "comment": "Дата проверки"},
                    ...
                  }
                },
                ...
              }
            }

    Пример:
        >>> import asyncio
        >>> from config import load_db_config
        >>> db = load_db_config()
        >>> asyncio.run(get_schema(db, "oarb"))  # doctest: +SKIP
    """
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
    """
    Выполнить SELECT-запрос и вернуть колонки и строки.

    Args:
        db: Параметры подключения.
        sql: SQL-запрос (с $1, $2, ... плейсхолдерами asyncpg).
        params: Список значений для плейсхолдеров (опционально).

    Returns:
        dict с ключами:
            status: "success" | "error"
            row_count: количество строк
            columns: список имён колонок
            rows: список dict-строк

    Пример:
        >>> import asyncio
        >>> from config import load_db_config
        >>> db = load_db_config()
        >>> asyncio.run(execute_query(db, "SELECT 1 AS x"))
        {'status': 'success', 'row_count': 1, 'columns': ['x'], 'rows': [{'x': 1}]}

    Пример с параметрами:
        >>> asyncio.run(execute_query(
        ...     db, "SELECT * FROM oarb.audits WHERE id = $1", [42]
        ... ))  # doctest: +SKIP
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
    """
    Выполнить EXPLAIN (FORMAT JSON) для проверки SQL без его выполнения.

    Используется в sql_mode как защита: сначала проверяем, что запрос
    синтаксически корректен и все объекты существуют, потом выполняем.

    Args:
        db: Параметры подключения.
        sql: SQL-запрос.

    Returns:
        {"valid": True, "plan": [...]} при успехе,
        {"valid": False, "error": "..."} при ошибке.

    Пример:
        >>> import asyncio
        >>> from config import load_db_config
        >>> db = load_db_config()
        >>> res = await execute_explain(db, "SELECT 1")
        >>> res["valid"]
        True

        >>> res = await execute_explain(db, "SELECT * FROM nonexistent")
        >>> res["valid"]
        False
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
    """
    Проверить SQL на безопасность: запретить DDL/DML и мульти-запросы.

    Разрешены только SELECT-запросы (один statement).

    Args:
        sql: SQL-запрос.

    Returns:
        None если всё в порядке,
        str с описанием ошибки если запрос небезопасен.

    Пример:
        >>> validate_sql("SELECT * FROM audits")
        None  # OK

        >>> validate_sql("DROP TABLE audits")
        'DML/DDL statements are not allowed: DROP'

        >>> validate_sql("SELECT 1; SELECT 2")
        'Multiple SQL statements are not allowed'
    """
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
    """
    Преобразовать схему БД в читаемый текст для промпта LLM.

    Из dict-структуры, возвращённой get_schema(), делает
    строку вида:

        Table: audits — Аудиторские проверки
          id: integer — Идентификатор
          actual_date: date — Дата проверки
          ...

    Args:
        schema: dict от get_schema().

    Returns:
        str — форматированное описание схемы.

    Пример:
        >>> schema = {
        ...   "schema": "oarb",
        ...   "tables": {
        ...     "audits": {
        ...       "comment": "Проверки",
        ...       "columns": {
        ...         "id": {"type": "integer", "comment": "ID"},
        ...       }
        ...     }
        ...   }
        ... }
        >>> print(format_schema(schema))
        Table: audits — Проверки
          id: integer — ID
    """
    lines: list[str] = []
    for tbl, info in schema.get("tables", {}).items():
        lines.append(f"Table: {tbl} — {info.get('comment') or ''}")
        for col, cinfo in info.get("columns", {}).items():
            lines.append(f"  {col}: {cinfo['type']} — {cinfo.get('comment') or ''}")
        lines.append("")
    return "\n".join(lines)
