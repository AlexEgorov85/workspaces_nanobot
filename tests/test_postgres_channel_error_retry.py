"""Повторяемая ошибка задачи должна возвращаться в пул.

Дефект, который этот файл закрывает, был в SQL захвата: подзапрос выбирал
задачу и по ``status='error'`` с истёкшим backoff'ом, но внешний ``WHERE``
требовал ``status = 'pending'`` и отсекал её. Обещание ``_mark_failed``
«задача вернётся в пул после ``error_retry_delay``» не выполнялось никогда:
задача оставалась в ``error`` навсегда, а ``retry_count`` рос вхолостую.

**SQL захвата уехал на платформу** (``claim_task``), поэтому проверки его
структуры живут теперь в ``mcp-platform/tests/test_data_task_queue.py`` —
у них один источник правды, а не два, расходящихся копиями. Здесь остаётся
то, за что отвечает канал: он обязан передать платформе backoff и список
priority-команд, иначе ветка повтора выключается на его стороне.

Раньше тесты читали текст SQL канала. Это проверяло не поведение, а наличие
строк в исходнике: любая переформулировка запроса ломала тест, не меняя
ничего в рантайме, и наоборот — SQL мог уехать на платформу вместе с
дефектом, а тесты оставались зелёными.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config import runtime_table  # noqa: F401

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE_PATH = str(_PROJECT_ROOT / "workspace")
if _WORKSPACE_PATH not in sys.path:
    sys.path.insert(0, _WORKSPACE_PATH)

#: Значение backoff, которое тест выставляет явно. Конкретное число, а не
#: дефолт: тест, зависящий от дефолта, ломается вместе с дефолтом и молчит.
BACKOFF_SEC = 42


class _FakeMcpClient:
    """Подставной клиент ``enterprise-mcp``: операции и их аргументы."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: dict[str, object] = {}

    async def call(self, operation, arguments=None, identity=None):
        self.calls.append(
            {
                "operation": operation,
                "arguments": dict(arguments or {}),
                "identity": identity,
            }
        )
        value = self.responses.get(operation)
        if not isinstance(value, dict):
            return '{"status": "ok"}'
        import json

        return json.dumps({"status": "ok", **value})

    def last_call(self, operation: str) -> dict[str, object]:
        matching = [c for c in self.calls if c["operation"] == operation]
        assert matching, (
            f"операция {operation!r} не вызывалась; вызваны: "
            f"{[c['operation'] for c in self.calls]}"
        )
        return matching[-1]


@pytest.fixture(autouse=True)
def error_retry_mock_db():
    """Подставной клиент MCP вместо бывшего мока ``lib.utils.db``."""
    with patch.dict("sys.modules"):
        import importlib
        import importlib.util

        original_utils = sys.modules.get("utils")
        if original_utils is not None:
            real_utils_pkg = importlib.import_module("utils")
        else:
            spec = importlib.util.spec_from_file_location(
                "utils", Path(_WORKSPACE_PATH) / "utils" / "__init__.py"
            )
            real_utils_pkg = importlib.util.module_from_spec(spec)
            sys.modules["utils"] = real_utils_pkg
            spec.loader.exec_module(real_utils_pkg)
        assert real_utils_pkg is not None

        for name in (
            "lib.channels.postgres_channel",
            "lib.channels.message_exchange",
        ):
            sys.modules.pop(name, None)

        from lib.channels.postgres_channel import PostgresChannel

        client = _FakeMcpClient()

        class _Holder:
            def __init__(self):
                self.PostgresChannel = PostgresChannel
                self.db = client
                self.client = client

            def __iter__(self):
                yield PostgresChannel
                yield client

        yield _Holder()


def _make_channel(holder, **overrides):
    PostgresChannel = holder.PostgresChannel
    config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": runtime_table("conversation_messages"),
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
        "error_retry_delay": BACKOFF_SEC,
        "_print_worker_activity": False,
        "_print_db_activity": False,
    }
    config.update(overrides)
    return PostgresChannel(config, MagicMock(), enterprise_mcp=holder.db)


