"""Рукопожатие enterprise-mcp на старте gateway.

Инвариант: сервер поднимается **до** каналов и работы агента. Проверяются
обе его половины — поведение ``_connect_enterprise_mcp`` и порядок вызовов
в ``_run``, потому что правильная функция, вызванная не в том месте,
инвариант не держит ровно так же, как её отсутствие.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = REPO_ROOT / "gateway.py"


class _FakeClient:
    """Клиент, который только считает вызовы."""

    def __init__(self, operations: list[str] | None = None, error: Exception | None = None):
        self.calls = 0
        self._operations = operations or ["run_script", "schema_check"]
        self._error = error

    async def list_operations(self) -> list[str]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._operations


def _ctx(client: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(enterprise_mcp=client)


def _connect(client: object) -> None:
    from gateway import _connect_enterprise_mcp

    asyncio.run(_connect_enterprise_mcp(_ctx(client)))


class TestHandshake:
    def test_disabled_section_is_not_an_error(self):
        """Раздел выключен — сервера нет по решению оператора, падать не на что."""

        _connect(None)

    def test_client_is_probed_once(self):
        client = _FakeClient()

        _connect(client)

        assert client.calls == 1

    def test_unavailable_server_is_not_swallowed(self):
        """Отказ обязан дойти до GatewayRunner: иначе каналы поднялись бы,
        задачи забирались бы, а tool'ы отвечали бы ошибкой."""
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(EnterpriseMcpUnavailable):
            _connect(client)

    def test_operation_count_reported(self, capsys):
        _connect(_FakeClient(operations=["a", "b", "c"]))

        assert "3 операций" in capsys.readouterr().out


class TestOrdering:
    def test_handshake_precedes_channels(self):
        """Порядок в исходнике, а не в тесте: вызвать рукопожатие после
        ``channels.start_all()`` — значит отдать первую задачу в никуда."""
        source = GATEWAY.read_text(encoding="utf-8")

        handshake = source.index("await _connect_enterprise_mcp(ctx)")
        channels = source.index("channels.start_all()")

        assert handshake < channels, (
            "enterprise-mcp поднимается ПОСЛЕ старта каналов: "
            f"рукопожатие на строке {handshake}, каналы на {channels}"
        )