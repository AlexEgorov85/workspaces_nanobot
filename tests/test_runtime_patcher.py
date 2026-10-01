from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        патчи upstream runtime API).
        """
        from lib.services.runtime_patcher import RuntimePatcher

        spec_dict = getattr(RuntimePatcher, "_PATCH_SPECS", {})
        assert "context_bridge_seed" not in spec_dict, (
            "context_bridge_seed удалён из _PATCH_SPECS — apply_all "
            "больше не регистрирует этот patch"
        )


#: Патчи, перенесённые на нативные точки расширения в фазе 6
#: (change ``enterprise-mcp-platform``, п. 6.2/6.3/6.5/6.6/6.11).
#: Ключ — имя метода ``patch_*``. Значение — ``(путь, символ)`` нативной
#: замены; ``None`` — замены нет by design (см. ``NO_NATIVE_REPLACEMENT``).
#: Если патч вернётся — заработают две реализации одного механизма, а это
#: прямо запрещено правилом проекта. Поэтому проверяются обе стороны:
#: старого нет, новое есть.
REMOVED_PATCHES: dict[str, tuple[str, str] | None] = {
    "patch_save_turn": (
        "lib/hooks/tool_result_archive_hook.py",
        "ToolResultArchiveHook",
    ),
    "patch_async_session_saves": (
        "lib/services/session_storage.py",
        "install_async_save",
    ),
    "patch_session_content_cleanup": (
        "lib/session/pg_session_manager.py",
        "clean_session_content",
    ),
    "patch_document_text_threshold": (
        "workspace/tools/document_read.py",
        "DocumentReadTool",
    ),
    # Временный диагностический патч (гейт ``runtime_diagnostics.
    # session_dir_watch``, по умолчанию выключен, поведение save не менял).
    # Нативной замены нет и не должно быть: фича была нужна только для
    # расследования одной ошибки и удалена вместе с её причиной.
    "patch_session_dir_watch": None,
}

#: Удалённые патчи, у которых нативной замены нет by design.
NO_NATIVE_REPLACEMENT: frozenset[str] = frozenset({"patch_session_dir_watch"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


class TestRemovedPatchesHaveNoLiveImplementation:
    """Удалённые патчи не должны вернуться ни в каком виде.

    Инвариант миграции «monkey-patch → нативная точка расширения»:
    у каждого удалённого патча ровно одна реализация — новая, нативная.
    Проверки сделаны так, чтобы падать на заведомо плохих данных:
    верни ``patch_save_turn`` в ``_PATCH_SPECS`` — упадёт
    ``test_specs_absent_from_patch_specs``; верни вызов в тело
    ``apply_all`` — упадёт ``test_absent_from_apply_all_report``.
    """

    def test_methods_removed(self):
        for method_name, replacement in REMOVED_PATCHES.items():
            assert not hasattr(RuntimePatcher, method_name), (
                f"{method_name} удалён в фазе 6; нативная замена — "
                f"{replacement}. Две работающие реализации одного "
                f"механизма запрещены."
            )

    def test_specs_absent_from_patch_specs(self):
        spec_dict = getattr(RuntimePatcher, "_PATCH_SPECS", {})
        removed_spec_names = {name[len("patch_"):] for name in REMOVED_PATCHES}
        leaked = sorted(removed_spec_names & set(spec_dict))
        assert not leaked, (
            f"spec'ы удалённых патчей остались в _PATCH_SPECS: {leaked} — "
            f"apply_all их не регистрирует, инвентарь будет врать"
        )

    def test_absent_from_apply_all_report(self):
        """Ни тело ``apply_all``, ни пустой отчёт не упоминают удалённое."""
        import ast
        import inspect
        import textwrap

        from lib.services.runtime_patcher import PatchReport

        src = textwrap.dedent(inspect.getsource(RuntimePatcher.apply_all))
        called = {
            node.func.attr
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        still_called = sorted(set(REMOVED_PATCHES) & called)
        assert not still_called, (
            f"apply_all всё ещё вызывает удалённые патчи: {still_called}"
        )

        details = getattr(PatchReport(), "details", {})
        leaked_details = sorted(
            {name[len("patch_"):] for name in REMOVED_PATCHES} & set(details)
        )
        assert not leaked_details, leaked_details

    def test_native_replacements_are_importable(self):
        """Новые реализации существуют, импортируются и лежат по путям.

        Зеркало ``test_methods_removed``: если удалённый патч не вернётся,
        но нативную замену потеряют — миграция развалилась в другую
        сторону (механизм исчез совсем, а не переехал).
        """
        import importlib.util

        root = _repo_root()
        for method_name, replacement in REMOVED_PATCHES.items():
            if method_name in NO_NATIVE_REPLACEMENT:
                continue
            rel_path, symbol = replacement
            abs_path = root / rel_path
            assert abs_path.is_file(), (
                f"{method_name}: нативная замена {rel_path} не найдена "
                f"(миграция объявляет замену, которой нет)"
            )
            mod_name = "_repl_" + rel_path.replace("/", "_").removesuffix(".py")
            spec = importlib.util.spec_from_file_location(mod_name, abs_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:  # noqa: BLE001 — падение = дефект замены
                raise AssertionError(
                    f"{method_name}: нативная замена {rel_path} не импортируется: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            assert hasattr(module, symbol), (
                f"{method_name}: в {rel_path} нет символа {symbol}"
            )

    def test_every_removed_patch_is_classified(self):
        """Каждый удалённый патч либо имеет замену, либо помечен «by design».

        Страж на саму таблицу: забытый патч без замены и без пометки
        выглядел бы как «переехал», хотя нативного пути нет.
        """
        for method_name, replacement in REMOVED_PATCHES.items():
            if method_name in NO_NATIVE_REPLACEMENT:
                assert replacement is None, (
                    f"{method_name} помечен «by design», но замена задана"
                )
            else:
                assert replacement is not None, (
                    f"{method_name}: нет ни нативной замены, ни пометки "
                    f"в NO_NATIVE_REPLACEMENT"
                )

    def test_save_turn_replacement_covers_both_cases(self):
        """``ToolResultArchiveHook`` архивирует, но НЕ подменяет контент.

        Фиксирует осознанное ограничение переноса (см. докстринг хука):
        ``after_execute_tool`` не может изменить то, что уйдёт в историю.
        Тест ловит регрессию, при которой хук снова начнёт «молча»
        переписывать содержимое результата.
        """
        from lib.hooks.tool_result_archive_hook import (
            _PERSISTED_PREFIX,
            ToolResultArchiveHook,
        )

        assert hasattr(ToolResultArchiveHook, "after_execute_tool")
        # Переопределения нет (метод базового AgentHook не считается):
        # хук не подменяет содержимое, только пишет его на диск.
        assert "before_execute_tool" not in ToolResultArchiveHook.__dict__
        assert _PERSISTED_PREFIX.startswith("[Result saved to data_store/")


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
        """``[INTERNAL_FAILED]`` маркер → failed (не applied, не skipped)."""
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report,
            "save_turn",
            (True, "[INTERNAL_FAILED] 3 turns saved: foo; 1 failed: Bar"),
        )
        assert report.failed == [
            (
                "save_turn",
                "[INTERNAL_FAILED] 3 turns saved: foo; 1 failed: Bar",
            ),
        ]
        assert report.skipped == []
        assert "save_turn" not in report.applied

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
        report.applied.append("context_governor")
        report.skipped.append(("exec_limits", "persist_threshold <= 0"))
        report.failed.append(("turn_delivery_fail", "import failed: boom"))
        rendered = report.render()
        assert "✓ context_governor" in rendered
        assert "⚠ exec_limits skipped: persist_threshold <= 0" in rendered
        assert "✗ turn_delivery_fail failed: import failed: boom" in rendered

    def test_render_includes_spec_purpose_for_failed(self):
        """Правило: ``purpose`` из ``PatchSpec`` попадает в рендер
        для КАЖДОГО патча, а не для одного выбранного имени.

        Раньше тест брал один патч (``assemble_outbound``) и одну
        строку его ``purpose``. Стоило удалить spec — тест падал с
        ``AssertionError`` не про то, что проверял. Теперь красным
        становится только реальное расхождение «spec есть, purpose в
        рендер не попал».
        """
        from lib.services.runtime_patcher import PatchReport, RuntimePatcher

        specs = RuntimePatcher.patch_specs()
        assert specs, "patch_specs() пуст — тест ничего бы не проверял"

        for name, spec in specs.items():
            report = PatchReport()
            report.failed.append((name, "import failed: boom"))
            rendered = report.render(specs=specs)
            assert f"✗ {name} failed: import failed: boom" in rendered
            # Первая строка purpose обязана быть в выводе.
            purpose_head = spec.purpose.strip().splitlines()[0].strip()
            assert purpose_head, f"{name}: пустой purpose"
            assert purpose_head in rendered, (
                f"{name}: purpose {purpose_head!r} не попал в рендер отчёта"
            )


class TestPatchSpecs:
    """Каждый патч из ``apply_all`` должен иметь ``PatchSpec``,
    три множества (apply_all AST / _PATCH_SPECS / canonical) —
    попарно равны.

    Количество патчей намеренно НЕ выписано числом: после переезда
    части патчей на нативные точки расширения (change
    ``enterprise-mcp-platform``, фаза 6) любое число в тесте требовало
    бы правки без содержательной причины. Вместо числа проверяются
    правила — см. ``test_inventory_is_exact`` и
    ``test_every_applied_patch_has_an_implementation``.
    """

    def _extract_apply_all_names(self) -> set[str]:
        """AST-извлечение имён patches из тела ``RuntimePatcher.apply_all``.

        Берём все строки второго позиционного аргумента
        ``self._record(report, "<name>", ...)``. Это даёт фактический
        набор имён, которые ``apply_all`` пишет в ``PatchReport``.
        """
        import ast
        import inspect
        import textwrap
        from lib.services.runtime_patcher import RuntimePatcher

        source = textwrap.dedent(inspect.getsource(RuntimePatcher.apply_all))
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Цель — атрибут ``self._record``
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "_record":
                continue
            if len(node.args) < 2:
                continue
            second = node.args[1]
            if not isinstance(second, ast.Constant) or not isinstance(second.value, str):
                continue
            names.add(second.value)
        return names

    def test_inventory_is_exact(self):
        """Main invariant: три множества попарно равны."""
        from lib.services.runtime_patcher import RuntimePatcher
        from lib.services.runtime_inventory import canonical_runtime_patches

        apply_all_names = self._extract_apply_all_names()
        patch_specs_names = set(RuntimePatcher.patch_specs())
        canonical_names = {p.name for p in canonical_runtime_patches()}

        assert apply_all_names == patch_specs_names, (
            f"apply_all != _PATCH_SPECS: "
            f"only in apply_all={apply_all_names - patch_specs_names}, "
            f"only in _PATCH_SPECS={patch_specs_names - apply_all_names}",
        )
        assert apply_all_names == canonical_names, (
            f"apply_all != canonical: "
            f"only in apply_all={apply_all_names - canonical_names}, "
            f"only in canonical={canonical_names - apply_all_names}",
        )
        assert patch_specs_names == canonical_names, (
            f"_PATCH_SPECS != canonical: "
            f"only in _PATCH_SPECS={patch_specs_names - canonical_names}, "
            f"only in canonical={canonical_names - patch_specs_names}",
        )

    def test_every_applied_patch_has_an_implementation(self):
        """Правило вместо числа: у каждого патча из ``apply_all`` есть
        и зарегистрированный метод ``patch_<name>``, и ``PatchSpec``.

        Заменяет прежний ``test_inventory_size_is_12``: тот фиксировал
        количество (12), из-за чего переезд патчей на нативные точки
        расширения ломал его без содержательной причины, а любое
        легитимное добавление патча требовало правки числа. Теперь
        красным становится только реальное расхождение «объявлено в
        отчёте, но не реализовано» (и наоборот).
        """
        apply_all_names = self._extract_apply_all_names()
        assert apply_all_names, "apply_all не регистрирует ни одного патча"

        for name in sorted(apply_all_names):
            method = getattr(RuntimePatcher, f"patch_{name}", None)
            assert method is not None, (
                f"apply_all регистрирует {name!r}, но метода "
                f"patch_{name} у RuntimePatcher нет"
            )
            assert callable(method), f"patch_{name} не callable"

        for name in sorted(RuntimePatcher.patch_specs()):
            assert hasattr(RuntimePatcher, f"patch_{name}"), (
                f"spec {name!r} объявлен, но метода patch_{name} нет"
            )

        # Ожидание не выписывается числом: канонический инвентарь обязан
        # совпадать с фактическим apply_all (детальнее —
        # test_inventory_is_exact), а количество — производная от них.
        from lib.services.runtime_inventory import canonical_runtime_patches

        assert len(canonical_runtime_patches()) == len(apply_all_names)

    def test_no_patch_is_gated_behind_debug_only_flag(self):
        """Ни один патч не гейтится debug-флагом ``runtime_diagnostics``.

        ``patch_session_dir_watch`` был временной диагностикой и удалён
        вместе с её гейтом. Проверка на заведомо плохих данных: верни в
        ``PatchSpec.reason`` упоминание ``runtime_diagnostics`` — тест
        упадёт, и временная диагностика не проскочит в прод-инвентарь.
        """
        for name, spec in RuntimePatcher.patch_specs().items():
            haystack = " ".join(
                (spec.purpose, spec.reason, spec.nanobot_target, spec.alternatives_checked)
            )
            assert "runtime_diagnostics" not in haystack, (
                f"{name}: патч гейтится debug-флагом runtime_diagnostics — "
                f"такие патчи удаляются, а не регистрируются"
            )

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
        """Правило: ``details`` содержит запись для КАЖДОГО патча,
        который ``apply_all`` регистрирует, и ни одного лишнего.

        Раньше тест перечислял имена патчей (``document_text_threshold``,
        ``save_turn``, ``subagent_logging``) — после переезда части
        патчей на нативные точки расширения список устарел и тест
        падал, не сообщая о причине. Теперь сверка идёт с фактическим
        ``apply_all``, поэтому переезд патчей её не ломает, а реальное
        расхождение (заявлен, но не зарегистрирован / зарегистрирован
        дважды) — ловит.
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
        d = report.to_dict()
        assert "details" in d

        expected = TestPatchSpecs()._extract_apply_all_names()
        assert set(d["details"]) == expected, (
            f"details != apply_all: "
            f"only in details={sorted(set(d['details']) - expected)}, "
            f"only in apply_all={sorted(expected - set(d['details']))}"
        )
        assert all(
            isinstance(v, str) and v for v in d["details"].values()
        ), "у каждого патча должна быть непустая detail-строка"

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
            "Я не справился с вашим вопросом. "
            "Попробуйте, пожалуйста, переформулировать конкретнее — "
            "например, уточните ключевую часть или приведите пример."
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
