"""Общие условия прогона платформы.

DSN задаётся один раз на сессию. Причина не в удобстве: сервер обязан
отказываться подниматься без DSN (иначе буфер журнала молча теряет каждое
событие), поэтому тестам нужен адрес — настоящий он не требуется, пул
подключается лениво.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))


@pytest.fixture(autouse=True, scope="session")
def _platform_environment() -> None:
    """Задать DSN окружения платформы на время сессии."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"
    yield
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous
