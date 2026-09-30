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

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


# ---------------------------------------------------------------------------
# Fixture: подмена utils.db с захватом SQL
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    """Подменить ``utils.db`` и прокинуть ``transaction`` через ``patch``.

    Патчим атрибуты модуля ``lib.channels.postgres_channel`` напрямую, не
    делая ``importlib.reload`` — это надёжнее при множественных вызовах.
    """
    from lib.channels import postgres_channel as pg_mod

    db_mod = types.ModuleType("utils.db")
    db_mod.async_fetchval = AsyncMock(return_value=None)
    db_mod.async_execute = AsyncMock()
    db_mod.async_fetchone = AsyncMock(return_value=None)
    db_mod.async_fetch = AsyncMock(return_value=[])
    db_mod.async_transaction = MagicMock()
    db_mod.DB_RETRYABLE_ERRORS = (Exception,)

    with patch.object(pg_mod, "execute", db_mod.async_execute), \
         patch.object(pg_mod, "fetchone", db_mod.async_fetchone), \
         patch.object(pg_mod, "fetchval", db_mod.async_fetchval), \
         patch.object(pg_mod, "fetch", db_mod.async_fetch), \
         patch.object(pg_mod, "transaction", db_mod.async_transaction), \
         patch.object(pg_mod, "_decode_jsonb",
                      lambda x: json.loads(x) if isinstance(x, str) and x else {}):
        yield db_mod, pg_mod


def _make_channel(pg_mod):
    config = {
        "dsn": "postgresql://u@h/db",
        "schema": "public",
        "table_name": "agent_conversation_messages",
        "max_concurrent": 1,
    }
    return pg_mod.PostgresChannel(config, MagicMock())


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
    """``_claim_one``: SQL не содержит ``agent_worker_claims``."""

    def test_claim_one_returns_row_from_fetchone(self, mock_db):
        """``_claim_one`` возвращает строку, полученную из ``fetchone``."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod)
        row = {"id": "msg-1", "chat_id": "chat-1"}
        db_mod.async_fetchone.return_value = row

        import asyncio
        result = asyncio.run(ch._claim_one())
        assert result == row
        db_mod.async_fetchone.assert_called_once()

    def test_claim_one_uses_update_returning(self, mock_db):
        """SQL в ``_claim_one`` — ``UPDATE ... RETURNING``, без claims."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod)
        _capture_conn(db_mod)

        import asyncio
        asyncio.run(ch._claim_one())

        fetchone_calls = db_mod.async_fetchone.call_args_list
        assert fetchone_calls, "_claim_one не вызвал fetchone"
        sql = fetchone_calls[0].args[0]
        assert "agent_worker_claims" not in sql
        assert "UPDATE" in sql
        assert "RETURNING" in sql

    def test_start_creates_no_lease_task(self, mock_db):
        """``start()`` не создаёт lease-задачу, но создаёт ``_unstick_task``
        (фоновый unstick для отката зависших processing)."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod)
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
    async def test_unstick_processing_updates_status(self, mock_db):
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod)

        # Мокаем transaction — возвращает conn
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])  # нет зависших
        conn.execute = AsyncMock()
        tx_cm = MagicMock()
        tx_cm.__aenter__ = AsyncMock(return_value=conn)
        tx_cm.__aexit__ = AsyncMock(return_value=None)
        db_mod.async_transaction.return_value = tx_cm

        recovered = await ch._unstick_processing()
        assert recovered == []
        # Должен быть fetch (SELECT зависших)
        conn.fetch.assert_called()
        # SQL fetch не должен содержать claims
        for call in conn.fetch.call_args_list:
            sql = call.args[0]
            assert "agent_worker_claims" not in sql
