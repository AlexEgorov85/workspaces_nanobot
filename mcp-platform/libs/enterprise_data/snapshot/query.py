"""Исполнитель запросов к снимку: rewrite / выполнение / EXPLAIN / схема.

Портировано из агента: ``lib/utils/duckdb_query.py``, читающая половина
(``rewrite_duck_sql``, ``run_query``, ``explain_query``, ``build_schema``).
Удаление агентской копии — фаза 4; позже capability ``audit`` пользуется теми же
четырьмя функциями, поэтому вынесены в отдельный модуль, а не внутрь хранилища.

Роль: **владелец DuckDB**. Единственное место платформы, где допустим
``import duckdb`` (правило 8 «один владелец на разделяемый ресурс»). Пул
PostgreSQL принадлежит ``libs/enterprise_data/db.py``, векторные индексы —
``libs/vectors``; здесь живут только строки снимка.

Модуль намеренно не импортирует ``duckdb`` на верхнем уровне: все функции
работают на переданном соединении ``conn``, поэтому импорт остаётся лёгким, а
тесты могут подставить соединение-подделку вместо файла на диске.
"""

from __future__ import annotations

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
    """Выполнить запрос на соединении DuckDB, вернуть нормализованный результат.

    Ошибка возвращается значением ``{"status": "error", ...}``, а не исключением:
    так же ведёт себя агентская копия, и отличать «запрос невалиден» от «снимок
    недоступен» (второе — исключение) обязан вызывающий.
    """
    duck_sql = rewrite_duck_sql(sql)
    try:
        if params:
            result = conn.execute(duck_sql, params)
        else:
            result = conn.execute(duck_sql)
    except Exception as e:  # noqa: BLE001 - текст ошибки уходит вызывающему
        return {
            "status": "error",
            "row_count": 0,
            "columns": [],
            "rows": [],
            "error": f"Ошибка выполнения запроса: {e}",
        }
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
    except Exception as e:  # noqa: BLE001 - текст ошибки уходит вызывающему
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
    мета, поэтому передаётся коллбеком из класса-владельца.

    Порядок таблиц в результате совпадает с порядком входного ``tables`` — это
    нужно для стабильного вывода описания схемы. SQL всё равно сортирует строки
    ``information_schema`` по ``table_name, ordinal_position`` (это часть схемы),
    но порядок самих таблиц — по входному списку.
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
            "character varying",
            "character",
            "varchar",
            "char",
        ):
            col_type = f"varchar({max_len})"
        result[tbl]["columns"][row[1]] = {
            "type": col_type,
            "not_null": row[3] == "NO",
            "comment": meta_value(tbl, row[1], 0),
        }
    if tables:
        # Упорядочить result по входному ``tables`` — стабильный порядок для
        # downstream. Колонки внутри таблицы остаются в порядке
        # ordinal_position из information_schema.
        ordered: dict[str, Any] = {}
        for t in tables:
            if t in result:
                ordered[t] = result[t]
        # Любые таблицы, не упомянутые в ``tables``, идут в конец (на случай,
        # если information_schema вернул лишние — не должно быть при IN (...)).
        for t, info in result.items():
            if t not in ordered:
                ordered[t] = info
        result = ordered
    return {"schema": schema, "tables": result}
