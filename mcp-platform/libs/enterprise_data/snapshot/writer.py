"""Служебные функции писателя снимка: типы PostgreSQL → DuckDB, маршалинг строк.

Портировано из агента: ``lib/services/duckdb_cache_store.py`` (``_PG_TO_DUCKDB``,
``_map_pg_type``, ``_infer_duckdb_type``, ``_records_to_arrow``, ``_safe_str``) —
писательная половина, миграция ``enterprise-mcp-platform``, фаза 5 пункт 5.1.
Удаление агентской копии — фаза 9.

Состояние (соединение DuckDB, мета-таблица, счётчики) держит
:class:`~libs.enterprise_data.snapshot.store.DuckDbSnapshotStore`; здесь только
чистые преобразования — по той же причине, что и в ``query.py``: единственное
определение поведения в одном месте, а не размазанное по методам хранилища.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

#: Ключи описания колонки, приходящего от PostgreSQL (``information_schema``).
COLUMN_SPEC_KEYS = frozenset({"name", "type", "not_null", "comment"})

#: Псевдоколонка описания: её ``comment`` — это комментарий к **таблице**,
#: а не к колонке, поэтому в ``CREATE TABLE`` она не попадает.
TABLE_COMMENT_KEY = "__table__"

#: Длины кортежей, принимаемых как ключ ``schema_meta``: ``(table, column)`` и
#: ``(schema, table, column)``.
_META_KEY_SHAPES = (2, 3)

PG_TO_DUCKDB = {
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


def map_pg_type(pg_type: str) -> str:
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
    if (
        t.startswith("array")
        or t.startswith("text[]")
        or t.startswith("_")
        or t.endswith("[]")
    ):
        # массивы в DuckDB сложны — сводим к строке-представлению
        return "VARCHAR"
    return PG_TO_DUCKDB.get(t, "VARCHAR")


def infer_duckdb_type(values: Iterable[Any]) -> str:
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


def safe_str(value: Any) -> str | None:
    """Строковое представление для гетерогенных/нестандартных значений."""
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def record_columns(records: list[Any]) -> list[str]:
    """Имена колонок батча: объединение ключей, порядок появления."""
    cols: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in record:
            name = str(key)
            if name not in seen:
                seen.add(name)
                cols.append(name)
    return cols


def validate_records(records: Any, *, table: str = "") -> list[dict[str, Any]]:
    """Проверить батч строк и вернуть его как список словарей.

    Мусорная фикстура (``None``, список чисел, список строк, ``[["a", "b"]]``)
    обязана падать **громко** с ``ValueError``: вызывающий превращает это в
    ``False`` + ``_last_error``, а не в тихую запись нуля строк.

    Raises:
        ValueError: батч не является списком словарей.
    """
    where = f" для {table!r}" if table else ""
    if not isinstance(records, list):
        raise ValueError(
            f"Ожидался список словарей{where}, получено {type(records).__name__}"
        )
    if not records:
        return []
    bad = [type(r).__name__ for r in records if not isinstance(r, dict)]
    if bad:
        raise ValueError(
            f"Ожидался список словарей{where}, не-словари во входе: {bad[:5]}"
        )
    return records


def _looks_like_column_specs(items: list[dict[str, Any]]) -> bool:
    """Похож ли список на описания колонок, а не на батч строк.

    Второй аргумент ``ensure_schema`` в контракте называется ``records``, но
    вызывающий (загрузчик) передаёт в него описание колонок из PostgreSQL.
    Оба прочтения поддержаны, поэтому форму приходится определять по форме:
    описанием считается список словарей, у каждого есть строковый ``name``,
    ключи не выходят за ``COLUMN_SPEC_KEYS`` и хотя бы у одного есть ключ
    кроме ``name`` (иначе ``[{"name": "x"}]`` — это строка данных, а не
    описание колонки без типа).
    """
    if not items:
        return False
    if not all(isinstance(item.get("name"), str) and item["name"] for item in items):
        return False
    if not all(set(item) <= COLUMN_SPEC_KEYS for item in items):
        return False
    return any(set(item) - {"name"} for item in items)


def _column_specs_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Описать колонки по значениям батча (типы выводятся, комментариев нет)."""
    return [
        {
            "name": name,
            "type": infer_duckdb_type(row.get(name) for row in rows),
            "not_null": False,
            "comment": None,
        }
        for name in record_columns(rows)
    ]


