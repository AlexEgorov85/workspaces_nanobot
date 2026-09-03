"""Общие примитивы для работы с DuckDB-кэшем: query/schema/explain и группировка чанков.

Используется классами, которые держат собственное DuckDB-соединение
(``PostgresDuckDbProvider``, ``DuckDbCacheStore``). DuckDB здесь намеренно
не импортируется — все функции работают на переданном соединении ``conn``,
поэтому импорт модуля остаётся лёгким и без побочных эффектов.

Единая точка для:
  * адаптации SQL к DuckDB (``%s`` → ``?``, ``TO_CHAR`` → ``strftime``);
  * выполнения запроса / EXPLAIN в нормализованном виде;
  * сборки схемы из ``information_schema``;
  * группировки векторных совпадений (один документ = одно место в top_k).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

# DuckDB не поддерживает TO_CHAR(date, 'Month') — переписываем в strftime.
REWRITE_TO_CHAR = re.compile(r"TO_CHAR\((\w+)\s*,\s*'Month'\)", re.IGNORECASE)


def rewrite_duck_sql(sql: str) -> str:
    """Адаптировать SQL к DuckDB: ``%s`` → ``?``, ``TO_CHAR(.., 'Month')`` → ``strftime``."""
    duck_sql = sql.replace("%s", "?")
    return REWRITE_TO_CHAR.sub(r"strftime(\1, '%B')", duck_sql)


def run_query(
    conn: Any,
    sql: str,
    params: list[Any] | None = None,
) -> dict[str, Any]:
    """Выполнить запрос на DuckDB-соединении, вернуть нормализованный результат."""
    duck_sql = rewrite_duck_sql(sql)
    try:
        if params:
            result = conn.execute(duck_sql, params)
        else:
            result = conn.execute(duck_sql)
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
        "rows": [dict(zip(columns, r, strict=False)) for r in rows],
    }


def explain_query(conn: Any, sql: str) -> dict[str, Any]:
    """EXPLAIN на DuckDB — синтаксическая проверка без выполнения."""
    duck_sql = rewrite_duck_sql(sql)
    try:
        result = conn.execute(f"EXPLAIN {duck_sql}")
        columns = [desc[0] for desc in result.description]
        plan = [dict(zip(columns, r, strict=False)) for r in result.fetchall()]
        return {"valid": True, "plan": plan}
    except Exception as e:
        return {"valid": False, "error": f"EXPLAIN failed: {e}"}


def build_schema(
    conn: Any,
    schema: str,
    tables: list[str] | None,
    meta_reader: Callable[[str], dict[tuple, tuple]],
) -> dict[str, Any]:
    """Собрать схему таблиц из ``information_schema`` DuckDB.

    ``meta_reader(schema)`` возвращает ``{(table, column): (comment, pg_type)}``
    — комментарии и исходные PG-типы. Реализация зависит от того, где хранится
    мета (в DuckDB-файле, в зеркале): передаётся коллбеком из класса-владельца.
    """
    sql = (
        "SELECT table_name, column_name, data_type, is_nullable, "
        "character_maximum_length "
        "FROM information_schema.columns WHERE table_schema = ?"
    )
    params: list[Any] = [schema]
    if tables:
        placeholders = ",".join("?" for _ in tables)
        sql += f" AND table_name IN ({placeholders})"
        params.extend(tables)
    sql += " ORDER BY table_name, ordinal_position"

    rows = conn.execute(sql, params).fetchall()
    meta = meta_reader(schema)

    def meta_value(table: str, column: str | None, idx: int) -> Any:
        val = meta.get((table, column))
        return val[idx] if val else None

    result: dict[str, Any] = {}
    for row in rows:
        tbl = row[0]
        if tbl not in result:
            result[tbl] = {"comment": meta_value(tbl, None, 0), "columns": {}}
        col_type = row[2]
        max_len = row[4]
        # Исходный PG-тип (если сохранён в schema-meta) — точнее DuckDB
        pg_type = meta_value(tbl, row[1], 1)
        if pg_type:
            col_type = pg_type
        elif max_len and str(col_type).lower() in (
            "character varying", "character", "varchar", "char",
        ):
            col_type = f"varchar({max_len})"
        result[tbl]["columns"][row[1]] = {
            "type": col_type,
            "not_null": row[3] == "NO",
            "comment": meta_value(tbl, row[1], 0),
        }
    return {"schema": schema, "tables": result}


def build_raw_items(
    meta_items: dict[str, Any],
    scores,
    ids,
    index_name: str,
    threshold: float | None,
) -> list[dict[str, Any]]:
    """Собрать сырые чанки-строки из результатов поиска FAISS."""
    raw: list[dict[str, Any]] = []
    for score, doc_id in zip(scores[0], ids[0], strict=False):
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
    return raw


def group_vector_hits(
    raw: list[dict[str, Any]],
    top_k: int = 5,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Группировка чанков: один документ = одно место в top_k.

    Принимает сырые чанки (содержат ``source``, ``table``, ``pk_value``,
    ``score``, ``chunk_index``, ``chunk_total``) и возвращает документы с
    ``matched_chunks``, отсортированные по ``score`` (срезанные до ``top_k``).
    """
    doc_groups: dict[tuple, dict[str, Any]] = {}
    for r in raw:
        key = (r["source"], r["table"], r["pk_value"])
        if key not in doc_groups or r["score"] > doc_groups[key]["score"]:
            entry = dict(r)
            entry["matched_chunks"] = 1
            doc_groups[key] = entry
        else:
            doc_groups[key]["matched_chunks"] += 1

    results = sorted(doc_groups.values(), key=lambda r: r["score"], reverse=True)
    threshold_active = threshold is not None and threshold > 0
    if top_k and not threshold_active:
        results = results[:top_k]

    for r in results:
        r.pop("chunk_index", None)
        r.pop("chunk_total", None)

    return results


