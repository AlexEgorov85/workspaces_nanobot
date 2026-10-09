"""Единый источник списка priority-команд nanobot для каналов.

Каналы (PostgresChannel, RedisChannel) фильтруют priority polling
через этот список. Список читается из
``nanobot.command.router.CommandRouter._priority`` — единственного
реестра priority-команд в nanobot 0.3.5.

Семантика: возвращаем **объединение** встроенных команд
(``_DEFAULT_PRIORITY_COMMANDS``) и того, что зарегистрировано в
``CommandRouter``. Это гарантирует, что ``/stop``, ``/restart``,
``/status`` всегда присутствуют в выдаче — даже если у свежего
``CommandRouter._priority`` пустой ``{}`` (в nanobot 0.3.5+ атрибут
существует, но пуст до регистрации хендлеров).

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

    Шаги:
      1. Стартуем с базовых встроенных (``_DEFAULT_PRIORITY_COMMANDS``).
      2. Если в ``CommandRouter`` есть публичный атрибут
         ``priority_commands`` (dict/list/tuple/set) — добавляем его
         ключи/значения поверх.
      3. Если доступен приватный ``_priority`` (dict[str, Handler]) —
         добавляем его ключи поверх.

    Возвращаем объединение (без дублей). Никогда не возвращаем
    ``()`` если базовые непусты — это даёт стабильный floor для
    priority-polling каналов.

    Метод ``CommandRouter.priority`` НЕ вызываем — он принимает
    ``(cmd, handler)`` для регистрации, а не возвращает данные.
    """
    found: set[str] = set(_DEFAULT_PRIORITY_COMMANDS)
    extra: list[str] = []
    router = CommandRouter()

    if hasattr(router, "priority_commands"):
        value = router.priority_commands
        if isinstance(value, dict):
            extra.extend(value.keys())
        elif isinstance(value, (list, tuple, set)):
            extra.extend(value)

    if hasattr(router, "_priority") and isinstance(router._priority, dict):
        # ВАЖНО: в nanobot 0.3.5+ ``_priority`` существует как
        # пустой ``{}`` сразу после ``CommandRouter()`` — атрибут
        # есть, но содержимого нет. ``tuple({}.keys())`` = ``()``,
        # что роняет priority-polling. Здесь мы ОБЪЕДИНЯЕМ с
        # defaults, поэтому «пустой router» не даёт пустой результат.
        extra.extend(router._priority.keys())

    found.update(extra)

    # Порядок детерминирован: сначала defaults (их порядок зафиксирован
    # в ``_DEFAULT_PRIORITY_COMMANDS``), затем discovered — в порядке
    # обнаружения. ``tuple(set)`` давал плавающий порядок между
    # вызовами в пределах процесса (hash seed для ``str`` не меняется,
    # но порядок всё равно не отражает приоритет).
    ordered = [cmd for cmd in _DEFAULT_PRIORITY_COMMANDS if cmd in found]
    ordered.extend(cmd for cmd in extra if cmd in found and cmd not in ordered)
    return tuple(ordered)
