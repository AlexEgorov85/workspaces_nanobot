"""Обёртка над ``lib.core.skill_config`` для текущего skill'а (audit_analyzer).

Все функции параметризованы в ``lib.core.skill_config`` по ``skill_name``.
Здесь — тонкие обёртки, чтобы внутренний код skill'а мог продолжать
вызывать ``from skill_config import get_db_tables`` и т.д.

Имя skill'а фиксировано в ``_SKILL_NAME``. При добавлении нового skill'а
он получает свою копию этого файла с другим ``_SKILL_NAME`` (либо
вызывает ``lib.core.skill_config`` напрямую со своим именем).
"""

from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _SKILL_ROOT.parents[2]

import sys  # noqa: E402

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.core import skill_config as _lib  # noqa: E402

_SKILL_NAME = "audit_analyzer"


def _root() -> Path:
    return _SKILL_ROOT


def get_db_tables() -> list[str]:
    return _lib.get_db_tables(_SKILL_NAME)


def get_db_schema() -> str:
    return _lib.get_db_schema(_SKILL_NAME)


def get_predefined_scripts_table() -> str:
    return _lib.get_predefined_scripts_table(_SKILL_NAME)


def load_db_config() -> dict[str, Any]:
    return _lib.load_db_config(_SKILL_NAME)


def get_llm_config() -> dict[str, Any]:
    return _lib.get_llm_config(_SKILL_NAME)


def get_tool_config() -> dict[str, Any]:
    return _lib.get_tool_config(_SKILL_NAME)


def get_cli_config() -> dict[str, Any]:
    return _lib.get_cli_config(_SKILL_NAME)


def get_max_retries() -> int:
    return _lib.get_max_retries(_SKILL_NAME)


def get_in_memory_cache_path() -> str:
    return _lib.get_in_memory_cache_path(_root())


def get_vector_index_path() -> str:
    return _lib.get_vector_index_path(_SKILL_NAME, _root())


def get_vector_db_table() -> str:
    return _lib.get_vector_db_table(_SKILL_NAME)


def get_vector_store_table() -> str:
    return _lib.get_vector_store_table()


def build_cache_provider() -> Any:
    return _lib.build_cache_provider(_SKILL_NAME, _root())


def get_vector_indexes() -> dict[str, Any]:
    return _lib.get_vector_indexes(_SKILL_NAME)


def get_embedding_config() -> dict[str, Any]:
    return _lib.get_embedding_config()


def get_embedding_model() -> str:
    return _lib.get_embedding_model()
