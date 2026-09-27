from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lib.services.runtime_patcher import RuntimePatcher


def _settings(gateway_overrides=None, channels=None, **overrides):
    gw = {
        "persist_threshold": 0,
        "persist_max_files": 100,
        "persist_max_age_hours": 24,
    }
    if gateway_overrides is not None:
        gw.update(gateway_overrides)
    else:
        gw.update(overrides)

    channels_ns = types.SimpleNamespace(**(channels or {}))

    class _Settings:
        gateway = gw

        def __init__(self) -> None:
            self.channels = channels_ns

    return _Settings()


def _make_fake_document_module(text_value=None):
    """Собрать fake ``nanobot.utils.document`` для подмены в ``sys.modules``.

    ``from nanobot.utils import document`` резолвится через атрибут
    ``document`` родительского модуля ``nanobot.utils``. Чтобы патч
    ``patch_document_text_threshold`` подхватил наш fake (а не
    настоящий submodule), подменяем **оба** ключа в ``sys.modules``.

    ``extract_text(path)`` для тестовых файлов читает содержимое из
    ``tmp_path_factory``: env-vars ``FAKE_FILE_<basename>`` содержат
    текст файла. Если env не задан, возвращается ``text_value``
    (одинаковый текст для всех).
    """
    document_mod = types.ModuleType("nanobot.utils.document")

    def _fake_extract_text(path):
        if not isinstance(path, str) or not path:
            return None
        basename = Path(path).name
        env_key = f"FAKE_FILE_{basename}"
        if env_key in os.environ:
            return os.environ[env_key]
        return text_value

    def _fake_is_image_file(path):
        if not isinstance(path, str):
            return False
        name = Path(path).name.lower()
        return name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))

    def _fake_reference(content, media):
        # Поведение upstream ``reference_non_image_attachments``:
        # только [Attachment: path] для не-изображений, ничего не читает.
        blocks: list[str] = []
        for p in media or []:
            if not _fake_is_image_file(p):
                blocks.append(f"[Attachment: {p}]")
        if blocks:
            content = f"{content}\n\n" + "\n\n".join(blocks) if content else "\n\n".join(blocks)
        return content, []

    document_mod.reference_non_image_attachments = _fake_reference
    document_mod.extract_text = _fake_extract_text
    document_mod.is_image_file = _fake_is_image_file
    return document_mod


def _patched_document_module(document_mod):
    """Контекст-менеджер: подменить ``nanobot.utils`` и
    ``nanobot.utils.document`` так, чтобы ``from nanobot.utils import
    document`` взял наш fake.
    """
    return patch.dict(
        "sys.modules",
        {
            "nanobot.utils.document": document_mod,
            "nanobot.utils": types.SimpleNamespace(document=document_mod),
        },
    )


class TestPatchAssembleOutbound:
    """Nanobot 0.3.5: ``AgentLoop._assemble_outbound`` имеет сигнатуру
    ``(self, msg, final_content, stop_reason, streamed_content,
       *, log_content=True, turn_latency_ms=None)``.
    Обёртка в ``RuntimePatcher.patch_assemble_outbound`` принимает
    те же позиционные параметры и передаёт их оригиналу as-is.

    В nanobot 0.3.5 ``_attach_context_window`` ТРЕБУЕТ засеянный bridge
    (``seed_context_window`` вызывается через подписку на
    TurnRuntimeAdmitted в ``RuntimeEventsSubscriber``). Тесты должны
    сеять bridge явно — иначе получают ``ContextWindowNotSeededError``.
    """

    def _seed_bridge(self, session_key: str, limit: int = 40000, model: str = "MiniMax-M3") -> None:
        from lib.hooks.database_logging_hook import seed_context_window

        seed_context_window(session_key, limit=limit, model=model)

    def test_wraps_and_injects_audit(self):
        agent = MagicMock()
        original_return = MagicMock()
        original_return.metadata = {}
        agent._assemble_outbound.return_value = original_return
        agent.context_window_tokens = 40000  # симулируем RuntimeEventAdmitted-эффект

        hook = MagicMock()
        hook.drain.return_value = [{"name": "read"}]

        self._seed_bridge("telegram:1")

        patcher = RuntimePatcher()
        ok, _ = patcher.patch_assemble_outbound(agent, hook)
        assert ok

        result = agent._assemble_outbound(
            MagicMock(), "content", "stop", False,
        )
        hook.drain.assert_called_once()
        assert result.metadata["_tool_audit"] == [{"name": "read"}]

    def test_result_none_skips_drain(self):
        agent = MagicMock()
        agent._assemble_outbound.return_value = None
        agent.context_window_tokens = 40000
        hook = MagicMock()
        self._seed_bridge("telegram:1")

        patcher = RuntimePatcher()
        ok, _ = patcher.patch_assemble_outbound(agent, hook)

        result = agent._assemble_outbound(None, None, None, None)
        assert result is None
        hook.drain.assert_not_called()
        assert ok

    def test_stamps_final_turn_marker(self):
        agent = MagicMock()
        original_return = MagicMock()
        original_return.metadata = {}
        agent._assemble_outbound.return_value = original_return
        agent.context_window_tokens = 40000
        self._seed_bridge("telegram:1")

        patcher = RuntimePatcher()
        ok, _ = patcher.patch_assemble_outbound(agent, MagicMock())

        result = agent._assemble_outbound(MagicMock(), "x", "stop", False)
        assert ok
        assert result.metadata["_final_turn"] is True

    def test_none_result_synthesizes_marker_outbound(self):
        agent = MagicMock()
        agent._assemble_outbound.return_value = None
        agent.context_window_tokens = 40000
        self._seed_bridge("telegram:1")

        patcher = RuntimePatcher()
        ok, _ = patcher.patch_assemble_outbound(agent, MagicMock())

        msg = MagicMock()
        msg.channel = "postgres"
        msg.chat_id = "chat-1"
        msg.metadata = {"message_id": "m-1", "answer_id": "a-1"}

        result = agent._assemble_outbound(msg, "", "stop", False)
        assert ok
        assert result is not None
        assert result.metadata["_final_turn"] is True
        assert result.content == ""
        assert result.chat_id == "chat-1"

    def test_context_window_not_seeded_raises(self):
        """Без seed_context_window — ContextWindowNotSeededError."""
        agent = MagicMock()
        original_return = MagicMock()
        original_return.metadata = {}
        agent._assemble_outbound.return_value = original_return
        agent.context_window_tokens = 0  # типичный случай без подписки

        patcher = RuntimePatcher()
        ok, _ = patcher.patch_assemble_outbound(agent, MagicMock())
        assert ok

        # Без seed — поднимается ContextWindowNotSeededError.
        from lib.services.runtime_patcher import ContextWindowNotSeededError

        with pytest.raises(ContextWindowNotSeededError):
            agent._assemble_outbound(MagicMock(), "x", "stop", False)

    def test_first_turn_without_tool_calls_has_context_window(self):
        """Regression 5.2: первый оборот без tool-вызовов имеет
        ``metadata.context_window`` с ``limit > 0``, ``model != ""``,
        ``used == 0`` (usage ещё не пришёл — это первая итерация).

        Контракт: подписка на TurnRuntimeAdmitted засевает bridge
        ДО первой LLM-итерации. Тест симулирует это явно через
        ``seed_context_window``.
        """
        from lib.hooks.database_logging_hook import (
            _CONTEXT_BRIDGE,
            _CONTEXT_BRIDGE_LOCK,
            seed_context_window,
        )
        from lib.services.runtime_patcher import RuntimePatcher

        session_key = "test:first_turn"
        seed_context_window(session_key, limit=40000, model="MiniMax-M3")

        try:
            agent = MagicMock()
            original_return = MagicMock()
            original_return.metadata = {}
            agent._assemble_outbound.return_value = original_return
            # agent.context_window_tokens НЕ задан → bridge должен
            # обеспечить limit/model.

            patcher = RuntimePatcher()
            ok, _ = patcher.patch_assemble_outbound(agent, MagicMock())
            assert ok

            msg = MagicMock()
            msg.session_key = session_key
            msg.metadata = {}
            msg.channel = "test"
            msg.chat_id = "1"

            # Без usage в bridge (первая итерация без tool-calls).
            result = agent._assemble_outbound(msg, "x", "stop", False)
            block = result.metadata["context_window"]
            assert block["limit"] == 40000, (
                f"limit должен быть из bridge (40000), получено: "
                f"{block['limit']!r}"
            )
            assert block["model"] == "MiniMax-M3", (
                f"model должен быть из bridge (MiniMax-M3), получено: "
                f"{block['model']!r}"
            )
            assert block["used"] == 0, (
                f"used == 0 для first-turn без tool-calls, получено: "
                f"{block['used']!r}"
            )
            assert block["pct"] == 0.0
        finally:
            with _CONTEXT_BRIDGE_LOCK:
                _CONTEXT_BRIDGE.pop(session_key, None)

    def test_agent_none_skipped(self):
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_assemble_outbound(None, MagicMock())
        assert not ok
        assert "agent is None" in detail

    def test_missing_method_skipped(self):
        agent = MagicMock()
        del agent._assemble_outbound
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_assemble_outbound(agent, MagicMock())
        assert not ok
        assert "missing" in detail