def build_faiss_index(
    records: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any] | None]:
    """Построить FAISS ``IndexFlatIP`` + metadata из списка записей.

    ``records`` — список словарей с ключами: ``source``, ``content``,
    ``search_text``, ``table``, ``pk_value``, ``chunk_index``, ``chunk_count``,
    ``row_data``, ``embedding``.

    Возвращает ``(index, metadata)`` или ``(None, None)`` (пусто/несоответствие
    размерности). Единая точка сборки FAISS для gateway-кэша и снимка навыка.
    """
    import faiss
    import numpy as np

    if not records:
        return None, None
    dimension = len(records[0]["embedding"])
    vectors = np.zeros((len(records), dimension), dtype=np.float32)
    metadata: dict[str, Any] = {"metadata": {}}

    for i, rec in enumerate(records):
        emb = rec["embedding"]
        if isinstance(emb, (list, tuple)) and len(emb) == dimension:
            vectors[i] = np.array(emb, dtype=np.float32)
        else:
            return None, None

        row_data = rec.get("row_data")
        if isinstance(row_data, str):
            try:
                row_data = json.loads(row_data)
            except (json.JSONDecodeError, TypeError):
                row_data = {}
        elif not isinstance(row_data, dict):
            row_data = {}

        metadata["metadata"][str(i)] = {
            "content": rec.get("content") or rec.get("search_text") or "",
            "search_text": rec.get("search_text") or "",
            "source": rec.get("source") or "",
            "table": rec.get("table") or "",
            "pk_value": rec.get("pk_value") if rec.get("pk_value") is not None else i,
            "chunk_index": rec.get("chunk_index") or 0,
            "chunk_count": rec.get("chunk_count") or 1,
            "row": row_data or {},
        }

    index = faiss.IndexFlatIP(dimension)
    index.add(vectors)
    return index, metadata

