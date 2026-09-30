"""Каталог runtime-векторных индексов.

Портировано из агента: ``lib/services/cache_provider_impl.py``
(``list_runtime_vector_indexes``). Удаление агентской копии — фазы 4/5/9.

Сохранённое требование: **источник данных обязателен.** Функция НЕ открывает
файл снимка сама и НЕ разрешает путь к нему — единственный доступ уже
полученный ``fetch_fn``. Раньше здесь был собственный ``duckdb.connect`` плюс
self-resolve ``gateway.cache.local_path``, из-за чего относительный
``local_path`` ронял ``ImportError`` и молча превращался в пустой каталог
индексов, а ``local_path`` трактовался как файл вместо каталога.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Колбэк чтения снимка: выполняет SELECT через уже открытое хранилище.
#: Возвращает нормализованный результат ``{"status", "rows", ...}`` —
#: тот же контракт, что у ``CacheProvider.query_sql``.
SnapshotFetch = Callable[[str, list[Any]], dict[str, Any]]


def list_runtime_vector_indexes(
    store_table: str,
    *,
    fetch_fn: SnapshotFetch,
) -> list[dict[str, Any]]:
    """Прочитать runtime-артефакты vector-индексов из снимка.

    Runtime-состояние индексов — это просто набор ``source`` (index_name),
    присутствующих в таблице сырых эмбеддингов (``storage_table``,
    синхронизированной в снимок). Persisted FAISS-кеша на диске нет (change
    ``remove-vector-index-store``).

    Args:
        store_table: ``schema.table`` (или ``table``) векторного хранилища.
        fetch_fn: ``(sql, params) -> result`` — чтение через уже открытое
            хранилище. **Обязателен**: пути к файлу здесь не знают.

    Returns:
        Список dict'ов с полями ``source``, ``vector_count``; остальные
        (``dimension``, ``updated_at``, ``metric``, ``signature``,
        ``metadata``) заполняются владельцем индекса — эта функция их не
        вычисляет.

        ``[]`` означает «индексов нет» (таблица-хранилище ещё не создана), а
        не «снимок недоступен»: недоступность MUST подниматься вызывающим, а
        не превращаться в «индексов нет».
    """
    if not store_table:
        return []

    schema, name = (
        store_table.split(".", 1) if "." in store_table else ("", store_table)
    )
    full = f'"{schema}"."{name}"' if schema else f'"{name}"'

    sql = (
        f"SELECT source, COUNT(*) AS vector_count "
        f"FROM {full} GROUP BY source ORDER BY source"
    )
    result = fetch_fn(sql, [])
    status = (result or {}).get("status")
    if status != "success":
        error = str((result or {}).get("error") or "unknown error")
        if _is_missing_relation(error):
            logger.warning(
                "list_runtime_vector_indexes: storage table %s отсутствует (%s); "
                "runtime-индексов нет",
                store_table, error,
            )
            return []
        raise RuntimeError(
            f"list_runtime_vector_indexes({store_table}) failed: {error}"
        )

    out: list[dict[str, Any]] = []
    for row in (result.get("rows") or []):
        if isinstance(row, dict):
            source = row.get("source")
            count = row.get("vector_count")
        else:  # хранилище, отдающее кортежи
            source = row[0] if hasattr(row, "__getitem__") else None
            count = row[1] if hasattr(row, "__getitem__") else None
        out.append({
            "source": source,
            "dimension": None,
            "vector_count": count,
            "updated_at": None,
            "metric": None,
            "signature": None,
            "metadata": {},
        })
    return out


def _is_missing_relation(error: str) -> bool:
    """Отсутствует ли в сообщении признак «таблицы/представления нет».

    Отличается от «снимок недоступен»: отсутствие таблицы-хранилища — это
    нормальное состояние (индексы ещё не собраны), а любая другая ошибка
    чтения MUST подниматься вызывающему.
    """
    lowered = error.lower()
    return (
        "does not exist" in lowered
        or "no such table" in lowered
        or "catalog error" in lowered
        or "was not found" in lowered
    )
