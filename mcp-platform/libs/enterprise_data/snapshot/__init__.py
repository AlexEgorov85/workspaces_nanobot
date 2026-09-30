"""Снимок (файл кэша) во владельце ``data``.

Портировано из агента: ``lib/services/duckdb_cache_store.py`` (читающая
половина), ``lib/services/cache_provider.py`` (контракты),
``lib/utils/duckdb_query.py`` (исполнитель запросов). Удаление агентских копий
— фазы 4/5/9; до тех пор обе копии живут намеренно, платформенная — рабочая.

Состав подпакета:

* :mod:`contracts` — ``CacheAccessMode``, ``SearchResult``,
  ``IndexIntegrityError``, ошибки режима, ABC ``CacheProvider`` /
  ``CacheIngestion`` / ``CacheStore``. Одно определение
  ``SearchResult``/``IndexIntegrityError`` на платформу: отсюда их импортирует
  владелец векторов ``libs/vectors``.
* :mod:`sql_guard` — классификация запроса (DDL / DML / SELECT / OTHER) и
  политика режима доступа.
* :mod:`query` — ``rewrite_duck_sql`` / ``run_query`` / ``explain_query`` /
  ``build_schema``: исполнитель запросов, общий для capability ``data`` и
  (в фазе 4) capability ``audit``.
* :mod:`store` — :class:`~libs.enterprise_data.snapshot.store.DuckDbSnapshotStore`
  и фабрика :func:`~libs.enterprise_data.snapshot.store.open_snapshot_store`.
  Единственное место платформы, где допустим ``import duckdb``.

Правила слоя:

* FAISS-индексы здесь **не** живут — их владелец ``libs/vectors``. Хранилище
  отдаёт строки векторного хранилища и получает построенный индекс
  сверху (``index_accessor``), поэтому не знает, кто строит индексы.
* Роль записи не портирована (фаза 5): методы ``CacheIngestion`` поднимают
  ``NotImplementedError`` с явной ссылкой на фазу.
* Путь к файлу — параметр. Каталог (``gateway.cache.local_path`` у агента)
  разворачивается в файл через ``resolve_snapshot_path``.
"""

from libs.enterprise_data.snapshot.contracts import (
    CacheAccessMode,
    CacheBusyError,
    CacheIngestion,
    CacheOpenError,
    CacheProvider,
    CacheStore,
    ConfigurationError,
    IndexIntegrityError,
    ReadOnlyAssertionError,
    SearchResult,
    UnsupportedSqlError,
)
from libs.enterprise_data.snapshot.query import (
    build_schema,
    explain_query,
    rewrite_duck_sql,
    run_query,
)
from libs.enterprise_data.snapshot.sql_guard import classify_sql
from libs.enterprise_data.snapshot.store import (
    SNAPSHOT_FILENAME,
    DuckDbSnapshotStore,
    UnsupportedFilesystemError,
    extract_lock_holder,
    open_snapshot_store,
    reject_unsupported_filesystem,
    resolve_snapshot_path,
    split_table,
)

__all__ = [
    "SNAPSHOT_FILENAME",
    "CacheAccessMode",
    "CacheBusyError",
    "CacheIngestion",
    "CacheOpenError",
    "CacheProvider",
    "CacheStore",
    "ConfigurationError",
    "DuckDbSnapshotStore",
    "IndexIntegrityError",
    "ReadOnlyAssertionError",
    "SearchResult",
    "UnsupportedFilesystemError",
    "UnsupportedSqlError",
    "build_schema",
    "classify_sql",
    "explain_query",
    "extract_lock_holder",
    "open_snapshot_store",
    "reject_unsupported_filesystem",
    "resolve_snapshot_path",
    "rewrite_duck_sql",
    "run_query",
    "split_table",
]
