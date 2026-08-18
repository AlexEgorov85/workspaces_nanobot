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

Тяжёлые зависимости (duckdb, psycopg2, faiss, numpy, httpx)
импортируются лениво внутри методов, чтобы импорт модуля оставался лёгким
и gateway мог управлять жизненным циклом без побочных эффектов.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Пути к проекту и workspace — чтобы `from utils.db import ...` работал
# независимо от рабочего каталога.
_ROOT = Path(__file__).resolve().parents[2]        # корень проекта
_WORKSPACE = _ROOT / "workspace"
for _p in (str(_ROOT), str(_WORKSPACE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.services.cache_provider import CacheProvider, SearchResult

# Внутренняя таблица метаданных схемы (комментарии таблиц/колонок, PG-типы).
# Та же структура, что в audit_memory_store, но в файле SQL-кэша навыка.
_META_SCHEMA = "__nanobot_meta"
_META_TABLE = "__schema_meta"


# =============================================================================
# СЛУЖЕБНЫЕ МОДУЛЬНЫЕ ФУНКЦИИ (используются провайдером и клиентами: gateway, навык)
# =============================================================================


def get_embedding(text: str) -> Optional[List[float]]:
    """Единая точка получения эмбеддинга текста через Ollama /api/embed.

    Параметры (``embedding_base_url`` / ``embedding_model`` /
    ``embedding_http_timeout_sec``) всегда читаются из
    ``skills.audit_analyzer`` (project.json). Единый retry-цикл через
    ``retry_on_exception`` (exponential backoff), как и в LLM-клиенте
    (``lib/services/llm_client.py``).

    Возвращает ``None`` при отсутствии base_url, ошибке конфигурации
    или ошибке после ``retries`` попыток — вызывающий код не должен
    перехватывать исключения.
    """
    try:
        from lib.services.audit_settings import audit_vector_settings
        s = audit_vector_settings()
        base_url = s.embedding_base_url
        model = s.embedding_model
        timeout_sec = s.embedding_http_timeout_sec
        retries = 3
    except Exception:
        return None

    if not base_url:
        return None

    def _embed() -> Optional[List[float]]:
        import httpx

        payload = {"model": model, "input": text}
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(base_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        embeddings = data.get("embeddings")
        if embeddings and isinstance(embeddings, list) and embeddings:
            return embeddings[0]
        return None

    from lib.utils.retry import retry_on_exception

    try:
        return retry_on_exception(
            _embed,
            exceptions=(Exception,),
            max_retries=retries,
            base_delay=1.0,
            max_delay=16.0,
            label="embedding",
        )
    except Exception as e:
        print(f"[vector] Ошибка эмбеддинга после {retries} попыток: {e}",
              file=sys.stderr)
        return None


def read_embedding_config(cfg: dict) -> Dict[str, Any]:
    """Параметры Ollama-эмбеддинга из конфиг-секции навыка."""
    from lib.services.audit_settings import audit_vector_settings
    s = audit_vector_settings()
    return {
        "base_url": s.embedding_base_url,
        "model": s.embedding_model,
        "dimension": s.embedding_dimension,
    }


def read_vector_index_config(cfg: dict) -> Dict[str, Any]:
    """Конфиг векторных индексов: таблица agent_vector_index_config (источник — БД).

    Читается только из БД. При ошибке БД исключение пробрасывается —
    тихой подстановки значений из ``cfg`` нет.
    """
    from lib.services.audit_settings import audit_vector_settings
    from utils.db import fetch

    table = audit_vector_settings().mode_vector_index_config_table
    rows = fetch(
        "SELECT index_name, source_table, src_table, pk_column, "
        "content_cols, embedding_cols, track_column, enabled "
        f"FROM {table} ORDER BY index_name"
    )
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


def build_cache_provider(cfg: dict, base_dir: str = "") -> "PostgresDuckDbProvider":
    """Универсальная фабрика: собрать провайдера из конфиг-секции навыка.

    cfg — секция skills.<name> (например, skills.audit_analyzer из project.json).
    base_dir — каталог, относительно которого разрешаются относительные пути
    кэша/индексов (для навыка это корень навыка).
    """
    base = Path(base_dir) if base_dir else Path.cwd()

    from lib.services.audit_settings import audit_vector_settings
    s = audit_vector_settings()

    cache_path = s.in_memory_cache_path
    if cache_path and not Path(cache_path).is_absolute():
        cache_path = str(base / cache_path)

    index_path = s.vector_index_default_path or ""
    if index_path and not Path(index_path).is_absolute():
        index_path = str(base / index_path)

    emb = read_embedding_config(cfg)
    tables = s.db_tables
    additional = s.db_additional_tables
    return PostgresDuckDbProvider(
        schema=s.db_schema,
        tables=list(tables) if isinstance(tables, (list, tuple)) else None,
        additional_tables=_normalize_additional_tables(additional),
        cache_path=cache_path,
        vector_db_table=s.mode_vector_db_table,
        vector_index_path=index_path,
        vector_indexes=read_vector_index_config(cfg),
        vector_store_table=s.mode_vector_store_table,
        embedding_base_url=emb.get("base_url", ""),
        embedding_model=emb.get("model", "mxbai-embed-large:latest"),
    )


def _normalize_additional_tables(value: Any) -> List[Tuple[str, str]]:
    """
    Приводит db_additional_tables к канону: List[Tuple[schema, table]].

    Допустимые форматы:
      - [["public", "predefined_scripts"], {"schema": "audit", "table": "rules"}]
      - ["public.predefined_scripts", "audit.rules"]
    """
    out: List[Tuple[str, str]] = []
    if not value:
        return out
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            sch, tbl = item
            if sch and tbl:
                out.append((str(sch), str(tbl)))
        elif isinstance(item, dict) and item.get("schema") and item.get("table"):
            out.append((str(item["schema"]), str(item["table"])))
        elif isinstance(item, str) and "." in item:
            sch, tbl = item.split(".", 1)
            if sch and tbl:
                out.append((sch, tbl))
    return out


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


def _store_meta(conn: Any, pg_conn: Any, schema: str, table_list: List[str]) -> None:
    """Сохранить метку MAX(updated) для каждой таблицы в __cache_meta."""
    conn.execute("DROP TABLE IF EXISTS __cache_meta")

    meta_rows: List[Tuple[str, Optional[str]]] = []
    for tbl in table_list:
        cur = pg_conn.cursor()
        try:
            cur.execute(f'SELECT MAX(updated_at) FROM "{schema}"."{tbl}"')
            max_ts = cur.fetchone()[0]
            meta_rows.append((tbl, str(max_ts) if max_ts else None))
        except Exception:
            meta_rows.append((tbl, None))
        finally:
            cur.close()

    conn.execute("CREATE TABLE __cache_meta (table_name VARCHAR, max_updated_at VARCHAR)")
    conn.executemany(
        "INSERT INTO __cache_meta VALUES (?, ?)",
        meta_rows,
    )


def _capture_schema_meta(
    conn: Any,
    pg_conn: Any,
    schema_pairs: List[tuple],
) -> None:
    """
    Сохранить комментарии таблиц/колонок и исходные PG-типы в DuckDB-кэш.

    ``schema_pairs`` — список ``(schema, [table, ...])``. Для каждой таблицы
    из PostgreSQL снимаются ``COMMENT ON TABLE``/``COMMENT ON COLUMN`` и
    ``data_type`` из ``information_schema``, результат кладётся в
    ``__nanobot_meta.__schema_meta`` (строка с ``column_name = NULL`` — это
    комментарий таблицы).

    ``build_schema()`` (lib/utils/duckdb_query.py) подставляет эти комментарии
    в промпт при формировании описания схемы, а ``pg_type`` (точнее инференса
    DuckDB из CSV) использует вместо типов, выведенных ``read_csv_auto``.
    """
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{_META_SCHEMA}"')
    conn.execute(f'DROP TABLE IF EXISTS "{_META_SCHEMA}"."{_META_TABLE}"')
    conn.execute(
        f'CREATE TABLE "{_META_SCHEMA}"."{_META_TABLE}" ('
        "schema_name TEXT, table_name TEXT, column_name TEXT, "
        "comment TEXT, pg_type TEXT)"
    )

    insert_rows: List[tuple] = []
    for schema, table_list in schema_pairs:
        if not table_list:
            continue
        cur = pg_conn.cursor()
        try:
            cur.execute(
                """
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.character_maximum_length,
                    pgd.description AS column_comment,
                    obj_description(pc.oid) AS table_comment
                FROM information_schema.columns c
                JOIN pg_class pc
                    ON pc.relname = c.table_name
                   AND pc.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s)
                LEFT JOIN pg_catalog.pg_description pgd
                    ON pgd.objsubid = c.ordinal_position
                   AND pgd.objoid = pc.oid
                WHERE c.table_schema = %s
                  AND c.table_name = ANY(%s)
                ORDER BY c.table_name, c.ordinal_position
                """,
                [schema, schema, table_list],
            )
            rows = cur.fetchall()
        except Exception as e:
            print(f"[LOAD] Не удалось снять схему-мета для {schema}: {e}",
                  file=sys.stderr)
            continue
        finally:
            cur.close()

        per_table: Dict[str, tuple] = {}
        for row in rows:
            tbl, col, data_type, max_len, col_comment, table_comment = row
            if tbl not in per_table:
                per_table[tbl] = (table_comment, [])
            col_type = data_type
            if max_len and col_type in ("character varying", "character"):
                col_type = f"varchar({max_len})"
            per_table[tbl][1].append((col, col_type, col_comment))

        for tbl, (table_comment, cols) in per_table.items():
            if table_comment:
                insert_rows.append((schema, tbl, None, table_comment, None))
            for col, col_type, col_comment in cols:
                insert_rows.append((schema, tbl, col, col_comment, col_type))

    if insert_rows:
        conn.executemany(
            f'INSERT INTO "{_META_SCHEMA}"."{_META_TABLE}" '
            "(schema_name, table_name, column_name, comment, pg_type) "
            "VALUES (?, ?, ?, ?, ?)",
            insert_rows,
        )


def _copy_table(
    pg_conn: Any,
    conn: Any,
    schema: str,
    tbl: str,
) -> None:
    """
    Скопировать таблицу schema.tbl из PG в DuckDB через COPY ... TO STDOUT → CSV.

    Без pandas, без pyarrow-IPC. Поток:
      1) DESCRIBE TABLE в PG → имена колонок
      2) COPY (SELECT * FROM schema.tbl) TO STDOUT WITH CSV HEADER
      3) DuckDB read_csv_auto() с авто-типизацией → INSERT INTO

    Преимущества:
      - никакой материализации в Python, стрим идёт PG → DuckDB;
      - DuckDB сам выводит типы из CSV-выборки (sniff_rows);
      - работает на psycopg2-binary (не требует пересборки).
    """
    import io
    import tempfile
    from pathlib import Path as _P

    full_name = f"{schema}.{tbl}"
    print(f"[LOAD] Copying {full_name} (CSV stream)...", file=sys.stderr)

    # 1) Снимаем имена колонок (если таблицы нет в PG — placeholder)
    cur = pg_conn.cursor()
    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """, [schema, tbl])
    col_names = [r[0] for r in cur.fetchall()]
    cur.close()

    if not col_names:
        conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{tbl}"')
        conn.execute(f'CREATE TABLE "{schema}"."{tbl}" (placeholder VARCHAR)')
        print(f"[LOAD]  {full_name} not found in PG, placeholder created", file=sys.stderr)
        return

    # 2) Стрим PG → временный CSV-файл (binary mode — copy_expert пишет байты)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".csv",
        delete=False,
    ) as tmp:
        tmp_path = _P(tmp.name)
        cur = pg_conn.cursor()
        try:
            sql_copy = f'COPY "{schema}"."{tbl}" TO STDOUT WITH CSV HEADER'
            cur.copy_expert(sql_copy, tmp)
        finally:
            cur.close()

    try:
        # 3) Создаём/наполняем таблицу через read_csv_auto (auto-sniff типов)
        conn.execute(
            f"CREATE OR REPLACE TABLE \"{schema}\".\"{tbl}\" AS "
            f"SELECT * FROM read_csv_auto('{tmp_path.as_posix()}', "
            f"header=true, all_varchar=false)"
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    count = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{tbl}"').fetchone()[0]
    print(f"[LOAD]  {count} rows loaded into {tbl}", file=sys.stderr)


def load_cache_from_postgres(cache_path: str, db_config: dict) -> None:
    """
    Подключиться к канонической БД (PostgreSQL) и скопировать таблицы в SQL-кэш.

    DSN берётся через resolve_dsn() (configure(dsn) должен быть вызван ранее).

    Структура db_config:
      schema           — основная схема (audit data)
      tables           — список таблиц в основной схеме
      additional_tables — список [(schema, table), ...] для копирования из
                          произвольных схем (метаданные, реестры и т.п.)
    """
    import duckdb
    from utils.db import run

    schema = db_config.get("schema", "public")
    tables = db_config.get("tables")
    additional_tables = db_config.get("additional_tables") or []

    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(path))
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    # Все дополнительные схемы (для CREATE SCHEMA IF NOT EXISTS)
    extra_schemas = {sch for sch, _ in additional_tables}
    for sch in extra_schemas:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch}"')

    def _load(pg_conn) -> None:
        table_list = tables or _discover_tables(pg_conn, schema)

        for tbl in table_list:
            _copy_table(pg_conn, conn, schema, tbl)

        # Копируем дополнительные таблицы (например, public.agent_predefined_scripts)
        for sch, tbl in additional_tables:
            _copy_table(pg_conn, conn, sch, tbl)

        _store_meta(conn, pg_conn, schema, table_list)

        # Метаданные схемы (комментарии таблиц/колонок + исходные PG-типы)
        # для основной и дополнительных схем — build_schema использует их при
        # формировании описания таблиц в DuckDB-кэше.
        schema_pairs: List[tuple] = [(schema, table_list)]
        extra_by_schema: Dict[str, List[str]] = {}
        for sch, tbl in additional_tables:
            extra_by_schema.setdefault(sch, []).append(tbl)
        schema_pairs.extend((sch, tbls) for sch, tbls in extra_by_schema.items())
        _capture_schema_meta(conn, pg_conn, schema_pairs)

    try:
        run(_load)
        print(f"[LOAD] Cache saved to {cache_path}", file=sys.stderr)
    finally:
        conn.close()


def check_cache_stale(cache_path: str, db_config: dict) -> Dict[str, Any]:
    """
    Проверить, устарел ли кэш, сравнив MAX(updated) с канонической БД.
    """
    import duckdb
    from utils.db import run

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

    if not cache_meta:
        return {"fresh": False, "stale_tables": [], "cache_meta": {}, "pg_meta": {},
                "error": "No cache metadata"}

    def _check(pg_conn) -> None:
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

    pg_meta = {}
    stale = []
    try:
        run(_check)
    except Exception:
        pass

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
        additional_tables: Optional[List[Tuple[str, str]]] = None,
        cache_path: str = "",
        vector_db_table: str = "",
        vector_index_path: str = "",
        vector_indexes: Optional[Dict[str, Any]] = None,
        vector_store_table: str = "",
        embedding_base_url: str = "",
        embedding_model: str = "mxbai-embed-large:latest",
        embedding_timeout_sec: float = 60.0,
    ) -> None:
        self._dsn = dsn
        self._schema = schema
        self._tables = list(tables) if tables else None
        self._additional_tables = list(additional_tables) if additional_tables else []
        self._cache_path = Path(cache_path)
        self._vector_db_table = vector_db_table
        self._vector_index_path = vector_index_path
        self._vector_indexes = dict(vector_indexes) if vector_indexes else {}
        self._vector_store_table = vector_store_table
        self._embedding_base_url = embedding_base_url
        self._embedding_model = embedding_model or "mxbai-embed-large:latest"
        self._embedding_timeout_sec = float(embedding_timeout_sec)

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
        return {
            "schema": self._schema,
            "tables": self._tables,
            "additional_tables": self._additional_tables,
        }

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

        from lib.utils.duckdb_query import build_schema

        return build_schema(self._conn, schema, tables, self._read_schema_meta)

    def _read_schema_meta(self, schema: str) -> Dict[tuple, tuple]:
        """Комментарии и исходные PG-типы из __nanobot_meta.__schema_meta (снимка)."""
        result: Dict[tuple, tuple] = {}
        try:
            rows = self._conn.execute(
                'SELECT table_name, column_name, comment, pg_type '
                'FROM "__nanobot_meta"."__schema_meta" WHERE schema_name = ?',
                [schema],
            ).fetchall()
        except Exception:
            return result
        for table, column, comment, pg_type in rows:
            result[(table, column)] = (comment, pg_type)
        return result

    def query_sql(self, sql: str, params: Optional[list] = None) -> Dict[str, Any]:
        if self._conn is None:
            return {"status": "error", "row_count": 0, "columns": [], "rows": [],
                    "error": "Cache is not ready"}
        from lib.utils.duckdb_query import run_query

        return run_query(self._conn, sql, params)

    def explain(self, sql: str) -> Dict[str, Any]:
        """EXPLAIN на DuckDB-кэше — синтаксическая проверка без выполнения."""
        if self._conn is None:
            return {"valid": False, "error": "Cache is not ready"}
        from lib.utils.duckdb_query import explain_query

        return explain_query(self._conn, sql)

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

        records = [
            {
                "source": r.get("source") or source or "",
                "content": r.get("content") or "",
                "search_text": r.get("search_text") or "",
                "table": r.get("table") or "",
                "pk_value": r.get("pk_value"),
                "chunk_index": r.get("chunk_index") or 0,
                "chunk_count": r.get("chunk_count") or 1,
                "row_data": r.get("row_data"),
                "embedding": r.get("embedding"),
            }
            for r in rows
        ]
        from lib.utils.duckdb_query import build_faiss_index

        return build_faiss_index(records)

    def _load_index_from_cache(
        self, source: str,
    ) -> tuple[Any, Optional[dict]]:
        """Построить FAISS-индекс из локального DuckDB-кэша навыка.

        Навык работает только со своим снимком (``audit_cache.duckdb``) — без
        PostgreSQL. Индекс строится из ``oarb.audit_vectors`` файла кэша и
        кешируется в ``_index_cache``.
        """
        if not self._vector_db_table:
            return None, None
        if self._conn is None:
            if not self.open_cache():
                return None, None

        cached = self._index_cache.get(source)
        if cached is not None:
            return cached

        schema, name = (
            self._vector_db_table.split(".", 1)
            if "." in self._vector_db_table else ("", self._vector_db_table)
        )
        full = f'"{schema}"."{name}"' if schema else f'"{name}"'
        try:
            rows = self._conn.execute(
                f'SELECT id, source, content, search_text, "table", pk_value, '
                f'chunk_index, chunk_count, row_data, embedding '
                f'FROM {full} WHERE source = ? ORDER BY id',
                [source],
            ).fetchall()
        except Exception:
            return None, None
        if not rows:
            return None, None

        records = [
            {
                "source": r[1] or source,
                "content": r[2] or "",
                "search_text": r[3] or "",
                "table": r[4] or "",
                "pk_value": r[5] if r[5] is not None else i,
                "chunk_index": r[6] or 0,
                "chunk_count": r[7] or 1,
                "row_data": r[8],
                "embedding": r[9],
            }
            for i, r in enumerate(rows)
        ]
        from lib.utils.duckdb_query import build_faiss_index

        idx, meta = build_faiss_index(records)
        if idx is not None:
            self._index_cache[source] = (idx, meta)
        return idx, meta

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

        # Индекс строится ТОЛЬКО из локального снимка кэша навыка (без PostgreSQL).
        idx, meta = self._load_index_from_cache(index_name)
        if idx is None:
            cache_txt = str(self._cache_path) if self._cache_path else "нет кэша"
            self._search_error = (
                f"Индекс '{index_name}' не найден в кэше ({cache_txt})"
            )
            return []

        embedding = get_embedding(query)
        if embedding is None:
            self._search_error = "Не удалось получить эмбеддинг запроса."
            return []

        if idx.d != len(embedding):
            self._search_error = (
                f"Размерность индекса '{index_name}' ({idx.d}) не совпадает "
                f"с размерностью эмбеддинга запроса ({len(embedding)}). Пересоберите "
                f"снимок (gateway publish) той же моделью эмбеддинга."
            )
            return []

        query_vec = np.array([embedding], dtype=np.float32)
        n = idx.ntotal if threshold is not None else min(top_k, idx.ntotal)
        scores, ids = idx.search(query_vec, n)

        meta_items = (meta or {}).get("metadata", {})

        from lib.utils.duckdb_query import build_raw_items, group_vector_hits

        raw = build_raw_items(meta_items, scores, ids, index_name, threshold)
        results = group_vector_hits(raw, top_k, threshold)

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

    def rebuild_and_store_index(self, source: str, db_table: str) -> Optional[int]:
        """Перестроить индекс для source и сохранить в store (для индексаторов).

        Returns:
            Количество векторов построенного индекса, или ``None`` если данных
            нет / индекс не собран.
        """
        idx, meta = self._load_vectors_from_db(db_table, source=source)
        if idx is not None:
            self._save_index_to_store(source, idx, meta)
            self._index_cache.pop(source, None)
            print(f"[vector] Индекс '{source}' перестроен и сохранён в store "
                  f"({idx.ntotal} векторов)", file=sys.stderr)
            return idx.ntotal
        return None

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