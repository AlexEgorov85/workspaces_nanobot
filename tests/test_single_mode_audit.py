"""Строгий аудит: в single-режиме ни один SQL не должен трогать agent_worker_claims.

Тест вызывает все hot-path методы PostgresChannel в single-режиме и через
патчинг ``utils.db`` (execute/fetchone/fetch/fetchval/transaction) собирает
**все** SQL-строки, отправленные в БД. После каждого метода делается
assertion: нет ни одной строки, содержащей ``agent_worker_claims``.

Это динамическая проверка контракта single-режима, дополняющая статический
audit (см. ``test_postgres_channel_static_audit.py``).
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
# Fixture: подмена utils.db с глобальным перехватом всех SQL
# ---------------------------------------------------------------------------


class _SqlRecorder:
    """Захватывает все SQL-строки, отправленные через utils.db.

    Заменяет ``utils.db.execute``/``fetchone``/``fetch``/``fetchval``/
    ``transaction`` на моки, которые записывают SQL в общий список.
    """

    def __init__(self) -> None:
        self.sql: list[str] = []
        self._executed_results: dict[str, Any] = {}

    def attach(self, pg_mod) -> MagicMock:
        """Подменить ``utils.db`` и пропатчить ``pg_mod`` ссылки на него."""
        db_mod = types.ModuleType("utils.db")
        db_mod.async_fetchval = AsyncMock(side_effect=self._wrap_fetchval)
        db_mod.async_execute = AsyncMock(side_effect=self._wrap_execute)
        db_mod.async_fetchone = AsyncMock(side_effect=self._wrap_fetchone)
        db_mod.async_fetch = AsyncMock(side_effect=self._wrap_fetch)
        db_mod.async_transaction = MagicMock(side_effect=self._wrap_transaction)
        db_mod.DB_RETRYABLE_ERRORS = (Exception,)

        self._patcher = patch.multiple(
            pg_mod,
            execute=db_mod.async_execute,
            fetchone=db_mod.async_fetchone,
            fetchval=db_mod.async_fetchval,
            fetch=db_mod.async_fetch,
            transaction=db_mod.async_transaction,
            _decode_jsonb=lambda x: json.loads(x) if isinstance(x, str) and x else {},
        )
        self._patcher.start()
        return db_mod

    def detach(self) -> None:
        self._patcher.stop()

    def reset(self) -> None:
        self.sql.clear()

    def _record(self, sql: str) -> None:
        if not sql:
            return
        self.sql.append(str(sql))

    async def _wrap_execute(self, sql: str, *args, **kwargs) -> None:
        self._record(sql)

    async def _wrap_fetchval(self, sql: str, *args, **kwargs):
        self._record(sql)
        return False

    async def _wrap_fetchone(self, sql: str, *args, **kwargs):
        self._record(sql)
        return None

    async def _wrap_fetch(self, sql: str, *args, **kwargs):
        self._record(sql)
        return []

    def _wrap_transaction(self):
        # Возвращаем CM с conn, у которого тоже есть async методы-захваты
        captured_conn = MagicMock()

        async def rec_fetchrow(sql, *a, **kw):
            self._record(sql)
            return None

        async def rec_fetch(sql, *a, **kw):
            self._record(sql)
            return []

        async def rec_execute(sql, *a, **kw):
            self._record(sql)
            return None

        async def rec_fetchval(sql, *a, **kw):
            self._record(sql)
            return None

        captured_conn.fetchrow = rec_fetchrow
        captured_conn.fetch = rec_fetch
        captured_conn.execute = rec_execute
        captured_conn.fetchval = rec_fetchval

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=captured_conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    def assert_no_claims_access(self, context: str = "") -> None:
        """Assert: ни один захваченный SQL не содержит agent_worker_claims."""
        bad = [sql for sql in self.sql if "agent_worker_claims" in sql]
        if bad:
            pytest.fail(
                f"В single-режиме найден SQL к agent_worker_claims "
                f"({context}):\n" + "\n---\n".join(bad)
            )


# Стаб для Any в аннотации _wrap_transaction
from typing import Any  # noqa: E402


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder():
    """Fixture: создать рекордер и PostgresChannel в single-режиме."""
    rec = _SqlRecorder()

    from lib.channels import postgres_channel as pg_mod
    rec.attach(pg_mod)

    config = {
        "dsn": "postgresql://u@h/db",
        "schema": "public",
        "table_name": "agent_conversation_messages",
        "claims_table": "agent_worker_claims",
        "max_concurrent": 1,
        "claim_strategy": "single",
    }
    ch = pg_mod.PostgresChannel(config, MagicMock())

    yield rec, ch

    rec.detach()


class TestSingleModeHotPath:
    """Каждый метод hot-path в single-режиме не должен трогать claims."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method_name", [
        "_claim_one_single",
        "_unstick_processing",
        "_delete_claim",
        "_lease_loop",
        "_reclaim_needed",
        "_reclaim_and_heal",
    ])
    async def test_method_emits_no_claims_sql(self, recorder, method_name):
        """Каждый метод в single-режиме → 0 SQL к claims."""
        rec, ch = recorder
        rec.reset()

        method = getattr(ch, method_name)
        try:
            result = await method()
        except Exception:
            # некоторые методы могут бросить (например _lease_loop ожидает
            # self._running=True); проверим хотя бы что до исключения
            # claims не было
            rec.assert_no_claims_access(f"in {method_name}")
            return

        rec.assert_no_claims_access(f"in {method_name}")
        # _delete_claim в single — explicit None return
        if method_name == "_delete_claim":
            await method(None, "task-1")
            rec.assert_no_claims_access("in _delete_claim (None conn)")
            await method(MagicMock(), "task-2")
            rec.assert_no_claims_access("in _delete_claim (MagicMock conn)")

    @pytest.mark.asyncio
    async def test_claim_one_routes_to_single(self, recorder):
        """``_claim_one`` в single идёт через ``_claim_one_single``."""
        rec, ch = recorder
        rec.reset()

        # _claim_one_single возвращает None (нет задач) → _claim_one → None
        result = await ch._claim_one()
        assert result is None
        rec.assert_no_claims_access("in _claim_one → single path")

    @pytest.mark.asyncio
    async def test_poll_inbound_uses_single_claim(self, recorder):
        """``poll_inbound`` в single не вызывает _unstick_processing
        (только _claim_one через _poll_once) → 0 SQL к claims.

        В single-режиме unstick теперь — фоновая задача (_unstick_loop),
        а не часть poll_inbound. Это убирает 5 лишних подключений каждые
        poll_interval на пустом столе.
        """
        rec, ch = recorder
        # отключаем noisy activity output
        ch._print_worker_activity = False

        # Чтобы poll_inbound получил slot — нужен _poll_once.
        # Подменяем _poll_once чтобы не дёргать остальную логику
        ch._poll_once = AsyncMock(return_value=False)

        rec.reset()
        exchange = MagicMock()
        exchange.is_slot_free = MagicMock(return_value=True)

        result = await ch.poll_inbound(exchange)
        assert result is False
        rec.assert_no_claims_access("in poll_inbound (single mode)")

    @pytest.mark.asyncio
    async def test_poll_inbound_does_not_call_unstick(self, recorder):
        """``poll_inbound`` НЕ зовёт ``_unstick_processing`` (это фоновая задача).

        Раньше _unstick_processing дёргался каждые poll_interval — 5 лишних
        SQL на пустом столе каждые 10 сек. Сейчас unstick_interval по дефолту
        = 120 сек, независимо от poll_interval.
        """
        rec, ch = recorder
        ch._print_worker_activity = False
        ch._poll_once = AsyncMock(return_value=False)

        called = False
        original_unstick = ch._unstick_processing

        async def spy_unstick():
            nonlocal called
            called = True
            await original_unstick()

        ch._unstick_processing = spy_unstick

        exchange = MagicMock()
        exchange.is_slot_free = MagicMock(return_value=True)

        await ch.poll_inbound(exchange)
        assert not called, (
            "_unstick_processing не должен вызываться из poll_inbound"
        )


