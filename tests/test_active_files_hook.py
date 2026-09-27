"""Защитный тест: ActiveFilesHook удалён и не должен восстанавливаться.

Инцидент 2026-08-27 (consolidator archives file references) остаётся
нерешённым (см. ``docs/architecture/decisions/active-files-hook-removal.md``).
Этот тест защищает от регрессии: если кто-то случайно восстановит
``workspace/hooks/active_files_hook.py``, тест упадёт.

Контракт:
* Файл ``workspace/hooks/active_files_hook.py`` НЕ ДОЛЖЕН существовать.
* ``lib/cli/hook_loader.py::scan_and_register`` silent-пропускает файлы,
  которых нет в allowlist (active_files_hook в нём нет).
* Никакой runtime-код не читает
  ``session.metadata["user_attachments"|"agent_files"]`` (проверено
  grep'ом в docs/architecture/decisions/active-files-hook-removal.md).
"""

from __future__ import annotations

from pathlib import Path


def test_active_files_hook_file_does_not_exist() -> None:
    repo = Path(__file__).resolve().parent.parent
    hook_file = repo / "workspace" / "hooks" / "active_files_hook.py"
    assert not hook_file.exists(), (
        f"{hook_file} удалён как мёртвый код после upgrade до "
        f"nanobot-ai 0.3.5; см. docs/architecture/decisions/"
        f"active-files-hook-removal.md. Если нужно восстановить "
        f"side-channel — откройте OpenSpec change с обоснованием."
    )


def test_hook_loader_allowlist_excludes_active_files_hook() -> None:
    """``hook_loader._allowed_hook_names()`` НЕ содержит active_files_hook."""
    from lib.cli.hook_loader import _allowed_hook_names

    allowed = _allowed_hook_names()
    assert "active_files_hook" not in allowed, (
        "ActiveFilesHook удалён — allowlist hook_loader должен "
        "содержать только reviewed плагины"
    )
    # Sanity-check: актуальные reviewed-плагины присутствуют.
    assert "session_file_redirect_hook" in allowed
    assert "recent_files_hook" in allowed
