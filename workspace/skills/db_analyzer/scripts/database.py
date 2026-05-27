"""
Класс Database — подключение к PostgreSQL с пулом соединений,
кешированием схемы и фильтрацией таблиц.

Использование:
    from database import Database
    from config import load_db_config

    cfg = load_db_config()
    async with Database(cfg) as db:
        schema = await db.get_schema()
        result = await db.execute_query("SELECT * FROM oarb.audits LIMIT $1", [5])
"""

import json
import time
from pathlib import Path
from typing import Any, Optional

import asyncpg


class Database:
    """
    Подключение к PostgreSQL через пул соединений.

    Принимает dict конфигурации из load_db_config():
        connection_string — DSN для asyncpg (используется напрямую, без парсинга)
        schema           — имя схемы по умолчанию
        tables           — список таблиц для фильтрации (опционально)
        schema_cache     — настройки кеша: enabled, path, ttl_seconds
    """

    def __init__(self, db_config: dict):
        self._dsn = db_config.get("connection_string", "")
        self._schema_name = db_config.get("schema", "public")
        self._table_names: Optional[list[str]] = db_config.get("tables") or None
        self._pool: Optional[asyncpg.Pool] = None

        cache_cfg = db_config.get("schema_cache", {})
        self._cache_enabled = bool(cache_cfg.get("enabled", False))
        self._cache_path: Optional[str] = cache_cfg.get("path") or None
        self._cache_ttl = int(cache_cfg.get("ttl_seconds", 3600))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self):
        """Создать пул соединений к PostgreSQL."""
        if not self._dsn:
            raise ValueError("Database connection string is empty")
        self._pool = await asyncpg.create_pool(dsn=self._dsn, min_size=1, max_size=2)

    async def close(self):
        """Закрыть пул соединений."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def get_schema(
        self,
        schema_name: Optional[str] = None,
        table_names: Optional[list[str]] = None,
        use_cache: Optional[bool] = None,
    ) -> dict:
        """
        Получить структуру таблиц из information_schema.

        Args:
            schema_name: Имя схемы (по умолчанию из конфига).
            table_names: Список таблиц для фильтрации (по умолчанию из конфига).
            use_cache: Использовать файловый кеш (по умолчанию из конфига).

        Returns:
            dict: {"schema": str, "tables": {table: {comment, columns: {col: {type, comment}}}}}
        """
        schema = schema_name or self._schema_name
        tables = table_names if table_names is not None else self._table_names
        cache = use_cache if use_cache is not None else self._cache_enabled

        if cache:
            cached = self._read_cache(schema, tables)
            if cached:
                return cached

        data = await self._fetch_schema(schema, tables)

        if cache:
            self._write_cache(schema, data)

        return data

    async def _fetch_schema(self, schema: str, tables: Optional[list[str]]) -> dict:
        if not self._pool:
            raise RuntimeError("Database not connected. Call connect() first.")

        query = """
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
        """
        params: list[Any] = [schema]

        if tables:
            query += " AND c.table_name = ANY($2::text[])"
            params.append(tables)

        query += " ORDER BY c.table_name, c.ordinal_position"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        result: dict = {}
        for row in rows:
            tbl = row["table_name"]
            if tbl not in result:
                result[tbl] = {"comment": row["table_comment"], "columns": {}}
            result[tbl]["columns"][row["column_name"]] = {
                "type": row["data_type"],
                "comment": row["column_comment"],
            }

        return {"schema": schema, "tables": result}

    # ------------------------------------------------------------------
    # Schema cache (файловый)
    # ------------------------------------------------------------------

    def _cache_file(self) -> Optional[Path]:
        return Path(self._cache_path) if self._cache_path else None

    def _read_cache(self, schema: str, tables: Optional[list[str]]) -> Optional[dict]:
        path = self._cache_file()
        if not path or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if data.get("schema") != schema:
            return None
        if time.time() - data.get("cached_at", 0) > self._cache_ttl:
            return None

        cached_tables = data.get("tables", {})
        if tables:
            if not all(t in cached_tables for t in tables):
                return None
            filtered = {t: cached_tables[t] for t in tables if t in cached_tables}
            return {"schema": schema, "tables": filtered}

        return {"schema": schema, "tables": cached_tables}

    def _write_cache(self, schema: str, schema_data: dict):
        path = self._cache_file()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {**schema_data, "cached_at": time.time()}
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    async def execute_query(self, sql: str, params: Optional[list] = None) -> dict:
        """
        Выполнить SELECT-запрос, вернуть колонки и строки.

        Returns:
            dict: {status, row_count, columns, rows}
        """
        if not self._pool:
            raise RuntimeError("Database not connected. Call connect() first.")

        async with self._pool.acquire() as conn:
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

    async def execute_explain(self, sql: str) -> dict:
        """
        EXPLAIN (FORMAT JSON) — проверка синтаксиса без выполнения.

        Returns:
            {"valid": True, "plan": [...]} или {"valid": False, "error": "..."}
        """
        if not self._pool:
            raise RuntimeError("Database not connected. Call connect() first.")

        explain_sql = f"EXPLAIN (FORMAT JSON) {sql}"
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(explain_sql)
            plan = rows[0][0] if rows else None
            return {"valid": True, "plan": plan}
        except asyncpg.PostgresError as e:
            return {"valid": False, "error": str(e)}
        except Exception as e:
            return {"valid": False, "error": f"EXPLAIN failed: {e}"}

    # ------------------------------------------------------------------
    # Static helpers (не требуют подключения)
    # ------------------------------------------------------------------

    @staticmethod
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

    @staticmethod
    def format_schema(schema: dict) -> str:
        """
        Преобразовать схему БД в читаемый текст для промпта LLM.

        Пример:
            Table: audits — Аудиторские проверки
              id: integer — Идентификатор
        """
        lines: list[str] = []
        for tbl, info in schema.get("tables", {}).items():
            lines.append(f"Table: {tbl} — {info.get('comment') or ''}")
            for col, cinfo in info.get("columns", {}).items():
                lines.append(f"  {col}: {cinfo['type']} — {cinfo.get('comment') or ''}")
            lines.append("")
        return "\n".join(lines)