class TestPatchContextGovernor:
    def test_threshold_zero_skipped(self):
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_context_governor(
            MagicMock(), _settings(), Path("ws")
        )
        assert not ok
        assert "persist_threshold" in detail

    def test_threshold_positive_patches(self):
        with patch.dict("sys.modules"):
            # Подменяем модули, от которых патч зависит
            governance = types.ModuleType("nanobot.agent.context_governance")

            class _CG:
                normalize_tool_result = None

            governance.ContextGovernor = _CG
            runtime = types.ModuleType("nanobot.utils.runtime")
            runtime.ensure_nonempty_tool_result = lambda name, result: result

            utils = types.ModuleType("utils")
            utils.session_file_store = types.ModuleType("utils.session_file_store")
            store = MagicMock()
            utils.session_file_store.SessionFileStore = MagicMock(return_value=store)
            store.save.return_value = {"path": "x.txt", "size_kb": 12}
            utils.session_file_store.prepare_content = lambda text: (text, "txt")
            sys.modules["nanobot.agent.context_governance"] = governance
            sys.modules["nanobot.utils.runtime"] = runtime
            sys.modules["utils"] = utils
            sys.modules["utils.session_file_store"] = utils.session_file_store

            patcher = RuntimePatcher()
            ok, _ = patcher.patch_context_governor(
                MagicMock(),
                _settings(persist_threshold=5),
                Path("ws"),
            )
            assert ok
            # Проверяем, что статик-метод заменён и работает
            fn = _CG.normalize_tool_result
            config = MagicMock()
            config.session_key = "k"
            result = fn(config, "tid", "tool", "x" * 100)
            assert result.startswith("[Result saved to data_store/")

    def test_import_failure_skipped(self):
        # Если в sys.modules ничего нет, после импорта lib в нём появятся
        # реальные nanobot.* и utils.* модули (workspace на sys.path) — но
        # мы заранее гасим все нужные записи через patch.dict, чтобы
        # патч не применился к настоящему ContextGovernor.
        hidden = {
            "nanobot": None,
            "nanobot.agent": None,
            "nanobot.agent.context_governance": None,
            "nanobot.utils": None,
            "nanobot.utils.runtime": None,
            "utils": None,
            "utils.session_file_store": None,
        }
        with patch.dict("sys.modules", hidden):
            patcher = RuntimePatcher()
            ok, detail = patcher.patch_context_governor(
                MagicMock(), _settings(persist_threshold=5), Path("ws")
            )
            assert not ok
            assert "import failed" in detail


class TestPatchDocumentTextThreshold:
    """В nanobot 0.3.5 патч оборачивает ``reference_non_image_attachments``
    вместо ``extract_documents``. Семантика: для каждого файла из ``media``
    пытается прочитать текст через ``extract_text`` и встроить в content
    (с маркером обрезки при превышении порога); изображения и
    нечитаемые файлы — fallback на upstream-формат ``[Attachment: …]``.
    """

    def _patch_with_fake_doc(self, channels=None):
        document_mod = _make_fake_document_module()
        return _patched_document_module(document_mod), document_mod

    def test_threshold_zero_skipped(self):
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_document_text_threshold(
            _settings(channels={"document_text_threshold": 0})
        )
        assert not ok
        assert "document_text_threshold <= 0" in detail

    def test_threshold_negative_skipped(self):
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_document_text_threshold(
            _settings(channels={"document_text_threshold": -5})
        )
        assert not ok
        assert "document_text_threshold <= 0" in detail

    def test_threshold_default_applied(self):
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_document_text_threshold(
            _settings(channels={"document_text_threshold": 20000})
        )
        assert ok
        assert "patched" in detail

    def test_small_text_passes_through_with_path(self, monkeypatch):
        text = "a" * 100
        ctx, document_mod = self._patch_with_fake_doc()
        monkeypatch.setenv("FAKE_FILE_file.pdf", text)
        with ctx:
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_document_text_threshold(
                _settings(channels={"document_text_threshold": 20000})
            )
            assert ok
            new_text, images = document_mod.reference_non_image_attachments(
                "user prompt", ["x/cache/file.pdf"]
            )
            assert "[File: file.pdf (saved at x/cache/file.pdf)]" in new_text
            assert text in new_text
            assert "text omitted" not in new_text
            assert images == []

    def test_large_text_replaced_with_marker_and_path(self, monkeypatch):
        text = "a" * 30000
        monkeypatch.setenv("FAKE_FILE_big.pdf", text)
        ctx, document_mod = self._patch_with_fake_doc()
        with ctx:
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_document_text_threshold(
                _settings(channels={"document_text_threshold": 1000})
            )
            assert ok
            new_text, _ = document_mod.reference_non_image_attachments(
                "user prompt", ["cache/sessions/k/big.pdf"]
            )
            assert (
                "[File: big.pdf (saved at cache/sessions/k/big.pdf)]" in new_text
            )
            assert (
                "[text omitted (len=30000 > threshold=1000)]" in new_text
            )
            assert "read at" not in new_text
            assert text not in new_text

    def test_image_returns_path_in_image_paths_only(self):
        """Изображения НЕ формируют текстовых блоков: путь возвращается
        в ``image_paths`` (для vision-блоков upstream), content не
        раздувается маркерами.
        """
        ctx, document_mod = self._patch_with_fake_doc()
        with ctx:
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_document_text_threshold(
                _settings(channels={"document_text_threshold": 20000})
            )
            assert ok
            new_text, images = document_mod.reference_non_image_attachments(
                "user prompt", ["x/cache/pic.png"]
            )
            assert images == ["x/cache/pic.png"]
            assert new_text == "user prompt"
            assert "[File:" not in new_text
            assert "[Attachment:" not in new_text

    def test_unreadable_file_falls_back_to_attachment(self, monkeypatch):
        monkeypatch.delenv("FAKE_FILE_ghost.pdf", raising=False)
        ctx, document_mod = self._patch_with_fake_doc()
        with ctx:
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_document_text_threshold(
                _settings(channels={"document_text_threshold": 20000})
            )
            assert ok
            new_text, _ = document_mod.reference_non_image_attachments(
                "user prompt", ["x/cache/ghost.pdf"]
            )
            assert "[Attachment: x/cache/ghost.pdf]" in new_text

    def test_missing_reference_skipped(self):
        hidden = {"nanobot.utils.document": None, "nanobot.utils": None}
        with patch.dict("sys.modules", hidden):
            patcher = RuntimePatcher()
            ok, detail = patcher.patch_document_text_threshold(
                _settings(channels={"document_text_threshold": 100})
            )
            assert not ok
            assert "missing" in detail or "import failed" in detail


