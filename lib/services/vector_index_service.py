"""Единый сервисный слой работы с векторными индексами.

Собирает в одном месте операции build-слоя:
  * создание эмбеддинга (Ollama /api/embed)         — ``get_embedding``
    (re-export из ``lib/services/cache_provider_impl`` — единая функция)
  * владение общим ``PostgresDuckDbProvider``        — ``VectorIndexBuildService``

Инструменты (``tools/build_vectors.py``) переиспользуют этот слой вместо
собственных реализаций эмбеддинга. Низкоуровневая работа делегируется
провайдеру кэша (``DuckDbCacheStore``, ``lib/services/duckdb_cache_store.py``):
поиск ``search_vector``, построение ``IndexFlatIP``, сборка индекса из
таблицы-хранилища ``gateway.vector.index.storage_table`` (``preload_indexes``).

Persisted FAISS-кеша нет: индексы не пишутся в PG и не сериализуются на диск —
они живут в памяти процесса (внутри провайдера) и собираются заново из файла
кэша при каждом старте. Поиск и прогрев индексов в память также живут в
провайдере — здесь они не дублируются, этот модуль отвечает только за build-слой.
"""

from __future__ import annotations

# Пути к проекту и workspace — чтобы `from utils.db import ...` работал
# независимо от рабочего каталога (как в cache_provider_impl).
import sys
from pathlib import Path
from typing import Any

# Единая функция эмбеддинга (сами резолвит настройки из project.json).
# Re-export сохранён, чтобы импорт `from ...vector_index_service import
# get_embedding` у внешних потребителей (tools/build_vectors.py и пр.)
# продолжал работать без изменений.
from lib.services.cache_provider_impl import get_embedding as get_embedding

_ROOT = Path(__file__).resolve().parents[2]        # корень проекта
_WORKSPACE = _ROOT / "workspace"
for _p in (str(_ROOT), str(_WORKSPACE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class VectorIndexBuildService:
    """Build-слой над общим провайдером кэша.

    Держит ОДИН экземпляр ``CacheProvider`` (не создаёт новый на каждый
    вызов), поэтому кэш индексов переиспользуется между операциями. FAISS
    собирается провайдером (``preload_indexes``) из файла кэша; персиста
    нет.

    Провайдер берётся из **единой точки создания**
    (``open_cache_provider``) — тот же путь, что у runtime и у skills.
    Имя конкретной реализации здесь не фигурирует: смена реализации не
    должна требовать правок в этом классе.

    Использование::

        >>> svc = VectorIndexBuildService()
        >>> svc.provider.search_vector("текст", index_name="audits_index")
    """

    def __init__(self, cfg: dict[str, Any] | None = None, base_dir: str = "") -> None:
        from lib.services.cache_provider import CacheAccessMode
        from lib.services.cache_provider import open_cache_provider

        self._cfg = cfg if cfg is not None else {}
        self._base_dir = base_dir
        self._provider = open_cache_provider(mode=CacheAccessMode.READ_ONLY)

    @property
    def provider(self) -> Any:
        """Общий ``CacheProvider`` — для чтения/поиска."""
        return self._provider
