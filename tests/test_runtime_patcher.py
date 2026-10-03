from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.services.runtime_patcher import RuntimePatcher


def _settings(gateway_overrides=None, channels=None, **overrides):
    # persist_* больше не читает ни один патч: персист результатов
    # tool'ов уехал в upstream (change ``use-upstream-tool-result-persist``),
    # порог — ``agents.defaults.max_tool_result_chars``.
    gw: dict = {}
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


class TestContextGovernorPatchIsGone:
    """Патч `context_governor` снят в пользу upstream.

    В nanobot 0.3.5 `ContextGovernor.normalize_tool_result`
    (`nanobot/agent/context_governance.py:709-759`) делает построчно то же,
    что делал патч: `ensure_nonempty_tool_result` -> исключение для
    `read_file` -> `maybe_persist_tool_result` с публично настраиваемым
    порогом `agents.defaults.max_tool_result_chars`.

    Держать оба было нельзя не только из-за дублирования: у патча и у
    upstream РАЗНЫЕ маркеры результата, поэтому наш дедуп-гейт видел в
    upstream-записи «свежий» результат и персистил его повторно, в третий
    каталог. Тесты живут на месте патча намеренно: его возвращение снова
    сделает один результат тройным.
    """

    def test_patch_method_removed(self):
        assert not hasattr(RuntimePatcher, "patch_context_governor"), (
            "patch_context_governor вернулся; он дублирует upstream "
            "maybe_persist_tool_result и пишет второй раз"
        )

    def test_patch_absent_from_specs(self):
        from lib.services.runtime_patcher import _PATCH_SPECS

        assert "context_governor" not in _PATCH_SPECS, (
            "context_governor остался в _PATCH_SPECS — патч снова будет "
            "применяться при каждом старте"
        )

    def test_patch_not_applied_by_apply_all(self):
        applied = []
        patcher = RuntimePatcher()
        patcher._record = lambda report, name, outcome: applied.append(name)
        patcher.patch_exec_timeout_cap = lambda *a, **k: (True, "ok")
        patcher.patch_assemble_outbound = lambda *a, **k: (True, "ok")
        patcher.patch_subagent_logging = lambda *a, **k: (True, "ok")
        patcher.patch_repeat_guard_block = lambda *a, **k: (True, "ok")

        patcher.apply_all(
            MagicMock(), _settings(), Path("ws"),
            agent=MagicMock(), tool_audit_hook=MagicMock(),
        )

        assert "context_governor" not in applied, applied

    def test_dead_skip_reason_removed(self):
        from lib.services.runtime_patcher import _SKIPPABLE_REASONS

        assert "persist_threshold <= 0" not in _SKIPPABLE_REASONS, (
            "мёртвая запись про persist_threshold осталась в _SKIPPABLE_REASONS "
            "после сноса патча, который её выдавал"
        )

    def test_no_data_store_write_in_patcher(self):
        """Patcher больше не персистит результаты tool'ов в data_store/.

        Проверяем поимённо: ни ``SessionFileStore``, ни ``prepare_content``
        (пара, которой патч писал результат на диск) в модуле не осталось.
        Проверять «нет вызова ``.save``» нельзя — патчер зовёт
        ``agent.save``/``session.save`` в других местах, и проверка была бы
        всегда-зелёной (ложно-отрицательный страж).
        """
        source = (
            Path(__file__).resolve().parents[1]
            / "lib/services/runtime_patcher.py"
        ).read_text(encoding="utf-8")
        for name in ("SessionFileStore", "prepare_content", "data_store/"):
            assert name not in source, (
                f"runtime_patcher всё ещё упоминает {name}: персист "
                "результатов tool'ов уехал в upstream maybe_persist_tool_result"
            )

    def test_upstream_still_persists(self):
        """Снос патча не оставил дыры: персист делает библиотека."""
        import inspect

        from nanobot.agent.context_governance import ContextGovernor

        src = inspect.getsource(ContextGovernor.normalize_tool_result)
        assert "maybe_persist_tool_result" in src
        assert "ensure_nonempty_tool_result" in src
        assert "TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS" in src


