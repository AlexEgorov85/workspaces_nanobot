"""Тесты захвата задач PostgresChannel (протокол аренды снят).

Проверяют, что ``_claim_one`` захватывает задачу одним
``UPDATE ... RETURNING`` — без таблицы ``agent_worker_claims``, без
lease-loop и reclaim. Захват хранится в самой строке задачи.

Ветка ``worker_pool`` (INSERT INTO claims + lease/heartbeat + reclaim)
удалена вместе с ``channels.postgres.claims_table`` / ``claim_strategy``.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config import runtime_table  # noqa: F401

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


# ---------------------------------------------------------------------------
# Fixture: подмена utils.db с захватом SQL
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    """Подставной клиент ``enterprise-mcp`` вместо мока ``lib.utils.db``.

    Канал больше не пишет SQL: данные задач обслуживает платформа, а канал
    зовёт её операциями. Патчить больше нечего — атрибутов ``execute``,
    ``fetchone`` и ``transaction`` у модуля нет, и их возврат означал бы
    возврат прямого доступа к базе.
    """
    from lib.channels import postgres_channel as pg_mod

    from tests.conftest import FakeEnterpriseMcp

    client = FakeEnterpriseMcp()
    yield client, pg_mod


def _make_channel(pg_mod, client=None):
    config = {
        "dsn": "postgresql://u@h/db",
        "schema": "public",
        "table_name": runtime_table("conversation_messages"),
        "max_concurrent": 1,
    }
    return pg_mod.PostgresChannel(config, MagicMock(), enterprise_mcp=client)


def _capture_conn(db_mod):
    """Возвращает conn + список захваченных SQL-строк."""
    captured: list[str] = []
    conn = MagicMock()

    async def capture_fetchrow(sql, *args):
        captured.append(sql)
        return None

    async def capture_execute(sql, *args):
        captured.append(sql)

    conn.fetchrow = capture_fetchrow
    conn.execute = capture_execute

    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=conn)
    tx_cm.__aexit__ = AsyncMock(return_value=None)
    db_mod.async_transaction.return_value = tx_cm
    return conn, captured


# ---------------------------------------------------------------------------
# Tests: захват задачи
# ---------------------------------------------------------------------------


class TestClaimOneSqlAudit:
    """``_claim_one``: захват идёт операцией платформы, без таблицы аренды.

    Раньше здесь проверялся текст SQL захвата. Теперь его нет и быть не
    может: канал не импортирует драйвер (см.
    ``test_single_mode_audit.py::TestChannelHasNoDirectDatabaseAccess``),
    а структуру SQL проверяет платформа. Здесь — только то, за что отвечает
    канал: что он зовёт ``claim_task`` и что не зовёт ничего арендного.
    """

    def test_claim_one_returns_row_from_platform(self, mock_db):
        """``_claim_one`` возвращает строку, полученную от платформы."""
        client, pg_mod = mock_db
        ch = _make_channel(pg_mod, client)
        row = {"id": "msg-1", "chat_id": "chat-1"}
        client.responses["data.claim_task"] = {"claimed": [row]}

        import asyncio
        result = asyncio.run(ch._claim_one())
        assert result == row
        assert client.was_called("data.claim_task")

    def test_claim_one_uses_single_operation(self, mock_db):
        """Захват — одна операция, а не цепочка SQL-вызовов."""
        client, pg_mod = mock_db
        ch = _make_channel(pg_mod, client)

        import asyncio
        asyncio.run(ch._claim_one())

        assert client.operations() == ["data.claim_task"], (
            f"захват должен быть одной операцией, получено: {client.operations()!r}"
        )

    def test_start_creates_no_lease_task(self, mock_db):
        """``start()`` не создаёт lease-задачу, но создаёт ``_unstick_task``
        (фоновый unstick для отката зависших processing)."""
        client, pg_mod = mock_db
        ch = _make_channel(pg_mod, client)
        ch.exchange.start = AsyncMock()
        ch._flush_reasoning_loop = AsyncMock()
        ch._unstick_loop = AsyncMock()  # мокаем чтобы не зацикливаться

        import asyncio
        asyncio.run(ch.start())
        try:
            assert not hasattr(ch, "_lease_task"), (
                f"lease-задача не должна существовать, "
                f"got {getattr(ch, '_lease_task', None)}"
            )
            assert ch._unstick_task is not None, (
                "_unstick_task should be created "
                "(фоновая задача для отката зависших processing)"
            )
        finally:
            asyncio.run(ch.stop())


# ---------------------------------------------------------------------------
# Tests: _unstick_processing (единственный обработчик зависших задач)
# ---------------------------------------------------------------------------


class TestUnstickProcessingInSingle:
    """``_unstick_processing`` возвращает зависшие задачи в пул (без claims)."""

    @pytest.mark.asyncio
    async def test_unstick_processing_asks_the_platform(self, mock_db):
        client, pg_mod = mock_db
        ch = _make_channel(pg_mod, client)
        client.responses["data.unstick_tasks"] = {"recovered": []}

        recovered = await ch._unstick_processing()
        assert recovered == []
        assert client.was_called("data.unstick_tasks"), (
            "откат зависших должен идти операцией unstick_tasks"
        )
        # Пороги передаются платформе: счётчик попыток и терминальный
        # переход считаются там же, где живёт сам счётчик.
        arguments = client.last_call("data.unstick_tasks")["arguments"]
        assert "max_stuck_retries" in arguments
        assert "processing_timeout_sec" in arguments
