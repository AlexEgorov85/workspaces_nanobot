"""Регрессионные тесты: hook allowlist действительно блокирующий.

Спека: opencode change ``runtime-patcher-composition-cleanup``,
Requirement "Hook allowlist действительно ограничивает"
(см. ``specs/runtime/runtime-patcher/spec.md``).

Прежнее поведение: warn + продолжить импорт (false sense of security).
Новое: файл вне ``_allowed_hook_names()`` — пропускается целиком
(без импорта, без ``exec_module``, без регистрации).
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    """Временная ``workspace/hooks/`` директория."""
    d = tmp_path / "hooks"
    d.mkdir()
    return d


def test_non_allowlisted_hook_blocks_module_exec(
    tmp_path: Path,
    hooks_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Файл вне allowlist: модуль не импортируется, тело не выполняется."""
    from lib.cli import hook_loader

    monkeypatch.setattr(
        hook_loader, "_allowed_hook_names",
        lambda: frozenset(),
    )

    # Hook с side-effect на уровне модуля — открывает файл marker.
    marker = tmp_path / "marker.txt"
    (hooks_dir / "evil_hook.py").write_text(
        f"from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )

    hooks = hook_loader.scan_and_register(hooks_dir, tmp_path)

    assert hooks == []
    assert not marker.exists(), (
        "Файл вне allowlist выполнил module body — allowlist НЕ блокирует импорт!"
    )


def test_allowlisted_hook_is_registered(
    tmp_path: Path,
    hooks_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alwisted hook импортируется и инстанцируется."""
    from lib.cli import hook_loader

    monkeypatch.setattr(
        hook_loader, "_allowed_hook_names",
        lambda: frozenset({"test_hook"}),
    )

    (hooks_dir / "test_hook.py").write_text(
        "from nanobot.agent import AgentHook\n"
        "\n"
        "class TestHook(AgentHook):\n"
        "    def __init__(self, workspace_dir=None, **kwargs):\n"
        "        self.workspace_dir = workspace_dir\n"
        "\n"
        "    async def on_event(self, *args, **kwargs):\n"
        "        return None\n",
        encoding="utf-8",
    )

    hooks = hook_loader.scan_and_register(hooks_dir, tmp_path)
    assert len(hooks) == 1
    assert type(hooks[0]).__name__ == "TestHook"


def test_unknown_hook_silent_skip(
    tmp_path: Path,
    hooks_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Смесь allowlisted / non-allowlisted: только allowlisted инстанцируется."""
    from lib.cli import hook_loader

    monkeypatch.setattr(
        hook_loader, "_allowed_hook_names",
        lambda: frozenset({"known_hook"}),
    )

    (hooks_dir / "known_hook.py").write_text(
        "from nanobot.agent import AgentHook\n"
        "\n"
        "class KnownHook(AgentHook):\n"
        "    def __init__(self, workspace_dir=None, **kwargs):\n"
        "        self.workspace_dir = workspace_dir\n"
        "\n"
        "    async def on_event(self, *args, **kwargs):\n"
        "        return None\n",
        encoding="utf-8",
    )
    (hooks_dir / "unknown_hook.py").write_text(
        "from nanobot.agent import AgentHook\n"
        "\n"
        "class UnknownHook(AgentHook):\n"
        "    def __init__(self, workspace_dir=None, **kwargs):\n"
        "        self.workspace_dir = workspace_dir\n"
        "\n"
        "    async def on_event(self, *args, **kwargs):\n"
        "        return None\n",
        encoding="utf-8",
    )

    hooks = hook_loader.scan_and_register(hooks_dir, tmp_path)
    type_names = [type(h).__name__ for h in hooks]
    assert "KnownHook" in type_names
    assert "UnknownHook" not in type_names


def test_allowlist_excludes_active_files_hook() -> None:
    """``_allowed_hook_names()`` НЕ содержит ``active_files_hook`` —
    наследие предыдущего incident (см. ``tests/test_active_files_hook.py``).
    """
    from lib.cli.hook_loader import _allowed_hook_names

    allowed = _allowed_hook_names()
    assert "active_files_hook" not in allowed
    # Sanity-check: актуальные reviewed-плагины присутствуют.
    assert "session_file_redirect_hook" in allowed
    assert "recent_files_hook" in allowed
