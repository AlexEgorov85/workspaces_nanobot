"""
Реализация CacheProvider — универсальный инфраструктурный слой поверх
канонической PostgreSQL, локального SQL-кэша (DuckDB-файл) и векторных
индексов (FAISS).

Не завязана на предметную область: таблицы/схема/индексные источники
передаются в конструктор, методы работают с любыми данными.

Состав (логика выделена из навыка audit_analyzer в универсальный слой):
  * создание/обновление SQL-кэша из PostgreSQL      (load_cache_from_postgres)
  * проверка устаревания кэша                        (check_cache_stale)
  * чтение схемы и SQL-запросы к кэшу                (query_sql / get_schema)
  * прогрев векторных индексов в память              (preload_indexes)
  * семантический поиск по индексам                  (search_vector)

Тяжёлые зависимости (duckdb, psycopg2, faiss, numpy, pandas, httpx)
импортируются лениво внутри методов, чтобы импорт модуля оставался лёгким
и gateway мог управлять жизненным циклом без побочных эффектов.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Пути к проекту и workspace — чтобы `from utils.db import ...` работал
# независимо от рабочего каталога.
_ROOT = Path(__file__).resolve().parents[2]        # .nanobot/
_WORKSPACE = _ROOT / "workspace"
for _p in (str(_ROOT), str(_WORKSPACE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.services.cache_provider import CacheProvider, SearchResult

# DuckDB не поддерживает TO_CHAR(date, 'Month') — переписываем в strftime.
_REWRITE_TO_CHAR = re.compile(r"TO_CHAR\((\w+)\s*,\s*'Month'\)", re.IGNORECASE)


# =============================================================================
# СЛУЖЕБНЫЕ МОДУЛЬНЫЕ ФУНКЦИИ (используются провайдером и клиентами: gateway, навык)
# =============================================================================


def get_embedding(text: str, base_url: str = "", model: str = "mxbai-embed-large:latest",
                  retries: int = 3) -> Optional[List[float]]:
    """Получить эмбеддинг текста через Ollama /api/embed."""
    if not base_url:
        return None
    import httpx

    payload = {"model": model, "input": text}
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(base_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            embeddings = data.get("embeddings")
            if embeddings and isinstance(embeddings, list) and embeddings:
                return embeddings[0]
            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
            else:
                print(f"[vector] Ошибка эмбеддинга после {retries} попыток: {e}",
                      file=sys.stderr)
                return None
    return None


def read_embedding_config(cfg: dict) -> Dict[str, Any]:
    """Параметры Ollama-эмбеддинга из конфиг-секции навыка."""
    return {
        "base_url": cfg.get("embedding_base_url", "http://localhost:11434/api/embed"),
        "model": cfg.get("embedding_model", "mxbai-embed-large:latest"),
        "dimension": int(cfg.get("embedding_dimension", 1024)),
    }


def read_vector_index_config(cfg: dict) -> Dict[str, Any]:
    """Конфиг векторных индексов: таблица oarb.vector_index_config → fallback в настройках."""
    from utils.db import fetch

    try:
        rows = fetch(
            "SELECT index_name, source_table, src_table, pk_column, "
            "content_cols, embedding_cols, track_column, enabled "
            "FROM oarb.vector_index_config ORDER BY index_name"
        )
        if rows:
            result = {}
            for r in rows:
                ec = r["embedding_cols"]
                if isinstance(ec, str):
                    try:
                        ec = json.loads(ec)
                    except (json.JSONDecodeError, TypeError):
                        ec = {}
                result[r["index_name"]] = {
                    "table": r["src_table"],
                    "pk": r["pk_column"],
                    "source_table": r["source_table"],
                    "content_columns": list(r["content_cols"]) if isinstance(r.get("content_cols"), (list, tuple)) else [],
                    "embedding_columns": ec,
                    "track_column": r["track_column"],
                    "enabled": r["enabled"],
                }
            return result
    except Exception:
        pass
    val = cfg.get("vector_indexes", {})
    return dict(val) if isinstance(val, dict) else {}


def build_cache_provider(cfg: dict, base_dir: str = "") -> "PostgresDuckDbProvider":
    """Универсальная фабрика: собрать провайдера из конфиг-секции навыка.

    cfg — секция skills.<name> (например, skills.audit_analyzer из project.json).
    base_dir — каталог, относительно которого разрешаются относительные пути
    кэша/индексов (для навыка это корень навыка).
    """
    base = Path(base_dir) if base_dir else Path.cwd()

    cache_path = cfg.get("in_memory_cache_path", "cache/audit_cache.duckdb") or ""
    if cache_path and not Path(cache_path).is_absolute():
        cache_path = str(base / cache_path)

    index_path = cfg.get("mode_vector_index_path", "") or ""
    if not index_path:
        index_path = str(Path.home() / ".nanobot" / "vectors" / "audits_index")
    elif not Path(index_path).is_absolute():
        index_path = str(base / index_path)

    emb = read_embedding_config(cfg)
    tables = cfg.get("db_tables", [])
    return PostgresDuckDbProvider(
        schema=cfg.get("db_schema", "oarb"),
        tables=list(tables) if isinstance(tables, (list, tuple)) else None,
        cache_path=cache_path,
        vector_db_table=cfg.get("mode_vector_db_table", ""),
        vector_index_path=index_path,
        vector_indexes=read_vector_index_config(cfg),
        vector_store_table=cfg.get("mode_vector_store_table", "oarb.vector_index_store"),
        embedding_base_url=emb.get("base_url", ""),
        embedding_model=emb.get("model", "mxbai-embed-large:latest"),
    )


def _discover_tables(pg_conn, schema: str) -> List[str]:
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


def _store_meta(conn, pg_conn, schema: str, table_list: List[str]) -> None:
    """Сохранить метку MAX(updated) для каждой таблицы в таблицу __cache_meta."""
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


def load_cache_from_postgres(cache_path: str, db_config: dict) -> None:
    """
    Подключиться к канонической БД (PostgreSQL) и скопировать таблицы в SQL-кэш.

    DSN берётся через resolve_dsn() (configure(dsn) должен быть вызван ранее).
    """
    import duckdb
    import psycopg2
    from utils.db import resolve_dsn

    schema = db_config.get("schema", "public")
    tables = db_config.get("tables")

    dsn = resolve_dsn()
    if not dsn:
        raise RuntimeError("DSN is not configured")

    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(path))
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    pg_conn = psycopg2.connect(dsn)
    pg_conn.autocommit = True

    try:
        table_list = tables or _discover_tables(pg_conn, schema)

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

        _store_meta(conn, pg_conn, schema, table_list)
        print(f"[LOAD] Cache saved to {cache_path}", file=sys.stderr)

    finally:
        pg_conn.close()
        conn.close()


def check_cache_stale(cache_path: str, db_config: dict) -> Dict[str, Any]:
    """
    Проверить, устарел ли кэш, сравнив MAX(updated) с канонической БД.
    """
    import duckdb
    import psycopg2
    from utils.db import resolve_dsn

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
                if cache_meta[tbl] != pg_meta[tbl]:
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


# =============================================================================
# CACHE PROVIDER
# =============================================================================


class PostgresDuckDbProvider(CacheProvider):
    """Провайдер кэша: локальный SQL-файл + векторные индексы поверх PostgreSQL.

    Управляется из gateway: refresh() / check_stale() / preload_indexes()
    загружают данные и индексы, после чего провайдер готов отвечать на
    query_sql() / search_vector() / get_schema().
    """

    def __init__(
        self,
        *,
        dsn: str = "",
        schema: str = "public",
        tables: Optional[List[str]] = None,
        cache_path: str = "",
        vector_db_table: str = "",
        vector_index_path: str = "",
        vector_indexes: Optional[Dict[str, Any]] = None,
        vector_store_table: str = "",
        embedding_base_url: str = "",
        embedding_model: str = "mxbai-embed-large:latest",
    ) -> None:
        self._dsn = dsn
        self._schema = schema
        self._tables = list(tables) if tables else None
        self._cache_path = Path(cache_path)
        self._vector_db_table = vector_db_table
        self._vector_index_path = vector_index_path
        self._vector_indexes = dict(vector_indexes) if vector_indexes else {}
        self._vector_store_table = vector_store_table
        self._embedding_base_url = embedding_base_url
        self._embedding_model = embedding_model or "mxbai-embed-large:latest"

        if dsn:
            # Провайдер может работать сам по себе: подключаем DSN к utils.db.
            from utils.db import configure
            configure(dsn)

        self._conn = None          # DuckDB read-only connection
        self._is_ready = False     # кэш открыт (файл существует и прочитан)
        self._index_cache: Dict[str, tuple[Any, Optional[dict]]] = {}
        self._search_error: Optional[str] = None  # последняя ошибка search_vector

    # -- config --------------------------------------------------------

    @property
    def cache_path(self) -> Path:
        """Путь к SQL-кэшу (пустое значение Path('') если не задан)."""
        return self._cache_path

    @property
    def vector_table(self) -> str:
        """Таблица сырых векторов (schema.table), если векторные индексы настроены."""
        return self._vector_db_table

    def _db_config(self) -> dict:
        return {"schema": self._schema, "tables": self._tables}

    # -- lifecycle ------------------------------------------------------

    def is_ready(self) -> bool:
        return self._is_ready

    def refresh(self) -> bool:
        if not self._cache_path:
            return False
        try:
            load_cache_from_postgres(str(self._cache_path), self._db_config())
            self._open_cache()
            self._is_ready = True
            return True
        except Exception:
            self._is_ready = False
            return False

    def check_stale(self) -> Dict[str, Any]:
        if not self._cache_path:
            return {"fresh": False, "stale_tables": [], "cache_meta": {}, "pg_meta": {},
                    "error": "no cache path"}
        return check_cache_stale(str(self._cache_path), self._db_config())

    def open_cache(self) -> bool:
        """Открыть существующий SQL-кэш (read-only) без пересоздания."""
        try:
            self._open_cache()
            self._is_ready = True
            return True
        except Exception:
            return False

    def _open_cache(self) -> None:
        import duckdb

        if not self._cache_path.exists():
            raise FileNotFoundError(
                f"SQL cache not found at {self._cache_path}. "
                f"Preload data first (refresh/init) before querying."
            )
        self._conn = duckdb.connect(str(self._cache_path), read_only=True)

    # -- query ----------------------------------------------------------

    def get_schema(
        self,
        schema_name: Optional[str] = None,
        table_names: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        schema = schema_name or self._schema
        tables = table_names if table_names is not None else self._tables
        if self._conn is None:
            raise RuntimeError("Cache is not ready")

        sql = (
            "SELECT table_name, column_name, data_type, is_nullable, "
            "character_maximum_length "
            "FROM information_schema.columns WHERE table_schema = ?"
        )
        params = [schema]
        if tables:
            placeholders = ",".join("?" for _ in tables)
            sql += f" AND table_name IN ({placeholders})"
            params.extend(tables)
        sql += " ORDER BY table_name, ordinal_position"

        rows = self._conn.execute(sql, params).fetchall()
        result: Dict[str, Any] = {}
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

    def query_sql(self, sql: str, params: Optional[list] = None) -> Dict[str, Any]:
        if self._conn is None:
            return {"status": "error", "row_count": 0, "columns": [], "rows": [],
                    "error": "Cache is not ready"}
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

    def explain(self, sql: str) -> Dict[str, Any]:
        """EXPLAIN на DuckDB-кэше — синтаксическая проверка без выполнения."""
        if self._conn is None:
            return {"valid": False, "error": "Cache is not ready"}
        duck_sql = sql.replace("%s", "?")
        duck_sql = _REWRITE_TO_CHAR.sub(r"strftime(\1, '%B')", duck_sql)
        try:
            result = self._conn.execute(f"EXPLAIN {duck_sql}")
            columns = [desc[0] for desc in result.description]
            plan = [dict(zip(columns, r)) for r in result.fetchall()]
            return {"valid": True, "plan": plan}
        except Exception as e:
            return {"valid": False, "error": f"EXPLAIN failed: {e}"}

    # -- vector indexes --------------------------------------------------

    def _load_index_from_files(self, index_dir: str, index_name: str) -> tuple[Any, Optional[dict]]:
        import faiss
        import os
        import shutil
        import tempfile

        index_path = os.path.join(index_dir, f"{index_name}.faiss")
        meta_path = os.path.join(index_dir, f"{index_name}_metadata.json")

        if not os.path.exists(index_path):
            return None, None

        meta = None
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as mf:
                meta = json.load(mf)

        try:
            return faiss.read_index(index_path), meta
        except RuntimeError:
            pass

        tmp_dir = os.path.join(tempfile.gettempdir(), "nanobot_vectors")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_idx = os.path.join(tmp_dir, f"{index_name}.faiss")
        tmp_meta = os.path.join(tmp_dir, f"{index_name}_metadata.json")
        shutil.copy2(index_path, tmp_idx)
        if meta_path and os.path.exists(meta_path):
            shutil.copy2(meta_path, tmp_meta)

        try:
            return faiss.read_index(tmp_idx), meta
        except Exception:
            return None, None

    def _save_index_to_store(self, source: str, index, metadata: dict) -> None:
        if not self._vector_store_table:
            return
        import faiss
        from utils.db import execute, fetch

        store = self._vector_store_table
        blob = bytes(faiss.serialize_index(index))
        meta_json = json.dumps(metadata, ensure_ascii=False, default=str)
        dim = index.d
        ntotal = index.ntotal

        exists = fetch(f"SELECT 1 FROM {store} WHERE source = %s", source)
        if exists:
            execute(
                f"UPDATE {store} SET index_binary = %s, metadata = %s::jsonb, "
                f"dimension = %s, vector_count = %s, updated_at = NOW() "
                f"WHERE source = %s",
                blob, meta_json, dim, ntotal, source,
            )
        else:
            execute(
                f"INSERT INTO {store} (source, index_binary, metadata, dimension, vector_count, updated_at) "
                f"VALUES (%s, %s, %s::jsonb, %s, %s, NOW())",
                source, blob, meta_json, dim, ntotal,
            )

    def _load_index_from_store(self, source: str) -> tuple[Any, Optional[dict]]:
        if not self._vector_store_table:
            return None, None
        import numpy as np
        import faiss
        from utils.db import fetch

        store = self._vector_store_table
        rows = fetch(f"SELECT index_binary, metadata FROM {store} WHERE source = %s", source)
        if not rows:
            return None, None

        row = rows[0]
        blob = row["index_binary"]
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)

        try:
            if isinstance(blob, memoryview):
                blob = bytes(blob)
            blob_array = np.frombuffer(blob, dtype=np.uint8)
            return faiss.deserialize_index(blob_array), meta
        except Exception:
            return None, None

    def _load_vectors_from_db(
        self, table_name: str, source: Optional[str] = None
    ) -> tuple[Any, Optional[dict]]:
        import numpy as np
        import faiss
        from utils.db import fetch

        where = " WHERE source = %s" if source else ""
        params = [source] if source else []
        sql = (
            f'SELECT id, source, content, search_text, "table", pk_value, '
            f'chunk_index, chunk_count, row_data, embedding '
            f'FROM {table_name}{where} ORDER BY id'
        )

        try:
            rows = fetch(sql, *params)
        except Exception:
            return None, None

        if not rows:
            return None, None

        dimension = len(rows[0]["embedding"])
        vectors = np.zeros((len(rows), dimension), dtype=np.float32)
        metadata: dict = {"metadata": {}}

        for i, row in enumerate(rows):
            emb = row["embedding"]
            if isinstance(emb, (list, tuple)):
                vectors[i] = np.array(emb, dtype=np.float32)
            else:
                return None, None

            row_data = row.get("row_data")
            if isinstance(row_data, str):
                try:
                    row_data = json.loads(row_data)
                except (json.JSONDecodeError, TypeError):
                    row_data = {}

            metadata["metadata"][str(i)] = {
                "content": row.get("content") or row.get("search_text") or "",
                "search_text": row.get("search_text") or "",
                "source": row.get("source") or source or "",
                "table": row.get("table") or "",
                "pk_value": row.get("pk_value") or i,
                "chunk_index": row.get("chunk_index", 0),
                "chunk_count": row.get("chunk_count", 1),
                "row": row_data or {},
            }

        index = faiss.IndexFlatIP(dimension)
        index.add(vectors)
        return index, metadata

    def _load_index(
        self,
        index_dir: str,
        index_name: str,
        db_table: Optional[str] = None,
    ) -> tuple[Any, Optional[dict]]:
        table = db_table or self._vector_db_table
        if table:
            cached = self._index_cache.get(index_name)
            if cached is not None:
                return cached

            idx, meta = self._load_index_from_store(index_name)
            if idx is not None:
                self._index_cache[index_name] = (idx, meta)
                return idx, meta

            idx, meta = self._load_vectors_from_db(table, source=index_name)
            if idx is not None:
                self._save_index_to_store(index_name, idx, meta)
                self._index_cache[index_name] = (idx, meta)
                return idx, meta

        return self._load_index_from_files(index_dir, index_name)

    def preload_indexes(self, db_table: Optional[str] = None) -> List[Dict[str, Any]]:
        """Прогреть кеш индексов в память."""
        from utils.db import fetch

        table = db_table or self._vector_db_table
        if not table:
            return []

        names: Dict[str, bool] = {}
        cfg = self._vector_indexes or {}
        for name, c in cfg.items():
            names[name] = not (isinstance(c, dict) and c.get("enabled") is False)
        if self._vector_store_table:
            try:
                for r in fetch(f"SELECT DISTINCT source FROM {self._vector_store_table}"):
                    names.setdefault(r["source"], True)
            except Exception:
                pass

        loaded = []
        for name, enabled in names.items():
            if not enabled:
                continue
            idx, _ = self._load_index("", name, table)
            if idx is not None:
                loaded.append({"index_name": name, "vectors": idx.ntotal})
        return loaded

    def invalidate_cache(self, source: Optional[str] = None) -> None:
        """Сбросить кеш индекса (после обновления данных)."""
        if source:
            self._index_cache.pop(source, None)
        else:
            self._index_cache.clear()

    def search_vector(
        self,
        query: str,
        index_name: str = "default_index",
        index_path: Optional[str] = None,
        top_k: int = 5,
        threshold: Optional[float] = None,
    ) -> List[SearchResult]:
        """Семантический поиск по векторному индексу FAISS.

        Возвращает пустой список при отсутствии эмбеддинга, индекса или результатов.
        Диагностика последней ошибки — в атрибуте ``self._search_error``.
        """
        self._search_error = None
        try:
            import numpy as np
            import faiss  # noqa: F401
        except ImportError:
            self._search_error = "Не установлены зависимости: faiss и numpy. Установите: pip install faiss-cpu numpy"
            return []

        vpath = index_path or self._vector_index_path
        idx_path = str(Path(vpath).resolve()) if vpath else ""
        idx, meta = self._load_index(idx_path, index_name, db_table=self._vector_db_table)
        if idx is None:
            if self._vector_db_table:
                self._search_error = f"Индекс '{index_name}' не найден в таблице {self._vector_db_table}"
            else:
                self._search_error = f"Индекс '{index_name}' не найден в {idx_path or 'файлах'}"
            return []

        embedding = get_embedding(query, self._embedding_base_url, self._embedding_model)
        if embedding is None:
            self._search_error = "Не удалось получить эмбеддинг запроса."
            return []

        query_vec = np.array([embedding], dtype=np.float32)
        n = idx.ntotal if threshold is not None else min(top_k, idx.ntotal)
        scores, ids = idx.search(query_vec, n)

        meta_items = (meta or {}).get("metadata", {})
        raw: List[Dict[str, Any]] = []
        for score, doc_id in zip(scores[0], ids[0]):
            if doc_id < 0:
                continue
            if threshold is not None and score < threshold:
                continue
            item = meta_items.get(str(doc_id), {})
            chunk_idx = item.get("chunk_index", 0)
            chunk_total = item.get("chunk_count", 1)
            pk = item.get("pk_value", int(doc_id))
            tbl = item.get("table", "")
            src = item.get("source", index_name)
            content = item.get("content", item.get("search_text", ""))
            raw.append({
                "content": content,
                "score": float(score),
                "source": src,
                "table": tbl,
                "pk_value": pk,
                "chunk_index": chunk_idx,
                "chunk_total": chunk_total,
                "chunk": f"{chunk_idx + 1}/{chunk_total}" if chunk_total > 1 else "",
                "row": item.get("row", {}),
            })

        # Группировка чанков: один документ = одно место в top_k.
        doc_groups: Dict[tuple[str, str, Any], Dict[str, Any]] = {}
        for r in raw:
            key = (r["source"], r["table"], r["pk_value"])
            if key not in doc_groups or r["score"] > doc_groups[key]["score"]:
                entry = dict(r)
                entry["matched_chunks"] = 1
                doc_groups[key] = entry
            else:
                doc_groups[key]["matched_chunks"] += 1

        results = sorted(doc_groups.values(), key=lambda r: r["score"], reverse=True)
        if top_k and threshold is None:
            results = results[:top_k]

        for r in results:
            r.pop("chunk_index", None)
            r.pop("chunk_total", None)

        return [
            SearchResult(
                content=r["content"],
                score=r["score"],
                source=r["source"],
                table=r["table"],
                pk_value=r["pk_value"],
                chunk=r.get("chunk", ""),
                matched_chunks=r.get("matched_chunks", 1),
                row=r.get("row", {}),
            )
            for r in results
        ]

    def rebuild_and_store_index(self, source: str, db_table: str) -> None:
        """Перестроить индекс для source и сохранить в store (для индексаторов)."""
        idx, meta = self._load_vectors_from_db(db_table, source=source)
        if idx is not None:
            self._save_index_to_store(source, idx, meta)
            self._index_cache.pop(source, None)
            print(f"[vector] Индекс '{source}' перестроен и сохранён в store "
                  f"({idx.ntotal} векторов)", file=sys.stderr)

    # -- resource --------------------------------------------------------

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._is_ready = False

    def __enter__(self) -> "PostgresDuckDbProvider":
        return self

    def __exit__(self, *args) -> None:
        self.close()