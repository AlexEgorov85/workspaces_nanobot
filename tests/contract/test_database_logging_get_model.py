"""Контракт-тесты для ``DatabaseLoggingHook.get_model`` (nanobot 0.3.5+)."""
from __future__ import annotations

import asyncio
import dataclasses
import types

import pytest


pytestmark = pytest.mark.contract


def _make_response() -> object:
    from nanobot.providers.base import LLMResponse

    return LLMResponse(content="ok", tool_calls=[], finish_reason="stop")


def _make_ctx(response: object | None = None, usage: object | None = None) -> object:
    return dataclasses.replace(
        _ctx_template(),
        response=response or _ctx_template().response,
        usage=usage,
    )


def _ctx_template() -> object:
    from nanobot.agent import AgentHookContext

    return AgentHookContext(
        iteration=0,
        messages=[{"role": "user", "content": "hi"}],
        response=None,
        usage=None,
    )


class FakeService:
    def __init__(self) -> None:
        self.llm_calls: list[dict] = []

    def get_request_id(self, k: str) -> str:  # noqa: ARG002
        return "r1"

    def register_request(self, *a, **kw) -> None:  # noqa: ARG002
        pass

    def log_tool_call(self, **kw) -> None:  # noqa: ARG002
        pass

    def log_tool_result(self, **kw) -> None:  # noqa: ARG002
        pass

    def log_event(self, *a, **kw) -> None:  # noqa: ARG002
        pass

    def finish_request(self, *a, **kw) -> None:  # noqa: ARG002
        pass

    def clear_request(self, *a) -> None:  # noqa: ARG002
        pass

    def log_llm_call(self, **kw) -> None:
        self.llm_calls.append(kw)


def _build_hook(get_model=None):
    from lib.hooks.database_logging_hook import DatabaseLoggingHook

    service = FakeService()
    hook = DatabaseLoggingHook(
        service,
        session_key="s",
        request_id="r1",
        get_model=get_model,
    )
    return hook, service


