"""Priority-команды: источник истины — реестр библиотеки.

Раньше список ``/stop``, ``/restart``, ``/status`` был продублирован у нас
в коде, а снятие снимался ещё и с пустым ``CommandRouter._priority`` из
nanobot 0.3.5+. Теперь перечень берётся из самого реестра: поднимаем
``CommandRouter``, регистрируем встроенные команды и читаем результат.

Тест защищает две вещи, которые ломают priority-поллинг тихо:
  * перечень непустой (иначе SQL-фильтр ``content = ANY('{}')`` отберёт
    ноль кандидатов и ``/stop`` перестанет доходить мимо очереди);
  * перечень совпадает с тем, что реально зарегистрировано в библиотеке,
    то есть канал фильтрует по тем же командам, которые обрабатывает
    ``CommandRouter``.
"""

from __future__ import annotations

import pytest


class TestPriorityCommandContents:
    def test_contains_floor_set(self) -> None:
        """Регрессия для nanobot 0.3.5+: ``_priority`` — пустой dict сразу
        после ``CommandRouter()``, поэтому регистрируем встроенные команды
        перед чтением."""
        from lib.channels.message_exchange import priority_command_contents

        cmds = priority_command_contents()
        assert "/stop" in cmds
        assert "/restart" in cmds
        assert "/status" in cmds

    def test_shape_is_deterministic(self) -> None:
        from lib.channels.message_exchange import priority_command_contents

        cmds = priority_command_contents()
        assert isinstance(cmds, tuple)
        assert len(cmds) >= 3
        assert all(isinstance(c, str) and c.startswith("/") for c in cmds)
        # Порядок детерминирован между вызовами: SQL-фильтр получает один и
        # тот же список, иначе меняется текст запроса на каждый poll.
        assert cmds == priority_command_contents()

    def test_matches_router_registry(self) -> None:
        """Перечень обязан совпадать с реестром библиотеки: канал отбирает
        кандидата по этому списку, а обрабатывает его ``CommandRouter``."""
        from nanobot.command.builtin import register_builtin_commands
        from nanobot.command.router import CommandRouter

        from lib.channels.message_exchange import priority_command_contents

        router = CommandRouter()
        register_builtin_commands(router)

        assert set(priority_command_contents()) == set(router._priority)

    def test_agrees_with_public_is_priority(self) -> None:
        """Публичный ``is_priority`` и наш перечень не должны разойтись."""
        from nanobot.command.builtin import register_builtin_commands
        from nanobot.command.router import CommandRouter

        from lib.channels.message_exchange import priority_command_contents

        router = CommandRouter()
        register_builtin_commands(router)

        for cmd in priority_command_contents():
            assert router.is_priority(cmd), cmd

    def test_every_registered_command_is_covered(self) -> None:
        """Обратная сторона: команда из реестра не должна выпасть из
        перечня, иначе она потеряет priority-путь."""
        from nanobot.command.builtin import register_builtin_commands
        from nanobot.command.router import CommandRouter

        from lib.channels.message_exchange import priority_command_contents

        router = CommandRouter()
        register_builtin_commands(router)

        assert set(router._priority) <= set(priority_command_contents())
