"""Единый источник списка priority-команд nanobot для каналов.

Каналы (PostgresChannel, RedisChannel) фильтруют priority polling
через этот список. Список читается из
``nanobot.command.router.CommandRouter._priority`` — единственного
реестра priority-команд в nanobot 0.3.0.

Если nanobot в будущем добавит публичный API
(``CommandRouter.priority_commands()`` или аналог), здесь стоит
переключиться на него — сейчас доступ к ``_priority`` идёт через
duck typing (``hasattr``), чтобы не зависеть от приватного API.
"""
from __future__ import annotations

import nanobot.agent  # noqa: F401  # фикс circular import в nanobot 0.3.0

from nanobot.command.router import CommandRouter


_DEFAULT_PRIORITY_COMMANDS: tuple[str, ...] = (
    "/stop",
    "/restart",
    "/status",
)


def get_priority_commands() -> tuple[str, ...]:
    """Вернуть актуальный список priority-команд nanobot.

    Порядок:
      1. Если в ``CommandRouter`` есть публичный атрибут
         ``priority_commands`` (dict/list/tuple/set) — используем его.
      2. Если доступен приватный ``_priority`` (dict[str, Handler]) —
         берём ключи.
      3. Fallback — ``_DEFAULT_PRIORITY_COMMANDS`` (захардкоженный
         список из встроенных команд nanobot 0.3.0).

    Метод ``CommandRouter.priority`` НЕ вызываем — он принимает
    ``(cmd, handler)`` для регистрации, а не возвращает данные.
    """
    router = CommandRouter()
    if hasattr(router, "priority_commands"):
        value = router.priority_commands
        if isinstance(value, dict):
            return tuple(value.keys())
        if isinstance(value, (list, tuple, set)):
            return tuple(value)
    if hasattr(router, "_priority") and isinstance(router._priority, dict):
        return tuple(router._priority.keys())
    return _DEFAULT_PRIORITY_COMMANDS