class TestGetModelPlumbing:
    def test_captures_model_when_get_model_returns_value(self):
        async def runner():
            hook, service = _build_hook(get_model=lambda: "MiniMax-M3")
            ctx = _ctx_template()
            ctx.response = _make_response()
            await hook.after_iteration(ctx)
            return service.llm_calls

        captured = asyncio.run(runner())
        assert len(captured) == 1
        assert captured[0]["model"] == "MiniMax-M3"

    def test_none_when_get_model_not_provided(self):
        """Legacy-поведение: ``model=None`` → ``log_llm_call`` name='llm'."""

        async def runner():
            hook, service = _build_hook(get_model=None)
            ctx = _ctx_template()
            ctx.response = _make_response()
            await hook.after_iteration(ctx)
            return service.llm_calls

        captured = asyncio.run(runner())
        assert len(captured) == 1
        assert captured[0]["model"] is None

    def test_fail_soft_when_get_model_raises(self):
        def boom():
            raise RuntimeError("agent disconnected")

        async def runner():
            hook, service = _build_hook(get_model=boom)
            ctx = _ctx_template()
            ctx.response = _make_response()
            await hook.after_iteration(ctx)
            return service.llm_calls

        # Должно НЕ падать — просто записать model=None.
        captured = asyncio.run(runner())
        assert len(captured) == 1
        assert captured[0]["model"] is None

    def test_factory_accepts_get_model(self):
        """make_db_logging_hook_factory примет get_model и пробросит
        его в новый инстанс на КАЖДЫЙ оборот."""
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        service = FakeService()
        service_box = [service]
        counter = {"calls": 0}

        def get_model():
            counter["calls"] += 1
            return "f-MODEL"

        factory = make_db_logging_hook_factory(service, get_model=get_model)

        # Симулируем 3 оборота.
        for _ in range(3):
            service2 = service_box[0]
            service2.llm_calls.clear()
            hook = factory(type("TC", (), {"session_key": "s"})())

            async def go(hook=hook, service=service2):
                ctx = _ctx_template()
                ctx.response = _make_response()
                await hook.after_iteration(ctx)
                return service.llm_calls

            asyncio.run(go())

        # Каждый оборот должен звать get_model и попасть в llm_calls
        assert counter["calls"] == 3

    def test_get_model_called_each_iteration_lazily(self):
        """get_model — lazy: при смене модели на лету логгируется
        актуальное значение на момент записи llm_call, не захардкоженное
        в конструкторе."""
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        holder = {"model": "A"}

        def gm():
            return holder["model"]

        service = FakeService()
        hook = DatabaseLoggingHook(
            service, session_key="s", request_id="r", get_model=gm
        )

        async def run():
            ctx = _ctx_template()
            ctx.response = _make_response()
            await hook.after_iteration(ctx)
            holder["model"] = "B"
            ctx2 = _ctx_template()
            ctx2.response = _make_response()
            await hook.after_iteration(ctx2)
            return service.llm_calls

        captured = asyncio.run(run())
        assert [c["model"] for c in captured] == ["A", "B"], captured

    def test_agent_factory_wiring_populates_model(self, monkeypatch):
        """AgentFactory.create() MUST заполнить ``_agent_box`` ПОСЛЕ
        ``AgentLoop.from_config`` (backfill на строке 194) — иначе
        get_model() вечно возвращает None и llm_call.model в
        agent_gateway_logs остаётся пустым.

        Проверяем сквозной путь: реальный ``AgentFactory.create`` →
        ``hook_factories[0]`` → инстанс ``DatabaseLoggingHook`` →
        его ``get_model`` отдаёт текущий ``agent.model``.
        """
        from nanobot.agent.loop import AgentLoop

        from lib.core.agent_factory import AgentFactory

        built: dict[str, object] = {}

        class FakeAgent:
            def __init__(self) -> None:
                self.model = "gpt-test"

        def fake_from_config(config, bus, **kwargs):
            built["hook_factories"] = kwargs.get("hook_factories") or []
            built["kwargs"] = kwargs
            agent = FakeAgent()
            built["agent"] = agent
            return agent

        monkeypatch.setattr(AgentLoop, "from_config", staticmethod(fake_from_config))

        class FakeService:
            def get_request_id(self, session_key):
                return "req-1"

        agent, _hooks, hook_factories = AgentFactory().create(
            config=object(),
            bus=object(),
            db_logging_service=FakeService(),
            agent_id="a1",
        )

        assert isinstance(agent, FakeAgent), "create() MUST вернуть собранный agent"
        assert len(hook_factories) == 1, hook_factories
        assert hook_factories is built["hook_factories"], "фабрика MUST уйти в AgentLoop"

        turn_ctx = types.SimpleNamespace(session_key="cli:s1")
        hook = hook_factories[0](turn_ctx)

        # Проверяем НЕ приватный атрибут, а наблюдаемое поведение:
        # модель реально доезжает в llm_call при after_iteration.
        assert callable(getattr(hook, "_get_model", None)), (
            "DatabaseLoggingHook MUST хранить get_model callable, "
            f"got {hook!r}"
        )

        class RecordingService(FakeService):
            def __init__(self) -> None:
                self.llm_calls: list[dict] = []

            def log_llm_call(self, **kwargs) -> None:
                self.llm_calls.append(kwargs)

        rec = RecordingService()
        hook2 = hook_factories[0](turn_ctx)
        hook2._service = rec

        async def run():
            ctx = _ctx_template()
            ctx.response = _make_response()
            await hook2.after_iteration(ctx)

        asyncio.run(run())
        assert rec.llm_calls, "after_iteration MUST записать llm_call"
        assert rec.llm_calls[0].get("model") == "gpt-test", rec.llm_calls[0]

        # runtime-switch: смена модели на лету видна без пересоздания хука
        built["agent"].model = "claude-other"
        assert hook2._get_model() == "claude-other", hook2._get_model()
