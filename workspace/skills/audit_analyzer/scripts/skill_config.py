"""Обёртка над ``lib.core.skill_config`` для текущего skill'а (audit_analyzer).

Все функции параметризованы в ``lib.core.skill_config`` по ``skill_name``.
Здесь — только реально используемые skill'ом обёртки, чтобы внутренний код
мог продолжать вызывать ``from skill_config import get_db_tables`` и т.д.

Имя skill'а фиксировано в ``_SKILL_NAME``. Обёртки для embedding/vector
delivery (после commit «skill configuration boundary» — общий runtime)
здесь не дублируются: используйте напрямую ``lib.core.skill_config``
без ``skill_name``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _SKILL_ROOT.parents[1]

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.core import skill_config as _lib  # noqa: E402


_SKILL_NAME = "audit_analyzer"


__all__ = [
    "get_db_tables",
    "get_db_schema",
    "get_predefined_scripts_table",
    "get_llm_config",
    "get_cli_config",
    "get_max_retries",
    "get_in_memory_cache_path",
    "build_cache_provider",
]


def get_db_tables() -> list[str]:
    return _lib.get_db_tables(_SKILL_NAME)


def get_db_schema() -> str:
    return _lib.get_db_schema(_SKILL_NAME)


def get_predefined_scripts_table() -> str:
    return _lib.get_predefined_scripts_table(_SKILL_NAME)


def get_llm_config() -> dict[str, Any]:
    return _lib.get_llm_config(_SKILL_NAME)


def get_cli_config() -> dict[str, Any]:
    return _lib.get_cli_config(_SKILL_NAME)


def get_max_retries() -> int:
    return _lib.get_max_retries(_SKILL_NAME)


def get_in_memory_cache_path() -> str:
    """Путь к DuckDB-кэшу skill'а.

    Использует ``TableRegistry.snapshot_path(workspace_root)`` — единый
    runtime-снимок ``workspace/data_store/duckdb/cache.duckdb``. Этот
    файл публикует gateway (см. ``PgDuckDbSyncService``); standalone
    CLI читает его без предварительной инициализации.
    """
    return _lib.get_in_memory_cache_path(_SKILL_ROOT)


def build_cache_provider() -> Any:
    return _lib.build_cache_provider(_SKILL_NAME, _SKILL_ROOT)
