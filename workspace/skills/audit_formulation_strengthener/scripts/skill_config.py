"""Обёртка над ``lib.core.skill_config`` для audit_formulation_strengthener.

Ровно четыре обёртки — get_llm_config / get_cli_config / get_max_retries /
get_chunking_config. Параметр ``execution.max_chunks_for_execution`` читается
напрямую через ``lib.core.skill_config.get_tool_config`` в
``scripts/vnd_io.py`` — пятой обёртки здесь не добавляем.

Имя skill'а фиксировано в ``_SKILL_NAME``.
"""

from __future__ import annotations

from typing import Any

from lib.core import skill_config as _lib


_SKILL_NAME = "audit_formulation_strengthener"


__all__ = [
    "get_chunking_config",
    "get_llm_config",
    "get_cli_config",
    "get_max_retries",
]


def get_chunking_config() -> dict[str, Any]:
    """Параметры чанкования ВНД (``chunk_size``, ``chunk_overlap``)."""
    return _lib.get_chunking_config(_SKILL_NAME)


def get_llm_config() -> dict[str, Any]:
    """LLM-параметры для skill'а (``max_tokens``, ``temperature``)."""
    return _lib.get_llm_config(_SKILL_NAME)


def get_cli_config() -> dict[str, Any]:
    """CLI-параметры (``default_mode``, ``timeout_sec``, ``max_retries``)."""
    return _lib.get_cli_config(_SKILL_NAME)


def get_max_retries() -> int:
    """Максимум retry при невалидном JSON от LLM (из cli.max_retries)."""
    return _lib.get_max_retries(_SKILL_NAME)
