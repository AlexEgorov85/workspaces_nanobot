"""
Единый протокол для бэкенда запросов (DuckDB-кэш / PostgreSQL).

Заменяет устаревший ``from database import Database, QueryBackend``.
Теперь ни один модуль не делает импорт ``database``, который на module-level
пускал connect-backoff к PostgreSQL (resolve_dsn → configure).

Обратная совместимость:
    ``database.py`` переписан так, чтобы при прямом импорте не подключаться
    к PG и не писать DeprecationWarning — просто экспортирует Protocol +
    тонкую обёртку над CacheProvider (lib.services).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class QueryBackend(Protocol):
    """Интерфейс бэкенда запросов (какой возвращает build_cache_provider())."""

    def query_sql(self, sql: str, params: Optional[list] = None) -> Dict[str, Any]: ...

    def explain(self, sql: str) -> Dict[str, Any]: ...

    def get_schema(
        self,
        schema_name: Optional[str] = None,
        table_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]: ...