class TestSingleModeFullLifecycle:
    """Симулируем полный жизненный цикл сообщения в single-режиме.

    claim → dispatch → finalize (success path).
    Собираем все SQL и проверяем, что НИ ОДИН не содержит agent_worker_claims.
    """

    @pytest.mark.asyncio
    async def test_lifecycle_success_no_claims(self, recorder):
        rec, ch = recorder
        # _print_worker_activity off
        ch._print_worker_activity = False
        rec.reset()

        # Симулируем user-сообщение через _claim_one_single
        # (patch'нем fetchone чтобы вернул задачу).
        from lib.channels import postgres_channel as pg_mod
        row = {
            "id": "msg-1",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "content": "hello",
            "media": [],
            "metadata": "{}",
            "created_at": None,
        }
        # _claim_one_single через fetchone
        with patch.object(pg_mod, "fetchone", AsyncMock(return_value=row)):
            claimed_row = await ch._claim_one_single()
        rec.reset()  # сбрасываем claim SQL — дальше проверяем только finalize/failed

        assert claimed_row is not None

        # Симулируем finalize через _finalize_turn
        # Подменяем всё что нужно для транзакции + reasoning_io_lock
        ch._reasoning_buffers = {}
        ch._msg_ctx = {"msg-1": {"assistant_msg_id": "assistant-1"}}
        ch._msg_chat = {"msg-1": "chat-1"}
        ch._leases = {"msg-1"}
        ch.exchange.add_inflight("msg-1")
        ch._reasoning_io_lock = asyncio.Lock()

        # OutboundMessage мокаем
        outbound = MagicMock()
        outbound.event = None
        outbound.content = "response"
        outbound.chat_id = "chat-1"
        outbound.metadata = {"origin_message_id": "msg-1", "answer_id": "assistant-1"}
        outbound.media = []
        outbound.buttons = []

        # Подменяем _embed_media и _release_slot для упрощения
        ch._embed_media_for_db = AsyncMock(return_value=[])
        ch.exchange.release_slot = MagicMock()

        # Drop context bridge
        from contextlib import suppress
        with suppress(Exception):
            from lib.hooks.database_logging_hook import pop_context_bridge
            pop_context_bridge("postgres:chat-1")

        # Симулируем финал через _finalize_turn
        # В single _finalize_turn вызывает conn.execute(UPDATE completed) +
        # _delete_claim (no-op). Проверим, что DELETE из claims не уходит.
        await ch._finalize_turn(
            outbound, outbound.metadata, "msg-1",
        )

        # К этому моменту все SQL'ы, отправленные на финализацию
        rec.assert_no_claims_access(
            "during finalize_turn (single mode)"
        )

    @pytest.mark.asyncio
    async def test_mark_failed_no_claims(self, recorder):
        rec, ch = recorder
        ch._print_worker_activity = False
        ch._msg_chat = {"msg-1": "chat-1"}
        ch._leases = {"msg-1"}
        ch._msg_ctx = {"msg-1": {"assistant_msg_id": "assistant-1"}}
        ch.exchange.release_slot = MagicMock()
        ch._reasoning_buffers = {"assistant-1": ""}
        ch._reasoning_io_lock = asyncio.Lock()

        rec.reset()

        # Drop context bridge заранее (он зовётся в _drop_context_bridge)
        from contextlib import suppress
        with suppress(Exception):
            from lib.hooks.database_logging_hook import pop_context_bridge
            pop_context_bridge("postgres:chat-1")

        # _mark_failed идёт через async with transaction → conn.execute
        await ch._mark_failed("msg-1", "assistant-1", "test_error")

        rec.assert_no_claims_access("during _mark_failed (single mode)")

    @pytest.mark.asyncio
    async def test_release_all_leases_no_claims(self, recorder):
        rec, ch = recorder
        ch._print_worker_activity = False
        ch._leases = {"msg-1"}

        rec.reset()
        await ch._release_all_leases()

        rec.assert_no_claims_access("during _release_all_leases (single mode)")

    @pytest.mark.asyncio
    async def test_poll_once_busy_chat_no_claims(self, recorder):
        """В _poll_once ветка chat_inflight → обновляет pending + DELETE claim.

        В single DELETE claim → no-op. Проверяем.
        """
        rec, ch = recorder

        # _claim_one_single вернёт задачу
        async def stub_claim_one():
            return {
                "id": "msg-1",
                "chat_id": "busy-chat",
                "user_id": "user-1",
                "content": "hello",
                "media": [],
                "metadata": "{}",
                "created_at": None,
            }
        ch._claim_one = stub_claim_one

        # Имитируем, что chat уже инфлайтится → должно произойти откат
        ch._chat_inflight.add("busy-chat")

        rec.reset()
        exchange = MagicMock()
        exchange.is_slot_free = MagicMock(return_value=True)
        exchange.acquire_slot = AsyncMock()
        exchange.add_inflight = MagicMock()

        result = await ch._poll_once(exchange)
        assert result is False  # deferred
        rec.assert_no_claims_access("in _poll_once deferred branch (single mode)")

    @pytest.mark.asyncio
    async def test_send_delta_stream_end_no_claims(self, recorder):
        """send_delta с stream_end=True → UPDATE completed + DELETE claim.

        В single DELETE claim → no-op.
        """
        rec, ch = recorder
        ch._msg_ctx = {"msg-1": {"assistant_msg_id": "assistant-1"}}
        ch.exchange.release_slot = MagicMock()
        ch._leases = {"msg-1"}

        rec.reset()
        await ch.send_delta(
            "chat-1", "delta", {"origin_message_id": "msg-1", "answer_id": "assistant-1"},
            stream_id="stream-1",
            stream_end=True,
        )

        rec.assert_no_claims_access("in send_delta stream_end (single mode)")
