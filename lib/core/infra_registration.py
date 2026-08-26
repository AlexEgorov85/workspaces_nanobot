"""Регистрация инфраструктурных ресурсов в ``TableRegistry``.

Единая точка для runtime (``ApplicationContext``) и standalone-утилит
(``tools/build_vectors.py``). Читает конфиг из ``gateway.vector.index.*``
и регистрирует инфра-ресурсы через ``TableRegistry.register_infra``.
"""

from __future__ import annotations

from typing import Any

from lib.services.table_registry import (
    VectorResource,
    table_registry,
)


INFRA_KEY_VECTOR_STORAGE = "vector.storage"


def _settings() -> dict[str, Any]:
    from config import SETTINGS

    return SETTINGS


def register_vector_storage() -> bool:
    """Зарегистрировать ``vector.storage`` (PG-таблица-хранилище эмбеддингов).

    Источник — ``project.json::gateway.vector.index.storage_table``.
    Регистрируется как ``VectorResource`` под ключом ``"vector.storage"``.
    Если уже зарегистрировано — не перезатирает.

    Returns:
        ``True`` если зарегистрировано в этом вызове.
    """
    settings = _settings()
    gateway_cfg = settings.get("gateway") or {}
    vector_cfg = gateway_cfg.get("vector") or {} if isinstance(gateway_cfg, dict) else {}
    index_cfg = vector_cfg.get("index") or {} if isinstance(vector_cfg, dict) else {}
    storage_table = index_cfg.get("storage_table") if isinstance(index_cfg, dict) else None

    if not (
        isinstance(storage_table, str)
        and "." in storage_table
        and not table_registry.get_infra(INFRA_KEY_VECTOR_STORAGE)
    ):
        return False

    table_registry.register_infra(
        INFRA_KEY_VECTOR_STORAGE,
        (VectorResource(name=storage_table, tracking_column="id"),),
    )
    return True
