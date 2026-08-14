"""
Класс Database — обёртка над PostgreSQL для навыка audit_analyzer (fallback).

.. deprecated:: 2.0
    Навык работает через DuckDB-кэш (lib.services.cache_provider_impl).
    Прямой psycopg2-доступ остался как legacy-фоллбэк, когда
    ``in_memory_enabled: false`` в project.json. Не используйте в новом коде.

Инфраструктура (DuckDB-кэш, векторные индексы) вынесена в универсальный
слой lib/services (CacheProvider, см. cache_provider_impl.py). CLI навыка
использует провайдера напрямую; Database остаётся как прямой доступ к
PostgreSQL, когда in-memory кэш выключен.

Оба бэкенда реализуют единый интерфейс QueryBackend:
    get_schema(schema_name, table_names) -> dict
    query_sql(sql, params) -> dict
    explain(sql) -> dict
что позволяет mode-модулям (predefined/sql) работать с любым из них.

Использование (PG):
    from database import Database
    from skill_config import load_db_config
    db = Database(load_db_config())
    schema = db.get_schema()
    result = db.query_sql("SELECT * FROM oarb.audits LIMIT 5")
"""

import re
import sys
import warnings
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

_workspace_root = Path(__file__).resolve().parents[3]  # workspace/
_nanobot_root = Path(__file__).resolve().parents[4]   # корень проекта
for _p in [str(_nanobot_root), str(_workspace_root)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import SETTINGS
from utils.db import configure, fetch, resolve_dsn

warnings.warn(
    "database.Database is deprecated; use CacheProvider (lib.services) instead. "
    "The skill now reads everything from DuckDB cache.",
    DeprecationWarning,
    stacklevel=2,
)

_pg_dsn = resolve_dsn()
if _pg_dsn:
    configure(_pg_dsn)

# DuckDB не поддерживает TO_CHAR(date, 'Month') — переписываем в strftime.
_REWRITE_TO_CHAR = re.compile(r"TO_CHAR\((\w+)\s*,\s*'Month'\)", re.IGNORECASE)


@runtime_checkable
class QueryBackend(Protocol):
    """Единый интерфейс бэкенда запросов (PostgreSQL напрямую или DuckDB-кэш)."""

    def get_schema(
        self,
        schema_name: Optional[str] = None,
        table_names: Optional[list[str]] = None,
    ) -> dict: ...

    def query_sql(self, sql: str, params: Optional[list] = None) -> dict: ...

    def explain(self, sql: str) -> dict: ...


def validate_sql(sql: str) -> Optional[str]:
    """
    Проверить SQL на безопасность: только SELECT, один statement.

    Returns:
        None если всё в порядке, str с ошибкой иначе.
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
    Преобразовать схему БД в промпт для LLM.

    Пример:
        === Schema: oarb ===

        Table: audits — Аудиторские проверки
          id: integer NOT NULL — Идентификатор
          actual_date: date — Дата проверки
          title: varchar(500) — Название проверки
    """
    schema_name = schema.get("schema", "?")
    parts: list[str] = [f"=== Schema: {schema_name} ===", ""]
    for tbl, info in schema.get("tables", {}).items():
        comment = info.get("comment") or ""
        parts.append(f"Table: \"{schema_name}\".{tbl} — {comment}")
        for col, cinfo in info.get("columns", {}).items():
            nn = " NOT NULL" if cinfo.get("not_null") else ""
            col_comment = cinfo.get("comment") or ""
            if col_comment:
                parts.append(f"  {col}: {cinfo['type']}{nn} — {col_comment}")
            else:
                parts.append(f"  {col}: {cinfo['type']}{nn}")
        parts.append("")
    return "\n".join(parts)


class Database(QueryBackend):
    """
    Прямое подключение к PostgreSQL через utils.db.

    DSN берётся из resolve_dsn() (channels.postgres.dsn в project.json /
    DATABASE_URL); навык не имеет собственного DSN.
    configure(dsn) должен быть вызван до создания Database.

    Принимает dict конфигурации из load_db_config():
        schema           — имя схемы по умолчанию
        tables           — список таблиц для фильтрации (опционально)
    """

    def __init__(self, db_config: dict):
        self._schema_name = db_config.get("schema", "public")
        self._table_names: Optional[list[str]] = db_config.get("tables") or None

    # ------------------------------------------------------------------
    # Lifecycle (no-op, подключение через utils.db)
    # ------------------------------------------------------------------

    def connect(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def get_schema(
        self,
        schema_name: Optional[str] = None,
        table_names: Optional[list[str]] = None,
    ) -> dict:
        """
        Получить структуру таблиц из information_schema.

        Args:
            schema_name: Имя схемы (по умолчанию из конфига).
            table_names: Список таблиц для фильтрации (по умолчанию из конфига).

        Returns:
            dict: {"schema": str, "tables": {table: {comment, columns: {col: {type, not_null, comment}}}}}
        """
        schema = schema_name or self._schema_name
        tables = table_names if table_names is not None else self._table_names
        return self._fetch_schema(schema, tables)

    def _fetch_schema(self, schema: str, tables: Optional[list[str]]) -> dict:
        """
        Выполнить сложный SQL-запрос с JOIN к information_schema и pg_catalog
        для получения структуры таблиц: колонки, типы, NOT/NULL, комментарии.
        """
        query = """
            SELECT
                c.table_name,
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.character_maximum_length,
                pgd.description AS column_comment,
                obj_description(pc.oid) AS table_comment
            FROM information_schema.columns c
            JOIN pg_class pc ON pc.relname = c.table_name
                AND pc.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s)
            LEFT JOIN pg_catalog.pg_description pgd
                ON pgd.objsubid = c.ordinal_position
               AND pgd.objoid = pc.oid
            WHERE c.table_schema = %s
        """
        params: list[Any] = [schema, schema]

        if tables:
            query += " AND c.table_name = ANY(%s)"
            params.append(tables)

        query += " ORDER BY c.table_name, c.ordinal_position"

        rows = fetch(query, *params)

        result: dict = {}
        for row in rows:
            tbl = row["table_name"]
            if tbl not in result:
                result[tbl] = {"comment": row["table_comment"], "columns": {}}

            col_type = row["data_type"]
            max_len = row["character_maximum_length"]
            if max_len and col_type in ("character varying", "character"):
                col_type = f"varchar({max_len})"

            result[tbl]["columns"][row["column_name"]] = {
                "type": col_type,
                "not_null": row["is_nullable"] == "NO",
                "comment": row["column_comment"],
            }

        return {"schema": schema, "tables": result}

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def query_sql(self, sql: str, params: Optional[list] = None) -> dict:
        """
        Выполнить SELECT-запрос, вернуть колонки и строки.

        Returns:
            dict: {status, row_count, columns, rows}
        """
        try:
            rows = fetch(sql, *(params or []))
        except Exception as e:
            return {"status": "error", "row_count": 0, "columns": [], "rows": [],
                    "error": f"Ошибка выполнения запроса: {e}"}

        if not rows:
            return {"status": "success", "row_count": 0, "columns": [], "rows": []}

        columns = list(rows[0].keys())
        return {
            "status": "success",
            "row_count": len(rows),
            "columns": columns,
            "rows": [dict(r) for r in rows],
        }

    def explain(self, sql: str) -> dict:
        """
        EXPLAIN (FORMAT JSON) — проверка синтаксиса без выполнения.

        Returns:
            {"valid": True, "plan": [...]} или {"valid": False, "error": "..."}
        """
        explain_sql = f"EXPLAIN (FORMAT JSON) {sql}"
        try:
            rows = fetch(explain_sql)
            plan = list(rows[0].values())[0] if rows else None
            return {"valid": True, "plan": plan}
        except Exception as e:
            return {"valid": False, "error": f"EXPLAIN failed: {e}"}