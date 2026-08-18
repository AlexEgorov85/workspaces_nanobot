"""Тесты для ``patch_message_tool`` в ``RuntimePatcher``.

Это ЛЕЧЕНИЕ, а не костыль: ``MessageTool.execute`` wrap'ает так, чтобы
если бот вызвал ``message(content)`` без media, в media подмешивались
свежие файлы из ``AutoAttachRegistry``. Раньше LLM забывал про media
(потому что описание tool "message" в nanobot 0.3.0 запрещает
использовать его для normal reply в текущем чате), и файлы терялись.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path, PurePath
from typing import Any
from unittest.mock import MagicMock

import pytest

# Workspace в sys.path, чтобы импортировать workspace.hooks.auto_attach_hook.
_HOOKS_DIR = str(Path(__file__).resolve().parent.parent / "workspace")
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from workspace.hooks.auto_attach_hook import AutoAttachRegistry  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    """Чистим class-level registry между тестами."""
    AutoAttachRegistry._fresh.clear()
    AutoAttachRegistry._sizes.clear()
    yield
    AutoAttachRegistry._fresh.clear()
    AutoAttachRegistry._sizes.clear()


def _make_message_tool(tmp_path: Path) -> Any:
    """Создаёт реальный MessageTool с минимальным контекстом."""
    from nanobot.agent.tools.message import MessageTool

    return MessageTool(
        send_callback=MagicMock(return_value=None),
        default_channel="postgres",
        default_chat_id="555dcae0-abc",
        default_message_id=None,
        workspace=str(tmp_path),
        restrict_to_workspace=False,
    )


def _fake_agent(tool: Any) -> Any:
    """Возвращает агент-stub, у которого ``tools.get('message')`` → tool."""
    agent = MagicMock()
    agent.tools.get = MagicMock(
        side_effect=lambda name: tool if name == "message" else None,
    )
    return agent


class TestPatchMessageTool:
    """Smoke / structural тесты патча."""

    def test_returns_true_when_message_tool_present(self, tmp_path):
        from lib.services.runtime_patcher import RuntimePatcher

        tool = _make_message_tool(tmp_path)
        agent = _fake_agent(tool)
        ok, detail = RuntimePatcher().patch_message_tool(agent)
        assert ok, detail
        assert "wrapped" in detail.lower()
        assert getattr(tool.execute, "_audit_track_attached", False) is True

    def test_returns_skipped_when_no_message_tool(self):
        from lib.services.runtime_patcher import RuntimePatcher

        agent = MagicMock()
        agent.tools.get = MagicMock(return_value=None)
        ok, detail = RuntimePatcher().patch_message_tool(agent)
        assert ok is False
        assert "missing" in detail.lower() or "not messagetool" in detail.lower()

    def test_returns_skipped_when_tool_is_not_messagetool(self, tmp_path):
        """Любой не-MessageTool → пропуск."""
        from lib.services.runtime_patcher import RuntimePatcher

        agent = MagicMock()
        not_a_message_tool = MagicMock()
        agent.tools.get = MagicMock(return_value=not_a_message_tool)
        ok, _ = RuntimePatcher().patch_message_tool(agent)
        assert ok is False

    def test_idempotent(self, tmp_path):
        """Повторный patch_message_tool — no-op (маркер _audit_track_attached)."""
        from lib.services.runtime_patcher import RuntimePatcher

        tool = _make_message_tool(tmp_path)
        agent = _fake_agent(tool)
        ok1, _ = RuntimePatcher().patch_message_tool(agent)
        assert ok1
        first_execute = tool.execute
        ok2, detail2 = RuntimePatcher().patch_message_tool(agent)
        assert ok2
        assert "already wrapped" in detail2.lower()
        assert tool.execute is first_execute

    def test_returns_false_when_agent_is_none(self):
        from lib.services.runtime_patcher import RuntimePatcher

        ok, detail = RuntimePatcher().patch_message_tool(None)
        assert ok is False
        assert "agent is none" in detail.lower()


class TestMessageToolWrapBehavior:
    """Поведение обёртки: media подмешивается, если бот забыл."""

    def test_message_without_media_autopopulates_files(self, tmp_path):
        """message(content) → media = [свежие_файлы]."""
        from lib.services.runtime_patcher import RuntimePatcher

        tool = _make_message_tool(tmp_path)
        target = tmp_path / "report.docx"
        target.write_bytes(b"x" * 10)
        AutoAttachRegistry.record_pending(
            "postgres:555dcae0-abc", PurePath(target), -1,
        )
        AutoAttachRegistry.confirm(
            "postgres:555dcae0-abc", PurePath(target),
            require_size_change=False,
        )

        agent = _fake_agent(tool)
        RuntimePatcher().patch_message_tool(agent)

        # Бот вызвал message(content) без media.
        asyncio.run(tool.execute(content="Готово"))

        # Send_callback был вызван с OutboundMessage, в media — файл.
        cb = tool._send_callback
        assert cb.called
        out_msg = cb.call_args.args[0]
        assert str(target) in list(out_msg.media)
        # Registry дренирован.
        assert AutoAttachRegistry._fresh.get("postgres:555dcae0-abc") is None

    def test_message_with_explicit_media_does_not_overwrite(self, tmp_path):
        """Если бот САМ передал media — автоподмес не перетирает (дедуп)."""
        from lib.services.runtime_patcher import RuntimePatcher

        tool = _make_message_tool(tmp_path)
        target_a = tmp_path / "a.docx"
        target_a.write_bytes(b"x")
        target_b = tmp_path / "b.docx"
        target_b.write_bytes(b"y")

        # b в реестре, a бот передал сам.
        AutoAttachRegistry.record_pending(
            "postgres:555dcae0-abc", PurePath(target_b), -1,
        )
        AutoAttachRegistry.confirm(
            "postgres:555dcae0-abc", PurePath(target_b),
            require_size_change=False,
        )

        agent = _fake_agent(tool)
        RuntimePatcher().patch_message_tool(agent)

        asyncio.run(tool.execute(
            content="Два файла",
            media=[str(target_a)],
        ))

        out_msg = tool._send_callback.call_args.args[0]
        assert len(out_msg.media) == 2
        assert str(target_a) in out_msg.media
        assert str(target_b) in out_msg.media

    def test_message_with_same_file_dedupes(self, tmp_path):
        """Если бот передал тот же файл → нет дубля."""
        from lib.services.runtime_patcher import RuntimePatcher

        tool = _make_message_tool(tmp_path)
        target = tmp_path / "shared.docx"
        target.write_bytes(b"x")
        AutoAttachRegistry.record_pending(
            "postgres:555dcae0-abc", PurePath(target), -1,
        )
        AutoAttachRegistry.confirm(
            "postgres:555dcae0-abc", PurePath(target),
            require_size_change=False,
        )

        agent = _fake_agent(tool)
        RuntimePatcher().patch_message_tool(agent)

        asyncio.run(tool.execute(
            content="Дубль",
            media=[str(target)],
        ))

        out_msg = tool._send_callback.call_args.args[0]
        assert len(out_msg.media) == 1
        assert PurePath(out_msg.media[0]) == PurePath(target)

    def test_message_with_no_fresh_files_passes_media_as_is(self, tmp_path):
        """Реестр пуст → media остаётся как есть."""
        from lib.services.runtime_patcher import RuntimePatcher

        tool = _make_message_tool(tmp_path)
        agent = _fake_agent(tool)
        RuntimePatcher().patch_message_tool(agent)

        asyncio.run(tool.execute(content="Без файлов"))
        out_msg = tool._send_callback.call_args.args[0]
        assert list(out_msg.media) == []

    def test_message_with_explicit_media_for_different_session_no_overlap(
        self, tmp_path,
    ):
        """Файлы из другой сессии не подмешиваются (разделение по session_key)."""
        from lib.services.runtime_patcher import RuntimePatcher

        tool = _make_message_tool(tmp_path)
        target = tmp_path / "other_session.docx"
        target.write_bytes(b"x")
        # Файл лежит в реестре под session_key ДРУГОЙ сессии.
        AutoAttachRegistry.record_pending(
            "telegram:someone", PurePath(target), -1,
        )
        AutoAttachRegistry.confirm(
            "telegram:someone", PurePath(target),
            require_size_change=False,
        )

        agent = _fake_agent(tool)
        RuntimePatcher().patch_message_tool(agent)

        asyncio.run(tool.execute(content="Из postgres"))
        out_msg = tool._send_callback.call_args.args[0]
        # Чужой файл не попал.
        assert str(target) not in list(out_msg.media)
        assert list(out_msg.media) == []
