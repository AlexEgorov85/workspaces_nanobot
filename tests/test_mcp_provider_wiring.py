"""Модельная поверхность операций платформы: проводка и громкий отказ.

Дефект, который эти тесты закрывают, был неочевиден: объявление
``config.json -> tools.mcpServers.enterprise`` было живым, кода, который его
читает, не существовало, и семь операций ``mcp_enterprise_*`` не доходили до
модели. Ни один тест этого не ловил — проверялось объявление, а не
доставка. Поэтому здесь проверяется доставка: реестр общий, соединение
поднято, отказ слышен.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lib.services.mcp_provider import (
    McpProviderUnavailable,
    build_mcp_provider,
    close_mcp_provider,
    connect_mcp_provider,
)

REPO = Path(__file__).resolve().parents[1]


class _FakeProvider:
    """Провайдер с заданным исходом connect().

    Отдельный класс, а не MagicMock, потому что тут важно одно свойство
    библиотеки: ``MCPProvider.connect()`` отказ НЕ бросает — он пишет
    warning и пробует позже. Фейк воспроизводит именно это молчание, и
    проверки ниже падают, если мы перестанем сверять соединённые серверы.
    """

    def __init__(self, configured: list[str], connected: list[str],
                 statuses: dict[str, str] | None = None) -> None:
        self._configured = list(configured)
        self._connected = list(connected)
        self._statuses = statuses or {name: "failed" for name in configured}
        self.connect_calls = 0
        self.closed = False

    @property
    def configured_server_names(self) -> set[str]:
        return set(self._configured)

    @property
    def connected_server_names(self) -> set[str]:
        return set(self._connected)

    def runtime_status(self) -> dict[str, str]:
        return dict(self._statuses)

    async def connect(self) -> None:
        # Библиотека глушит отказ: ни raise, ни лог у теста не видно.
        self.connect_calls += 1

    async def aclose(self) -> None:
        self.closed = True


def _install_fake_module(monkeypatch, provider: _FakeProvider) -> None:
    """Подменить ``nanobot.agent.tools.mcp`` — ради from_config в build."""
    module = SimpleNamespace(
        MCPProvider=SimpleNamespace(from_config=lambda config, registry: provider)
    )
    monkeypatch.setitem(sys.modules, "nanobot.agent.tools.mcp", module)


class TestBuild:
    def test_no_declared_servers_gives_no_provider(self, monkeypatch) -> None:
        _install_fake_module(monkeypatch, _FakeProvider([], []))
        assert build_mcp_provider(SimpleNamespace(), MagicMock()) is None

    def test_declared_servers_give_a_provider(self, monkeypatch) -> None:
        provider = _FakeProvider(["enterprise"], [])
        _install_fake_module(monkeypatch, provider)
        assert build_mcp_provider(SimpleNamespace(), MagicMock()) is provider


class TestConnectFailsLoudly:
    @pytest.mark.asyncio
    async def test_connected_server_is_reported(self) -> None:
        provider = _FakeProvider(["enterprise"], ["enterprise"])
        assert await connect_mcp_provider(provider) == ["enterprise"]
        assert provider.connect_calls == 1

    @pytest.mark.asyncio
    async def test_declared_but_missing_server_raises_with_name_and_status(self) -> None:
        # Ровно тот случай, который молчал: connect() не бросил, сервер не
        # поднялся, инструментов в реестре нет — а навык продолжал обещать
        # вызовы. Сообщение обязано называть сервер и его статус.
        provider = _FakeProvider(["enterprise"], [], {"enterprise": "failed"})
        with pytest.raises(McpProviderUnavailable) as excinfo:
            await connect_mcp_provider(provider)
        assert "enterprise" in str(excinfo.value)
        assert excinfo.value.servers == {"enterprise": "failed"}

    @pytest.mark.asyncio
    async def test_partial_connection_still_raises(self) -> None:
        provider = _FakeProvider(["a", "b"], ["a"], {"b": "failed"})
        with pytest.raises(McpProviderUnavailable) as excinfo:
            await connect_mcp_provider(provider)
        assert set(excinfo.value.servers) == {"b"}

    @pytest.mark.asyncio
    async def test_absent_provider_is_noop(self) -> None:
        assert await connect_mcp_provider(None) == []


class TestClose:
    @pytest.mark.asyncio
    async def test_close_closes(self) -> None:
        provider = _FakeProvider(["enterprise"], ["enterprise"])
        await close_mcp_provider(provider)
        assert provider.closed is True

    @pytest.mark.asyncio
    async def test_absent_provider_is_noop(self) -> None:
        await close_mcp_provider(None)

    @pytest.mark.asyncio
    async def test_close_failure_does_not_break_shutdown(self) -> None:
        provider = MagicMock()
        provider.aclose = MagicMock(side_effect=RuntimeError("уже закрыт"))
        await close_mcp_provider(provider)  # не должно бросить


class TestCompositionWiring:
    """Проводка в composition root и в точках входа.

    Проверяется текстом исходников, потому что контракт тут про ПОРЯДОК шагов
    старта, а не про значения. Числовое поведение того же шага — в
    ``TestConnectFailsLoudly`` выше.
    """

    def _source(self, relative: str) -> str:
        return io.open(REPO / relative, encoding="utf-8").read()

    def test_context_builds_registry_and_provider_before_agent(self) -> None:
        source = self._source("lib/core/application_context.py")
        registry_at = source.index("ctx.tool_registry = ToolRegistry()")
        provider_at = source.index("ctx.mcp_provider = build_mcp_provider(")
        factory_at = source.index("agent_factory.create(")
        assert registry_at < provider_at < factory_at, (
            "реестр и провайдер обязаны собираться раньше агента: цикл "
            "получает ссылку на реестр, и наполнить его позже нельзя"
        )

    def test_agent_gets_the_same_registry(self) -> None:
        source = self._source("lib/core/application_context.py")
        assert "tool_registry=ctx.tool_registry," in source, (
            "AgentFactory получает не тот реестр, который у провайдера: "
            "провайдер наполнит свой, а модель поедет с пустым"
        )

    def test_factory_does_not_replace_a_given_registry(self) -> None:
        source = self._source("lib/core/agent_factory.py")
        assert (
            "tool_registry if tool_registry is not None else ToolRegistry()"
            in source
        ), "фабрика создаёт свой реестр и затирает переданный"

    def test_stop_closes_the_provider(self) -> None:
        source = self._source("lib/core/application_context.py")
        assert "close_mcp_provider(self.mcp_provider)" in source, (
            "остановка не закрывает соединения провайдера — дочерний процесс "
            "останется висеть"
        )

    def test_gateway_startup_order(self) -> None:
        source = self._source("gateway.py")
        handshake = source.index("await _connect_enterprise_mcp(ctx)")
        provider = source.index("await _connect_mcp_provider(ctx)")
        journal = source.index("ctx.attach_log_transport()", handshake)
        assert handshake < provider < journal, (
            "порядок старта нарушен: рукопожатие платформы, затем соединение "
            "провайдера, затем транспорт журнала"
        )

    def test_cli_startup_order(self) -> None:
        source = self._source("cli_agent.py")
        handshake = source.index("await _connect_enterprise_mcp(ctx)")
        provider = source.index("await _connect_mcp_provider(ctx)")
        journal = source.index("ctx.attach_log_transport()", handshake)
        repl = source.index("await run_repl(", handshake)
        assert handshake < provider < journal < repl, (
            "порядок старта нарушен: рукопожатие, провайдер, транспорт "
            "журнала, и только потом REPL — иначе первый вопрос едет без "
            "инструментов платформы"
        )

    @pytest.mark.parametrize("entry", ["gateway.py", "cli_agent.py"])
    def test_provider_failure_is_raised_not_swallowed(self, entry: str) -> None:
        source = self._source(entry)
        start = source.index("async def _connect_mcp_provider(")
        rest = source[start + 10:]
        end = rest.index("\ndef ") if "\ndef " in rest else len(rest)
        body = rest[:end]
        assert "McpProviderUnavailable" in body
        assert "raise" in body, (
            "отказ провайдера проглочен — инструментов не будет, а старт пройдёт"
        )
