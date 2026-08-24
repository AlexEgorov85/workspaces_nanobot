"""Фикстуры contract-тестов.

``import nanobot.agent`` первым — фиксирует порядок импорта: прямое
``from nanobot.command.router import ...`` без загруженного ``nanobot.agent``
упирается в циркулярный импорт внутри библиотеки.
"""

from __future__ import annotations

import nanobot.agent  # noqa: F401
