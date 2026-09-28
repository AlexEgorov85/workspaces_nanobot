"""Регресс-тесты для priority_commands source of truth.

Покрывает контракт с ``nanobot.command.router.CommandRouter``:
  * в 0.3.5+ ``_priority`` атрибут существует, но ``{}`` сразу после
    ``CommandRouter()`` — старый код ронял функцию до ``()``;
  * если router регистрирует commands — объединение с defaults;
  * если router недоступен — defaults.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def reset_module():
    """Переимпортируем модуль, чтобы has-fallback был свежим."""
    import importlib
    import lib.channels.priority_commands as m

    importlib.reload(m)
    return m


class TestGetPriorityCommandsDefaults:
    def test_returns_defaults_when_router_empty(self, reset_module):
        """Регрессия для nanobot 0.3.5+: ``CommandRouter._priority`` —
        это пустой dict после ``CommandRouter()``. Старый код
        делал ``tuple({}.keys())`` = ``()`` и был fallback.
        """
        cmds = reset_module.get_priority_commands()
        assert "/stop" in cmds
        assert "/restart" in cmds
        assert "/status" in cmds

    def test_defaults_contain_floor_set(self, reset_module):
        cmds = reset_module.get_priority_commands()
        assert len(cmds) >= 3
        assert isinstance(cmds, tuple)
        for c in cmds:
            assert isinstance(c, str)
            assert c.startswith("/")


class TestGetPriorityCommandsWithRouter:
    def test_unions_router_with_defaults(self, reset_module):
        """Если router регистрирует свои команды — они добавляются
        ПОВЕРХ defaults (не заменяют)."""

        class FakeRouter:
            priority_commands = {"my_command": object()}
            _priority = {}

        with patch.object(reset_module, "CommandRouter", FakeRouter):
            cmds = reset_module.get_priority_commands()
        assert "/stop" in cmds
        assert "my_command" in cmds

    def test_unions_with_private_priority(self, reset_module):
        """Legacy / private атрибут ``_priority`` тоже учитывается."""

        class FakeHandler:
            pass

        class FakeRouter:
            priority_commands = None
            _priority = {"/foo": FakeHandler(), "/bar": FakeHandler()}

        with patch.object(reset_module, "CommandRouter", FakeRouter):
            cmds = reset_module.get_priority_commands()
        assert "/stop" in cmds
        assert "/foo" in cmds
        assert "/bar" in cmds

    def test_deduplicates(self, reset_module):
        """Если router регистрирует ``/stop`` — он не дублируется."""

        class FakeRouter:
            priority_commands = {}
            _priority = {"/stop": object()}

        with patch.object(reset_module, "CommandRouter", FakeRouter):
            cmds = reset_module.get_priority_commands()
        assert cmds.count("/stop") == 1

    def test_accepts_list_attribute(self, reset_module):
        class FakeRouter:
            priority_commands = ["/alpha", "/beta"]
            _priority = {}

        with patch.object(reset_module, "CommandRouter", FakeRouter):
            cmds = reset_module.get_priority_commands()
        assert "/alpha" in cmds
        assert "/beta" in cmds
        assert "/stop" in cmds
