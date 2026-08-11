"""
Классы Database и InMemoryDatabase — обёртки над PostgreSQL / DuckDB для навыка audit_analyzer.

Использование (PG):
    from database import Database
    from skill_config import load_db_config
    cfg = load_db_config()
    db = Database(cfg)
    schema = db.get_schema()
    result = db.execute_query("SELECT * FROM oarb.audits LIMIT 5")

Использование (DuckDB in-memory):
    from database import InMemoryDatabase
    from skill_config import load_db_config
    cfg = load_db_config()
    db = InMemoryDatabase(cfg)
    schema = db.get_schema()
    result = db.execute_query("SELECT * FROM audits LIMIT 5")

Загрузка DuckDB-кеша из PostgreSQL (вызывается стартапом агента):
    InMemoryDatabase.load_from_postgres(cache_path, db_config)
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

_workspace_root = Path(__file__).resolve().parents[3]  # workspace/
_nanobot_root = Path(__file__).resolve().parents[4]   # .nanobot/
for _p in [str(_nanobot_root), str(_workspace_root)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import SETTINGS
from utils.db import configure, fetch, resolve_dsn

_pg_dsn = resolve_dsn()
if _pg_dsn:
    configure(_pg_dsn)


class Database:
    """
    Прямое подключение к PostgreSQL через utils.db.

    DSN берётся из resolve_dsn() (channels.postgres.dsn в project.json /
    DATABASE_URL); навык не имеет собственного DSN.
    configure(dsn) должен быть вызван до создания Database.

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
        """
        Выполнить сложный SQL-запрос с JOIN к information_schema и pg_catalog
        для получения структуры таблиц: колонки, типы, NOT/NULL, комментарии.
        Результат собирается в dict {schema, tables: {table: {comment, columns}}}.
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
    # Schema cache (файловый)
    # ------------------------------------------------------------------

    def _cache_file(self) -> Optional[Path]:
        """
        Вернуть Path к файлу кеша схемы или None, если путь не задан.
        """
        return Path(self._cache_path) if self._cache_path else None

    def _cache_log(self, msg: str):
        """
        Вывести сообщение кеша в stderr с префиксом [CACHE].
        """
        print(f"[CACHE] {msg}", file=sys.stderr)

    def _read_cache(self, schema: str, tables: Optional[list[str]]) -> Optional[dict]:
        """
        Прочитать кеш схемы из JSON-файла.
        Проверяет TTL (age > ttl_seconds), совпадение имени схемы,
        и при фильтрации по таблицам — наличие всех запрошенных таблиц.
        Возвращает dict с отфильтрованными данными или None при промахе.
        """
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
        """
        Записать JSON-файл кеша схемы.
        Создаёт родительскую директорию при необходимости.
        Добавляет timestamp cached_at. В случае ошибки выводит предупреждение.
        """
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


# =============================================================================
# InMemoryDatabase — DuckDB-based замена PostgreSQL (только чтение)
# =============================================================================

_REWRITE_TO_CHAR = re.compile(r"TO_CHAR\((\w+)\s*,\s*'Month'\)", re.IGNORECASE)


class InMemoryDatabase:
    """
    DuckDB-based in-memory/disk database.

    Открывает существующий DuckDB-файл для чтения. НЕ подключается к PostgreSQL.
    Кеш должен быть предварительно загружен через load_from_postgres().

    Принимает dict конфигурации из load_db_config() (с секцией in_memory):
        schema           — имя схемы по умолчанию
        tables           — список таблиц для фильтрации (опционально)
        in_memory        — {enabled, cache_path}
    """

    def __init__(self, db_config: dict):
        import duckdb

        self._schema_name = db_config.get("schema", "public")
        self._table_names: Optional[list[str]] = db_config.get("tables") or None

        im_config = db_config.get("in_memory", {})
        cache_path = im_config.get("cache_path", "")
        if not cache_path:
            raise ValueError("in_memory.cache_path is required")

        path = Path(cache_path)
        if not path.exists():
            raise FileNotFoundError(
                f"DuckDB cache not found at {path}. "
                f"Run 'audit_analyze.bat --mode init' or start the agent to preload data."
            )

        self._conn = duckdb.connect(str(path), read_only=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

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
        Получить структуру таблиц из DuckDB information_schema (PG-совместимая).

        Args:
            schema_name: Имя схемы (по умолчанию из конфига).
            table_names: Список таблиц для фильтрации (по умолчанию из конфига).
            use_cache: Игнорируется (кеш схемы не используется — данные уже в DuckDB).

        Returns:
            dict: {"schema": str, "tables": {table: {comment: None, columns: {col: {type, not_null, comment: None}}}}}
        """
        schema = schema_name or self._schema_name
        tables = table_names if table_names is not None else self._table_names

        sql = """
            SELECT table_name, column_name, data_type, is_nullable,
                   character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = ?
        """
        params = [schema]
        if tables:
            placeholders = ",".join("?" for _ in tables)
            sql += f" AND table_name IN ({placeholders})"
            params.extend(tables)
        sql += " ORDER BY table_name, ordinal_position"

        rows = self._conn.execute(sql, params).fetchall()

        result: dict = {}
        for row in rows:
            tbl = row[0]
            if tbl not in result:
                result[tbl] = {"comment": None, "columns": {}}

            col_type = row[2]
            max_len = row[4]
            if max_len and col_type in ("character varying", "character"):
                col_type = f"varchar({max_len})"

            result[tbl]["columns"][row[1]] = {
                "type": col_type,
                "not_null": row[3] == "NO",
                "comment": None,
            }

        return {"schema": schema, "tables": result}

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(self, sql: str, params: Optional[list] = None) -> dict:
        """
        Выполнить SELECT-запрос на DuckDB.

        Автоматически конвертирует %s (psycopg2) в ? (DuckDB)
        и TO_CHAR(date, 'Month') → strftime(date, '%B').

        Returns:
            dict: {status, row_count, columns, rows}
        """
        duck_sql = sql.replace("%s", "?")
        duck_sql = _REWRITE_TO_CHAR.sub(r"strftime(\1, '%B')", duck_sql)

        try:
            if params:
                result = self._conn.execute(duck_sql, params)
            else:
                result = self._conn.execute(duck_sql)
        except Exception as e:
            return {"status": "error", "row_count": 0, "columns": [], "rows": [],
                    "error": f"Ошибка выполнения запроса: {e}"}

        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()

        if not rows:
            return {"status": "success", "row_count": 0, "columns": columns, "rows": []}

        return {
            "status": "success",
            "row_count": len(rows),
            "columns": columns,
            "rows": [dict(zip(columns, r)) for r in rows],
        }

    def execute_explain(self, sql: str) -> dict:
        """
        EXPLAIN на DuckDB.

        Returns:
            {"valid": True, "plan": [...]} или {"valid": False, "error": "..."}
        """
        try:
            duck_sql = sql.replace("%s", "?")
            duck_sql = _REWRITE_TO_CHAR.sub(r"strftime(\1, '%B')", duck_sql)
            result = self._conn.execute(f"EXPLAIN {duck_sql}")
            columns = [desc[0] for desc in result.description]
            plan = [dict(zip(columns, r)) for r in result.fetchall()]
            return {"valid": True, "plan": plan}
        except Exception as e:
            return {"valid": False, "error": f"EXPLAIN failed: {e}"}

    # ------------------------------------------------------------------
    # Static helpers (те же, что в Database)
    # ------------------------------------------------------------------

    @staticmethod
    def validate_sql(sql: str) -> Optional[str]:
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

    # ------------------------------------------------------------------
    # Загрузка кеша (вызывается стартапом агента, не навыком)
    # ------------------------------------------------------------------

    @staticmethod
    def _store_cache_meta(conn, pg_conn, schema: str, table_list: list[str]) -> None:
        """
        Сохранить метаданные кеша: MAX(updated_at) для каждой таблицы.
        """
        conn.execute("DROP TABLE IF EXISTS __cache_meta")
        meta_rows = []
        for tbl in table_list:
            cur = pg_conn.cursor()
            try:
                cur.execute(f'SELECT MAX(updated_at) FROM "{schema}"."{tbl}"')
                max_ts = cur.fetchone()[0]
                meta_rows.append({"table_name": tbl, "max_updated_at": str(max_ts) if max_ts else None})
            except Exception:
                meta_rows.append({"table_name": tbl, "max_updated_at": None})
            finally:
                cur.close()

        import pandas as pd
        df = pd.DataFrame(meta_rows)
        conn.register("__meta_df", df)
        conn.execute("CREATE TABLE __cache_meta AS SELECT * FROM __meta_df")
        conn.unregister("__meta_df")

    @staticmethod
    def load_from_postgres(cache_path: str, db_config: dict) -> None:
        """
        Подключиться к PostgreSQL, скопировать все таблицы в DuckDB-файл.

        Это единственное место, где навык коннектится к PG.
        Вызывается из cli_agent.py / gateway.py (--mode init) при старте агента.

        Args:
            cache_path: Абсолютный путь к DuckDB-файлу.
            db_config: dict из load_db_config().
        """
        import duckdb
        import psycopg2
        from utils.db import configure

        schema = db_config.get("schema", "public")
        tables = db_config.get("tables")

        dsn = resolve_dsn()
        if not dsn:
            raise RuntimeError("DSN is not configured")

        configure(dsn)

        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = duckdb.connect(str(path))
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

        pg_conn = psycopg2.connect(dsn)
        pg_conn.autocommit = True

        try:
            table_list = tables or InMemoryDatabase._discover_tables(pg_conn, schema)

            for tbl in table_list:
                full_name = f"{schema}.{tbl}"
                print(f"[LOAD] Copying {full_name}...", file=sys.stderr)

                cur = pg_conn.cursor()
                cur.execute(f"SELECT * FROM {full_name}")
                pg_rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description]
                cur.close()

                conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{tbl}"')

                if pg_rows:
                    import pandas as pd
                    df = pd.DataFrame(pg_rows, columns=columns)
                    conn.register("_df_temp", df)
                    conn.execute(f'CREATE TABLE "{schema}"."{tbl}" AS SELECT * FROM _df_temp')
                    conn.unregister("_df_temp")
                    count = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{tbl}"').fetchone()[0]
                    print(f"[LOAD]  {count} rows loaded into {tbl}", file=sys.stderr)
                else:
                    col_defs = ", ".join(f'"{c}" VARCHAR' for c in columns)
                    conn.execute(f'CREATE TABLE "{schema}"."{tbl}" ({col_defs})')
                    print(f"[LOAD]  Table {tbl} created (empty)", file=sys.stderr)

            InMemoryDatabase._store_cache_meta(conn, pg_conn, schema, table_list)
            print(f"[LOAD] Cache saved to {cache_path}", file=sys.stderr)

        finally:
            pg_conn.close()
            conn.close()

    @staticmethod
    def check_stale(cache_path: str, db_config: dict) -> dict:
        """
        Проверить, устарел ли кеш, сравнив MAX(updated_at) с PostgreSQL.

        Args:
            cache_path: Абсолютный путь к DuckDB-файлу.
            db_config: dict из load_db_config().

        Returns:
            {"fresh": bool, "stale_tables": [str], "cache_meta": {...}, "pg_meta": {...}}
        """
        import duckdb
        import psycopg2
        from utils.db import configure

        schema = db_config.get("schema", "public")
        path = Path(cache_path)
        if not path.exists():
            return {"fresh": False, "stale_tables": [], "cache_meta": {}, "pg_meta": {},
                    "error": "Cache file not found"}

        cache_conn = duckdb.connect(str(path), read_only=True)
        try:
            meta_rows = cache_conn.execute(
                "SELECT table_name, max_updated_at FROM __cache_meta ORDER BY table_name"
            ).fetchall()
            cache_meta = {r[0]: r[1] for r in meta_rows}
        except Exception:
            cache_conn.close()
            return {"fresh": False, "stale_tables": [], "cache_meta": {}, "pg_meta": {},
                    "error": "No cache metadata found"}
        cache_conn.close()

        dsn = resolve_dsn()
        if not dsn:
            return {"fresh": False, "stale_tables": [], "cache_meta": cache_meta, "pg_meta": {},
                    "error": "DSN is not configured"}

        configure(dsn)

        pg_conn = psycopg2.connect(dsn)
        try:
            pg_meta = {}
            stale = []
            for tbl in cache_meta:
                cur = pg_conn.cursor()
                try:
                    cur.execute(f'SELECT MAX(updated_at) FROM "{schema}"."{tbl}"')
                    max_ts = cur.fetchone()[0]
                    pg_meta[tbl] = str(max_ts) if max_ts else None
                    cached = cache_meta[tbl]
                    if cached != pg_meta[tbl]:
                        stale.append(tbl)
                except Exception:
                    pg_meta[tbl] = None
                    stale.append(tbl)
                finally:
                    cur.close()
        finally:
            pg_conn.close()

        return {
            "fresh": len(stale) == 0,
            "stale_tables": stale,
            "cache_meta": cache_meta,
            "pg_meta": pg_meta,
        }

    @staticmethod
    def _discover_tables(pg_conn, schema: str) -> list[str]:
        cur = pg_conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
            "ORDER BY table_name",
            [schema],
        )
        tables = [r[0] for r in cur.fetchall()]
        cur.close()
        return tables