def resolve_column_specs(records: Any, *, table: str = "") -> list[dict[str, Any]]:
    """Привести второй аргумент ``ensure_schema`` к описаниям колонок.

    Принимаются обе формы, потому что так объявлен контракт
    (``CacheIngestion.ensure_schema``) и так его зовёт единственный писатель:

    * описание колонок из источника —
      ``[{"name", "type", "not_null", "comment"}, ...]``;
    * батч строк — структура выводится по значениям, типы — ``infer_duckdb_type``.

    Raises:
        ValueError: вход не является ни описанием колонок, ни батчем строк.
    """
    items = validate_records(records, table=table)
    if not items:
        return []
    if _looks_like_column_specs(items):
        return [
            {
                "name": item["name"],
                "type": item.get("type", ""),
                "not_null": bool(item.get("not_null", False)),
                "comment": item.get("comment"),
            }
            for item in items
        ]
    # Любой другой список словарей — батч строк: описания колонок выводятся по
    # значениям. Молча выбрасывать такой вход нельзя.
    return _column_specs_from_rows(items)


def meta_column_name(key: Any) -> str | None:
    """Имя колонки из ключа ``schema_meta`` (``None`` — ключ не распознан).

    Ключ приходит как ``(table, column)`` — та же форма значения, что и
    ``_load_schema_meta`` отдаёт на чтение. Плоский ``(column,)`` тоже
    принимается: привести его к ``schema_meta`` проще, чем отказать.
    """
    if isinstance(key, str):
        return key
    if not isinstance(key, tuple) or not key:
        return None
    if len(key) == 1 and isinstance(key[0], str):
        return key[0]
    if len(key) in _META_KEY_SHAPES and isinstance(key[-1], str) and key[-1]:
        return key[-1]
    return None


def arrow_module() -> Any:
    """Лениво импортировать ``pyarrow``; ``ImportError`` — вызывающий решает.

    Импорт внутри функции по двум причинам: пакет объявлен в манифесте сервера,
    но capability, с ним не связанная, обязана подниматься и без него; и
    отсутствие пакета должно быть **решением писателя** (построчный
    ``INSERT``), а не падением импорта на старте процесса.
    """
    import pyarrow as pa

    return pa


def records_to_arrow(records: list[dict[str, Any]]) -> Any:
    """Сериализовать ``list[dict]`` в ``pyarrow.Table`` (без pandas).

    Сохраняет вложенные типы:

    * ``list[number]`` → ``DOUBLE[]`` (DuckDB при ``register``);
    * ``dict``/``list[str]`` → JSON-строки (в ``VARCHAR``-колонку);
    * ``None`` → null.

    pyarrow умеет сам вывести типы; для ``embedding`` (``list[float]``) это
    даёт ``list<float64>``, который DuckDB читает как ``DOUBLE[]``.

    Returns:
        ``pyarrow.Table`` либо ``None``, если колонок нет.

    Raises:
        ImportError: ``pyarrow`` не установлен (вызывающий переходит на
            построчный ``INSERT``).
    """
    pa = arrow_module()
    if not records:
        return None

    cols = record_columns(records)
    if not cols:
        return None

    # Сборка по колонкам: pa.array() с auto-типом
    arrays = {}
    for col in cols:
        col_data = [r.get(col) for r in records]
        try:
            arrays[col] = pa.array(col_data)
        except (pa.lib.ArrowInvalid, TypeError):
            # фоллбэк: всё строкой
            arrays[col] = pa.array([safe_str(v) for v in col_data])

    return pa.table(arrays)


def bindable_values(
    records: list[dict[str, Any]],
    cols: list[str],
) -> list[list[Any]]:
    """Значения батча по списку колонок — для построчного ``INSERT``."""
    return [[record.get(col) for col in cols] for record in records]


def nested_columns(records: list[dict[str, Any]], cols: list[str]) -> list[str]:
    """Колонки со вложенными значениями (``list``/``dict``).

    Их нельзя положить в скалярную колонку построчным ``INSERT``: DuckDB либо
    не примет значение, либо — что хуже — приведёт его к строке, и данные
    изменятся молча. Путь без ``pyarrow`` обязан отказать громко.
    """
    return sorted(
        {
            col
            for record in records
            for col in cols
            if isinstance(record.get(col), (list, tuple, dict))
        }
    )