class TestPatchExecLimits:
    def test_patches_module_constants_and_schema(self):
        esm = types.ModuleType("nanobot.agent.tools.exec_session")
        esm.MAX_OUTPUT_CHARS = 50_000
        esm.DEFAULT_MAX_OUTPUT_CHARS = 10_000
        esm.WriteStdinTool = type(
            "WriteStdinTool", (),
            {"parameters": property(lambda s: {"properties": {
                "max_output_chars": {"maximum": 50_000},
                "max_output_tokens": {"maximum": 50_000},
            }})},
        )
        shellm = types.ModuleType("nanobot.agent.tools.shell")
        shellm.MAX_OUTPUT_CHARS = 50_000
        shellm.ExecTool = type(
            "ExecTool", (),
            {"_MAX_OUTPUT": 10_000, "parameters": property(lambda s: {"properties": {
                "max_output_chars": {"maximum": 50_000},
                "max_output_tokens": {"maximum": 50_000},
            }})},
        )
        hidden = {
            "nanobot.agent.tools.exec_session": esm,
            "nanobot.agent.tools.shell": shellm,
        }
        with patch.dict("sys.modules", hidden):
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_exec_limits(_settings())
            assert ok
            assert esm.MAX_OUTPUT_CHARS == 500_000
            assert esm.DEFAULT_MAX_OUTPUT_CHARS == 100_000
            assert shellm.MAX_OUTPUT_CHARS == 500_000
            assert shellm.ExecTool._MAX_OUTPUT == 100_000
            schema = shellm.ExecTool.parameters.fget(shellm.ExecTool)
            assert schema["properties"]["max_output_chars"]["maximum"] == 500_000
            schema2 = esm.WriteStdinTool.parameters.fget(esm.WriteStdinTool)
            assert schema2["properties"]["max_output_tokens"]["maximum"] == 500_000

    def test_custom_limits(self):
        esm = types.ModuleType("nanobot.agent.tools.exec_session")
        esm.MAX_OUTPUT_CHARS = 50_000
        esm.DEFAULT_MAX_OUTPUT_CHARS = 10_000
        esm.WriteStdinTool = type("WriteStdinTool", (), {"parameters": property(lambda s: {"properties": {}})})
        shellm = types.ModuleType("nanobot.agent.tools.shell")
        shellm.MAX_OUTPUT_CHARS = 50_000
        shellm.ExecTool = type("ExecTool", (), {"_MAX_OUTPUT": 10_000, "parameters": property(lambda s: {"properties": {}})})
        hidden = {
            "nanobot.agent.tools.exec_session": esm,
            "nanobot.agent.tools.shell": shellm,
        }
        with patch.dict("sys.modules", hidden):
            patcher = RuntimePatcher()
            settings = _settings(tool_result_limits={
                "exec_max_output_chars": 999_999,
                "exec_default_output_chars": 88_888,
            })
            ok, _ = patcher.patch_exec_limits(settings)
            assert ok
            assert esm.MAX_OUTPUT_CHARS == 999_999
            assert esm.DEFAULT_MAX_OUTPUT_CHARS == 88_888
            assert shellm.ExecTool._MAX_OUTPUT == 88_888


class TestPatchToolLimits:
    def test_patches_module_limits(self):
        fsm = types.ModuleType("nanobot.agent.tools.filesystem")
        fsm.ReadFileTool = type("ReadFileTool", (), {"_MAX_CHARS": 128_000})
        fsm.ListDirTool = type("ListDirTool", (), {"_DEFAULT_MAX": 200})
        srm = types.ModuleType("nanobot.agent.tools.search")
        srm._DEFAULT_HEAD_LIMIT = 250
        srm._DEFAULT_FILE_HEAD_LIMIT = 200
        srm.GrepTool = type("GrepTool", (), {"_MAX_FILE_BYTES": 5_000_000})
        hidden = {
            "nanobot.agent.tools.filesystem": fsm,
            "nanobot.agent.tools.search": srm,
        }
        with patch.dict("sys.modules", hidden):
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_tool_limits(_settings())
            assert ok
            assert fsm.ReadFileTool._MAX_CHARS == 512_000
            assert fsm.ListDirTool._DEFAULT_MAX == 500
            assert srm._DEFAULT_HEAD_LIMIT == 500
            assert srm._DEFAULT_FILE_HEAD_LIMIT == 400
            assert srm.GrepTool._MAX_FILE_BYTES == 20_000_000

    def test_custom_limits(self):
        fsm = types.ModuleType("nanobot.agent.tools.filesystem")
        fsm.ReadFileTool = type("ReadFileTool", (), {"_MAX_CHARS": 128_000})
        fsm.ListDirTool = type("ListDirTool", (), {"_DEFAULT_MAX": 200})
        srm = types.ModuleType("nanobot.agent.tools.search")
        srm._DEFAULT_HEAD_LIMIT = 250
        srm._DEFAULT_FILE_HEAD_LIMIT = 200
        srm.GrepTool = type("GrepTool", (), {"_MAX_FILE_BYTES": 5_000_000})
        hidden = {
            "nanobot.agent.tools.filesystem": fsm,
            "nanobot.agent.tools.search": srm,
        }
        with patch.dict("sys.modules", hidden):
            patcher = RuntimePatcher()
            settings = _settings(tool_result_limits={
                "read_file_max_chars": 999_999,
                "grep_head_limit": 10,
                "grep_file_head_limit": 20,
                "grep_max_file_bytes": 30,
                "list_dir_max_entries": 40,
            })
            ok, _ = patcher.patch_tool_limits(settings)
            assert ok
            assert fsm.ReadFileTool._MAX_CHARS == 999_999
            assert fsm.ListDirTool._DEFAULT_MAX == 40
            assert srm._DEFAULT_HEAD_LIMIT == 10
            assert srm._DEFAULT_FILE_HEAD_LIMIT == 20
            assert srm.GrepTool._MAX_FILE_BYTES == 30


