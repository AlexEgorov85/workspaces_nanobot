from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        assert any(name == "context_governor" for name, _ in d["skipped"])
        assert any(name == "subagent_logging" for name, _ in d["skipped"])
