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

        # Windows Python 3.7+: sys.stderr.encoding по умолчанию cp1251.
        # Без reconfigure loguru получает UnicodeEncodeError на кириллице
        # в аргументах logger.error/info и либо молча теряет, либо выводит
        # мусор ``. reconfigure() заставляет stderr писать UTF-8.
        # Флаг _nanobot_reconfigured защищает от повторного вызова.
        try:
            if not getattr(sys.stderr, "_nanobot_reconfigured", False):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
                sys.stderr._nanobot_reconfigured = True  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass  # старый Python или уже сконфигурирован

        logger.remove()
        logger.add(sys.stderr, level=level)
    except Exception:
        pass
