"""
Единый протокол для бэкенда запросов (DuckDB-кэш через CacheProvider).

Заменяет устаревший прямой psycopg2-доступ (``workspace.utils.db.fetch``)
в пользу единого ``build_cache_provider()`` — все skill-модули работают
через этот контракт, никаких обращений к PG на module-level.
"""

from __future__ import annotations

from typing import Any, Protocol


class QueryBackend(Protocol):
    """Интерфейс бэкенда запросов (какой возвращает build_cache_provider())."""

    def query_sql(self, sql: str, params: list | None = None) -> dict[str, Any]: ...

    def explain(self, sql: str) -> dict[str, Any]: ...

    def get_schema(
        self,
        schema_name: str | None = None,
        table_names: list[str] | None = None,
    ) -> dict[str, Any]: ...
