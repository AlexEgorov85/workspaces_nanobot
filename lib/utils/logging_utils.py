"""Общая настройка loguru для точек входа (gateway, cli_agent, runner).

Раньше каждый модуль писал собственный ``_configure_logging`` с одинаковым
``logger.remove(); logger.add(sys.stderr, level=...)``. Единая точка здесь;
вызывающий решает, из какой секции конфига брать уровень (разные дефолты:
``cli`` → WARNING, ``gateway`` → INFO).
"""

from __future__ import annotations

import sys


def configure_loguru(level: str, *, env_var: str | None = None) -> None:
    """Настроить loguru на вывод в ``sys.stderr`` с указанным уровнем.

    Args:
        level: Уровень логирования (DEBUG/INFO/WARNING/ERROR).
        env_var: Имя переменной окружения, куда продублировать уровень
            (``os.environ.setdefault`` — не перезатирает уже заданное).
    """
    if env_var:
        import os

        os.environ.setdefault(env_var, str(level))
    try:
        from loguru import logger

        logger.remove()
        logger.add(sys.stderr, level=level)
    except Exception:
        pass
