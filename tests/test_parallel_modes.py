"""Тесты переключателя ``channels.postgres.claim_strategy``.

Проверяют, что в single-режиме (``"single"``, дефолт) PostgresChannel
физически не обращается к ``agent_worker_claims``, а в worker_pool — работает
как раньше (INSERT/DELETE/UPDATE в claims + lease-loop).

В single-режиме:
  * ``_claim_one`` идёт в ``_claim_one_single`` (UPDATE ... RETURNING);
  * ``_delete_claim`` — no-op;
  * ``_lease_loop`` / ``_reclaim_and_heal`` / ``_reclaim_needed`` — no-op
    (гарды в начале метода);
  * ``poll_inbound`` вызывает ``_unstick_processing`` (защита от зависших
    сообщений, аналог reclaim в worker_pool).

В worker_pool — как в master: ``INSERT INTO claims`` + lease-loop + reclaim.
"""
from __future__ import annotations

import asyncio
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


def _make_channel(pg_mod, claim_strategy: str = "single"):
    config = {
        "dsn": "postgresql://u@h/db",
        "schema": "public",
        "table_name": "agent_conversation_messages",
        "claims_table": "agent_worker_claims",
        "max_concurrent": 1,
        "claim_strategy": claim_strategy,
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
# Tests: single-режим
# ---------------------------------------------------------------------------


class TestSingleModeSqlAudit:
    """Single-режим: SQL НЕ должен содержать ``agent_worker_claims``."""

    def test_claim_one_single_routes_in_single_mode(self, mock_db):
        """``_claim_one`` в single идёт в ``_claim_one_single`` (без claims)."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod, "single")
        _, captured = _capture_conn(db_mod)

        # _claim_one_single возвращает row → _claim_one возвращает тот же row
        # Через fetchone (не через транзакцию)
        ch._claim_one_single = AsyncMock(return_value={"id": "msg-1"})
        import asyncio
        result = asyncio.run(ch._claim_one())
        assert result is not None
        # В single — fetchone возвращает результат, _claim_one_single вызван
        ch._claim_one_single.assert_called_once()

    def test_claim_one_single_uses_update_returning(self, mock_db):
        """SQL в ``_claim_one_single`` — ``UPDATE ... RETURNING``, без claims."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod, "single")
        _, captured = _capture_conn(db_mod)

        # _claim_one_single использует fetchone (не transaction)
        # fetchone уже замокан в db_mod
        import asyncio
        asyncio.run(ch._claim_one_single())

        # fetchone был вызван
        fetchone_calls = db_mod.async_fetchone.call_args_list
        assert fetchone_calls, "_claim_one_single не вызвал fetchone"
        sql = fetchone_calls[0].args[0]
        assert "agent_worker_claims" not in sql
        assert "UPDATE" in sql
        assert "RETURNING" in sql

    def test_delete_claim_is_noop_in_single(self, mock_db):
        """``_delete_claim`` в single — no-op."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod, "single")
        conn = MagicMock()
        conn.execute = AsyncMock()

        import asyncio
        asyncio.run(ch._delete_claim(conn, "task-1"))
        conn.execute.assert_not_called()
        asyncio.run(ch._delete_claim(None, "task-2"))
        db_mod.async_execute.assert_not_called()

    def test_lease_methods_are_noop_in_single(self, mock_db):
        """``_lease_loop``, ``_reclaim_needed``, ``_reclaim_and_heal`` — no-op."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod, "single")

        import asyncio
        # _lease_loop — гард в начале, сразу return
        asyncio.run(ch._lease_loop())  # не должно бросить исключение
        # _reclaim_needed — False
        result = asyncio.run(ch._reclaim_needed())
        assert result is False
        # _reclaim_and_heal — no-op, не должно бросить
        asyncio.run(ch._reclaim_and_heal())

    def test_lease_task_not_created_in_single(self, mock_db):
        """``start()`` в single не создаёт ``_lease_task``, но создаёт
        ``_unstick_task`` (фоновый unstick для single-режима)."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod, "single")
        ch.exchange.start = AsyncMock()
        ch._flush_reasoning_loop = AsyncMock()
        ch._unstick_loop = AsyncMock()  # мокаем чтобы не зацикливаться

        import asyncio
        asyncio.run(ch.start())
        try:
            assert ch._lease_task is None, (
                f"_lease_task should be None in single mode, "
                f"got {ch._lease_task}"
            )
            assert ch._unstick_task is not None, (
                "_unstick_task should be created in single mode "
                "(фоновая задача для отката зависших processing)"
            )
        finally:
            asyncio.run(ch.stop())


# ---------------------------------------------------------------------------
# Tests: worker_pool-режим
# ---------------------------------------------------------------------------


class TestWorkerPoolMode:
    """Worker_pool: старое поведение — claims используются."""

    def test_delete_claim_writes_sql_in_worker_pool(self, mock_db):
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod, "worker_pool")
        conn = MagicMock()
        conn.execute = AsyncMock()

        import asyncio
        asyncio.run(ch._delete_claim(conn, "task-1"))
        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        assert "DELETE FROM" in sql
        assert "agent_worker_claims" in sql

    def test_reclaim_needed_queries_db_in_worker_pool(self, mock_db):
        """``_reclaim_needed`` в worker_pool делает запрос к БД."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod, "worker_pool")
        db_mod.async_fetchval.return_value = False

        import asyncio
        result = asyncio.run(ch._reclaim_needed())
        assert result is False
        # Был вызов fetchval с SQL
        fetchval_calls = db_mod.async_fetchval.call_args_list
        assert fetchval_calls
        sql = fetchval_calls[0].args[0]
        assert "agent_worker_claims" in sql

    def test_lease_task_created_in_worker_pool(self, mock_db):
        """``start()`` в worker_pool создаёт ``_lease_task``."""
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod, "worker_pool")
        ch.exchange.start = AsyncMock()
        ch._flush_reasoning_loop = AsyncMock()
        ch._lease_loop = AsyncMock()

        import asyncio
        asyncio.run(ch.start())
        try:
            assert ch._lease_task is not None
        finally:
            asyncio.run(ch.stop())