class TestToolLimitPatchesAreGone:
    """``exec_limits`` и ``tool_limits`` сняты решением владельца.

    Оба патча поднимали потолки вывода инструментов, которых в nanobot 0.3.5
    нет в конфигурации, поэтому нативной замены нет by design: лимиты
    вернулись к дефолтам библиотеки. Тесты живут на месте патчей намеренно —
    их возвращение снова сделает потолки ненастраиваемыми, и без явного
    решения владельца это будет молчаливая регрессия.

    Вторая половина класса фиксирует САМИ действующие лимиты. После сноса они
    больше не перекрываются конфигом, поэтому единственная защита от их тихой
    смены апгрейдом nanobot — этот тест.
    """

    def test_patch_methods_removed(self):
        assert not hasattr(RuntimePatcher, "patch_exec_limits"), (
            "patch_exec_limits вернулся; потолки вывода exec снова станут "
            "ненастраиваемыми без записи владельца в каталог патчей"
        )
        assert not hasattr(RuntimePatcher, "patch_tool_limits"), (
            "patch_tool_limits вернулся; grep снова перестанет видеть файлы "
            "крупнее 2 МБ без записи владельца в каталог патчей"
        )

    def test_patches_absent_from_specs(self):
        from lib.services.runtime_patcher import _PATCH_SPECS

        for name in ("exec_limits", "tool_limits"):
            assert name not in _PATCH_SPECS, (
                f"{name} остался в _PATCH_SPECS — патч снова будет "
                "применяться при каждом старте"
            )

    def test_not_applied_by_apply_all(self):
        applied = []
        patcher = RuntimePatcher()
        patcher._record = lambda report, name, outcome: applied.append(name)
        patcher.patch_exec_timeout_cap = lambda *a, **k: (True, "ok")
        patcher.patch_assemble_outbound = lambda *a, **k: (True, "ok")
        patcher.patch_subagent_logging = lambda *a, **k: (True, "ok")
        patcher.patch_repeat_guard_block = lambda *a, **k: (True, "ok")

        patcher.apply_all(
            MagicMock(), _settings(), Path("ws"),
            agent=MagicMock(), tool_audit_hook=MagicMock(),
        )

        assert "exec_limits" not in applied, applied
        assert "tool_limits" not in applied, applied

    def test_dead_skip_reasons_removed(self):
        from lib.services.runtime_patcher import _SKIPPABLE_REASONS

        for reason in (
            "exec_max_output_chars <= 0",
            "read_file_max_chars <= 0",
            "exec_session/shell module not loaded",
            "filesystem/search module not loaded",
        ):
            assert reason not in _SKIPPABLE_REASONS, (
                f"мёртвая запись {reason!r} осталась в _SKIPPABLE_REASONS "
                "после сноса патча, который её выдавал"
            )

    def test_config_section_not_read_anymore(self):
        """``gateway.tool_result_limits`` больше не читает ни один патч.

        Секция удалена из ``config.json``; если её вернуть в конфиг без
        патчей, она станет молчаливо игнорируемой, а оператор будет
        уверен, что потолки настроены.
        """
        source = (
            Path(__file__).resolve().parents[1]
            / "lib/services/runtime_patcher.py"
        ).read_text(encoding="utf-8")
        assert '"tool_result_limits"' not in source, (
            "runtime_patcher всё ещё читает tool_result_limits: секция "
            "конфига должна быть удалена вместе с патчами, иначе настройка "
            "будет выглядеть действующей, не будучи ею"
        )

    def test_framework_limits_are_the_ones_we_think(self):
        """Фиксируем лимиты, которые теперь действуют на живом пакете.

        Снимает патчей они больше не перекрываются, поэтому любое их
        изменение апгрейдом nanobot должно ломать этот тест, а не
        проявляться как тихая потеря данных.
        """
        from nanobot.agent.tools import exec_session, filesystem, search, shell

        assert exec_session.MAX_OUTPUT_CHARS == 50_000, (
            "потолок вывода exec-session изменился: в каталоге патчей и "
            "TROUBLESHOOTING надо обновить числа"
        )
        assert shell.ExecTool._MAX_OUTPUT == 10_000, (
            "дефолт вывода exec изменился: 10K — рабочее значение, а не 50K; "
            "в каталоге патчей это уже было зафиксировано как 50K"
        )
        assert filesystem.ReadFileTool._MAX_CHARS == 128_000
        assert filesystem.ListDirTool._DEFAULT_MAX == 200
        assert search._DEFAULT_HEAD_LIMIT == 250
        assert search._DEFAULT_FILE_HEAD_LIMIT == 200
        assert search.GrepTool._MAX_FILE_BYTES == 2_000_000, (
            "grep перестал искать по файлам крупнее 2 МБ: это молчаливое "
            "ложное отрицание, а не ошибка — повышать лимит патчем было "
            "осознанно"
        )


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
                              if c.args[0].event_type == "agent.completed"]
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
                         if c.args[0].event_type == "agent.completed"]
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
            MagicMock(), _settings(), Path("ws"), agent, hook,
            db_logging_service=None,
        )
        d = report.to_dict()
        assert "assemble_outbound" in d["applied"]
        # context_bridge_seed удалён в nanobot 0.3.5; seed лимита
        # делает RuntimeEventsSubscriber через bus.subscribe.
        assert "context_bridge_seed" not in d["applied"]
        assert "context_bridge_seed" not in d.get("skipped", [])
        # context_governor больше не патч: выгрузка результатов делает
        # сам upstream (nanobot 0.3.5, change use-upstream-tool-result-persist).
        assert "context_governor" not in d.get("skipped", [])
        assert "context_governor" not in d["applied"]
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
    # Fallback на internal-ошибку ушёл на публичный параметр
    # ``AgentLoop(turn_delivery_factory=...)``: настраиваемый текст ошибки
    # рождается в ``TurnDelivery.fail``, а не в ответе модели, поэтому хук
    # ``finalize_content`` его не видит (ADR turn-delivery-public-extension).
    "patch_turn_delivery_fail": (
        "lib/services/turn_delivery_factory.py",
        "FallbackTurnDeliveryFactory",
    ),
    # Временный диагностический патч (гейт ``runtime_diagnostics.
    # session_dir_watch``, по умолчанию выключен, поведение save не менял).
    # Нативной замены нет и не должно быть: фича была нужна только для
    # расследования одной ошибки и удалена вместе с её причиной.
    "patch_session_dir_watch": None,
    # Потолки вывода инструментов — сняты решением владельца (2026-10-03).
    # Нативной замены нет by design: в nanobot 0.3.5 у exec / read_file /
    # list_dir / grep НЕТ конфигурируемых лимитов, потолки живут в модульных
    # константах и атрибутах классов. Снос вернул дефолты библиотеки —
    # exec 10 000 (дефолт) / 50 000 (потолок), read_file 128 000,
    # list_dir 200, grep 250/200 и 2 МБ на файл — и удалил секцию
    # ``gateway.tool_result_limits`` из config.json как мёртвую.
    # Возврат потолков возможен только новым патчем или апгрейдом nanobot.
    "patch_exec_limits": None,
    "patch_tool_limits": None,
}