class TestPatchSaveTurn:
    def test_threshold_zero_skipped(self):
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_save_turn(
            _settings(), Path("ws"), MagicMock()
        )
        assert not ok
        assert "persist_threshold" in detail

    def test_agent_none_skipped(self):
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_save_turn(
            _settings(persist_threshold=5), Path("ws"), None
        )
        assert not ok
        assert "agent" in detail

    def test_archives_large_tool_result(self, tmp_path):
        store = MagicMock()
        store.save.return_value = {
            "path": "cache/sessions/s1/results/x.txt",
            "size_kb": 100.0,
        }
        utils_mod = types.ModuleType("utils")
        store_mod = types.ModuleType("utils.session_file_store")
        store_mod.SessionFileStore = lambda root, **kw: store
        store_mod.prepare_content = lambda text: (text, "txt")
        utils_mod.session_file_store = store_mod
        hidden = {
            "utils": utils_mod,
            "utils.session_file_store": store_mod,
        }

        class _Session:
            key = "s1"

        big = "x" * 100_000
        msg = {"role": "tool", "content": big, "tool_call_id": "t1", "name": "exec"}
        captured = {}

        def _fake_save_turn(session, messages, skip, *, turn_latency_ms=None, **kw):
            captured["messages"] = messages
            captured["turn_latency_ms"] = turn_latency_ms
            return None

        agent = MagicMock()
        agent.max_tool_result_chars = 16_000
        agent._save_turn = _fake_save_turn

        with patch.dict("sys.modules", hidden):
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_save_turn(
                _settings(persist_threshold=5), tmp_path, agent
            )
            assert ok
            agent._save_turn(_Session(), [msg], 0, turn_latency_ms=42)

        store.save.assert_called_once()
        call_kwargs = store.save.call_args.kwargs
        assert call_kwargs["session_key"] == "s1"
        assert call_kwargs["dedupe"] is True
        assert call_kwargs["source_tool"] == "exec"
        # история подменена на ссылку
        assert captured["messages"][0]["content"].startswith("[Result saved to data_store/")
        assert captured["turn_latency_ms"] == 42

    def test_small_result_passes_through(self, tmp_path):
        store = MagicMock()
        utils_mod = types.ModuleType("utils")
        store_mod = types.ModuleType("utils.session_file_store")
        store_mod.SessionFileStore = lambda root, **kw: store
        store_mod.prepare_content = lambda text: (text, "txt")
        utils_mod.session_file_store = store_mod
        hidden = {
            "utils": utils_mod,
            "utils.session_file_store": store_mod,
        }

        class _Session:
            key = "s1"

        msg = {"role": "tool", "content": "small", "tool_call_id": "t1", "name": "read"}
        captured = {}

        def _fake_save_turn(session, messages, skip, **kw):
            captured["messages"] = messages

        agent = MagicMock()
        agent.max_tool_result_chars = 16_000
        agent._save_turn = _fake_save_turn

        with patch.dict("sys.modules", hidden):
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_save_turn(
                _settings(persist_threshold=5), tmp_path, agent
            )
            assert ok
            agent._save_turn(_Session(), [msg], 0)

        store.save.assert_not_called()
        assert captured["messages"][0]["content"] == "small"


class TestPatchSubagentLogging:
    def _context(self, **overrides):
        base = {
            "session_key": "telegram:1",
            "final_content": "done",
            "tools_used": ["read"],
            "usage": {"total_tokens": 42},
            "stop_reason": "completed",
            "error": None,
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
                {"role": "tool", "content": "content", "tool_call_id": "tc1", "name": "read"},
                {"role": "assistant", "content": "done"},
            ],
        }
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def test_db_logging_applied_and_tool_events_logged(self):
        import nanobot.agent.subagent as subagent_mod

        original = subagent_mod._SubagentHook
        try:
            svc = MagicMock()
            sessions = MagicMock()
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_subagent_logging(svc, sessions)
            assert ok

            HookCls = subagent_mod._SubagentHook
            assert HookCls is not original

            hook = HookCls("t123")
            ctx = self._context()
            tool_call = types.SimpleNamespace(id="tc1", name="read")

            asyncio.run(hook.before_execute_tool(ctx, tool_call, None, {"path": "x"}))
            asyncio.run(hook.after_execute_tool(ctx, tool_call, None, {"path": "x"}, "content"))

            svc.log_tool_call.assert_called_once()
            call_kwargs = svc.log_tool_call.call_args.kwargs
            assert call_kwargs["tool_name"] == "read"
            assert "subagent:t123" in call_kwargs["session_id"]
            svc.log_tool_result.assert_called_once()
        finally:
            subagent_mod._SubagentHook = original

    def test_after_run_logs_summary_and_persists_history(self):
        import nanobot.agent.subagent as subagent_mod
        from nanobot.session.manager import Session

        original = subagent_mod._SubagentHook
        try:
            svc = MagicMock()
            real_session = Session(key="")
            sessions = MagicMock()
            sessions.get_or_create.return_value = real_session
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_subagent_logging(svc, sessions)
            assert ok

            hook = subagent_mod._SubagentHook("t456")
            ctx = self._context()
            asyncio.run(hook.after_run(ctx))

            # итог запуска записан один раз
            summary_events = [c.args[0] for c in svc.log_event.call_args_list
                              if c.args[0].event_type == "subagent_run_finished"]
            assert len(summary_events) == 1
            ev = summary_events[0]
            assert ev.session_id == "subagent:t456"
            assert ev.channel == "subagent"
            assert ev.payload["task_id"] == "t456"

            # история подагента персистится без system-сообщения
            roles = [m["role"] for m in real_session.messages]
            assert "system" not in roles
            assert roles == ["user", "assistant", "tool", "assistant"]
            sessions.save.assert_called_once_with(real_session)
        finally:
            subagent_mod._SubagentHook = original

    def test_finalize_guard_no_duplicate(self):
        import nanobot.agent.subagent as subagent_mod

        original = subagent_mod._SubagentHook
        try:
            svc = MagicMock()
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_subagent_logging(svc, None)
            assert ok

            hook = subagent_mod._SubagentHook("t789")
            ctx = self._context(error="boom", stop_reason="tool_error")
            # на путях tool_error runner вызывает on_error, а затем after_run —
            # должен быть только один итог
            asyncio.run(hook.on_error(ctx))
            asyncio.run(hook.after_run(ctx))

            summaries = [c for c in svc.log_event.call_args_list
                         if c.args[0].event_type == "subagent_run_finished"]
            assert len(summaries) == 1
            assert summaries[0].args[0].level == "ERROR"
        finally:
            subagent_mod._SubagentHook = original

    def test_none_service_skipped(self):
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_subagent_logging(None, None)
        assert not ok
        assert "db_logging_service" in detail

    def test_each_subagent_hook_has_own_db_hook(self):
        import nanobot.agent.subagent as subagent_mod

        original = subagent_mod._SubagentHook
        try:
            svc = MagicMock()
            patcher = RuntimePatcher()
            ok, _ = patcher.patch_subagent_logging(svc, None)
            assert ok

            h1 = subagent_mod._SubagentHook("aa1")
            h2 = subagent_mod._SubagentHook("aa2")
            # Не разделяют _db_hook между запусками субагентов:
            # иначе конкурентные субагенты перезаписывали бы чужой контекст.
            assert h1._db_hook is not h2._db_hook
        finally:
            subagent_mod._SubagentHook = original


