"""
Класс Database — обёртка над PostgreSQL для навыка db_analyzer.

Использование:
    from database import Database
    from config import load_db_config

    cfg = load_db_config()
    db = Database(cfg)
    schema = db.get_schema()
    result = db.execute_query("SELECT * FROM oarb.audits LIMIT 5")
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

_project_root = Path(__file__).resolve().parents[3]  # workspace/
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from utils.db import configure, fetch

_dsn_env = os.environ.get("DATABASE_URL", "")
if _dsn_env:
    configure(_dsn_env)


class Database:
    """
    Прямое подключение к PostgreSQL через utils.db.

    DSN задаётся в gateway_settings.py (pg.dsn) — навык не имеет
    собственного DSN. configure(dsn) должен быть вызван до создания Database.

    Принимает dict конфигурации из load_db_config():
        schema           — имя схемы по умолчанию
        tables           — список таблиц для фильтрации (опционально)
        schema_cache     — настройки кеша: enabled, path, ttl_seconds
    """

    def __init__(self, db_config: dict):
        self._schema_name = db_config.get("schema", "public")
        self._table_names: Optional[list[str]] = db_config.get("tables") or None

        cache_cfg = db_config.get("schema_cache", {})
        self._cache_enabled = bool(cache_cfg.get("enabled", False))
        self._cache_path: Optional[str] = cache_cfg.get("path") or None
        self._cache_ttl = int(cache_cfg.get("ttl_seconds", 3600))

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
        use_cache: Optional[bool] = None,
    ) -> dict:
        """
        Получить структуру таблиц из information_schema.

        Args:
            schema_name: Имя схемы (по умолчанию из конфига).
            table_names: Список таблиц для фильтрации (по умолчанию из конфига).
            use_cache: Использовать файловый кеш (по умолчанию из конфига).

        Returns:
            dict: {"schema": str, "tables": {table: {comment, columns: {col: {type, not_null, comment}}}}}
        """
        schema = schema_name or self._schema_name
        tables = table_names if table_names is not None else self._table_names
        cache = use_cache if use_cache is not None else self._cache_enabled

        if cache:
            cached = self._read_cache(schema, tables)
            if cached:
                return cached

        data = self._fetch_schema(schema, tables)

        if cache:
            self._write_cache(schema, data)

        return data

    def _fetch_schema(self, schema: str, tables: Optional[list[str]]) -> dict:
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
            LEFT JOIN pg_catalog.pg_description pgd
                ON pgd.objsubid = c.ordinal_position
               AND pgd.objoid = pc.oid
            WHERE c.table_schema = %s
        """
        params: list[Any] = [schema]

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
    # Schema cache (файловый)
    # ------------------------------------------------------------------

    def _cache_file(self) -> Optional[Path]:
        return Path(self._cache_path) if self._cache_path else None

    def _cache_log(self, msg: str):
        print(f"[CACHE] {msg}", file=sys.stderr)

    def _read_cache(self, schema: str, tables: Optional[list[str]]) -> Optional[dict]:
        path = self._cache_file()
        if not path:
            self._cache_log("кеш отключён (нет пути)")
            return None
        if not path.exists():
            self._cache_log(f"{path.name}: miss (файл не найден)")
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            self._cache_log(f"{path.name}: miss (ошибка чтения: {e})")
            return None

        if data.get("schema") != schema:
            self._cache_log(f"{path.name}: miss (схема '{data.get('schema')}' не совпадает с '{schema}')")
            return None

        age = time.time() - data.get("cached_at", 0)
        if age > self._cache_ttl:
            self._cache_log(f"{path.name}: miss (просрочен TTL: {age:.0f}s > {self._cache_ttl}s)")
            return None

        cached_tables = data.get("tables", {})
        if tables:
            if not all(t in cached_tables for t in tables):
                missing = [t for t in tables if t not in cached_tables]
                self._cache_log(f"{path.name}: miss (нет таблиц в кеше: {missing})")
                return None
            filtered = {t: cached_tables[t] for t in tables if t in cached_tables}
            ncols = sum(len(c["columns"]) for c in filtered.values())
            self._cache_log(f"{path.name}: hit ({len(filtered)} таблиц, {ncols} колонок, возраст {age:.0f}с)")
            return {"schema": schema, "tables": filtered}

        ncols = sum(len(c["columns"]) for c in cached_tables.values())
        self._cache_log(f"{path.name}: hit ({len(cached_tables)} таблиц, {ncols} колонок, возраст {age:.0f}с)")
        return {"schema": schema, "tables": cached_tables}

    def _write_cache(self, schema: str, schema_data: dict):
        path = self._cache_file()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {**schema_data, "cached_at": time.time()}
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            ncols = sum(len(c["columns"]) for c in data.get("tables", {}).values())
            self._cache_log(f"{path.name}: записан ({len(data.get('tables', {}))} таблиц, {ncols} колонок)")
        except OSError as e:
            self._cache_log(f"{path.name}: ошибка записи: {e}")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(self, sql: str, params: Optional[list] = None) -> dict:
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

    def execute_explain(self, sql: str) -> dict:
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
        Преобразовать схему БД в промпт для LLM.

        Показывает для каждой колонки:
          - тип (с длиной для varchar)
          - NOT/NULL
          - комментарий (если есть)

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
