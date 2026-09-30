"""Контракт адаптера приватных CLI-хелперов nanobot.

`lib/cli/console_loop.py` не должен содержать version-specific импортов
приватных REPL-хелперов — их расположение мигрировало между
nanobot 0.3.0 (`nanobot.cli.commands`) и 0.3.5 (`nanobot.cli.terminal`).
Этот модуль фиксирует контракт `lib/cli/nanobot_cli_compat.py`.
"""

from __future__ import annotations

import pytest

from lib.cli.nanobot_cli_compat import (
    get_logo_version,
    get_repl_helpers,
    model_display,
)

pytestmark = pytest.mark.contract

_REQUIRED_HELPERS = frozenset(
    {
        "_init_prompt_session",
        "_is_exit_command",
        "_read_interactive_input_async",
        "_restore_terminal",
        "_model_display",
        "_sanitize_surrogates",
    }
)


def test_all_repl_helpers_resolve() -> None:
    helpers = get_repl_helpers()

    assert _REQUIRED_HELPERS <= set(helpers), (
        f"missing REPL helpers: {sorted(_REQUIRED_HELPERS - set(helpers))}"
    )
    for name, value in helpers.items():
        assert callable(value), f"{name} is not callable"


def test_terminal_helpers_live_in_035_layout() -> None:
    """nanobot 0.3.5 перенёс REPL-хелперы в ``nanobot.cli.terminal``."""
    from nanobot.cli import terminal

    for name in (
        "_init_prompt_session",
        "_is_exit_command",
        "_read_interactive_input_async",
        "_restore_terminal",
    ):
        assert hasattr(terminal, name), f"nanobot.cli.terminal.{name} missing"
        assert getattr(terminal, name) is get_repl_helpers()[name]


def test_commands_helpers_stay_in_commands() -> None:
    """``_model_display``/``_sanitize_surrogates`` доступны через ``nanobot.cli.commands``."""
    from nanobot.cli import commands

    for name in ("_model_display", "_sanitize_surrogates"):
        assert hasattr(commands, name), f"nanobot.cli.commands.{name} missing"
        assert getattr(commands, name) is get_repl_helpers()[name]


def test_console_loop_has_no_direct_private_imports() -> None:
    """REPL-helper'ы в console_loop берутся только через адаптер."""
    import inspect

    from lib.cli import console_loop

    source = inspect.getsource(console_loop.run_repl)

    assert "from nanobot.cli.commands import" not in source
    assert "from nanobot.cli.terminal import" not in source
    assert "nanobot_cli_compat" in source


def test_logo_and_version_from_public_package() -> None:
    import nanobot

    logo, version = get_logo_version()

    assert logo == nanobot.__logo__
    assert version == nanobot.__version__


def test_model_display_returns_str_pair() -> None:
    from unittest.mock import MagicMock

    model, preset_tag = model_display(MagicMock())

    assert isinstance(model, str), type(model)
    assert isinstance(preset_tag, str), type(preset_tag)


def test_resolved_cache_is_reused() -> None:
    """Повторный вызов не переимпортирует модули (кеш резолва)."""
    assert get_repl_helpers() == get_repl_helpers()


def test_missing_helper_reports_all_names(monkeypatch) -> None:
    """Отсутствие хелпера → AttributeError со списком имён, не ImportError."""
    import lib.cli.nanobot_cli_compat as compat

    monkeypatch.setattr(compat, "_resolved", None)
    monkeypatch.setattr(compat, "_TERMINAL_HELPERS", ("_init_prompt_session", "_nope_0"))

    with pytest.raises(AttributeError) as excinfo:
        compat.get_repl_helpers()

    message = str(excinfo.value)
    assert "_nope_0" in message
    assert "searched" in message
