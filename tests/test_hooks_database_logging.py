from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import runtime_table

# db_logging_bus импортирует utils.media (workspace на sys.path).
_workspace_path = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


@pytest.fixture
def sys_path():
    import sys
    from pathlib import Path
    p = str(Path(__file__).resolve().parent.parent)
    if p not in sys.path:
        sys.path.insert(0, p)
    return p


class TestDatabaseLoggingHook:
    def test_before_execute_tool(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        tool_call = MagicMock()
        tool_call.id = "tc1"
        tool_call.name = "read"
        tool = MagicMock()
        params = {"path": "x"}

        asyncio.run(hook.before_execute_tool(ctx, tool_call, tool, params))
        service.log_tool_call.assert_called_once()
        kwargs = service.log_tool_call.call_args.kwargs
        assert kwargs["session_id"] == "cli:1"
        assert kwargs["tool_name"] == "read"
        assert kwargs["args"] == {"path": "x"}
        assert kwargs["tool_call_id"] == "tc1"
        assert kwargs["request_id"] == "m1"

    def test_after_execute_tool_records_latency(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        tool_call = MagicMock()
        tool_call.id = "tc2"
        tool_call.name = "write"
        tool = MagicMock()
        params = {}
        # Seed tool start time
        hook._tool_start_times["tc2"] = 0.0

        with __import__("unittest.mock").mock.patch(
            "lib.hooks.database_logging_hook.time.time", return_value=0.1
        ):
            asyncio.run(hook.after_execute_tool(ctx, tool_call, tool, params, "ok"))

        service.log_tool_result.assert_called_once()
        kwargs = service.log_tool_result.call_args.kwargs
        assert kwargs["latency_ms"] == pytest.approx(100.0)
        assert kwargs["status"] == "ok"

    def test_on_execute_tool_error_defers_to_the_audit_hook(self, sys_path):
        """Отказ пишет ``ToolAuditHook``, и этот хук не пишет его второй раз.

        Писателей должно быть ровно два на весь отказ: платформа пишет свой
        (``metadata.source=enterprise_mcp``), агент — свой
        (``metadata.source=nanobot``). Третий, выглядящий как «подстраховка»,
        дал бы две строки ``tool.failed`` на один отказ, а журнал читают как
        одну запись на событие. Владелец — ``ToolAuditHook``, потому что он
        есть в ``ctx.hooks`` безусловно и переживает оборот, а этот хук — нет.

        Проверяется и то, что запись времени старта снята: иначе на каждый
        отказ оставалась бы запись в ``_tool_start_times``.
        """
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        tool_call = MagicMock()
        tool_call.id = "tc3"
        tool_call.name = "exec"
        tool = MagicMock()
        params = {}

        hook._tool_start_times["tc3"] = 1.0
        asyncio.run(
            hook.on_execute_tool_error(ctx, tool_call, tool, params, RuntimeError("boom"))
        )
        service.log_tool_result.assert_not_called()
        assert "tc3" not in hook._tool_start_times

    def test_after_run_emits_event(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.final_content = "hello"
        ctx.tools_used = ["read", "write"]
        ctx.stop_reason = "stop"
        ctx.had_injections = False
        ctx.error = None
        ctx.usage = {"total_tokens": 123}

        asyncio.run(hook.before_iteration(MagicMock(session_key="cli:1")))
        asyncio.run(hook.after_run(ctx))
        # after_run пишет ДВА события: agent.responded (текст ответа) и
        # agent.completed (исход оборота с длительностью). Раньше исхода в
        # журнале не было вовсе, и «успешен ли оборот и сколько занял»
        # приходилось выводить вручную. Проверяем нужное событие по типу,
        # а не «последний вызов».
        events = [c.args[0] for c in service.log_event.call_args_list]
        assert [e.event_type for e in events] == [
            "llm.requested", "agent.responded", "agent.completed",
        ]
        event = next(e for e in events if e.event_type == "agent.responded")
        assert event.event_type == "agent.responded"
        assert event.summary == "hello"
        assert event.payload["tools_used"] == ["read", "write"]
        assert event.session_id == "cli:1"
        assert event.request_id == "m1"
        assert event.payload["request_id"] == "m1"
        service.finish_request.assert_called_once_with(
            "m1", status="finished", summary="hello", response="hello"
        )
        service.clear_request.assert_called_once_with("cli:1")

    def test_after_iteration_emits_llm_call(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from nanobot.providers.base import LLMResponse, ToolCallRequest

        service = MagicMock()
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        ctx.iteration = 1
        ctx.messages = [{"role": "user", "content": "привет"}]
        ctx.usage = {"total_tokens": 7}
        ctx.response = LLMResponse(
            content="ответ",
            finish_reason="stop",
            usage={"output_tokens": 3},
            tool_calls=[ToolCallRequest(id="t1", name="read", arguments={})],
        )

        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.after_iteration(ctx))
        service.log_llm_call.assert_called_once()
        kwargs = service.log_llm_call.call_args.kwargs
        assert kwargs["session_id"] == "cli:1"
        assert kwargs["prompt"] == [{"role": "user", "content": "привет"}]
        assert kwargs["iteration"] == 1
        assert kwargs["finish_reason"] == "stop"
        assert kwargs["usage"] == {"total_tokens": 7}
        resp = kwargs["response"]
        assert resp["content"] == "ответ"
        assert resp["finish_reason"] == "stop"
        assert resp["tool_calls"][0]["name"] == "read"

    def test_after_iteration_no_response_skips(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        ctx.response = None
        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.after_iteration(ctx))
        service.log_llm_call.assert_not_called()

    def test_after_iteration_prints_llm_tokens(self, sys_path, monkeypatch):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from nanobot.providers.base import LLMResponse

        printed: list[str] = []
        monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
        service = MagicMock()
        hook = DatabaseLoggingHook(service, print_llm_calls=True)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        ctx.usage = {"prompt_tokens": 120, "completion_tokens": 45}
        ctx.response = LLMResponse(content="ответ", finish_reason="stop")

        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.after_iteration(ctx))
        assert any("отправлен промпт (120 токенов)" in p for p in printed)
        assert any("получен ответ (45 токенов)" in p for p in printed)

    def test_after_iteration_does_not_print_when_disabled(self, sys_path, monkeypatch):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from nanobot.providers.base import LLMResponse

        printed: list[str] = []
        monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
        service = MagicMock()
        hook = DatabaseLoggingHook(service, print_llm_calls=False)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        ctx.usage = {"prompt_tokens": 100, "completion_tokens": 50}
        ctx.response = LLMResponse(content="ответ", finish_reason="stop")

        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.after_iteration(ctx))
        assert not any("отправлен промпт" in p or "получен ответ" in p for p in printed)

    def test_before_execute_tool_captures_session_key(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        tool_call = MagicMock()
        tool_call.id = "tc1"
        tool_call.name = "read"
        tool = MagicMock()
        asyncio.run(hook.before_execute_tool(ctx, tool_call, tool, {}))
        assert hook._run_session_key == "cli:1"
        assert hook._request_id == "m1"


class TestDatabaseLoggingHookFactory:
    def _factory(self, service):
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        return make_db_logging_hook_factory(service, agent_id="agent-9")

    def _turn(self, session_key):
        from types import SimpleNamespace

        return SimpleNamespace(session_key=session_key)

    def test_creates_fresh_instance_per_turn(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.side_effect = lambda sk: {"cli:1": "m1", "cli:2": "m2"}.get(sk)
        factory = self._factory(service)

        hook_a = factory(self._turn("cli:1"))
        hook_b = factory(self._turn("cli:2"))

        assert isinstance(hook_a, DatabaseLoggingHook)
        assert hook_a is not hook_b
        assert hook_a._request_id == "m1"
        assert hook_b._request_id == "m2"
        assert hook_a._run_session_key == "cli:1"
        assert hook_b._run_session_key == "cli:2"
        assert hook_a._agent_id == "agent-9"

    def test_instance_without_session_key_has_no_context(self, sys_path):
        service = MagicMock()
        service.get_request_id.return_value = None
        factory = self._factory(service)
        hook = factory(self._turn(None))
        assert hook._run_session_key is None
        assert hook._request_id is None

    def test_factory_passes_print_llm_calls(self, sys_path):
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        service = MagicMock()
        factory = make_db_logging_hook_factory(
            service, agent_id="agent-9", print_llm_calls=True
        )
        assert factory(self._turn("cli:1"))._print_llm_calls is True
        default = make_db_logging_hook_factory(service, agent_id="agent-9")
        assert default(self._turn("cli:1"))._print_llm_calls is False

    def test_concurrent_sessions_do_not_mix_request_id(self, sys_path):
        """Регрессия: после т.зр. общей shared-инстанса после_execute_tool
        читал self._request_id «по памяти» и мог взять чужой вопрос.

        При per-turn инстансах состояние изолировано — tool_result и
        after_run сессии A не затрагивают сессию B.
        """
        service = MagicMock()
        service.get_request_id.side_effect = lambda sk: {"cli:1": "ridA", "cli:2": "ridB"}.get(sk)
        factory = self._factory(service)

        # Переплетение A↔B: B регистрирует свой контекст (before_execute_tool)
        # ДО того, как A завершает свой tool (после_execute_tool).
        hook_b = factory(self._turn("cli:2"))
        hook_a = factory(self._turn("cli:1"))

        ctx_a = MagicMock()
        ctx_a.session_key = "cli:1"
        tb_a = MagicMock()
        tb_a.id = "tcA"
        tb_a.name = "read"
        tc_b = MagicMock()
        tc_b.id = "tcB"
        tc_b.name = "write"

        asyncio.run(hook_b.before_execute_tool(ctx_a, tc_b, MagicMock(), {}))
        # Теперь B «загрязнил» свой next контекст — но НЕ общий инстанс.
        # A завершает свой вызов: результат должен нести ridA, а не ridB.
        asyncio.run(hook_a.after_execute_tool(ctx_a, tb_a, MagicMock(), {}, "result"))
        service.log_tool_result.assert_called_once()
        kwargs = service.log_tool_result.call_args.kwargs
        assert kwargs["request_id"] == "ridA"

        # after_run A не трогает вопрос B
        run_ctx = MagicMock()
        run_ctx.final_content = "done"
        run_ctx.tools_used = ["read"]
        run_ctx.stop_reason = "stop"
        run_ctx.had_injections = False
        run_ctx.error = None
        run_ctx.usage = {"total_tokens": 1}

        asyncio.run(hook_a.after_run(run_ctx))
        service.finish_request.assert_called_once_with(
            "ridA", status="finished", summary="done", response="done"
        )
        service.clear_request.assert_called_once_with("cli:1")


class TestBusLoggers:
    def test_inbound_logger(self):
        from lib.services.db_logging_bus import make_inbound_logger

        service = MagicMock()
        logger = make_inbound_logger(service)
        msg = MagicMock()
        msg.session_key = "cli:42"
        msg.channel = "cli"
        msg.content = "hi"
        msg.metadata = {"message_id": "m1"}
        msg.sender_id = "u1"
        msg.chat_id = "c1"
        msg.media = []
        asyncio.run(logger(msg))
        service.register_request.assert_called_once_with(
            "cli:42", "m1", user_id="u1", chat_id="c1", channel="cli",
            agent_id=None, question="hi", media=None,
        )
        service.log_inbound.assert_called_once_with(
            session_id="cli:42", channel="cli", content="hi",
            message_id="m1", sender_id="u1", chat_id="c1",
            request_id="m1", media=None,
        )

    def test_inbound_logger_with_media(self):
        from lib.services.db_logging_bus import make_inbound_logger

        service = MagicMock()
        logger = make_inbound_logger(service)
        msg = MagicMock()
        msg.session_key = "cli:42"
        msg.channel = "cli"
        msg.content = "см. файл"
        msg.metadata = {"message_id": "m1"}
        msg.sender_id = "u1"
        msg.chat_id = "c1"
        msg.media = ["doc.pdf", ""]
        asyncio.run(logger(msg))
        kwargs = service.register_request.call_args.kwargs
        assert kwargs["question"] == "см. файл"
        expected = [{
            "filename": "doc.pdf",
            "file_id": "doc.pdf",
            "mime_type": "",
            "file_size": 0,
        }]
        assert kwargs["media"] == expected
        kwargs = service.log_inbound.call_args.kwargs
        assert kwargs["media"] == expected

    def test_outbound_logger_drops_reasoning(self):
        from lib.services.db_logging_bus import make_outbound_logger

        service = MagicMock()
        logger = make_outbound_logger(service)
        msg = MagicMock()
        msg.channel = "cli"
        msg.content = "ignored"
        msg.metadata = {"_reasoning_delta": True}
        asyncio.run(logger(msg))
        service.log_outbound.assert_not_called()

    def test_outbound_logger_final(self):
        from lib.services.db_logging_bus import make_outbound_logger

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        logger = make_outbound_logger(service)
        msg = MagicMock()
        msg.channel = "cli"
        msg.chat_id = "42"
        msg.content = "final answer"
        msg.metadata = {"message_id": "m1", "_final_turn": True}
        msg.media = []
        asyncio.run(logger(msg))
        service.log_outbound.assert_called_once()
        kwargs = service.log_outbound.call_args.kwargs
        # Имя события — литерал внутри ``log_outbound``, а НЕ параметр.
        # Пока оно приходило параметром ``kind``, сравнение жило отдельно
        # от литерала, и переименование в одной строке роняло подпись —
        # финальный ответ оборота исчезал из журнала без ошибки. Регресс
        # на возврат параметра: см. tests/test_final_delivery_is_signed.py
        assert "kind" not in kwargs, (
            "имя исходящего обязано быть литералом в log_outbound; параметр "
            "kind — источник молчаливой потери финального ответа"
        )
        assert kwargs["content"] == "final answer"
        assert kwargs["session_id"] == "cli:42"
        assert kwargs["request_id"] == "m1"
        assert kwargs["media"] is None

    def test_outbound_logger_stream_delta_dropped(self):
        from lib.services.db_logging_bus import make_outbound_logger

        service = MagicMock()
        logger = make_outbound_logger(service)
        from nanobot.bus.outbound_events import StreamDeltaEvent
        msg = MagicMock()
        msg.channel = "cli"
        msg.chat_id = "42"
        msg.content = "токен"
        msg.metadata = {}
        msg.event = StreamDeltaEvent(content="токен", stream_id="s1")
        asyncio.run(logger(msg))
        service.log_outbound.assert_not_called()

    def test_outbound_logger_intermediate_dropped(self):
        """Промежуточное ``message(...)`` агента — НЕ-событие, а не «другое имя».

        Раньше тест ждал записи ``outbound_intermediate``. Канонического имени
        для него в словаре платформы нет (заказчик отнёс промежуточные ответы к
        непокрытым этапам), поэтому запись была бы отказом батча при ``strict``,
        а подстановка чужого имени (``agent.delivered``) была бы ложью: один
        и тот же тип означал бы и финальный ответ, и его черновик.

        Ожидание несуществующего события заменено ожиданием его отсутствия —
        иначе тест проверял бы то, чего в боевом журнале быть не может.
        """
        from lib.services.db_logging_bus import make_outbound_logger

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        logger = make_outbound_logger(service)
        msg = MagicMock()
        msg.channel = "cli"
        msg.chat_id = "42"
        msg.content = "промежуточное сообщение агента"
        msg.metadata = {"message_id": "m1"}
        msg.media = []
        asyncio.run(logger(msg))
        service.log_outbound.assert_not_called()

    def test_outbound_logger_with_media(self):
        from lib.services.db_logging_bus import make_outbound_logger

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        logger = make_outbound_logger(service)
        msg = MagicMock()
        msg.channel = "cli"
        msg.chat_id = "42"
        msg.content = "final answer"
        msg.metadata = {"message_id": "m1", "_final_turn": True}
        msg.media = ["https://example.com/out.png"]
        asyncio.run(logger(msg))
        kwargs = service.log_outbound.call_args.kwargs
        assert kwargs["media"] == [{
            "filename": "",
            "file_id": "https://example.com/out.png",
            "mime_type": "",
            "file_size": 0,
        }]

    def test_inbound_logger_does_not_invent_request_id(self):
        """Входящее без ``message_id`` не получает выдуманный идентификатор.

        По живому замеру этой подстановкой были подписаны 4087 строк журнала
        из 4138 (99,3 %): идентификатора, которого нет и не может быть в
        очереди. Поле вопроса обязано остаться пустым, а личность события
        обеспечивается отдельно (``user_id``) — см.
        ``tests/test_queue_anchor_identity.py``.
        """
        from lib.services.db_logging_bus import make_inbound_logger

        service = MagicMock()
        logger = make_inbound_logger(service)
        msg = MagicMock()
        msg.channel = "cli"
        msg.chat_id = "c1"
        msg.session_key = "cli:c1"
        msg.content = "привет"
        msg.metadata = {}  # канал не положил message_id взятой строки очереди
        msg.sender_id = "u9"
        msg.media = []
        asyncio.run(logger(msg))
        reg = service.register_request.call_args
        assert reg is not None
        assert reg.args[1] is None, (
            "идентификатор вопроса выдуман: у входящего нет message_id "
            "взятой строки очереди, значит и вопроса не было"
        )
        # Регистрация состоялась «вхолостую» — это не отказ, и счётчики у
        # этого состояния и у упавшей регистрации разные.
        service.register_request.return_value = False
        service.log_inbound.assert_called_once()
        assert service.log_inbound.call_args.kwargs["request_id"] is None
        # Личность входа уходит в регистрацию: снимок кладёт сервис.
        assert reg.kwargs["user_id"] == "u9"

    def test_inbound_logger_keeps_queue_message_id(self):
        """Контроль к предыдущему: у сообщения из очереди ``message_id``
        ЕСТЬ, и он без изменений становится идентификатором вопроса."""
        from lib.services.db_logging_bus import make_inbound_logger

        service = MagicMock()
        service.register_request.return_value = True
        logger = make_inbound_logger(service)
        msg = MagicMock()
        msg.channel = "postgres"
        msg.chat_id = "chat-A"
        msg.session_key = "postgres:chat-A"
        msg.content = "вопрос"
        msg.metadata = {"message_id": "m-123"}
        msg.sender_id = "u9"
        msg.media = []
        asyncio.run(logger(msg))
        reg = service.register_request.call_args
        assert reg.args[1] == "m-123"
        assert service.log_inbound.call_args.kwargs["request_id"] == "m-123"

    def test_factory_does_not_invent_request_id_when_missing(self):
        """Фабрика хука НЕ выдумывает ``request_id`` и НЕ регистрирует оборот.

        Раньше она брала индекс, а при ``None`` генерировала UUID и писала
        строку прогона от своего имени. Теперь оборота нет — значит, нет и
        строки ``agent_question_runs``: регистрировать нечего.
        """
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        svc = MagicMock()
        svc.get_request_id.return_value = None
        factory = make_db_logging_hook_factory(svc, agent_id="main")
        turn = MagicMock()
        turn.session_key = "cli:c1"
        hook = factory(turn)
        assert hook._request_id is None, (
            "выдуманный идентификатор занял поле идентификатора вопроса"
        )
        svc.register_request.assert_not_called()

    def test_factory_takes_request_id_from_index(self):
        """Контроль: у оборота с вопросом ``request_id`` берётся из индекса
        и остаётся в инстансе — все события оборота несут его и находят
        строку ``agent_question_runs``."""
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        svc = MagicMock()
        svc.get_request_id.return_value = "m-123"
        factory = make_db_logging_hook_factory(svc, agent_id="main")
        turn = MagicMock()
        turn.session_key = "postgres:chat-A"
        hook = factory(turn)
        assert hook._request_id == "m-123"
        svc.register_request.assert_not_called()

    def test_factory_does_not_register_turn_at_all(self):
        """Регистрация оборота — дело входящего сообщения, не хука.

        Проверка на отсутствие вызова, а не на его аргументы: пока фабрика
        звала ``register_request``, личность входа могла быть продублирована
        ею же, с пустым ``user_id``, и переписать снимок входа.
        """
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        svc = MagicMock()
        svc.get_request_id.return_value = None
        factory = make_db_logging_hook_factory(svc, agent_id="main")
        turn = MagicMock()
        turn.session_key = "telegram:42"
        with patch(
            "lib.hooks.database_logging_hook._current_request_sender_id",
            return_value="alice",
        ):
            factory(turn)
        svc.register_request.assert_not_called()

    def test_factory_ignores_identity_store_for_registration(self):
        """``RequestContext.sender_id`` хук больше не подставляет в оборот.

        Источник истины — входящее сообщение (``sender_id`` оттуда кладёт
        ``db_logging_bus`` в снимок личности входа). Подстановка отсюда
        переписывала бы снимок входа личностью, взятой не из входа.
        """
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        svc = MagicMock()
        svc.get_request_id.return_value = None
        factory = make_db_logging_hook_factory(svc, agent_id="main")
        turn = MagicMock()
        turn.session_key = "telegram:42"
        with patch(
            "lib.hooks.database_logging_hook._current_request_sender_id",
            return_value="alice",
        ):
            hook = factory(turn)
        assert hook._request_id is None
        svc.register_request.assert_not_called()
        # Подмена identity-store не влияет на решение фабрики: вопроса нет.
        with patch(
            "lib.hooks.database_logging_hook._current_request_sender_id",
            return_value=None,
        ):
            assert factory(turn)._request_id is None


class TestRunFinishedEventShape:
    """Регрессионный тест: ``run_finished`` фактически пишется хуком
    ``DatabaseLoggingHook.after_run`` (закрывает баг №3 из proposal:
    «пишется ли ``run_finished`` вообще»).
    """

    def test_after_run_records_run_finished_event_type(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from lib.services.db_logging_service import (
            DbLoggingService,
            LogEvent,
        )

        svc = DbLoggingService(
            dsn="", table_name="x", question_runs_table="y",
        )
        hook = DatabaseLoggingHook(svc)
        ctx = MagicMock()
        ctx.final_content = "hello world"
        ctx.tools_used = ["read", "write"]
        ctx.stop_reason = "stop"
        ctx.had_injections = False
        ctx.error = None
        ctx.usage = {"total_tokens": 123}

        asyncio.run(hook.before_iteration(MagicMock(session_key="cli:1")))
        asyncio.run(hook.after_run(ctx))
        events = [e for e in svc._queue.queue if isinstance(e, LogEvent)]
        assert any(e.event_type == "agent.responded" for e in events), (
            "after_run должен положить LogEvent с event_type='agent.responded'"
        )

    def test_after_run_payload_shape(self, sys_path):
        """Payload ``agent.responded`` содержит ожидаемые поля
        (для ``history_search``-парсинга)."""
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from lib.services.db_logging_service import (
            DbLoggingService,
            LogEvent,
        )

        svc = DbLoggingService(
            dsn="", table_name="x", question_runs_table="y",
        )
        hook = DatabaseLoggingHook(svc)
        ctx = MagicMock()
        ctx.final_content = "ответ"
        ctx.tools_used = ["a", "b"]
        ctx.stop_reason = "stop"
        ctx.had_injections = False
        ctx.error = None
        ctx.usage = {"total_tokens": 7}

        asyncio.run(hook.before_iteration(MagicMock(session_key="cli:1")))
        asyncio.run(hook.after_run(ctx))
        events = [e for e in svc._queue.queue if isinstance(e, LogEvent)]
        run_ev = next(e for e in events if e.event_type == "agent.responded")
        assert run_ev.payload["final_content"] == "ответ"
        assert run_ev.payload["tools_used"] == ["a", "b"]
        assert run_ev.payload["stop_reason"] == "stop"
        assert run_ev.payload["had_injections"] is False

    def test_run_finished_user_id_reaches_insert(self, sys_path):
        """Регрессия на fix-history-search-user-isolation: ``agent.responded``
        доходит до INSERT с ``user_id`` (через автозаполнение из
        индекса в ``_enqueue`` по request_id matching).

        Сценарий: register_request с user_id="alice" → эмиттим
        ``agent.responded`` с тем же request_id → INSERT содержит user_id="alice".
        """
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from lib.services.db_logging_service import (
            DbLoggingService,
            LogEvent,
        )

        svc = DbLoggingService(
            dsn="postgresql://x",
            table_name=runtime_table("gateway_logs"),
            question_runs_table=runtime_table("question_runs"),
        )
        svc.register_request("cli:1", "r1", user_id="alice", chat_id="c1")

        # Эмулируем прямой emit ``agent.responded`` с request_id=r1 и
        # пустым user_id — _enqueue должен подставить "alice" из индекса.
        event = LogEvent(
            event_type="agent.responded",
            session_id="cli:1",
            request_id="r1",
            user_id=None,
        )
        svc._enqueue(event)
        assert event.user_id == "alice"
