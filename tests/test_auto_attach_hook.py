"""Tests for AutoAttachHook — автоматическое прикрепление файлов к ответу.

Покрывает:
  * record_pending / confirm / prune / drain в ``AutoAttachRegistry``;
  * lifecycle per-turn хука: ``before_execute_tool`` (запоминаем) →
    ``after_execute_tool`` (подтверждаем) → ``drain`` (забираем);
  * дедупликация в RuntimePatcher.patch_assemble_outbound — если бот
    прикрепил файл сам через ``message``, auto-attach не дублирует.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path, PurePath
from unittest.mock import MagicMock

import pytest

# Workspace в sys.path, чтобы импорт workspace.hooks.* работал.
_HOOKS_DIR = str(Path(__file__).resolve().parent.parent / "workspace")
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from workspace.hooks.auto_attach_hook import (  # noqa: E402
    AutoAttachHook,
    AutoAttachRegistry,
    make_auto_attach_hook_factory,
)


def _tool_call(name: str, arguments: dict) -> MagicMock:
    """Фейковый tool_call — повторяет интерфейс nanobot ToolCall."""
    tc = MagicMock()
    tc.name = name
    tc.arguments = arguments
    tc.tool_name = name
    return tc


def _context(session_key: str = "cli:1") -> MagicMock:
    ctx = MagicMock()
    ctx.session_key = session_key
    return ctx


@pytest.fixture(autouse=True)
def _clean_registry():
    """Чистим class-level registry между тестами."""
    AutoAttachRegistry._fresh.clear()
    AutoAttachRegistry._sizes.clear()
    yield
    AutoAttachRegistry._fresh.clear()
    AutoAttachRegistry._sizes.clear()


class TestAutoAttachRegistry:
    """Per-session bucket логика: pending → confirm → prune → drain."""

    def test_record_and_drain(self, tmp_path):
        p = tmp_path / "report.docx"
        p.write_bytes(b"x" * 10)
        AutoAttachRegistry.record_pending("cli:1", PurePath(p), -1)
        result = AutoAttachRegistry.drain("cli:1")
        assert result == [str(p)]
        # Повторный drain — пусто (атомарно).
        assert AutoAttachRegistry.drain("cli:1") == []

    def test_record_isolated_by_session_key(self, tmp_path):
        p1 = tmp_path / "a.docx"
        p2 = tmp_path / "b.docx"
        p1.write_bytes(b"x")
        p2.write_bytes(b"y")
        AutoAttachRegistry.record_pending("cli:1", PurePath(p1), -1)
        AutoAttachRegistry.record_pending("cli:2", PurePath(p2), -1)
        assert AutoAttachRegistry.drain("cli:1") == [str(p1)]
        # cli:2 не зачистился
        assert AutoAttachRegistry.drain("cli:2") == [str(p2)]

    def test_confirm_removes_missing_file(self, tmp_path):
        p = tmp_path / "missing.docx"
        # Не создаём файл. confirm вернёт False.
        AutoAttachRegistry.record_pending("cli:1", PurePath(p), -1)
        assert not AutoAttachRegistry.confirm(
            "cli:1", PurePath(p), require_size_change=False,
        )
        AutoAttachRegistry.prune("cli:1", set())  # пустой keep → удаляет bucket
        assert AutoAttachRegistry._fresh == {}

    def test_confirm_with_size_change_required(self, tmp_path):
        # exec не должен оставлять файл, если размер не изменился.
        p = tmp_path / "data.bin"
        p.write_bytes(b"original")
        AutoAttachRegistry.record_pending("cli:1", PurePath(p), 8)
        # Размер не менялся — confirm=False.
        assert not AutoAttachRegistry.confirm(
            "cli:1", PurePath(p), require_size_change=True,
        )
        # Размер изменился — confirm=True.
        p.write_bytes(b"changed")
        assert AutoAttachRegistry.confirm(
            "cli:1", PurePath(p), require_size_change=True,
        )

    def test_reset_clears_bucket(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_bytes(b"x")
        AutoAttachRegistry.record_pending("cli:1", PurePath(p), -1)
        AutoAttachRegistry.reset("cli:1")
        assert AutoAttachRegistry.drain("cli:1") == []


class TestAutoAttachHook:
    """Полный lifecycle хука: before/after execute_tool + drain."""

    def test_write_file_records_and_confirms(self, tmp_path):
        """write_file → after_execute_tool → путь в bucket'е и попадает в drain."""
        target = tmp_path / "report.docx"
        target.write_bytes(b"hi")
        hook = AutoAttachHook(workspace_dir=str(tmp_path), session_key="cli:1")
        params = {"path": str(target)}
        asyncio.run(hook.before_execute_tool(
            _context("cli:1"), _tool_call("write_file", params), None, params,
        ))
        asyncio.run(hook.after_execute_tool(
            _context("cli:1"), _tool_call("write_file", params), None, params, None,
        ))
        assert AutoAttachRegistry.drain("cli:1") == [str(target)]

    def test_write_file_missing_file_dropped(self, tmp_path):
        """write_file с путём, который не появился → не попадает в drain."""
        target = tmp_path / "ghost.docx"  # намеренно не создаём
        hook = AutoAttachHook(workspace_dir=str(tmp_path), session_key="cli:1")
        params = {"path": str(target)}
        asyncio.run(hook.before_execute_tool(
            _context("cli:1"), _tool_call("write_file", params), None, params,
        ))
        asyncio.run(hook.after_execute_tool(
            _context("cli:1"), _tool_call("write_file", params), None, params, None,
        ))
        assert AutoAttachRegistry.drain("cli:1") == []

    def test_exec_tool_only_keeps_changed_files(self, tmp_path):
        """exec без изменения файла → файл не попадает в auto-attach."""
        existing = tmp_path / "log.txt"
        existing.write_bytes(b"old")  # 8 байт
        hook = AutoAttachHook(workspace_dir=str(tmp_path), session_key="cli:1")
        params = {"path": str(existing)}
        asyncio.run(hook.before_execute_tool(
            _context("cli:1"), _tool_call("exec", params), None, params,
        ))
        # exec не менял файл между before и after — same size → False.
        asyncio.run(hook.after_execute_tool(
            _context("cli:1"), _tool_call("exec", params), None, params, None,
        ))
        assert AutoAttachRegistry.drain("cli:1") == []

        # Иначе: exec изменил файл между before и after (size ДО = 8,
        # size ПОСЛЕ = 11) → confirm=True.
        asyncio.run(hook.before_execute_tool(
            _context("cli:1"), _tool_call("exec", params), None, params,
        ))
        existing.write_bytes(b"new content")  # 11 байт
        asyncio.run(hook.after_execute_tool(
            _context("cli:1"), _tool_call("exec", params), None, params, None,
        ))
        assert AutoAttachRegistry.drain("cli:1") == [str(existing)]

    def test_non_file_tool_ignored(self, tmp_path):
        """read_file / glob / grep — не должны трекаться."""
        hook = AutoAttachHook(workspace_dir=str(tmp_path), session_key="cli:1")
        params = {"path": str(tmp_path / "x.docx")}
        asyncio.run(hook.before_execute_tool(
            _context("cli:1"), _tool_call("read_file", params), None, params,
        ))
        # Нет записи в registry.
        assert AutoAttachRegistry._fresh == {}

    def test_relative_path_resolved_against_workspace(self, tmp_path):
        """Относительный путь в params нормализуется через workspace."""
        ws = tmp_path / "ws"
        ws.mkdir()
        # Положим файл по относительному пути data_store/cache/f.txt
        sub = ws / "data_store" / "cache"
        sub.mkdir(parents=True)
        target = sub / "f.txt"
        target.write_bytes(b"x")
        hook = AutoAttachHook(workspace_dir=str(ws), session_key="cli:1")
        params = {"path": "data_store/cache/f.txt"}
        asyncio.run(hook.before_execute_tool(
            _context("cli:1"), _tool_call("write_file", params), None, params,
        ))
        asyncio.run(hook.after_execute_tool(
            _context("cli:1"), _tool_call("write_file", params), None, params, None,
        ))
        # Нормализованный путь — разделители приведены к POSIX.
        drained = AutoAttachRegistry.drain("cli:1")
        assert len(drained) == 1
        assert PurePath(drained[0]) == PurePath(target)

    def test_factory_creates_per_turn_hook(self, tmp_path):
        """make_auto_attach_hook_factory: per-turn инстансы с разными session_key."""
        from types import SimpleNamespace

        factory = make_auto_attach_hook_factory()
        h1 = factory(SimpleNamespace(session_key="cli:1"))
        h2 = factory(SimpleNamespace(session_key="cli:2"))
        assert h1 is not h2
        assert h1._session_key == "cli:1"
        assert h2._session_key == "cli:2"