class TestBackoffReachesThePlatform:
    """Канал передаёт backoff операции — иначе ветка повтора выключена.

    Раньше backoff подставлялся в текст SQL канала. Теперь он уходит
    аргументом, и «забыть его» не падает, а молча возвращает задачу в
    ``error`` навсегда — ровно тот дефект, ради которого файл и написан.
    """

    @pytest.mark.asyncio
    async def test_backoff_is_passed_once_as_a_named_argument(
        self, error_retry_mock_db
    ):
        ch = _make_channel(error_retry_mock_db)

        await ch._claim_one()

        arguments = error_retry_mock_db.db.last_call("data.claim_task")["arguments"]
        assert arguments["error_retry_delay_sec"] == BACKOFF_SEC, (
            f"backoff не доехал до платформы: {arguments!r}"
        )

    @pytest.mark.asyncio
    async def test_priority_list_is_passed_through(self, error_retry_mock_db):
        ch = _make_channel(error_retry_mock_db)

        await ch._claim_one(priority_contents=("/stop", "/restart"))

        arguments = error_retry_mock_db.db.last_call("data.claim_task")["arguments"]
        assert arguments["priority_contents"] == ["/stop", "/restart"]

    @pytest.mark.asyncio
    async def test_absent_priority_is_not_an_empty_filter(
        self, error_retry_mock_db
    ):
        """Пустой список ≠ «взять только пустые тексты».

        Канал шлёт ``None``, а не ``[]``: платформа трактует ``[]`` как
        «ничего не захватывать», и очередь встала бы молча.
        """
        ch = _make_channel(error_retry_mock_db)

        await ch._claim_one(priority_contents=None)

        arguments = error_retry_mock_db.db.last_call("data.claim_task")["arguments"]
        assert arguments["priority_contents"] is None

    @pytest.mark.asyncio
    async def test_claim_is_signed_as_a_service_call(self, error_retry_mock_db):
        """Захват идёт вне оборота — личность служебная, не пользовательская."""
        ch = _make_channel(error_retry_mock_db)

        await ch._claim_one()

        identity = error_retry_mock_db.db.last_call("data.claim_task")["identity"]
        assert identity is not None
        assert identity.session_id.startswith("task-worker:"), (
            f"захват подписан сессией, которой нет: {identity.session_id!r}"
        )


class TestCompactionSubscriberLogging:
    """Свободное имя ``logger`` вместо ``self.logger`` (F821)."""

    @pytest.mark.asyncio
    async def test_send_event_survives_subscriber_failure(self, error_retry_mock_db):
        """Падение подписчика компакции логируется и не роняет ``send``.

        До правки в обработчике исключения стояло свободное ``logger``, и
        ветка падения подписчика поднимала ``NameError`` поверх исходной
        ошибки — то есть маскировала её и теряла.
        """
        import inspect

        from lib.channels import postgres_channel as pc

        src = inspect.getsource(pc.PostgresChannel.send)
        assert re.search(r"(?<!self\.)logger\b", src) is None, (
            "в send() осталось свободное имя logger без self."
        )

    @pytest.mark.asyncio
    async def test_send_does_not_raise_when_subscriber_fails(self, error_retry_mock_db):
        ch = _make_channel(error_retry_mock_db)
        from nanobot.bus.events import OutboundMessage

        failing = MagicMock()
        failing.feed = AsyncMock(side_effect=RuntimeError("compaction boom"))
        ch._compaction_event_subscriber = failing

        msg = MagicMock(spec=OutboundMessage)
        msg.event = object()  # любое не-None событие уходит в подписчика
        msg.metadata = {}

        # Не должно бросать: ошибка подписчика — его проблема, не транспорта.
        await ch.send(msg)