#: Удалённые патчи, у которых нативной замены нет by design.
NO_NATIVE_REPLACEMENT: frozenset[str] = frozenset({
    "patch_session_dir_watch",
    "patch_exec_limits",
    "patch_tool_limits",
})

#: Удалённые патчи, механизм которых живёт В САМОЙ БИБЛИОТЕКЕ.
#:
#: Отдельная категория, а не ``REMOVED_PATCHES``: замена лежит не в нашем
#: дереве, поэтому проверять её надо импортом настоящего nanobot (иначе
#: тест на «замена есть» проходил бы на файле, которого нет). Смысл
#: разделения — не дать этим патчам попасть в ``NO_NATIVE_REPLACEMENT``
#: («замены нет и не надо»): замена есть, просто не наша.
UPSTREAM_REPLACED_PATCHES: dict[str, tuple[str, str]] = {
    # change ``use-upstream-tool-result-persist`` (nanobot 0.3.5):
    # ContextGovernor.normalize_tool_result вызывает
    # maybe_persist_tool_result с порогом agents.defaults.max_tool_result_chars.
    "patch_context_governor": (
        "nanobot.agent.context_governance",
        "ContextGovernor.normalize_tool_result",
    ),
    # Тот же change: ``save_turn`` сначала переехал в локальный
    # ``ToolResultArchiveHook``, но и он оказался лишним — хук ловил
    # результат в момент возврата tool'а и персистил его ВТОРЫМ путём
    # (своим маркером), поверх того же upstream-механизма.
    "patch_save_turn": (
        "nanobot.agent.context_governance",
        "ContextGovernor.normalize_tool_result",
    ),
}


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
            # Патч с заменой В БИБЛИОТЕКЕ не должен попадать в локальную
            # таблицу: у него нет файла-замены в нашем дереве.
            assert method_name not in UPSTREAM_REPLACED_PATCHES, (
                f"{method_name} объявлен и локальной, и upstream-заменой"
            )

    def test_upstream_replacements_still_exist_in_library(self):
        """Механизм из ``UPSTREAM_REPLACED_PATCHES`` живёт в nanobot 0.3.5.

        Проверяется импортом НАСТОЯЩЕГО пакета, а не чтением нашего файла:
        после сноса патча осталась ровно одна реализация — библиотечная.
        Если nanobot переедут на версию, где персиста нет, тест упадёт
        на импорте, и дыра станет видна сразу, а не как «молча пропавший»
        архив больших результатов.
        """
        import importlib

        for method_name, (module_path, symbol) in UPSTREAM_REPLACED_PATCHES.items():
            assert method_name not in REMOVED_PATCHES, (
                f"{method_name} объявлен и локальной, и upstream-заменой"
            )
            assert method_name not in NO_NATIVE_REPLACEMENT, (
                f"{method_name}: замена есть (в библиотеке), помечать «by design» "
                "нельзя — механизм есть, он просто не наш"
            )
            module = importlib.import_module(module_path)
            obj: object = module
            for part in symbol.split("."):
                assert hasattr(obj, part), (
                    f"{method_name}: в {module_path} нет {symbol} — "
                    "upstream-механизм исчез, верни нативную замену"
                )
                obj = getattr(obj, part)
            assert callable(obj), (
                f"{method_name}: {symbol} не вызываема"
            )

    def test_save_turn_replacement_covers_both_cases(self):
        """Персист результатов tool'ов полностью принадлежит upstream.

        Раньше здесь жил тест на ``ToolResultArchiveHook``: хук архивировал
        результат, но НЕ подменял контент (``after_execute_tool`` не может
        изменить то, что уйдёт в историю). Теперь оба пути схлопнуты в
        ``ContextGovernor.normalize_tool_result``, который и чинит пустые
        результаты, и заменяет длинные ссылкой — то есть ограничение
        «архивируем, но не подменяем» устранено в самом месте записи.
        """
        import inspect

        from nanobot.agent.context_governance import ContextGovernor

        src = inspect.getsource(ContextGovernor.normalize_tool_result)
        # Пустой результат чинится до персиста...
        assert "ensure_nonempty_tool_result" in src
        # ...а длинный — заменяется ссылкой (значит, в историю идёт ссылка).
        assert "maybe_persist_tool_result" in src


