"""Классификация SQL-запроса снимка: DDL / DML / SELECT / OTHER.

Портировано из агента: ``lib/services/duckdb_cache_store.py`` (``_DDL_KEYWORDS``,
``_DML_KEYWORDS``, ``_classify_sql``). Удаление агентской копии — фаза 4.

Политика read-only держится на двух уровнях, и оба важны:

1. Соединение DuckDB, открытое с ``read_only=True``, **физически** блокирует
   ``INSERT/UPDATE/DELETE``.
2. Этот классификатор — второй уровень, для случая когда соединение переоткрыто
   в RW или код пишет мимо ``query_sql``.

Классификатор смотрит только на текст запроса и на режим доступа, поэтому
проверка выполняется **до** открытия файла: отказ не оставляет снимок открытым
(это проверяет ``test_snapshot_no_file_hold.py``).
"""

from __future__ import annotations

from libs.enterprise_data.snapshot.contracts import (
    ReadOnlyAssertionError,
    UnsupportedSqlError,
)

DDL_KEYWORDS = (
    "CREATE",
    "ALTER",
    "DROP",
    "TRUNCATE",
)

DML_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "REPLACE",
)


def _has_extra_statement(sql: str) -> bool:
    """Есть ли в тексте что-то кроме одного оператора.

    Проверка по первому слову пропускает ``SELECT 1; DROP TABLE t``: начинается
    с ``SELECT``, поэтому классификатор видит чтение — а DuckDB выполняет
    цепочку. Это отвергается здесь, а не в ``sqlglot``: политика режима должна
    работать и там, где AST-разбор недоступен.

    Ложное срабатывание (точка с запятой **внутри** строкового литерала)
    намеренно не разбирается: ложный отказ громкий — исключение называет
    причину, — а ложный пропуск тихий.
    """
    trimmed = sql.strip().rstrip().rstrip(";").rstrip()
    return ";" in trimmed


def classify_sql(sql: str) -> str:
    """Классифицировать тип SQL-запроса для валидации ``query_sql``.

    Returns:
        Один из ``"SELECT" / "DML" / "DDL" / "OTHER"``.

    Raises:
        UnsupportedSqlError: явный DDL (``CREATE/ALTER/DROP/TRUNCATE``) или
            цепочка операторов.
    """
    if not isinstance(sql, str):
        raise UnsupportedSqlError(str(sql), reason="non-string SQL not supported")

    if _has_extra_statement(sql):
        raise UnsupportedSqlError(
            sql, reason="multi-statement SQL is not supported by CacheProvider"
        )

    stripped = sql.strip().lstrip("(").lstrip()
    head = stripped.split(None, 1)[0].upper() if stripped else ""

    if head in DDL_KEYWORDS:
        raise UnsupportedSqlError(
            sql, reason=f"DDL ({head}) is not supported by CacheProvider"
        )

    if head in DML_KEYWORDS:
        return "DML"

    if head == "SELECT" or head.startswith("SELECT"):
        return "SELECT"

    if head == "WITH":
        return "SELECT"

    if head == "EXPLAIN":
        return "SELECT"

    if head == "PRAGMA":
        return "SELECT"

    return "OTHER"


def assert_query_allowed(sql: str, *, read_only: bool) -> str:
    """Проверить, что запрос допустим в текущем режиме, и вернуть его тип.

    Семантика:

      * DDL (``CREATE/ALTER/DROP/TRUNCATE``) → ``UnsupportedSqlError`` в любом
        режиме;
      * ``SELECT`` → всегда разрешён;
      * ``INSERT/UPDATE/DELETE`` при ``read_only`` → ``ReadOnlyAssertionError``;
      * всё остальное → ``UnsupportedSqlError``: неопознанный текст молча
        отправлять в СУБД нельзя, иначе «проверка режима» существует только
        для слов, которые кто-то заранее перечислил.
    """
    sql_kind = classify_sql(sql)
    if sql_kind == "OTHER":
        raise UnsupportedSqlError(
            sql, reason="only SELECT/INSERT/UPDATE/DELETE are supported"
        )
    if sql_kind == "DML" and read_only:
        raise ReadOnlyAssertionError(sql)
    return sql_kind
