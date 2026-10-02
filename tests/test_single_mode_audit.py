"""Строгий аудит: ни один SQL PostgresChannel не должен трогать таблицу аренды.

Тест вызывает hot-path методы PostgresChannel и через
патчинг ``utils.db`` (execute/fetchone/fetch/fetchval/transaction) собирает
**все** SQL-строки, отправленные в БД. После каждого метода делается
assertion: нет ни одной строки, содержащей ``agent_worker_claims``.

Протокол аренды задач снят (таблица ``agent_worker_claims`` и
``channels.postgres.claims_table`` удалены), поэтому это теперь регресс-гард
на случай, если SQL к таблице аренды вернётся в hot-path.
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

class _OpRecorder:
    """Собирает **все** операции, отправленные каналом платформе.

    Раньше здесь собирался текст SQL из перехваченного ``utils.db``.
    Теперь у канала нет SQL вообще: единственный путь к данным задач -
    операции ``enterprise-mcp``, и регрессионный гард формулируется в
    этих терминах.
    """

    def __init__(self) -> None:
        from tests.conftest import FakeEnterpriseMcp

        self.client = FakeEnterpriseMcp()
        self._start = 0

    def reset(self) -> None:
        self._start = len(self.client.calls)

    @property
    def operations(self) -> list[str]:
        return [str(c["operation"]) for c in self.client.calls[self._start:]]

    def assert_no_claims_access(self, context: str = "") -> None:
        """Ни одна операция не должна обслуживать таблицу аренды.

        Протокол аренды снят, а вместе с переездом канала на платформу
        вопрос стал структурным: аренды в списке операций быть не может,
        потому что нет ни такого SQL, ни такого сервиса. Проверка остаётся
        как регресс-гард на случай возврата.
        """
        forbidden = {"claim_worker_task", "renew_claim", "release_claim"}
        bad = [op for op in self.operations if op in forbidden]
        if bad:
            pytest.fail(
                f"вызваны операции аренды ({context}): {bad!r}"
            )


from config import runtime_table  # noqa: F401


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder():
    """Рекордер операций и ``PostgresChannel`` с подставным клиентом."""
    rec = _OpRecorder()

    from lib.channels import postgres_channel as pg_mod

    config = {
        "dsn": "postgresql://u@h/db",
        "schema": "public",
        "table_name": runtime_table("conversation_messages"),
        "max_concurrent": 1,
    }
    ch = pg_mod.PostgresChannel(
        config, MagicMock(), enterprise_mcp=rec.client
    )

    yield rec, ch


class TestChannelHasNoDirectDatabaseAccess:
    """Структурный гард: у канала нет пути в PostgreSQL.

    Раньше это был набор проверок «здесь не такой-то SQL». Теперь канал не
    импортирует драйвер и не знает про ``utils.db``, поэтому гарантия
    структурная: вернуть SQL к таблице аренды можно, только вернув
    зависимость, и это будет видно здесь, а не в семи разных местах
    с пустым списком SQL.
    """

    def test_module_does_not_import_utils_db(self):
        import ast
        from pathlib import Path

        from lib.channels import postgres_channel as pg_mod

        tree = ast.parse(Path(pg_mod.__file__).read_text(encoding="utf-8"))
        offenders = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert not any(m.startswith("utils.db") for m in offenders), (
            f"канал снова импортирует utils.db: {offenders!r}"
        )

    def test_module_does_not_import_psycopg(self):
        import ast
        from pathlib import Path

        from lib.channels import postgres_channel as pg_mod

        tree = ast.parse(Path(pg_mod.__file__).read_text(encoding="utf-8"))
        offenders = [
            (node.module or "")
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not any(m.startswith("psycopg") for m in offenders), (
            f"канал снова импортирует драйвер: {offenders!r}"
        )


class TestSingleModeHotPath:
    """Каждый метод hot-path не должен трогать таблицу аренды."""

    @pytest.mark.asyncio
    async def test_claim_one_emits_no_claims_sql(self, recorder):
        """``_claim_one`` захватывает задачу одним ``UPDATE ... RETURNING``."""
        rec, ch = recorder
        rec.reset()

        # fetchone замокан рекордером и возвращает None (нет задач).
        assert await ch._claim_one() is None
        rec.assert_no_claims_access("in _claim_one")

    @pytest.mark.asyncio
    async def test_unstick_processing_emits_no_claims_sql(self, recorder):
        """``_unstick_processing`` работает без таблицы аренды."""
        rec, ch = recorder
        rec.reset()

        await ch._unstick_processing()
        rec.assert_no_claims_access("in _unstick_processing")

    @pytest.mark.asyncio
    async def test_claim_one_returns_row_from_update_returning(self, recorder):
        """``_claim_one`` возвращает строку, полученную от платформы."""
        rec, ch = recorder
        rec.reset()

        row = {
            "id": "msg-1",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "content": "hello",
            "media": [],
            "metadata": "{}",
            "created_at": None,
        }
        rec.client.responses["claim_task"] = {"claimed": row}
        result = await ch._claim_one()
        assert result == row
        rec.assert_no_claims_access("in _claim_one")

    @pytest.mark.asyncio
    async def test_poll_inbound_uses_single_claim(self, recorder):
        """``poll_inbound`` не вызывает _unstick_processing
        (только _claim_one через _poll_once) → 0 SQL к claims.

        unstick — фоновая задача (_unstick_loop), а не часть poll_inbound.
        Это убирает 5 лишних подключений каждые
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
        rec.assert_no_claims_access("in poll_inbound")

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
    """Симулируем полный жизненный цикл сообщения.

    claim → dispatch → finalize (success path).
    Собираем все SQL и проверяем, что НИ ОДИН не содержит agent_worker_claims.
    """

    @pytest.mark.asyncio
    async def test_lifecycle_success_no_claims(self, recorder):
        rec, ch = recorder
        # _print_worker_activity off
        ch._print_worker_activity = False
        rec.reset()

        # Симулируем user-сообщение через _claim_one: платформа отдаёт задачу.
        row = {
            "id": "msg-1",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "content": "hello",
            "media": [],
            "metadata": "{}",
            "created_at": None,
        }
        rec.client.responses["claim_task"] = {"claimed": row}
        claimed_row = await ch._claim_one()
        rec.reset()  # дальше проверяем только finalize/failed

        assert claimed_row is not None

        # Симулируем finalize через _finalize_turn
        rec.client.responses["finalize_turn"] = {"outcome": "completed"}
        ch._reasoning_buffers = {}
        ch._msg_ctx = {"msg-1": {"assistant_msg_id": "assistant-1"}}
        ch._msg_chat = {"msg-1": "chat-1"}
        ch.exchange.add_inflight("msg-1")

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
        # _finalize_turn вызывает conn.execute(UPDATE completed) и чистит
        # локальное состояние. Проверим, что SQL к claims не уходит.
        await ch._finalize_turn(
            outbound, outbound.metadata, "msg-1",
        )

        # К этому моменту все SQL'ы, отправленные на финализацию
        rec.assert_no_claims_access("during finalize_turn")

    @pytest.mark.asyncio
    async def test_mark_failed_no_claims(self, recorder):
        rec, ch = recorder
        ch._print_worker_activity = False
        ch._msg_chat = {"msg-1": "chat-1"}
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

        rec.assert_no_claims_access("during _mark_failed")

    @pytest.mark.asyncio
    async def test_poll_once_busy_chat_no_claims(self, recorder):
        """В _poll_once ветка chat_inflight → обновляет статус на pending.

        SQL к таблице аренды при этом не отправляется.
        """
        rec, ch = recorder

        # _claim_one вернёт задачу
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
        rec.assert_no_claims_access("in _poll_once deferred branch")

    @pytest.mark.asyncio
    async def test_send_delta_stream_end_no_claims(self, recorder):
        """send_delta с stream_end=True финализирует оборот (UPDATE completed)."""
        rec, ch = recorder
        ch._msg_ctx = {"msg-1": {"assistant_msg_id": "assistant-1"}}
        ch.exchange.release_slot = MagicMock()

        rec.reset()
        await ch.send_delta(
            "chat-1", "delta", {"origin_message_id": "msg-1", "answer_id": "assistant-1"},
            stream_id="stream-1",
            stream_end=True,
        )

        rec.assert_no_claims_access("in send_delta stream_end")
