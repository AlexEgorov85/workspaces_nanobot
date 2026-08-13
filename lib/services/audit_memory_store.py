"""
AuditMemoryStore — локальное хранилище данных аудита (DuckDB + FAISS).

Отвечает за ДАННЫЕ, а не за их источник: данные приходят извне методом
``upsert_records(table, records)`` (обычно — из AuditSyncService через
callback), и ни одна строка кода здесь не знает про PostgreSQL.

Обязанности:
  * ведение локального SQL-кэша (DuckDB-файл) для query_sql / get_schema / explain
  * ведение векторных индексов (FAISS) для search_vector, перестроение
    индекса источника при обновлении его записей
  * потокобезопасность: запись приходит из worker-потока синхронизации,
    чтение — из основного (asyncio) потока; всё под RLock

Интерфейс запросов повторяет CacheProvider (cache_provider.py), поэтому
потребители (gateway, навык) работают с ним так же, как с провайдером.
SearchResult используется тот же (lib.services.cache_provider.SearchResult).

Тяжёлые зависимости (duckdb, faiss, numpy, pyarrow) импортируются лениво
внутри методов — импорт модуля остаётся лёгким и без побочных эффектов.

Bulk-вставка записей (list[dict]) идёт через pyarrow arrays по колонкам
+ pa.table() + DuckDB conn.register (без pandas, без pyarrow.Table.from_pylist).
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# DuckDB не поддерживает TO_CHAR(date, 'Month') — переписываем в strftime
# (общая логика — в lib.utils.duckdb_query.rewrite_duck_sql).


def _split_table(table: str) -> tuple[str, str]:
    """Разбить 'oarb.audit_vectors' на (schema, table)."""
    if "." in table:
        schema, name = table.split(".", 1)
        return schema, name
    return "", table


def _infer_duckdb_type(values) -> str:
    """Вывести тип DuckDB для колонки по её значениям (для ALTER ADD COLUMN)."""
    sample = [v for v in values if v is not None]
    if not sample:
        return "VARCHAR"
    if all(isinstance(v, bool) for v in sample):
        return "BOOLEAN"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in sample):
        return "BIGINT"
    if all(isinstance(v, float) for v in sample):
        return "DOUBLE"
    if all(isinstance(v, dict) for v in sample):
        return "JSON"
    return "VARCHAR"


def _records_to_arrow(records: List[Dict[str, Any]]):
    """
    Сериализовать list[dict] в pyarrow.Table (без pandas).

    Сохраняет вложенные типы:
      - list[number] → DOUBLE[] (DuckDB при register)
      - dict/list[str] → list[str] (json-строки)
      - None → null

    pyarrow умеет сам вывести типы; для embedding (list[float]) это даёт
    list<float64>, который DuckDB читает как DOUBLE[].
    """
    import pyarrow as pa

    if not records:
        return None

    # Собираем уникальные ключи в порядке появления
    cols: List[str] = []
    seen = set()
    for r in records:
        if not isinstance(r, dict):
            continue
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                cols.append(str(k))

    if not cols:
        return None

    # Сборка по колонкам: pa.array() с auto-типом
    arrays = {}
    for c in cols:
        col_data = [r.get(c) if isinstance(r, dict) else None for r in records]
        try:
            arrays[c] = pa.array(col_data)
        except (pa.lib.ArrowInvalid, TypeError):
            # фоллбэк: всё строкой
            arrays[c] = pa.array([_safe_str(v) for v in col_data])

    return pa.table(arrays)


def _safe_str(v: Any) -> Optional[str]:
    """Строковое представление для гетерогенных/нестандартных значений."""
    if v is None:
        return None
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


# Внутренняя таблица метаданных схемы (комментарии таблиц/колонок).
_META_TABLE = "__schema_meta"
# Схема, в которой живёт мета-таблица (общая для всех зеркал).
_META_SCHEMA = "__nanobot_meta"

_PG_TO_DUCKDB = {
    "boolean": "BOOLEAN",
    "smallint": "SMALLINT",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "real": "REAL",
    "double precision": "DOUBLE",
    "text": "VARCHAR",
    "date": "DATE",
    "time without time zone": "TIME",
    "time with time zone": "TIME",
    "timestamp without time zone": "TIMESTAMP",
    "timestamp with time zone": "TIMESTAMPTZ",
    "json": "JSON",
    "jsonb": "JSON",
    "uuid": "UUID",
    "bytea": "BLOB",
    "interval": "INTERVAL",
}


def _map_pg_type(pg_type: str) -> str:
    """Смаппить PG-тип колонки в DuckDB-тип.

    Возвращает тип, пригодный для ``CREATE TABLE`` / ``ALTER ADD COLUMN``
    в DuckDB. Неизвестные/сложные типы сводятся к VARCHAR, чтобы не ломать
    создание таблицы.
    """
    t = (pg_type or "").strip().lower()
    if not t:
        return "VARCHAR"
    # character varying(n) / character(n)
    if t.startswith("character varying") or t.startswith("varchar"):
        return t if "(" in t else "VARCHAR"
    if t.startswith("character(") or t.startswith("char("):
        return t
    if t.startswith("numeric") or t.startswith("decimal"):
        m = re.match(r"^(numeric|decimal)\((\d+)(?:\s*,\s*(\d+))?\)$", t)
        if m:
            prec, scale = m.group(2), m.group(3) or "0"
            return f"DECIMAL({prec},{scale})"
        return "DOUBLE"
    if t.startswith("timestamp"):
        return "TIMESTAMPTZ" if "with time zone" in t else "TIMESTAMP"
    if t.startswith("time"):
        return "TIME"
    if t.startswith("character") and not t == "character":
        return "CHAR"
    if t.startswith("array") or t.startswith("text[]") or t.startswith("_") or t.endswith("[]"):
        return "VARCHAR"  # массивы в DuckDB сложны — сводим к строке-представлению
    return _PG_TO_DUCKDB.get(t, "VARCHAR")


class AuditMemoryStore:
    """Локальное хранилище данных аудита: DuckDB-кэш + FAISS-индексы.

    Питается записями через :meth:`upsert_records` (из AuditSyncService),
    отвечает на SQL-запросы и семантический поиск. Не имеет доступа к
    PostgreSQL — это граница инфраструктуры, управляемая из gateway.
    """

    def __init__(
        self,
        *,
        cache_path: str = "",
        publish_path: str = "",
        schema: str = "oarb",
        tables: Optional[List[str]] = None,
        vector_db_table: str = "",
        embedding_base_url: str = "",
        embedding_model: str = "mxbai-embed-large:latest",
        embedding_timeout_sec: float = 60.0,
    ) -> None:
        self._cache_path = cache_path or ""      # строка; пустая => in-memory DuckDB
        self._publish_path = publish_path or ""  # целевой файл снимка для навыка (CLI)
        self._schema = schema or "oarb"
        self._tables = list(tables) if tables else None
        self._vector_db_table = vector_db_table or ""
        self._embedding_base_url = embedding_base_url
        self._embedding_model = embedding_model or "mxbai-embed-large:latest"
        self._embedding_timeout_sec = float(embedding_timeout_sec)

        self._lock = threading.RLock()
        self._conn: Any = None            # DuckDB (read-write)
        self._is_ready = False
        self._index_cache: Dict[str, tuple[Any, Optional[dict]]] = {}
        self._dirty_sources: set[str] = set()
        self._dirty = False               # были новые данные с момента последнего publish
        # Описания колонок (из PG information_schema) для пересоздания пустых таблиц
        self._schema_defs: Dict[str, List[Dict[str, Any]]] = {}

        # статистика для мониторинга
        self._upserts = 0
        self._upsert_errors = 0
        self._publishes = 0
        self._publish_errors = 0
        self._last_upsert_at: Optional[str] = None
        self._last_publish_at: Optional[str] = None
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> bool:
        """Открыть (создать при отсутствии) DuckDB-кэш."""
        with self._lock:
            try:
                self._open_locked()
                self._is_ready = True
                return True
            except Exception as e:
                self._last_error = f"open: {e}"
                self._is_ready = False
                return False

    def _open_locked(self) -> None:
        import duckdb

        if self._conn is not None:
            return
        if self._cache_path:
            p = Path(self._cache_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            conn = duckdb.connect(str(p))
        else:
            conn = duckdb.connect()
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
        self._conn = conn

    def is_ready(self) -> bool:
        return self._is_ready

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            self._index_cache.clear()
            self._dirty_sources.clear()
            self._is_ready = False

    def __enter__(self) -> "AuditMemoryStore":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Приём данных (вызывается из AuditSyncService/worker-потока)
    # ------------------------------------------------------------------

    def upsert_records(self, table: str, records: List[Dict[str, Any]]) -> bool:
        """Добавить/обновить строки таблицы в локальный кэш.

        Батч заменяет существующие записи с теми же id (upsert по ключу),
        новые id — добавляются. Если в записях нет колонки ``id``, таблица
        целиком пересоздаётся из батча (с предупреждением).

        Если таблица является векторной (``vector_db_table``), источники
        (source) из батча помечаются грязными — индекс перестроится лениво
        при следующем search_vector.

        Returns:
            True при успешном сохранении, False при ошибке.
        """
        if not records:
            return True
        with self._lock:
            try:
                self._open_locked()
                self._upsert_locked(table, records)
                self._upserts += 1
                self._last_upsert_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._dirty = True
                self._mark_vector_sources_dirty(table, records)
                return True
            except Exception as e:
                self._upsert_errors += 1
                self._last_error = f"upsert {table}: {e}"
                print(f"[memory_store] Ошибка upsert {table}: {e}", file=sys.stderr)
                return False

    def ensure_schema(self, table: str, columns: List[Dict[str, Any]]) -> bool:
        """Создать таблицу по описанию колонок из источника (типы, NOT NULL, комментарии).

        Используется вместо вывода структуры из значений: так в снимок попадают
        честные PG-типы (маппинг ``_map_pg_type``) и пустые таблицы тоже
        создаются. Комментарии сохраняются в ``__schema_meta`` и возвращаются
        через :meth:`get_schema`.

        Args:
            table: полное имя таблицы (``oarb.audits``).
            columns: список описаний колонок
                ``[{"name", "type", "not_null", "comment"}, ...]``.

        Returns:
            True при успехе, False при ошибке.
        """
        if not columns:
            return True
        with self._lock:
            try:
                self._open_locked()
                self._ensure_schema_locked(table, columns)
                return True
            except Exception as e:
                self._upsert_errors += 1
                self._last_error = f"ensure_schema {table}: {e}"
                print(f"[memory_store] Ошибка ensure_schema {table}: {e}", file=sys.stderr)
                return False

    def _ensure_schema_locked(self, table: str, columns: List[Dict[str, Any]]) -> None:
        schema, name = _split_table(table)
        schema = schema or self._schema
        if not name:
            raise ValueError(f"Некорректное имя таблицы: {table!r}")

        conn = self._conn
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        full = f'"{schema}"."{name}"'
        self._schema_defs[f"{schema}.{name}"] = list(columns)
        # "__table__" — не настоящая колонка, а комментарий таблицы
        real_cols = [c for c in columns if c.get("name") != "__table__"]

        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            'WHERE table_schema = ? AND table_name = ?', [schema, name]
        ).fetchone()

        if not exists:
            cols_sql = ", ".join(
                f'"{c["name"]}" {_map_pg_type(c.get("type", ""))}'
                for c in real_cols
            )
            conn.execute(f"CREATE TABLE {full} ({cols_sql})")
        else:
            existing = [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                'WHERE table_schema = ? AND table_name = ?', [schema, name]
            ).fetchall()]
            for c in real_cols:
                if c["name"] not in existing:
                    conn.execute(
                        f'ALTER TABLE {full} ADD COLUMN "{c["name"]}" '
                        f'{_map_pg_type(c.get("type", ""))}'
                    )

        self._save_schema_meta(schema, name, columns)

    def replace_records(self, table: str, records: List[Dict[str, Any]]) -> bool:
        """Полностью пересоздать содержимое таблицы из полного батча.

        Используется при полной пересинхронизации (сверка удалённых строк):
        структура таблицы сохраняется (из ``ensure_schema`` либо существующей),
        удаляются строки, отсутствующие в батче.

        Returns:
            True при успехе, False при ошибке.
        """
        with self._lock:
            try:
                self._open_locked()
                self._replace_locked(table, records)
                self._upserts += 1
                self._last_upsert_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._dirty = True
                self._mark_vector_sources_dirty(table, records)
                return True
            except Exception as e:
                self._upsert_errors += 1
                self._last_error = f"replace {table}: {e}"
                print(f"[memory_store] Ошибка replace {table}: {e}", file=sys.stderr)
                return False

    def _replace_locked(self, table: str, records: List[Dict[str, Any]]) -> None:
        schema, name = _split_table(table)
        schema = schema or self._schema
        if not name:
            raise ValueError(f"Некорректное имя таблицы: {table!r}")

        conn = self._conn
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        full = f'"{schema}"."{name}"'

        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            'WHERE table_schema = ? AND table_name = ?', [schema, name]
        ).fetchone()
        if not exists:
            # пустой источник без сохранённого описания — создаём из батча
            if records:
                self._upsert_locked(table, records)
            return

        # Транзакция: DELETE + INSERT. Если INSERT упадёт — таблица останется
        # в исходном состоянии, без потери данных.
        conn.execute("BEGIN")
        try:
            conn.execute(f"DELETE FROM {full}")
            if not records:
                conn.execute("COMMIT")
                return

            # Без pandas: pyarrow.Table + DuckDB conn.register
            arrow_tbl = _records_to_arrow(records)
            if arrow_tbl is None:
                conn.execute("COMMIT")
                return
            existing_cols = [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                'WHERE table_schema = ? AND table_name = ?', [schema, name]
            ).fetchall()]
            insert_cols = [c for c in arrow_tbl.column_names if c in existing_cols]
            if not insert_cols:
                conn.execute("COMMIT")
                return
            cols_csv = ",".join(f'"{c}"' for c in insert_cols)
            conn.register("_replace_arrow", arrow_tbl)
            try:
                conn.execute(
                    f"INSERT INTO {full} ({cols_csv}) "
                    f"SELECT {cols_csv} FROM _replace_arrow"
                )
            finally:
                conn.unregister("_replace_arrow")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    # -- метаданные схемы (комментарии + исходные PG-типы) -------------------

    def _ensure_meta_table(self) -> None:
        conn = self._conn
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{_META_SCHEMA}"')
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{_META_SCHEMA}"."{_META_TABLE}" ('
            "schema_name TEXT, table_name TEXT, column_name TEXT, "
            "comment TEXT, pg_type TEXT)"
        )

    def _save_schema_meta(self, schema: str, table: str, columns: List[Dict[str, Any]]) -> None:
        conn = self._conn
        self._ensure_meta_table()
        table_comment = next((c.get("comment") for c in columns if c.get("name") == "__table__"), None)
        conn.execute(
            f'DELETE FROM "{_META_SCHEMA}"."{_META_TABLE}" '
            "WHERE schema_name = ? AND table_name = ?", [schema, table]
        )
        rows = []
        if table_comment:
            rows.append((schema, table, None, table_comment, None))
        for c in columns:
            if c.get("name") == "__table__":
                continue
            rows.append((schema, table, c["name"], c.get("comment"), c.get("type")))
        if rows:
            conn.executemany(
                f'INSERT INTO "{_META_SCHEMA}"."{_META_TABLE}" '
                "(schema_name, table_name, column_name, comment, pg_type) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def _load_schema_meta(self, schema: str) -> Dict[tuple, tuple]:
        """Метаданные схемы: {(table, col|None) -> (comment, pg_type)}."""
        result: Dict[tuple, tuple] = {}
        if self._conn is None:
            return result
        try:
            self._ensure_meta_table()
            rows = self._conn.execute(
                f'SELECT table_name, column_name, comment, pg_type '
                f'FROM "{_META_SCHEMA}"."{_META_TABLE}" WHERE schema_name = ?',
                [schema],
            ).fetchall()
        except Exception:
            return result
        for table, column, comment, pg_type in rows:
            result[(table, column)] = (comment, pg_type)
        return result

    def _upsert_locked(self, table: str, records: List[Dict[str, Any]]) -> None:
        schema, name = _split_table(table)
        schema = schema or self._schema
        if not name:
            raise ValueError(f"Некорректное имя таблицы: {table!r}")

        if not records:
            return

        # Колонки — из объединения ключей records (порядок появления)
        df_cols: List[str] = []
        seen = set()
        for r in records:
            if not isinstance(r, dict):
                continue
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    df_cols.append(str(k))
        if not df_cols:
            return

        conn = self._conn
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        full = f'"{schema}"."{name}"'

        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            'WHERE table_schema = ? AND table_name = ?', [schema, name]
        ).fetchone()

        if not exists:
            defs = self._schema_defs.get(f"{schema}.{name}")
            if defs:
                self._ensure_schema_locked(table, defs)
                existing_cols = [r[0] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    'WHERE table_schema = ? AND table_name = ?', [schema, name]
                ).fetchall()]
            else:
                self._ingest_arrow(table, records, df_cols, create_table=True)
                return

        existing_cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            'WHERE table_schema = ? AND table_name = ?', [schema, name]
        ).fetchall()]

        # DDL (ALTER/DROP) вне транзакции — DuckDB не откатывает DDL.
        # новые колонки (появившиеся в источнике) — добавляем с выводом типа
        for c in df_cols:
            if c not in existing_cols:
                col_values = [r.get(c) for r in records if isinstance(r, dict)]
                conn.execute(
                    f'ALTER TABLE {full} ADD COLUMN "{c}" {_infer_duckdb_type(col_values)}'
                )
        # обновим existing_cols после ALTER
        existing_cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            'WHERE table_schema = ? AND table_name = ?', [schema, name]
        ).fetchall()]

        key_col = "id" if "id" in df_cols else None
        insert_cols = [c for c in df_cols if c in existing_cols]

        # Если нет ключа — DROP (DDL), дальше _ingest_arrow сделает CREATE OR REPLACE.
        if not (key_col and key_col in existing_cols):
            print(
                f"[memory_store] Таблица {full}: нет колонки 'id', "
                "таблица пересоздаётся из батча",
                file=sys.stderr,
            )
            self._ingest_arrow(table, records, insert_cols, create_table=True)
            return

        # Транзакция: DELETE + INSERT. Если INSERT упадёт — данные останутся.
        ids = [r[key_col] for r in records
               if isinstance(r, dict) and r.get(key_col) is not None]
        conn.execute("BEGIN")
        try:
            if ids:
                conn.execute(
                    f'DELETE FROM {full} WHERE "{key_col}" IN (SELECT unnest(?))',
                    [ids],
                )
            self._ingest_arrow(table, records, insert_cols, create_table=False)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def _ingest_arrow(
        self,
        table: str,
        records: List[Dict[str, Any]],
        cols: List[str],
        create_table: bool,
    ) -> None:
        """
        Залить записи в DuckDB через pyarrow + conn.register.

        create_table=True  → CREATE OR REPLACE TABLE
        create_table=False → INSERT INTO … SELECT

        Сохраняет вложенные типы (list[float] → DOUBLE[]).
        """
        schema, name = _split_table(table)
        schema = schema or self._schema
        full = f'"{schema}"."{name}"'

        arrow_tbl = _records_to_arrow(records)
        if arrow_tbl is None:
            return

        # Если переданы конкретные колонки — проекция
        if cols:
            arrow_tbl = arrow_tbl.select([c for c in cols if c in arrow_tbl.column_names])

        if not arrow_tbl.column_names:
            return

        cols_csv = ",".join(f'"{c}"' for c in arrow_tbl.column_names)
        self._conn.register("_upsert_arrow", arrow_tbl)
        try:
            if create_table:
                self._conn.execute(
                    f"CREATE OR REPLACE TABLE {full} AS "
                    f"SELECT {cols_csv} FROM _upsert_arrow"
                )
            else:
                # Проверим, что таблица ещё существует (могла быть пересоздана)
                exists = self._conn.execute(
                    "SELECT 1 FROM information_schema.tables "
                    'WHERE table_schema = ? AND table_name = ?', [schema, name]
                ).fetchone()
                if exists is None:
                    self._conn.execute(
                        f"CREATE TABLE {full} AS "
                        f"SELECT {cols_csv} FROM _upsert_arrow"
                    )
                else:
                    self._conn.execute(
                        f"INSERT INTO {full} ({cols_csv}) "
                        f"SELECT {cols_csv} FROM _upsert_arrow"
                    )
        finally:
            self._conn.unregister("_upsert_arrow")

    def _mark_vector_sources_dirty(self, table: str, records: List[Dict[str, Any]]) -> None:
        if not self._vector_db_table:
            return
        _, vec_name = _split_table(self._vector_db_table)
        _, tbl_name = _split_table(table)
        if tbl_name != vec_name:
            return
        for r in records:
            src = r.get("source")
            if src:
                self._dirty_sources.add(str(src))
                self._index_cache.pop(str(src), None)

    # ------------------------------------------------------------------
    # Публикация снимка для навыка (CLI читает файл на чтение)
    # ------------------------------------------------------------------

    def publish(self, tables: Optional[List[str]] = None) -> bool:
        """Атомарно записать снимок таблиц в ``publish_path``.

        Навык (CLI) открывает этот файл на чтение. Gateway НЕ держит его
        открытым: публикация пишет во временный файл, затем os.replace —
        поэтому читатель в любой момент видит целостный снимок, а конфликтов
        блокировок DuckDB (один писатель на файл) не возникает.

        Если данных с прошлой публикации не менялось (``_dirty``) или
        ``publish_path`` не задан — метод ничего не делает (no-op True).
        При неудаче замены (файл занят читателем) снимок останется грязным
        и будет повторён в следующем цикле.

        Args:
            tables: какие таблицы включить в снимок (по умолчанию — конфиг).
        """
        if not self._publish_path:
            return True
        with self._lock:
            if self._conn is None or not self._dirty:
                return True
            out = [t for t in (tables or self._tables or []) if t]
            if not out:
                self._dirty = False
                return True

            target = Path(self._publish_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

            import os

            try:
                tmp_literal = "'" + str(tmp).replace("'", "''") + "'"
                self._conn.execute(f"ATTACH {tmp_literal} AS __out (READ_WRITE)")
                try:
                    copied = set()
                    for t in out:
                        schema, name = _split_table(t)
                        schema = schema or self._schema
                        # только существующие таблицы (пустые источники не создаются)
                        exists = self._conn.execute(
                            "SELECT 1 FROM information_schema.tables "
                            "WHERE table_schema = ? AND table_name = ?",
                            [schema, name],
                        ).fetchone()
                        if exists is None:
                            continue
                        self._conn.execute(f'CREATE SCHEMA IF NOT EXISTS __out."{schema}"')
                        self._conn.execute(
                            f'CREATE OR REPLACE TABLE __out."{schema}"."{name}" '
                            f'AS SELECT * FROM "{schema}"."{name}"'
                        )
                        copied.add((schema, name))
                    # метаданные схемы (комментарии) — если есть что копировать
                    meta_exists = self._conn.execute(
                        f"SELECT 1 FROM information_schema.tables "
                        f"WHERE table_schema = ? AND table_name = ?",
                        [_META_SCHEMA, _META_TABLE],
                    ).fetchone()
                    if meta_exists is not None and copied:
                        src_schemas = [c[0] for c in copied]
                        placeholders = ",".join("?" for _ in src_schemas)
                        self._conn.execute(f'CREATE SCHEMA IF NOT EXISTS __out."{_META_SCHEMA}"')
                        self._conn.execute(
                            f'CREATE OR REPLACE TABLE __out."{_META_SCHEMA}"."{_META_TABLE}" '
                            f"AS SELECT * FROM \"{_META_SCHEMA}\".\"{_META_TABLE}\" "
                            f"WHERE schema_name IN ({placeholders})",
                            src_schemas,
                        )
                finally:
                    self._conn.execute("DETACH __out")
                os.replace(tmp, target)
                self._dirty = False
                self._publishes += 1
                self._last_publish_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                return True
            except OSError as e:
                # целевой файл открыт читателем (CLI) — повтор в следующем цикле
                self._last_error = f"publish (replace): {e}"
                return False
            except Exception as e:
                self._last_error = f"publish: {e}"
                self._publish_errors += 1
                return False

    # ------------------------------------------------------------------
    # SQL-запросы
    # ------------------------------------------------------------------

    def get_schema(
        self,
        schema_name: Optional[str] = None,
        table_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        schema = schema_name or self._schema
        tables = table_names if table_names is not None else self._tables
        with self._lock:
            if self._conn is None:
                raise RuntimeError("AuditMemoryStore is not ready")

            from lib.utils.duckdb_query import build_schema

            return build_schema(self._conn, schema, tables, self._load_schema_meta)

    def query_sql(self, sql: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
        with self._lock:
            if self._conn is None:
                return {"status": "error", "row_count": 0, "columns": [], "rows": [],
                        "error": "AuditMemoryStore is not ready"}

            from lib.utils.duckdb_query import run_query

            return run_query(self._conn, sql, params)

    def explain(self, sql: str) -> Dict[str, Any]:
        with self._lock:
            if self._conn is None:
                return {"valid": False, "error": "AuditMemoryStore is not ready"}

            from lib.utils.duckdb_query import explain_query

            return explain_query(self._conn, sql)

    # ------------------------------------------------------------------
    # Векторные индексы (FAISS)
    # ------------------------------------------------------------------

    def preload_indexes(self) -> List[Dict[str, Any]]:
        """Прогреть FAISS-индексы всех источников из DuckDB-кэша в память.

        Returns:
            Список построенных индексов [{"index_name", "vectors"}, ...].
        """
        loaded: List[Dict[str, Any]] = []
        with self._lock:
            if self._conn is None or not self._vector_db_table:
                return loaded
            schema, name = _split_table(self._vector_db_table)
            schema = schema or self._schema
            try:
                sources = [
                    r[0] for r in self._conn.execute(
                        f'SELECT DISTINCT source FROM "{schema}"."{name}" '
                        'WHERE source IS NOT NULL ORDER BY source'
                    ).fetchall()
                ]
            except Exception:
                return loaded
            for src in sources:
                if src in self._index_cache:
                    idx = self._index_cache[src][0]
                else:
                    idx, meta = self._load_source_index(src)
                    if idx is not None:
                        self._index_cache[src] = (idx, meta)
                    else:
                        continue
                loaded.append({"index_name": src, "vectors": idx.ntotal})
        return loaded

    def _load_source_index(self, source: str) -> tuple[Any, Optional[dict]]:
        """Прочитать векторы source из DuckDB и построить FAISS-индекс."""
        import numpy as np
        import faiss

        if not self._vector_db_table:
            return None, None
        schema, name = _split_table(self._vector_db_table)
        schema = schema or self._schema
        full = f'"{schema}"."{name}"'

        rows = self._conn.execute(
            f'SELECT id, source, content, search_text, "table", pk_value, '
            f'chunk_index, chunk_count, row_data, embedding '
            f'FROM {full} WHERE source = ? ORDER BY id',
            [source],
        ).fetchall()
        if not rows:
            return None, None

        dimension = len(rows[0][9])
        vectors = np.zeros((len(rows), dimension), dtype=np.float32)
        metadata: dict = {"metadata": {}}

        for i, row in enumerate(rows):
            emb = row[9]
            if isinstance(emb, (list, tuple)) and len(emb) == dimension:
                vectors[i] = np.array(emb, dtype=np.float32)
            else:
                return None, None

            row_data = row[8]
            if isinstance(row_data, str):
                try:
                    row_data = json.loads(row_data)
                except (json.JSONDecodeError, TypeError):
                    row_data = {}
            elif isinstance(row_data, dict):
                pass
            else:
                row_data = {}

            metadata["metadata"][str(i)] = {
                "content": row[2] or row[3] or "",
                "search_text": row[3] or "",
                "source": row[1] or source,
                "table": row[4] or "",
                "pk_value": row[5] if row[5] is not None else i,
                "chunk_index": row[6] or 0,
                "chunk_count": row[7] or 1,
                "row": row_data or {},
            }

        index = faiss.IndexFlatIP(dimension)
        index.add(vectors)
        return index, metadata

    def search_vector(
        self,
        query: str,
        index_name: str = "default_index",
        index_path: Optional[str] = None,
        top_k: int = 5,
        threshold: Optional[float] = None,
    ) -> List[Any]:
        """Семантический поиск по локальному FAISS-индексу.

        Возвращает список ``SearchResult`` (lib.services.cache_provider).
        Индекс источника строится из DuckDB-кэша лениво и перестраивается,
        если источник был помечен грязным после upsert.
        """
        from lib.services.cache_provider import SearchResult
        from lib.services.cache_provider_impl import get_embedding

        with self._lock:
            if self._conn is None or not self._vector_db_table:
                return []

            if index_name in self._dirty_sources or index_name not in self._index_cache:
                idx, meta = self._load_source_index(index_name)
                if idx is None:
                    self._index_cache.pop(index_name, None)
                    self._dirty_sources.discard(index_name)
                    return []
                self._index_cache[index_name] = (idx, meta)
            self._dirty_sources.discard(index_name)

            idx, meta = self._index_cache.get(index_name, (None, None))
            if idx is None:
                return []

        embedding = get_embedding(query, self._embedding_base_url, self._embedding_model,
                                  timeout_sec=self._embedding_timeout_sec)
        if embedding is None:
            return []

        import numpy as np

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

    # ------------------------------------------------------------------
    # Статистика / мониторинг
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Снимок состояния хранилища для мониторинга."""
        with self._lock:
            tables = {}
            vector_sources = {}
            if self._conn is not None:
                try:
                    rows = self._conn.execute(
                        "SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_schema = ? ORDER BY table_name",
                        [self._schema],
                    ).fetchall()
                    for schema, name in rows:
                        cnt = self._conn.execute(
                            f'SELECT COUNT(*) FROM "{schema}"."{name}"'
                        ).fetchone()[0]
                        tables[name] = {"rows": cnt}
                except Exception:
                    pass
                if self._vector_db_table:
                    schema, name = _split_table(self._vector_db_table)
                    schema = schema or self._schema
                    try:
                        src_rows = self._conn.execute(
                            f'SELECT source, COUNT(*) AS cnt FROM "{schema}"."{name}" '
                            "GROUP BY source ORDER BY source"
                        ).fetchall()
                        for src, cnt in src_rows:
                            vector_sources[src] = {"rows": cnt}
                    except Exception:
                        pass

            return {
                "is_ready": self._is_ready,
                "cache_path": str(self._cache_path),
                "publish_path": str(self._publish_path),
                "schema": self._schema,
                "tables": tables,
                "vector_sources": vector_sources,
                "indexes_in_memory": {
                    src: (idx.ntotal if idx is not None else 0)
                    for src, (idx, _m) in self._index_cache.items()
                },
                "dirty_sources": sorted(self._dirty_sources),
                "dirty": self._dirty,
                "upserts": self._upserts,
                "upsert_errors": self._upsert_errors,
                "publishes": self._publishes,
                "publish_errors": self._publish_errors,
                "last_upsert_at": self._last_upsert_at,
                "last_publish_at": self._last_publish_at,
                "last_error": self._last_error,
            }