# ---------------------------------------------------------------------------
# Tests: ChannelFactory прокидывает claim_strategy
# ---------------------------------------------------------------------------


class TestChannelFactoryClaimStrategy:
    """``ChannelFactory`` прокидывает ``claim_strategy`` из ``channels.postgres``."""

    def _setup(self):
        import sys
        import types
        from unittest.mock import MagicMock

        nano_channels = types.ModuleType("nanobot.channels")
        nano_manager = types.ModuleType("nanobot.channels.manager")
        cm = MagicMock()
        cm.channels = {}
        cm.enabled_channels = []
        nano_manager.ChannelManager = MagicMock(return_value=cm)
        sys.modules["nanobot.channels"] = nano_channels
        sys.modules["nanobot.channels.manager"] = nano_manager

        redis_mod = types.ModuleType("lib.channels.redis_channel")
        redis_mod.RedisChannel = MagicMock()
        sys.modules["lib.channels.redis_channel"] = redis_mod

        pg_mod = types.ModuleType("lib.channels.postgres_channel")
        pg_mod.PostgresChannel = MagicMock()
        sys.modules["lib.channels.postgres_channel"] = pg_mod
        return cm, redis_mod, pg_mod

    def _settings(self, channels):
        class S:
            pass
        s = S()
        s.channels = channels
        return s

    def _config(self):
        cfg = MagicMock()
        cfg.channels.send_progress = True
        cfg.channels.send_tool_hints = False
        cfg.channels.show_reasoning = True
        return cfg

    def test_default_claim_strategy_is_single(self):
        cm, _, pg_mod = self._setup()
        from lib.services.channel_factory import ChannelFactory

        factory = ChannelFactory()
        factory._add_postgres(
            cm, self._config(),
            self._settings({"postgres": {"enabled": True, "dsn": "postgresql://u@h/db"}}),
            MagicMock(),
        )
        cfg = pg_mod.PostgresChannel.call_args.args[0]
        assert cfg.get("claim_strategy") == "single"

    def test_worker_pool_strategy_passed_through(self):
        cm, _, pg_mod = self._setup()
        from lib.services.channel_factory import ChannelFactory

        factory = ChannelFactory()
        factory._add_postgres(
            cm, self._config(),
            self._settings({
                "postgres": {
                    "enabled": True,
                    "dsn": "postgresql://u@h/db",
                    "claim_strategy": "worker_pool",
                },
            }),
            MagicMock(),
        )
        cfg = pg_mod.PostgresChannel.call_args.args[0]
        assert cfg.get("claim_strategy") == "worker_pool"

    def test_create_all_propagates_claim_strategy(self):
        cm, _, pg_mod = self._setup()
        from lib.services.channel_factory import ChannelFactory

        factory = ChannelFactory()
        factory.create_all(
            self._config(),
            self._settings({
                "postgres": {
                    "enabled": True,
                    "dsn": "postgresql://u@h/db",
                    "claim_strategy": "worker_pool",
                },
            }),
            MagicMock(),
            MagicMock(),
        )
        cfg = pg_mod.PostgresChannel.call_args.args[0]
        assert cfg.get("claim_strategy") == "worker_pool"


# ---------------------------------------------------------------------------
# Tests: _unstick_processing в single-режиме
# ---------------------------------------------------------------------------


class TestUnstickProcessingInSingle:
    """``_unstick_processing`` работает в single (без claims)."""

    @pytest.mark.asyncio
    async def test_unstick_processing_updates_status(self, mock_db):
        db_mod, pg_mod = mock_db
        ch = _make_channel(pg_mod, "single")

        # Мокаем transaction — возвращает conn
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])  # нет зависших
        conn.execute = AsyncMock()
        tx_cm = MagicMock()
        tx_cm.__aenter__ = AsyncMock(return_value=conn)
        tx_cm.__aexit__ = AsyncMock(return_value=None)
        db_mod.async_transaction.return_value = tx_cm

        await ch._unstick_processing()
        # Должен быть fetch (SELECT зависших)
        conn.fetch.assert_called()
        # SQL fetch не должен содержать claims
        for call in conn.fetch.call_args_list:
            sql = call.args[0]
            assert "agent_worker_claims" not in sql