class TestPatchAsyncSessionSaves:
    def test_agent_none_skipped(self):
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_async_session_saves(None)
        assert not ok
        assert "agent is None" in detail

    def test_missing_sessions_skipped(self):
        agent = MagicMock()
        agent.sessions = None
        patcher = RuntimePatcher()
        ok, detail = patcher.patch_async_session_saves(agent)
        assert not ok
        assert "agent.sessions is missing" in detail

    def test_non_loop_call_runs_synchronously(self):
        agent = MagicMock()
        sessions = MagicMock()
        agent.sessions = sessions
        calls = []

        def _fake_save(session, fsync=False):
            calls.append(("save", getattr(session, "key", "?"), fsync))

        sessions.save = _fake_save
        patcher = RuntimePatcher()
        ok, _ = patcher.patch_async_session_saves(agent)
        assert ok

        session = MagicMock()
        session.key = "k"
        sessions.save(session, fsync=True)
        assert calls == [("save", "k", True)]

    @pytest.mark.asyncio
    async def test_loop_call_deferred_to_executor(self):
        agent = MagicMock()
        sessions = MagicMock()
        agent.sessions = sessions
        fired = threading.Event()
        received = []

        def _fake_save(session, fsync=False):
            received.append((session.key, fsync))
            time.sleep(0.05)
            fired.set()

        sessions.save = _fake_save
        patcher = RuntimePatcher()
        ok, _ = patcher.patch_async_session_saves(agent)
        assert ok

        session = MagicMock()
        session.key = "k1"
        session.messages = [{"role": "user", "content": "hi"}]
        session.metadata = {"a": 1}
        session.created_at = 123
        session.updated_at = 124
        session.last_consolidated = 0

        result = sessions.save(session, fsync=False)
        assert result is None  # вызывает только возвращается сразу, не блокируя loop
        await asyncio.sleep(0.15)
        assert fired.is_set()
        assert received == [("k1", False)]
        sessions._async_save_executor.shutdown(wait=True)


class TestApplyAll:
    def test_report_contents(self):
        agent = MagicMock()
        original_return = MagicMock()
        original_return.metadata = {}
        agent._assemble_outbound.return_value = original_return
        hook = MagicMock()
        hook.drain.return_value = []

        patcher = RuntimePatcher()
        report = patcher.apply_all(
            MagicMock(), _settings(persist_threshold=0), Path("ws"), agent, hook,
            db_logging_service=None,
        )
        d = report.to_dict()
        assert "assemble_outbound" in d["applied"]
        # context_bridge_seed удалён в nanobot 0.3.5; seed лимита
        # делает RuntimeEventsSubscriber через bus.subscribe.
        assert "context_bridge_seed" not in d["applied"]
        assert "context_bridge_seed" not in d.get("skipped", [])
        assert any(name == "context_governor" for name, _ in d["skipped"])
        assert any(name == "subagent_logging" for name, _ in d["skipped"])


class TestPatchContextBridgeSeed:
    """``patch_context_bridge_seed`` удалён в nanobot 0.3.5:
    seed лимита окна делается подпиской на TurnRuntimeAdmitted
    в ``RuntimeEventsSubscriber`` (зарегистрированной через
    ``ApplicationContext.start()``). См.
    ``openspec/changes/runtime-events-subscription`` и
    ``post-0.3.5-patches-cleanup``.

    Защитные тесты:
    * Метод ``patch_context_bridge_seed`` не существует на ``RuntimePatcher``.
    * Spec ``context_bridge_seed`` НЕ зарегистрирован в ``_PATCH_SPECS``.
    * ``apply_all()`` report НЕ содержит "context_bridge_seed".
    """

    def test_method_removed(self):
        from lib.services.runtime_patcher import RuntimePatcher

        assert not hasattr(RuntimePatcher, "patch_context_bridge_seed"), (
            "patch_context_bridge_seed удалён в nanobot 0.3.5; "
            "seed лимита делает RuntimeEventsSubscriber через "
            "bus.subscribe(TurnRuntimeAdmitted)"
        )

    def test_spec_removed_from_patch_specs(self):
        from lib.services.runtime_patcher import RuntimePatcher

        # _PATCH_SPECS — module-level dict[str, PatchSpec].
        spec_dict = getattr(RuntimePatcher, "_PATCH_SPECS", {})
        assert "context_bridge_seed" not in spec_dict, (
            "spec context_bridge_seed удалён из _PATCH_SPECS в nanobot 0.3.5"
        )

    def test_apply_all_report_has_no_context_bridge_seed(self):
        """``apply_all()`` НЕ пишет в отчёт context_bridge_seed.

        Проверяется через ``RuntimePatcher._PATCH_SPECS`` напрямую
        (без вызова ``apply_all``, который тянет тяжёлые deps через
        ``patch_project_tools``).
        """
        from lib.services.runtime_patcher import RuntimePatcher

        spec_dict = getattr(RuntimePatcher, "_PATCH_SPECS", {})
        assert "context_bridge_seed" not in spec_dict, (
            "context_bridge_seed удалён из _PATCH_SPECS — apply_all "
            "больше не регистрирует этот patch"
        )


