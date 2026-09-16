"""Обёртка над ``lib.core.skill_config`` для текущего skill'а (audit_formulation_strengthener).

Все функции параметризованы в ``lib.core.skill_config`` по ``skill_name``.
Здесь — только реально используемые skill'ом обёртки, чтобы внутренний код
мог продолжать вызывать ``from skill_config import get_chunking_config`` и т.д.

Имя skill'а фиксировано в ``_SKILL_NAME``. Обёртки для embedding/vector
delivery здесь не дублируются: skill работает только с файлами ВНД,
без векторных индексов и БД.
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


_SKILL_NAME = "audit_formulation_strengthener"


__all__ = [
    "get_chunking_config",
    "get_llm_config",
    "get_cli_config",
    "get_max_retries",
    "get_execution_config",
    "get_skill_root",
]


def get_chunking_config() -> dict[str, Any]:
    """Параметры чанкования ВНД (``chunk_size_input_ratio``, ``chunk_size``,
    ``chunk_overlap``, ``single_call_threshold``, ``max_chunks_for_execution``).
    """
    return _lib.get_chunking_config(_SKILL_NAME)


def get_llm_config() -> dict[str, Any]:
    """LLM-параметры для skill'а (max_tokens, temperature)."""
    return _lib.get_llm_config(_SKILL_NAME)


def get_cli_config() -> dict[str, Any]:
    """CLI-параметры (``default_mode``, ``timeout_sec``, ``max_retries``)."""
    return _lib.get_cli_config(_SKILL_NAME)


def get_max_retries() -> int:
    """Максимум retry при невалидном JSON от LLM."""
    return _lib.get_max_retries(_SKILL_NAME)


def get_execution_config() -> dict[str, Any]:
    """Execution-параметры (``confirmation_threshold_sec``, ``max_chunks_for_execution``)."""
    return _lib.get_execution_config(_SKILL_NAME)


def get_skill_root() -> Path:
    """Путь к корню skill'а (``workspace/skills/audit_formulation_strengthener``)."""
    return _SKILL_ROOT
