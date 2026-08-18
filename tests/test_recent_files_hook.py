"""Регрессионные тесты RecentFilesHook и auto-attach в RuntimePatcher.

Покрывают три сценария со скрина пользователя:
  1. модель забыла приложить созданный файл в message() → auto-attach
     добавляет его в result.media;
  2. модель приложила несуществующий путь (.docx не создан) → отбрасываем
     через Path.is_file();
  3. модель приложила существующий путь → не дублируем, сохраняем
     порядок (existing first, recent later);
  4. сессионная изоляция (конкурентные вопросы не путают файлы).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE = _PROJECT_ROOT / "workspace"
for p in (str(_PROJECT_ROOT), str(_WORKSPACE)):
    if p not in sys.path:
        sys.path.insert(0, p)


class _MockCtx:
    def __init__(self, session_key: str) -> None:
        self.session_key = session_key


class _MockToolCall:
    def __init__(self, name: str) -> None:
        self.name = name


def _run_after_execute_tool(hook, session_key, name, path):
    """Симулировать вызов hook.after_execute_tool после редиректа."""
    ctx = _MockCtx(session_key)
    tool_call = _MockToolCall(name)
    params = {"path": path}
    import asyncio
    asyncio.run(hook.after_execute_tool(ctx, tool_call, None, params, None))


def test_recent_files_hook_collects_write_file(tmp_path):
    from workspace.hooks.recent_files_hook import RecentFilesHook

    hook = RecentFilesHook()
    p = tmp_path / "report.md"
    p.write_bytes(b"# test")
    _run_after_execute_tool(hook, "cli:1", "write_file", str(p))
    assert hook.collected("cli:1") == [str(p)]


def test_recent_files_hook_collects_edit_and_create(tmp_path):
    from workspace.hooks.recent_files_hook import RecentFilesHook

    hook = RecentFilesHook()
    p1 = tmp_path / "a.md"
    p1.write_bytes(b"a")
    p2 = tmp_path / "b.txt"
    p2.write_bytes(b"b")
    p3 = tmp_path / "c.json"
    p3.write_bytes(b"{}")
    _run_after_execute_tool(hook, "cli:1", "write", str(p1))
    _run_after_execute_tool(hook, "cli:1", "edit", str(p2))
    _run_after_execute_tool(hook, "cli:1", "create_file", str(p3))
    collected = hook.collected("cli:1")
    assert len(collected) == 3
    assert str(p1) in collected
    assert str(p2) in collected
    assert str(p3) in collected


def test_recent_files_hook_ignores_non_file_tools(tmp_path):
    from workspace.hooks.recent_files_hook import RecentFilesHook

    hook = RecentFilesHook()
    _run_after_execute_tool(hook, "cli:1", "exec", "/tmp/whatever")
    _run_after_execute_tool(hook, "cli:1", "message", "/tmp/not-a-file.txt")
    assert hook.collected("cli:1") == []


def test_recent_files_hook_drain_clears_bucket(tmp_path):
    from workspace.hooks.recent_files_hook import RecentFilesHook

    hook = RecentFilesHook()
    p = tmp_path / "report.md"
    p.write_bytes(b"# test")
    _run_after_execute_tool(hook, "cli:1", "write_file", str(p))

    drained = hook.drain("cli:1")
    assert drained == [str(p)]
    assert hook.drain("cli:1") == []  # повторный drain пуст
    assert hook.collected("cli:1") == []


def test_recent_files_hook_session_isolation(tmp_path):
    """Разные session_key не путают файлы."""
    from workspace.hooks.recent_files_hook import RecentFilesHook

    hook = RecentFilesHook()
    p1 = tmp_path / "a.md"
    p1.write_bytes(b"a")
    p2 = tmp_path / "b.md"
    p2.write_bytes(b"b")
    _run_after_execute_tool(hook, "telegram:1", "write_file", str(p1))
    _run_after_execute_tool(hook, "cli:1", "write_file", str(p2))

    assert hook.collected("telegram:1") == [str(p1)]
    assert hook.collected("cli:1") == [str(p2)]
    assert hook.drain("telegram:1") == [str(p1)]
    # cli:1 не пострадал от drain telegram:1
    assert hook.collected("cli:1") == [str(p2)]


def test_recent_files_hook_extracts_path_from_kwargs(tmp_path):
    """Поддержка camelCase / snake_case вариантов ключа path."""
    from workspace.hooks.recent_files_hook import RecentFilesHook

    hook = RecentFilesHook()
    p = tmp_path / "a.md"
    p.write_bytes(b"a")
    _run_after_execute_tool(hook, "cli:1", "write", str(p))

    # Повторно с другим вариантом ключа
    ctx = _MockCtx("cli:1")
    tc = _MockToolCall("write")
    params = {"filePath": str(p)}
    import asyncio
    asyncio.run(hook.after_execute_tool(ctx, tc, None, params, None))

    collected = hook.collected("cli:1")
    assert len(collected) == 2


# ----------------------------------------------------------------------
# Тесты auto-attach в RuntimePatcher._wrap
# ----------------------------------------------------------------------


def _make_outbound(content="hi", media=None, session_key="cli:1"):
    """Симулировать результат original _assemble_outbound."""
    out = MagicMock()
    out.content = content
    out.media = list(media or [])
    out.metadata = {}
    return out


def test_patcher_auto_attaches_recent_files_when_media_empty(tmp_path):
    """Сценарий 1: модель создала файл, но забыла приложить в message()."""
    from lib.services.runtime_patcher import RuntimePatcher

    p = tmp_path / "report.html"
    p.write_bytes(b"<h1>Report</h1>")

    # SessionFileRedirectHook уже отредактировал params["path"] → путь реальный
    recent = MagicMock()
    recent.drain = MagicMock(return_value=[str(p)])

    patcher = RuntimePatcher()
    agent = MagicMock()

    def original_assemble(*args, **kwargs):
        return _make_outbound(content="Коллега, презентация готова!", media=[])

    agent._assemble_outbound = original_assemble

    ok, _ = patcher.patch_assemble_outbound(agent, MagicMock(), recent)
    assert ok

    msg = MagicMock()
    msg.metadata = {"session_key": "cli:1"}

    result = agent._assemble_outbound(msg, "x", [], "stop", False, None)
    assert result.media == [str(p)], (
        f"Файл должен быть auto-attached в media, получили: {result.media!r}"
    )


def test_patcher_skips_recent_files_that_dont_exist(tmp_path):
    """Сценарий 2: модель приложила несуществующий .docx (после SSRF) → отбрасываем."""
    from lib.services.runtime_patcher import RuntimePatcher

    existing = tmp_path / "report.md"
    existing.write_bytes(b"# ok")
    missing = tmp_path / "test.docx"  # не создаём!

    recent = MagicMock()
    recent.drain = MagicMock(return_value=[str(existing), str(missing)])

    patcher = RuntimePatcher()
    agent = MagicMock()

    def original_assemble(*args, **kwargs):
        return _make_outbound(content="done", media=[])

    agent._assemble_outbound = original_assemble
    ok, _ = patcher.patch_assemble_outbound(agent, MagicMock(), recent)
    assert ok

    msg = MagicMock()
    msg.metadata = {"session_key": "cli:1"}
    result = agent._assemble_outbound(msg, "x", [], "stop", False, None)

    assert str(existing) in result.media
    assert str(missing) not in result.media, (
        "Несуществующий .docx должен быть отброшен через Path.is_file()"
    )


def test_patcher_does_not_duplicate_existing_media(tmp_path):
    """Сценарий 3: модель приложила существующий файл → не дублируем."""
    from lib.services.runtime_patcher import RuntimePatcher

    p = tmp_path / "report.md"
    p.write_bytes(b"# ok")

    recent = MagicMock()
    recent.drain = MagicMock(return_value=[str(p), str(p)])  # дубль

    patcher = RuntimePatcher()
    agent = MagicMock()

    def original_assemble(*args, **kwargs):
        return _make_outbound(content="done", media=[str(p)])

    agent._assemble_outbound = original_assemble
    ok, _ = patcher.patch_assemble_outbound(agent, MagicMock(), recent)
    assert ok

    msg = MagicMock()
    msg.metadata = {"session_key": "cli:1"}
    result = agent._assemble_outbound(msg, "x", [], "stop", False, None)

    # Дубль по basename отброшен; существующий остался
    assert result.media == [str(p)]


def test_patcher_no_recent_hook_is_noop():
    """Если recent_files_hook=None — патчер работает как раньше."""
    from lib.services.runtime_patcher import RuntimePatcher

    patcher = RuntimePatcher()
    agent = MagicMock()
    agent._assemble_outbound = lambda *a, **k: _make_outbound(
        content="hi", media=["/tmp/already.md"]
    )

    ok, _ = patcher.patch_assemble_outbound(agent, MagicMock(), None)
    assert ok

    msg = MagicMock()
    msg.metadata = {"session_key": "cli:1"}
    result = agent._assemble_outbound(msg, "x", [], "stop", False, None)
    assert result.media == ["/tmp/already.md"]


def test_patcher_appends_after_existing(tmp_path):
    """Auto-attached файлы идут ПОСЛЕ тех, что модель приложила явно."""
    from lib.services.runtime_patcher import RuntimePatcher

    explicit = tmp_path / "explicit.md"
    explicit.write_bytes(b"explicit")
    auto = tmp_path / "auto.html"
    auto.write_bytes(b"<h1>auto</h1>")

    recent = MagicMock()
    recent.drain = MagicMock(return_value=[str(auto)])

    patcher = RuntimePatcher()
    agent = MagicMock()

    def original(*a, **k):
        return _make_outbound(content="x", media=[str(explicit)])

    agent._assemble_outbound = original
    ok, _ = patcher.patch_assemble_outbound(agent, MagicMock(), recent)
    assert ok

    msg = MagicMock()
    msg.metadata = {"session_key": "cli:1"}
    result = agent._assemble_outbound(msg, "x", [], "stop", False, None)
    assert result.media == [str(explicit), str(auto)]


def test_patcher_tool_audit_still_added(tmp_path):
    """Tool audit и auto-attach не конфликтуют: оба работают."""
    from lib.services.runtime_patcher import RuntimePatcher

    p = tmp_path / "report.md"
    p.write_bytes(b"x")

    audit_hook = MagicMock()
    audit_hook.drain = MagicMock(return_value=[{"name": "write_file", "status": "ok"}])
    recent = MagicMock()
    recent.drain = MagicMock(return_value=[str(p)])

    patcher = RuntimePatcher()
    agent = MagicMock()

    def original(*a, **k):
        return _make_outbound(content="x", media=[])

    agent._assemble_outbound = original
    ok, _ = patcher.patch_assemble_outbound(agent, audit_hook, recent)
    assert ok

    msg = MagicMock()
    msg.metadata = {"session_key": "cli:1"}
    result = agent._assemble_outbound(msg, "x", [], "stop", False, None)
    assert str(p) in result.media
    assert "_tool_audit" in result.metadata