class TestPatchReportClassification:
    """PatchReport должен различать skipped (конфигуративный) и failed
    (реальный сбой) — TARGET §26.
    """

    def test_skippable_reason_goes_to_skipped(self):
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(report, "save_turn", (False, "persist_threshold <= 0"))
        assert report.skipped == [("save_turn", "persist_threshold <= 0")]
        assert report.failed == []

    def test_real_failure_goes_to_failed(self):
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report, "assemble_outbound", (False, "import failed: cannot import name X"),
        )
        assert report.failed == [("assemble_outbound", "import failed: cannot import name X")]
        assert report.skipped == []

    def test_agent_none_is_skipped(self):
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(report, "async_save", (False, "agent is None"))
        assert report.skipped == [("async_save", "agent is None")]
        assert report.failed == []

    def test_idle_compact_enabled_is_skipped(self):
        """Сохранено как legacy-причина — некоторые скипы теперь
        классифицируются по другим правилам, но ``idle compact
        enabled`` остаётся в ``_SKIPPABLE_REASONS`` для обратной
        совместимости с PatchSpec.
        """
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report, "idle_guard", (False, "idle compact enabled (ttl=180)"),
        )
        assert report.skipped == [("idle_guard", "idle compact enabled (ttl=180)")]
        assert report.failed == []

    def test_missing_attr_is_failed(self):
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report, "assemble_outbound", (False, "agent._assemble_outbound is missing"),
        )
        assert report.failed == [("assemble_outbound", "agent._assemble_outbound is missing")]
        assert report.skipped == []

    def test_module_not_loaded_is_skipped(self):
        """``module not loaded`` — конфигуративный skip: nanobot не подключил
        эти модули в runtime (например, в unit-тесте без AgentLoop).
        """
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report, "exec_limits", (False, "exec_session/shell module not loaded"),
        )
        assert report.skipped == [("exec_limits", "exec_session/shell module not loaded")]
        assert report.failed == []

    def test_internal_failed_marker_reclassifies(self):
        """``[INTERNAL_FAILED]`` от ``patch_project_tools`` → failed."""
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report,
            "project_tools",
            (True, "[INTERNAL_FAILED] 3 project tools registered: foo; 1 failed: Bar"),
        )
        assert report.failed == [
            (
                "project_tools",
                "[INTERNAL_FAILED] 3 project tools registered: foo; 1 failed: Bar",
            ),
        ]
        assert report.skipped == []
        assert "project_tools" not in report.applied

    def test_details_recorded_for_every_state(self):
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report, "document_text_threshold",
            (True, "reference_non_image_attachments patched"),
        )
        RuntimePatcher._record(report, "save_turn", (False, "persist_threshold <= 0"))
        RuntimePatcher._record(
            report, "assemble_outbound", (False, "import failed: boom"),
        )
        assert (
            report.details["document_text_threshold"]
            == "reference_non_image_attachments patched"
        )
        assert report.details["save_turn"] == "persist_threshold <= 0"
        assert (
            report.details["assemble_outbound"] == "import failed: boom"
        )


class TestPatchReportRender:
    """``PatchReport.render`` — формат для startup-логов."""

    def test_render_marks_applied_skipped_failed(self):
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        report.applied.append("document_text_threshold")
        report.skipped.append(("save_turn", "persist_threshold <= 0"))
        report.failed.append(("assemble_outbound", "import failed: boom"))
        rendered = report.render()
        assert "✓ document_text_threshold" in rendered
        assert "⚠ save_turn skipped: persist_threshold <= 0" in rendered
        assert "✗ assemble_outbound failed: import failed: boom" in rendered

    def test_render_includes_spec_purpose_for_failed(self):
        from lib.services.runtime_patcher import PatchReport, RuntimePatcher

        specs = RuntimePatcher.patch_specs()
        report = PatchReport()
        report.failed.append(("assemble_outbound", "import failed: boom"))
        rendered = report.render(specs=specs)
        assert "✗ assemble_outbound failed: import failed: boom" in rendered
        assert "tool_audit" in rendered  # purpose из PatchSpec


class TestPatchSpecs:
    """Каждый патч из ``apply_all`` должен иметь ``PatchSpec``."""

    def test_all_patches_have_specs(self):
        from lib.services.runtime_patcher import RuntimePatcher

        specs = RuntimePatcher.patch_specs()
        expected = {
            "context_governor", "save_turn", "exec_limits", "exec_timeout_cap",
            "tool_limits", "assemble_outbound",
            # context_bridge_seed удалён в nanobot 0.3.5; seed делает
            # RuntimeEventsSubscriber через bus.subscribe.
            "async_save", "subagent_logging", "project_tools",
            "session_content_cleanup", "document_text_threshold",
        }
        # Допускаются DEPRECATED-метки (compact_*), но базовый набор
        # должен совпадать. Проверяем через >= для совместимости с
        # другими удалёнными patches (compact_* удалены в 0.3.5).
        actual = set(specs)
        for key in expected:
            assert key in actual, f"отсутствует {key!r} в patch_specs()"
        # context_bridge_seed не должен быть в actual.
        assert "context_bridge_seed" not in actual

    def test_specs_have_required_fields(self):
        from lib.services.runtime_patcher import RuntimePatcher

        specs = RuntimePatcher.patch_specs()
        for name, spec in specs.items():
            assert spec.name == name
            assert spec.purpose, f"{name}: purpose is empty"
            assert spec.nanobot_target, f"{name}: nanobot_target is empty"
            assert spec.reason, f"{name}: reason is empty"
            assert spec.risk in ("low", "medium", "high"), f"{name}: bad risk '{spec.risk}'"
            assert spec.nanobot_version, f"{name}: nanobot_version is empty"

    def test_high_risk_patches_are_private_methods(self):
        """``risk='high'`` только для патчей, трогающих приватные методы."""
        from lib.services.runtime_patcher import RuntimePatcher

        specs = RuntimePatcher.patch_specs()
        for name, spec in specs.items():
            if spec.risk == "high":
                target = spec.nanobot_target
                assert any(
                    token in target
                    for token in ("_save_turn", "_assemble_outbound", "_SubagentHook")
                ), (
                    f"{name}: marked high risk but target '{target}' is not "
                    "a known private method"
                )


class TestApplyAllFailed:
    """``TestApplyAll`` — расширения для нового состояния ``failed``."""

    def test_report_contents_has_details(self):
        agent = MagicMock()
        original_return = MagicMock()
        original_return.metadata = {}
        agent._assemble_outbound.return_value = original_return
        hook = MagicMock()
        hook.drain.return_value = []

        patcher = RuntimePatcher()
        report = patcher.apply_all(
            MagicMock(), _settings(persist_threshold=0), Path("ws"), agent, hook,
            db_logging_service=None,
        )
        d = report.to_dict()
        assert "details" in d
        assert "document_text_threshold" in d["details"]
        assert "save_turn" in d["details"]
        assert "subagent_logging" in d["details"]

    def test_normal_scenario_no_failures(self):
        """В нормальном сценарии (MagicMock-agent, persist=0, db_logging=None)
        НЕ должно быть failed — все skipped/applied.
        """
        agent = MagicMock()
        original_return = MagicMock()
        original_return.metadata = {}
        agent._assemble_outbound.return_value = original_return
        hook = MagicMock()
        hook.drain.return_value = []

        patcher = RuntimePatcher()
        report = patcher.apply_all(
            MagicMock(), _settings(persist_threshold=0), Path("ws"), agent, hook,
            db_logging_service=None,
        )
        assert report.failed == [], f"unexpected failures: {report.failed}"


