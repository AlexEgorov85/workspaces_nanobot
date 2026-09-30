"""Владелец векторных индексов платформы.

Портировано из агента: ``lib/utils/duckdb_query.py`` (``build_faiss_index``,
``group_vector_hits``, ``build_raw_items``), ``lib/services/cache_provider_impl.py``
(``get_embedding``, ``read_embedding_config``, ``read_vector_index_config``,
``compute_index_signature``, ``verify_index_signature``,
``list_runtime_vector_indexes``), ``lib/services/text_splitter.py`` и
векторная часть ``lib/services/preload_service.py``. Удаление агентских копий —
фазы 4/5/9.

Правила слоя:

* **Здесь живёт FAISS** и только здесь. Снимок (DuckDB) — владельцу
  ``libs/enterprise_data/snapshot``; пул PostgreSQL —
  ``libs/enterprise_data/db.py``. Владелец индекса сам файл снимка **не
  открывает** и пути к нему не знает: строки приходят от capability ``data``.
* ``duckdb`` в этом пакете не импортируется вовсе.
* Индекс собирается **лениво**, по первому векторному запросу, и **один раз на
  процесс** (см. :class:`~libs.vectors.owner.VectorIndexOwner`).
* ``SearchResult`` и ``IndexIntegrityError`` не дублируются: они определены
  один раз в ``libs.enterprise_data.snapshot.contracts`` и импортируются
  оттуда. Направление ``libs.vectors → enterprise_data.snapshot.contracts``
  безопасно (хранилище FAISS не знает), обратное создало бы цикл.
"""

from libs.enterprise_data.snapshot.contracts import (
    IndexIntegrityError,
    SearchResult,
)
from libs.vectors.config import (
    read_embedding_config,
    read_embedding_defaults,
    read_vector_index_config,
    read_vector_storage_table,
)
from libs.vectors.embedding import Embedder
from libs.vectors.grouping import build_raw_items, group_vector_hits
from libs.vectors.indexing import as_vector, build_faiss_index, vector_dimension
from libs.vectors.owner import (
    STATE_BUILDING,
    STATE_ERROR,
    STATE_MISSING,
    STATE_READY,
    SnapshotReader,
    VectorIndexOwner,
)
from libs.vectors.preload import compute_index_health, format_index_health_lines
from libs.vectors.runtime import list_runtime_vector_indexes
from libs.vectors.signature import compute_index_signature, verify_index_signature
from libs.vectors.text_splitter import build_chunks, split_text

__all__ = [
    "STATE_BUILDING",
    "STATE_ERROR",
    "STATE_MISSING",
    "STATE_READY",
    "Embedder",
    "IndexIntegrityError",
    "SearchResult",
    "SnapshotReader",
    "VectorIndexOwner",
    "as_vector",
    "build_chunks",
    "build_faiss_index",
    "build_raw_items",
    "compute_index_health",
    "compute_index_signature",
    "format_index_health_lines",
    "group_vector_hits",
    "list_runtime_vector_indexes",
    "read_embedding_config",
    "read_embedding_defaults",
    "read_vector_index_config",
    "read_vector_storage_table",
    "split_text",
    "vector_dimension",
    "verify_index_signature",
]
