from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lib.services.runtime_patcher import RuntimePatcher


def _settings(**overrides):
    gw = {
        "persist_threshold": 0,
        "persist_max_files": 100,
        "persist_max_age_hours": 24,
    }
    gw.update(overrides)

    class _Settings:
        gateway = gw

    return _Settings()


class TestPatchAssembleOutbound:
    def test_wraps_and_injects_audit(self):
        agent = MagicMock()
        original_return = MagicMock()
        original_return.metadata = {}
        agent._assemble_outbound.return_value = original_return

        hook = MagicMock()
        hook.drain.return_value = [{"name": "read"}]

        patcher = RuntimePatcher()
        ok, _ = patcher.patch_assemble_outbound(agent, hook)
        assert ok

        result = agent._assemble_outbound(
            MagicMock(), "content", [], "stop", False, None
        )
        hook.drain.assert_called_once()
        assert result.metadata["_tool_audit"] == [{"name": "read"}]

    def test_result_none_skips_drain(self):
        agent = MagicMock()
        agent._assemble_outbound.return_value = None
        hook = MagicMock()

        patcher = RuntimePatcher()
        ok, _ = patcher.patch_assemble_outbound(agent, hook)

        result = agent._assemble_outbound(None, None, None, None, False, None)
        assert result is None
        hook.drain.assert_not_called()
        assert ok

    def test_stamps_final_turn_marker(self):
        agent = MagicMock()
        original_return = MagicMock()
        original_return.metadata = {}
        agent._assemble_outbound.return_value = original_return

        patcher = RuntimePatcher()
        ok, _ = patcher.patch_assemble_outbound(agent, MagicMock())

        result = agent._assemble_outbound(MagicMock(), "x", [], "stop", False, None)
        assert ok
        assert result.metadata["_final_turn"] is True

    def test_none_result_synthesizes_marker_outbound(self):
        agent = MagicMock()
        agent._assemble_outbound.return_value = None

        patcher = RuntimePatcher()
        ok, _ = patcher.patch_assemble_outbound(agent, MagicMock())

        msg = MagicMock()
        msg.channel = "postgres"
        msg.chat_id = "chat-1"
        msg.metadata = {"message_id": "m-1", "answer_id": "a-1"}

        result = agent._assemble_outbound(msg, "", [], "stop", False, None)
        assert ok
        assert result is not None
        assert result.metadata["_final_turn"] is True
        assert result.content == ""
        assert result.chat_id == "chat-1"

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

        def _fake_save_turn(session, messages, skip, *, turn_latency_ms=None):
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


class TestPatchCompactCommand:
    """``patch_compact_command`` — регистрация ``/compact`` как slash-команды."""

    def test_registers_exact_and_prefix(self):
        from functools import partial

        from lib.commands.compact_command import cmd_compact

        class _Commands:
            def __init__(self):
                self.exact_reg = {}
                self.prefix_reg = []

            def exact(self, cmd, handler):
                self.exact_reg[cmd] = handler

            def prefix(self, pfx, handler):
                self.prefix_reg.append((pfx, handler))

        class _Agent:
            commands = _Commands()

        patcher = RuntimePatcher()
        ok, detail = patcher.patch_compact_command(_Agent(), _settings())
        assert ok, detail
        assert "/compact" in _Agent.commands.exact_reg
        handler = _Agent.commands.exact_reg["/compact"]
        assert isinstance(handler, partial)
        assert handler.func is cmd_compact
        assert any(pfx == "/compact " for pfx, _ in _Agent.commands.prefix_reg)

    def test_missing_commands_skipped(self):
        class _Agent:
            pass

        patcher = RuntimePatcher()
        ok, detail = patcher.patch_compact_command(_Agent(), _settings())
        assert ok is False
        assert "commands" in detail

    def test_apply_all_includes_compact_command(self):
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
        assert "compact_command" in report.to_dict()["applied"]


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
        assert "context_bridge_seed" in d["applied"]
        assert any(name == "context_governor" for name, _ in d["skipped"])
        assert any(name == "subagent_logging" for name, _ in d["skipped"])


class TestPatchContextBridgeSeed:
    """``patch_context_bridge_seed`` — патч ``agent._state_build`` для live-update."""

    @pytest.fixture(autouse=True)
    def _clean_bridge(self):
        from lib.hooks.database_logging_hook import pop_context_bridge
        pop_context_bridge("postgres:chat-1")
        yield
        pop_context_bridge("postgres:chat-1")

    def test_no_agent_skipped(self):
        from lib.services.runtime_patcher import RuntimePatcher

        ok, detail = RuntimePatcher().patch_context_bridge_seed(None)
        assert ok is False
        assert "agent is None" in detail

    def test_no_state_build_skipped(self):
        from lib.services.runtime_patcher import RuntimePatcher

        agent = MagicMock(spec=[])
        ok, detail = RuntimePatcher().patch_context_bridge_seed(agent)
        assert ok is False
        assert "_state_build is missing" in detail

    @pytest.mark.asyncio
    async def test_patches_state_build_and_seeds_bridge(self):
        from lib.hooks.database_logging_hook import _CONTEXT_BRIDGE
        from lib.services.runtime_patcher import RuntimePatcher

        call_count = {"n": 0}

        async def original_state_build(c):
            call_count["n"] += 1
            return {"fresh": True, "got": c}

        agent = MagicMock()
        agent._state_build = original_state_build

        runtime = MagicMock()
        runtime.context_window_tokens = 65536
        runtime.model = "MiniMax-M3"

        ctx = MagicMock()
        ctx.runtime = runtime
        ctx.session_key = "postgres:chat-1"

        ok, detail = RuntimePatcher().patch_context_bridge_seed(agent)
        assert ok is True

        result = await agent._state_build(ctx)
        assert result == {"fresh": True, "got": ctx}
        assert call_count["n"] == 1

        entry = _CONTEXT_BRIDGE.get("postgres:chat-1") or {}
        assert entry.get("limit") == 65536
        assert entry.get("model") == "MiniMax-M3"

    @pytest.mark.asyncio
    async def test_seed_errors_do_not_break_state_build(self):
        """Любой сбой внутри seed → оригинальный ``_state_build`` всё равно вызван."""
        from lib.services.runtime_patcher import RuntimePatcher

        called = {"n": 0}

        async def original_state_build(c):
            called["n"] += 1
            return {"fresh": True}

        agent = MagicMock()
        agent._state_build = original_state_build
        agent.runtime_for_session = MagicMock(side_effect=RuntimeError("boom"))

        ctx = MagicMock()
        ctx.runtime = None
        ctx.session_key = "postgres:chat-1"

        RuntimePatcher().patch_context_bridge_seed(agent)
        result = await agent._state_build(ctx)
        assert called["n"] == 1
        assert result == {"fresh": True}