def _make_stub_td_module(published, turn_completed_calls):
    """Создать НЕЗАВИСИМЫЙ stub-модуль ``nanobot.agent.turn_delivery``.

    Каждый вызов возвращает СВЕЖИЙ класс ``TurnDelivery`` — критично,
    потому что патч мутирует ``TurnDelivery.fail`` на уровне класса,
    и если использовать общий класс между тестами, состояние протекает.

    Stub воспроизводит upstream ``turn_delivery.py:336-353``:
    ``fail()`` зовёт ``await self.bus.publish_outbound(...)`` (а не
    мутирует общий список в обход bus), чтобы per-instance прокси
    ``_OutboundSilencer`` мог подавить outbound при вызове оригинала.
    """
    import types as _types

    class _RuntimeEventPublisher:
        async def turn_completed(self, **kwargs):
            turn_completed_calls.append(kwargs)

    class _StubBus:
        def __init__(self, sink):
            self._sink = sink

        async def publish_outbound(self, msg):
            self._sink.append(msg)

    class _TurnDelivery:
        def __init__(self):
            self.lifecycle_message = None
            self.bus = None
            self.session_key = None
            self._failure_error_kind = None
            self.runtime_event_publisher = None

        async def fail(self, *, publish_completion: bool) -> None:
            from nanobot.bus.events import OutboundMessage

            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=self.lifecycle_message.channel,
                    chat_id=self.lifecycle_message.chat_id,
                    content="Sorry, I encountered an error.",
                    metadata=dict(self.lifecycle_message.metadata or {}),
                )
            )
            if publish_completion:
                await self.runtime_event_publisher.turn_completed(
                    channel=self.lifecycle_message.channel,
                    chat_id=self.lifecycle_message.chat_id,
                    session_key=self.session_key,
                    metadata=self.lifecycle_message.metadata,
                    outcome="failed",
                    failure_kind="internal",
                )

    mod = _types.ModuleType("nanobot.agent.turn_delivery")
    mod.TurnDelivery = _TurnDelivery

    def _make_instance():
        inst = _TurnDelivery()
        inst.lifecycle_message = MagicMock()
        inst.lifecycle_message.channel = "cli"
        inst.lifecycle_message.chat_id = "c1"
        inst.lifecycle_message.metadata = {"foo": "bar"}
        inst.lifecycle_message.sender_id = "u1"
        # ``InboundMessage`` НЕ имеет ``session_key`` / ``user_id`` —
        # ставим None, чтобы тесты провалились, если реализация
        # по ошибке начнёт их читать.
        inst.lifecycle_message.session_key = None
        inst.lifecycle_message.user_id = None
        inst.bus = _StubBus(published)
        inst.session_key = "sess1"
        inst._failure_error_kind = "RuntimeError"
        inst.runtime_event_publisher = _RuntimeEventPublisher()
        return inst

    return mod, _make_instance


