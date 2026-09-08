"""Конфигурация и провайдеры для CLI навыка ``audit_analyzer``.

Это тонкая локальная обёртка над ``lib.core.skill_config`` —
даёт CLI-режимам ``predefined_mode`` / ``generated_sql_mode`` / ``cli`` короткие
имена без ``"audit_analyzer"`` (этот модуль импортируется из
``scripts/``, namespace уже определён).

Также собирает ``CacheProvider`` из существующего core
(``lib.services.cache_provider_impl.build_cache_provider``), который
умеет читать DuckDB-кэш + FAISS-индексы, ничего не зная про audit.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SKILL_NAME = "audit_analyzer"
_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _SKILL_ROOT.parents[1]

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.core import skill_config as _core_cfg  # noqa: E402

__all__ = [
    "get_llm_config",
    "get_db_schema",
    "get_db_tables",
    "get_in_memory_config",
    "get_vector_index_path",
    "build_cache_provider",
    "get_cli_config",
]


def get_llm_config() -> dict[str, Any]:
    """LLM-конфиг skill'а (для NL → SQL режима)."""
    return _core_cfg.get_llm_config(_SKILL_NAME)


def get_db_schema() -> str:
    """Схема skill'а (для LLM-схемы в ``generated_sql_mode``)."""
    return _core_cfg.get_db_schema(_SKILL_NAME)


def get_db_tables() -> list[str]:
    """Доменные таблицы skill'а (для LLM-схемы)."""
    return _core_cfg.get_db_tables(_SKILL_NAME)


def get_in_memory_config() -> dict[str, Any]:
    """Путь к DuckDB-кэшу skill'а.

    Использует ``TableRegistry.snapshot_path(workspace_root)`` — единый
    runtime-снимок ``workspace/data_store/duckdb/cache.duckdb``.
    Этот файл публикует gateway (см. ``PgDuckDbSyncService``); standalone
    CLI читает его без предварительной инициализации.
    """
    from lib.services.table_registry import table_registry

    workspace_root = _SKILL_ROOT.parent
    return {
        "cache_path": str(table_registry.snapshot_path(workspace_root)),
        "enabled": True,
        "engine": "duckdb",
    }


def get_vector_index_path() -> str:
    """Путь к FAISS-индексам skill'а (для vector mode CLI).

    Пусто — если не настроено (vector_search имеет свой путь из project.json).
    """
    cfg = _core_cfg.get_tool_config(_SKILL_NAME)
    raw = (
        cfg.get("mode_vector_index_path")
        or cfg.get("vector_index_default_path")
        or ""
    )
    if not raw:
        return ""
    p = Path(raw)
    return str(p if p.is_absolute() else _SKILL_ROOT / p)


def build_cache_provider() -> Any:
    """Построить ``CacheProvider`` для skill'а.

    Делегирует в ``lib.core.skill_config.build_cache_provider``
    (который собирает кэш-конфиг из ``project.json::skills.audit_analyzer``
    и возвращает готовый ``CacheProvider``). Это generic (не знает
    про audit_analyzer).
    """
    return _core_cfg.build_cache_provider(_SKILL_NAME, _SKILL_ROOT)


def get_cli_config() -> dict[str, Any]:
    """CLI-настройки: default_mode, max_retries, timeout."""
    return _core_cfg.get_cli_config(_SKILL_NAME)