class TestPatchReportClassification:
    """PatchReport должен различать skipped (конфигуративный) и failed
    (реальный сбой) — TARGET §26.
    """

    def test_skippable_reason_goes_to_skipped(self):
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(report, "exec_timeout_cap", (False, "exec_timeout_cap_sec <= 0"))
        assert report.skipped == [("exec_timeout_cap", "exec_timeout_cap_sec <= 0")]
        assert report.failed == []

    def test_dead_skip_reason_is_a_real_failure(self):
        """Причину, которой больше нет в ``_SKIPPABLE_REASONS``, нельзя
        выдать за «осознанный скип».

        Раньше ``persist_threshold <= 0`` был живым скипом патча
        ``context_governor``. Патч снят, причина вычищена — и если бы
        патчер снова выдал её, это был бы реальный сбой (неизвестная
        причина), а не штатное выключение фичи.
        """
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report, "assemble_outbound", (False, "persist_threshold <= 0"),
        )
        assert report.skipped == []
        assert report.failed == [("assemble_outbound", "persist_threshold <= 0")]

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
            report, "exec_timeout_cap", (False, "shell module not loaded"),
        )
        assert report.skipped == [("exec_timeout_cap", "shell module not loaded")]
        assert report.failed == []

    def test_internal_failed_marker_reclassifies(self):
        """``[INTERNAL_FAILED]`` маркер → failed (не applied, не skipped)."""
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report,
            "subagent_logging",
            (True, "[INTERNAL_FAILED] 3 turns saved: foo; 1 failed: Bar"),
        )
        assert report.failed == [
            (
                "subagent_logging",
                "[INTERNAL_FAILED] 3 turns saved: foo; 1 failed: Bar",
            ),
        ]
        assert report.skipped == []
        assert "subagent_logging" not in report.applied

    def test_details_recorded_for_every_state(self):
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        RuntimePatcher._record(
            report, "document_text_threshold",
            (True, "reference_non_image_attachments patched"),
        )
        RuntimePatcher._record(report, "exec_limits", (False, "agent is None"))
        RuntimePatcher._record(
            report, "assemble_outbound", (False, "import failed: boom"),
        )
        assert (
            report.details["document_text_threshold"]
            == "reference_non_image_attachments patched"
        )
        assert report.details["exec_limits"] == "agent is None"
        assert (
            report.details["assemble_outbound"] == "import failed: boom"
        )