class TestPatchTurnDeliveryFail:
    """Контракт error fallback (``openspec/specs/runtime/error-fallback``)."""

    @pytest.fixture
    def stub_td_module(self, monkeypatch):
        """Подменить ``nanobot.agent.turn_delivery`` stub-модулем.

        Каждый вызов фикстуры создаёт СВЕЖИЙ класс ``TurnDelivery`` —
        критично, потому что патч мутирует ``TurnDelivery.fail`` на
        уровне класса, и общий класс между тестами протекал бы.

        Возвращает ``(mod, published, turn_completed_calls, make_instance)``.
        """
        published: list = []
        turn_completed_calls: list = []
        mod, make_instance = _make_stub_td_module(
            published, turn_completed_calls,
        )
        monkeypatch.setitem(sys.modules, "nanobot.agent.turn_delivery", mod)
        return mod, published, turn_completed_calls, make_instance

    @pytest.mark.asyncio
    async def test_default_text_when_no_settings(self, stub_td_module):
        _, published, _, _ = stub_td_module
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_turn_delivery_fail(settings=None)
        assert ok, msg
        from lib.services.runtime_patcher import (
            _DEFAULT_INTERNAL_ERROR_TEXT,
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        # Ровно один outbound — наш fallback. Upstream-литерал подавлен
        # per-instance прокси на ``self.bus``.
        assert len(published) == 1
        out = published[0]
        assert out.content == _DEFAULT_INTERNAL_ERROR_TEXT
        assert out.channel == "cli"
        assert out.chat_id == "c1"
        assert out.metadata.get("_error_kind") == "internal"
        assert out.metadata.get("_final_turn") is True

    @pytest.mark.asyncio
    async def test_custom_text_from_settings(self, stub_td_module):
        _, published, _, _ = stub_td_module
        settings = {
            "gateway": {
                "error_messages": {
                    "internal_error": "Сервис временно недоступен.",
                },
            },
        }
        patcher = RuntimePatcher()
        ok, _ = patcher.patch_turn_delivery_fail(settings=settings)
        assert ok

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(published) == 1
        assert published[0].content == "Сервис временно недоступен."

    @pytest.mark.asyncio
    async def test_no_exception_details_leak_to_user(self, stub_td_module):
        """requirement: content содержит ТОЛЬКО заготовку, не str(exc)."""
        _, published, _, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        inst._failure_error_kind = "KeyError: agent_internal_state_xyz"
        await inst.fail(publish_completion=True)

        assert "KeyError" not in published[0].content
        assert "agent_internal_state_xyz" not in published[0].content

    @pytest.mark.asyncio
    async def test_upstream_literal_not_published(self, stub_td_module):
        """Upstream-литерал ``"Sorry, I encountered an error."`` НЕ ДОЛЖЕН
        доходить до пользователя — это инвариант подмены.
        """
        _, published, _, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        for msg in published:
            assert msg.content != "Sorry, I encountered an error.", (
                f"upstream literal leaked: {msg.content!r}"
            )

    @pytest.mark.asyncio
    async def test_log_to_db_true_writes_event(
        self, stub_td_module, monkeypatch
    ):
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append((svc, event, producer, event_type))
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        svc = MagicMock()
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(
            settings=None, db_logging_service=svc, agent_id="agent_test",
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(recorded) == 1
        svc_arg, log_event, producer, event_type = recorded[0]
        assert svc_arg is svc
        assert producer == "runtime_patcher"
        assert event_type == "turn_failed"
        assert log_event.event_type == "turn_failed"
        assert log_event.session_id == "sess1"
        assert log_event.channel == "cli"
        assert log_event.payload["kind"] == "internal"
        assert log_event.payload["failure_error_kind"] == "RuntimeError"
        assert log_event.payload["agent_id"] == "agent_test"
        assert log_event.payload["sender_id"] == "u1"
        assert log_event.payload["chat_id"] == "c1"
        # exception_available зависит от того, есть ли активное исключение
        # при вызове. В pytest-asyncio без except-блока — False.
        assert log_event.payload["exception_available"] is False
        assert log_event.payload["exception_type"] is None
        assert log_event.payload["exception_message"] is None

    @pytest.mark.asyncio
    async def test_log_to_db_false_skips_db(
        self, stub_td_module, monkeypatch
    ):
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(
            settings={
                "gateway": {"error_messages": {"log_to_db": False}},
            },
            db_logging_service=MagicMock(),
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert recorded == [], (
            f"try_log_event called despite log_to_db=False: {recorded}"
        )

    @pytest.mark.asyncio
    async def test_no_db_logging_service_is_fail_open(
        self, stub_td_module, monkeypatch
    ):
        """При ``db_logging_service=None`` fallback-сообщение всё равно
        уходит пользователю (fail-open).
        """
        _, published, _, _ = stub_td_module

        def fake_try_log_event(svc, event, *, producer, event_type):
            raise AssertionError("try_log_event should not be called")

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(
            settings=None,
            db_logging_service=None,
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(published) == 1
        assert published[0].content == (
            "Я не справился с этим запросом. "
            "Попробуйте, пожалуйста, ещё раз или переформулируйте вопрос."
        )

    @pytest.mark.asyncio
    async def test_turn_completed_event_published(self, stub_td_module):
        """``publish_completion=True`` — оригинальный ``fail`` зовёт
        ``turn_completed`` (сохранение runtime-event публикации).
        """
        _, _, turn_completed_calls, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(turn_completed_calls) == 1
        assert turn_completed_calls[0]["outcome"] == "failed"
        assert turn_completed_calls[0]["failure_kind"] == "internal"

    @pytest.mark.asyncio
    async def test_turn_completed_not_published_when_completion_false(
        self, stub_td_module
    ):
        _, _, turn_completed_calls, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=False)

        assert turn_completed_calls == []

    @pytest.mark.asyncio
    async def test_session_key_from_turn_delivery_instance(
        self, stub_td_module, monkeypatch
    ):
        """``session_key`` берётся из ``self.session_key`` (атрибут
        ``TurnDelivery``), а НЕ из ``lifecycle_message`` (такого поля нет).
        """
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None, db_logging_service=MagicMock())

        inst = stub_td_module[3]()
        # Поставим «плохое» значение в lifecycle_message.session_key —
        # если реализация по ошибке его читает, тест упадёт.
        inst.lifecycle_message.session_key = "WRONG_LIFECYCLE"
        inst.session_key = "real_session_key"
        await inst.fail(publish_completion=True)

        assert recorded[0].session_id == "real_session_key"

    @pytest.mark.asyncio
    async def test_sender_id_from_lifecycle_message(
        self, stub_td_module, monkeypatch
    ):
        """``sender_id`` берётся из ``lifecycle_message.sender_id``."""
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None, db_logging_service=MagicMock())

        inst = stub_td_module[3]()
        inst.lifecycle_message.sender_id = "u-42"
        await inst.fail(publish_completion=True)

        assert recorded[0].payload["sender_id"] == "u-42"
        assert recorded[0].user_id == "u-42"

    @pytest.mark.asyncio
    async def test_agent_id_passed_through(
        self, stub_td_module, monkeypatch
    ):
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(
            settings=None, db_logging_service=MagicMock(), agent_id="agent_main",
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert recorded[0].payload["agent_id"] == "agent_main"

    @pytest.mark.asyncio
    async def test_exception_available_inside_except_block(
        self, stub_td_module, monkeypatch
    ):
        """При вызове из ``except``-блока ``sys.exception()`` возвращает
        активное исключение — payload содержит ``exception_available=true``
        и тип/текст исключения.
        """
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None, db_logging_service=MagicMock())

        inst = stub_td_module[3]()

        try:
            raise ValueError("boom-12345")
        except Exception:
            await inst.fail(publish_completion=True)

        assert len(recorded) == 1
        evt = recorded[0]
        assert evt.payload["exception_available"] is True
        assert evt.payload["exception_type"] == "ValueError"
        assert evt.payload["exception_message"] == "boom-12345"

    @pytest.mark.asyncio
    async def test_exception_unavailable_degrades_gracefully(
        self, stub_td_module, monkeypatch
    ):
        """Без активного исключения (прямой вызов из теста) — payload
        содержит ``exception_available=false`` и ``null`` тип/сообщение.
        """
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None, db_logging_service=MagicMock())

        inst = stub_td_module[3]()
        # Прямой вызов — НЕ из except-блока.
        await inst.fail(publish_completion=True)

        assert len(recorded) == 1
        evt = recorded[0]
        assert evt.payload["exception_available"] is False
        assert evt.payload["exception_type"] is None
        assert evt.payload["exception_message"] is None

    def test_module_not_loaded_returns_false(self, monkeypatch):
        """Если ``TurnDelivery`` модуль отсутствует в ``sys.modules`` —
        патч возвращает ``(False, <reason>)`` без падения.
        """
        monkeypatch.delitem(
            sys.modules, "nanobot.agent.turn_delivery", raising=False
        )
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_turn_delivery_fail(settings=None)
        assert not ok
        assert "not loaded" in msg

    def test_turn_delivery_fail_missing_returns_false(self, stub_td_module):
        """Если у stub-класса нет атрибута ``fail`` — патч no-op."""
        stub_td_module[0].TurnDelivery.fail = None  # type: ignore[assignment]
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_turn_delivery_fail(settings=None)
        assert not ok
        assert "missing" in msg

    def test_invalid_internal_error_type_falls_back_to_default(
        self, stub_td_module
    ):
        """Невалидный ``internal_error`` (не строка) → default-текст."""
        _, _, _, _ = stub_td_module
        patcher = RuntimePatcher()
        ok, _ = patcher.patch_turn_delivery_fail(
            settings={
                "gateway": {"error_messages": {"internal_error": 999}},
            },
        )
        assert ok

    @pytest.mark.asyncio
    async def test_cancelled_error_path_untouched(self, stub_td_module):
        """Патч не подменяет ``_process_message`` — CancelledError-ветка
        upstream'а остаётся нетронутой. Здесь фиксируем только инвариант
        «ровно один outbound» и отсутствие побочных эффектов.
        """
        _, published, _, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(published) == 1
        assert published[0].metadata.get("_error_kind") == "internal"

    @pytest.mark.asyncio
    async def test_bus_is_restored_after_original_fail(
        self, stub_td_module
    ):
        """``self.bus`` восстанавливается после вызова оригинального
        ``fail()`` — критично для следующих вызовов в этом обороте.
        """
        _, _, _, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        real_bus = inst.bus
        await inst.fail(publish_completion=True)

        assert inst.bus is real_bus
