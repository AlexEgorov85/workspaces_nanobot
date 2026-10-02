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

    def __init__(
        self,
        operations: list[str] | None = None,
        error: Exception | None = None,
        responses: dict[str, str] | None = None,
        call_error: Exception | None = None,
    ):
        self.calls = 0
        self.call_ops: list[str] = []
        self._operations = operations or ["run_script", "schema_check"]
        self._error = error
        self._responses = responses or {}
        self._call_error = call_error

    async def list_operations(self) -> list[str]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._operations

    async def call(self, operation, arguments=None, *, identity=None) -> str:
        self.call_ops.append(operation)
        if self._call_error is not None:
            raise self._call_error
        return self._responses.get(operation, "{}")


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

    def test_unavailable_server_is_not_swallowed(self, capsys):
        """Отказ обязан дойти до GatewayRunner: иначе каналы поднялись бы,
        задачи забирались бы, а tool'ы отвечали бы ошибкой."""
        from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable

        client = _FakeClient(error=EnterpriseMcpUnavailable("процесс не поднялся"))

        with pytest.raises(EnterpriseMcpUnavailable):
            _connect(client)

        # Причина обязана быть в выводе: GatewayRunner сообщает только
        # «Gateway exited unexpectedly, restarting in 1.0s» — без этой
        # строки причина подъёма в логе не ищется нигде.
        out = capsys.readouterr().out
        assert "НЕ ПОДНЯЛСЯ" in out
        assert "процесс не поднялся" in out

    def test_operation_count_reported(self, capsys):
        _connect(_FakeClient(operations=["a", "b", "c"]))

        assert "3 операций" in capsys.readouterr().out

    def test_disabled_section_is_reported_not_silent(self, capsys):
        """Выключенный раздел — тоже результат: молчание неотличимо от
        «секция объявлена, но поднялась незаметно»."""
        _connect(None)

        out = capsys.readouterr().out
        assert "не объявлен" in out


class TestHealthSummary:
    """Процесс может подняться и быть частично нерабочим."""

    @staticmethod
    def _client(**kw):
        return _FakeClient(
            responses={
                "list_indexes": '{"indexes": ['
                '{"index_name": "audits_index", "state": "ready", "vector_count": 10},'
                '{"index_name": "violations_index", "state": "ready", "vector_count": 100}'
                ']}',
                "schema_check": '{"ok": true, "expected": 7, "found": 7, "missing": []}',
                "list_scripts": '{"count": 6, "scripts": []}',
            },
            **kw,
        )

    def test_each_capability_is_probed(self):
        client = self._client()

        _connect(client)

        assert set(client.call_ops) == {"list_indexes", "schema_check", "list_scripts"}

    def test_healthy_capabilities_are_summarised(self, capsys):
        _connect(self._client())

        out = capsys.readouterr().out
        assert "2/2 ready" in out
        assert "7/7 таблиц" in out
        assert "6 скриптов" in out

    def test_probe_failure_does_not_fail_startup(self, capsys):
        """Платформа отвечает, но одна capability сломана — это не повод
        ронять шлюз и уводить его в бесконечный рестарт."""
        client = self._client(call_error=RuntimeError("снимок недоступен"))

        _connect(client)  # не должно бросить

        out = capsys.readouterr().out
        assert "снимок недоступен" in out
        assert "процесс поднят" in out


class TestRenderers:
    """Разбор ответа capability в строку сводки."""

    def test_not_ready_index_is_named(self):
        from gateway import _indexes_line

        line = _indexes_line(
            {
                "indexes": [
                    {"index_name": "a", "state": "ready", "vector_count": 1},
                    {"index_name": "b", "state": "failed", "vector_count": 0},
                ]
            }
        )

        assert "1/2 ready" in line
        assert "b=failed" in line

    def test_missing_tables_are_named(self):
        from gateway import _schema_line

        line = _schema_line(
            {"ok": False, "expected": 7, "found": 5, "missing": ["public.x", "public.y"]}
        )

        assert "public.x" in line and "public.y" in line

    def test_empty_script_registry_is_flagged(self):
        from gateway import _scripts_line

        assert "пуст" in _scripts_line({"count": 0, "scripts": []})

    def test_scripts_counted_without_count_key(self):
        from gateway import _scripts_line

        assert "3 скриптов" in _scripts_line({"scripts": [1, 2, 3]})


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