class TestPatchReportRender:
    """``PatchReport.render`` — формат для startup-логов."""

    def test_render_marks_applied_skipped_failed(self):
        from lib.services.runtime_patcher import PatchReport

        report = PatchReport()
        report.applied.append("assemble_outbound")
        report.skipped.append(("exec_timeout_cap", "exec_timeout_cap_sec <= 0"))
        report.failed.append(("subagent_logging", "import failed: boom"))
        rendered = report.render()
        assert "✓ assemble_outbound" in rendered
        assert "⚠ exec_timeout_cap skipped: exec_timeout_cap_sec <= 0" in rendered
        assert "✗ subagent_logging failed: import failed: boom" in rendered

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
            MagicMock(), _settings(), Path("ws"), agent, hook,
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
            MagicMock(), _settings(), Path("ws"), agent, hook,
            db_logging_service=None,
        )
        assert report.failed == [], f"unexpected failures: {report.failed}"


class TestRepeatGuardBlock:
    """Юнит-контур седьмого патча ``repeat_guard_block``.

    Содержательный контур (блокировка превращается в синтетический
    tool-результат, соседние вызовы батча не отменяются, чужая ошибка
    не маскируется) живёт в
    ``tests/contract/test_repeat_guard_hook_contract.py`` — на настоящем
    upstream. Здесь проверяется управляющая часть: идемпотентность и
    отказ при несовместимом API, потому что это единственное, что
    юнит-контур может проверить без подмены фреймворка.

    Отдельная фикстура обязательна: соседние тесты зовут ``apply_all()``,
    который патчит ``_execute_tool_call`` в модуле **глобально** и не
    откатывает его. Без отката порядок прогон�� менял бы исходную точку
    отсчёта, и «патч подменил функцию» превратилось бы в ложное
    падение.
    """

    @pytest.fixture(autouse=True)
    def _isolate_module_state(self):
        from nanobot.agent.tools import execution as exec_mod

        saved = exec_mod._execute_tool_call
        yield
        exec_mod._execute_tool_call = saved

    def test_repeat_guard_block_applies(self):
        from nanobot.agent.tools import execution as exec_mod

        original = exec_mod._execute_tool_call
        ok, message = RuntimePatcher().patch_repeat_guard_block()
        assert ok, message
        if not getattr(original, "_repeat_guard_patched", False):
            # Чистая база: патч обязан реально подменить функцию.
            assert exec_mod._execute_tool_call is not original

    def test_repeat_guard_block_is_idempotent(self):
        """Повторный вызов в том же процессе не обязан наматывать
        функцию дважды: без флага второй выход в ``except`` сделал бы
        счётчик вызовов неверным."""
        from nanobot.agent.tools import execution as exec_mod

        patcher = RuntimePatcher()
        ok1, _ = patcher.patch_repeat_guard_block()
        first = exec_mod._execute_tool_call
        ok2, message2 = patcher.patch_repeat_guard_block()
        assert ok1 and ok2
        assert "already patched" in message2, message2
        assert exec_mod._execute_tool_call is first

    def test_repeat_guard_block_reports_missing_target(self):
        """Нет функции — внятный ``False``, а не подмена на заглушку.

        Молчаливый no-op означал бы, что режим ``block`` включён, а
        защиты нет; при отказе оператор хотя бы видит причину.
        """
        from nanobot.agent.tools import execution as exec_mod

        del exec_mod._execute_tool_call
        ok, message = RuntimePatcher().patch_repeat_guard_block()
        assert ok is False
        assert "_execute_tool_call is missing" in message, message


# Контракт fallback-а на internal-ошибку переехал вместе с патчем:
# ``tests/test_turn_delivery_factory.py`` (нативная фабрика вместо
# monkey-patch'а). Здесь тестировать нечего: метода
# ``patch_turn_delivery_fail`` больше нет, а ``REMOVED_PATCHES``
# запрещает его возврат.
