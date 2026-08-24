"""Единый сервисный слой работы с векторными индексами.

Собирает в одном месте все операции над FAISS-индексами:
  * создание эмбеддинга (Ollama /api/embed)         — ``get_embedding``
    (re-export из ``lib/services/cache_provider_impl`` — единая функция)
  * пересборка индекса из сырых векторов и персист
    в store (public.agent_vector_index_store)        — ``VectorIndexBuildService``

Навык (``workspace/skills/audit_analyzer``) и инструменты
(``tools/build_vectors.py``) переиспользуют этот слой вместо собственных
реализаций эмбеддинга/сборки. Низкоуровневая работа делегируется
``PostgresDuckDbProvider`` (``lib/services/cache_provider_impl.py``):
поиск ``search_vector``, построение ``IndexFlatIP``, сохранение blob'а.

Поиск и прогрев индексов в память уже живут в провайдере —
здесь они не дублируются, этот модуль отвечает только за build-слой.
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
    """Пересборка и персист FAISS-индексов через общий провайдер.

    Держит ОДИН экземпляр ``PostgresDuckDbProvider`` (не создаёт новый
    на каждый вызов), поэтому кэш индексов ``_index_cache`` переиспользуется
    между операциями. Используется ``tools/build_vectors.py`` после вставки
    новых/удаления старых векторов в ``mode_vector_db_table``.

    Пример:
        >>> svc = VectorIndexBuildService()
        >>> n = svc.rebuild_and_store("audits_index", "oarb.audit_vectors")
    """

    def __init__(self, cfg: dict[str, Any] | None = None, base_dir: str = "") -> None:
        from lib.services.cache_provider_impl import build_cache_provider

        self._cfg = cfg if cfg is not None else {}
        self._base_dir = base_dir
        self._provider = build_cache_provider(self._cfg, base_dir)

    @property
    def provider(self) -> Any:
        """Общий провайдер (PostgresDuckDbProvider) — для чтения/поиска."""
        return self._provider

    def rebuild_and_store(self, source: str, db_table: str) -> int | None:
        """Перестроить индекс ``source`` из сырых векторов и сохранить в store.

        Инвалидирует кэш, читает векторы ``source`` из ``db_table``,
        строит ``IndexFlatIP`` и сохраняет blob в
        ``mode_vector_store_table``. Возвращает число векторов или
        ``None`` при ошибке (в т.ч. отсутствии faiss/numpy).
        """
        self._provider.invalidate_cache(source)
        try:
            return self._provider.rebuild_and_store_index(source, db_table)
        except (ImportError, ModuleNotFoundError):
            return None
