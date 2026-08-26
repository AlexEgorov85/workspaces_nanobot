"""Обёртка над ``lib.core.skill_config`` для текущего skill'а (legal_summarizer).

Все функции параметризованы в ``lib.core.skill_config`` по ``skill_name``.
Здесь — тонкие обёртки, чтобы внутренний код skill'а мог продолжать
вызывать ``from skill_config import get_llm_config`` и т.д.

Имя skill'а фиксировано в ``_SKILL_NAME``. При добавлении нового skill'а
он получает свою копию этого файла с другим ``_SKILL_NAME`` (либо
вызывает ``lib.core.skill_config`` напрямую со своим именем).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _SKILL_ROOT.parents[2]

import sys  # noqa: E402

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.core import skill_config as _lib  # noqa: E402

_SKILL_NAME = "legal_summarizer"


def get_llm_config() -> dict[str, Any]:
    return _lib.get_llm_config(_SKILL_NAME)


def get_cli_config() -> dict[str, Any]:
    return _lib.get_cli_config(_SKILL_NAME)


def get_max_retries() -> int:
    return _lib.get_max_retries(_SKILL_NAME)


def get_chunking_config() -> dict[str, Any]:
    return _lib.get_chunking_config(_SKILL_NAME)


def get_default_length() -> str:
    return str(get_cli_config().get("default_length", "medium"))


def get_timeout_sec() -> float:
    return float(get_cli_config().get("timeout_sec", 120))
