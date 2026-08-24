"""PreloadService — прогрев FAISS-индексов при старте агента.

Тонкий сервис: только runtime-метод ``preload_vector_indexes(store)``,
который gateway вызывает после initial sync, чтобы FAISS-индексы
были готовы к первому запросу.

Legacy-методы ``preload_audit_cache`` / ``background_audit_cache_refresh``
/ ``start_audit_cache_tasks`` / ``stop_tasks`` / ``get_audit_cache_config``
/ ``_audit_settings`` удалены в рефакторинге
``refactor/core-extract-duckdb-faiss``: единственный писатель
``audit_cache.duckdb`` теперь — ``AuditMemoryStore.publish()`` через
gateway (AuditSyncService → in-memory mirror → snapshot file). CLI-агент
остаётся чистым читателем.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


class PreloadService:
    """Runtime-сервис: прогрев FAISS-индексов в память.

    Этот сервис НЕ знает о цикле запуска — он предоставляет async-метод,
    который точка входа (``gateway.py``) запускает после ``AuditSyncService``
    initial_load. Имя и API сохранены для back-compat (см.
    TARGET_ARCHITECTURE.md §34 — KEEP).
    """

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings

    async def preload_vector_indexes(self, store: Any) -> Optional[list]:
        """Прогреть FAISS-индексы из DuckDB-кэша в память (gateway).

        ``store.preload_indexes()`` — тяжёлая синхронная операция
        (читает из DuckDB, строит FAISS-индексы для каждого источника
        в ``vector_db_table``). Запускаем в ``asyncio.to_thread``, чтобы
        не блокировать event loop.

        Вызывающий (обычно ``gateway.py``) ОБЯЗАН дождаться
        ``AuditSyncService`` initial_load ПЕРЕД этим методом (иначе
        DuckDB пуст и preload вернёт ``[]``). В gateway это решается
        через ``asyncio.Event`` + таймаут 30с.

        Returns:
            Список построенных индексов вида
            ``[{"index_name": ..., "vectors": N}, ...]`` или ``None``,
            если ``store is None`` / ``not is_ready()`` / исключение.
        """
        if store is None or not store.is_ready():
            return None
        try:
            return await asyncio.to_thread(store.preload_indexes)
        except Exception:
            return None