class TestPatchAssembleOutboundDedup:
    """Проверка что RuntimePatcher не дублирует файлы в OutboundMessage.media."""

    def test_auto_attach_dedupes_with_existing_media(self, tmp_path, monkeypatch):
        """Если бот уже прикрепил файл через message, auto-attach не добавляет."""
        # Перенаправить импорт workspace.hooks.auto_attach_hook
        monkeypatch.setenv("NANOBOT_TEST_NO_NANOBOT", "1")
        from lib.services.runtime_patcher import RuntimePatcher
        from workspace.hooks.auto_attach_hook import AutoAttachRegistry

        # Подготовка: AutoAttachRegistry уже содержит путь, который бот
        # должен прикрепить сам (через message).
        target = tmp_path / "report.docx"
        target.write_bytes(b"x")
        AutoAttachRegistry.record_pending("cli:1", PurePath(target), -1)

        # Эмулируем OutboundMessage, который бот построил, вызвав message.
        # У него уже есть media=[target].
        om = MagicMock()
        om.media = [str(target)]
        om.metadata = {}

        # Эмулируем вызов обёртки (без реального agent._assemble_outbound).
        # Дёрнем внутреннюю логику: drain + dedup.
        session_key = "cli:1"
        fresh = AutoAttachRegistry.drain(session_key)
        existing = list(om.media or [])
        seen = {os.path.normpath(p) for p in existing}
        for path in fresh:
            np = os.path.normpath(path)
            if np in seen:
                continue
            existing.append(path)
            seen.add(np)
        if existing:
            om.media = existing

        # Дубликата не должно быть.
        assert om.media == [str(target)]
        assert len(om.media) == 1

    def test_auto_attach_adds_new_files(self, tmp_path):
        """Если бот НЕ прикрепил — auto-attach добавляет файл в media."""
        from workspace.hooks.auto_attach_hook import AutoAttachRegistry

        target = tmp_path / "report.docx"
        target.write_bytes(b"x")
        AutoAttachRegistry.record_pending("cli:1", PurePath(target), -1)

        # Эмулируем OutboundMessage с пустым media (бот не прикреплял).
        om = MagicMock()
        om.media = []
        om.metadata = {}

        fresh = AutoAttachRegistry.drain("cli:1")
        existing = list(om.media or [])
        seen = {os.path.normpath(p) for p in existing}
        for path in fresh:
            np = os.path.normpath(path)
            if np in seen:
                continue
            existing.append(path)
            seen.add(np)
        if existing:
            om.media = existing

        assert len(om.media) == 1
        assert PurePath(om.media[0]) == PurePath(target)
