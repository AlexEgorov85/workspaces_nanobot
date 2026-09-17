"""Тесты priority polling path — ``/stop`` независимо от обычных слотов.

Фикс: ``MessageExchange._poll_loop`` вызывает ``poll_priority_inbound``
**до** проверки ``is_slot_free()``. Это позволяет командам (например,
``/stop``) доходить до AgentLoop даже когда все обычные слоты заняты
активной задачей той же сессии.

Тесты проверяют:
  * priority claim фильтрует по ``content = '/stop'``;
  * priority path не вызывает ``acquire_slot`` / ``add_inflight``;
  * priority path не создаёт assistant-placeholder;
  * priority path обходит ``chat_inflight`` (даже если chat активен);
  * priority path освобождает claim + lease + msg_ctx + msg_chat;
  * ``MessageExchange._poll_loop`` вызывает ``poll_priority_inbound``
    первым и обрабатывает его даже при ``is_slot_free() == False``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


@pytest.fixture(autouse=True)
def priority_polling_mock_db(tmp_path):
    """Fake utils.db — моки async_fetch/fetchval/execute/transaction."""
    with (
        patch.dict("sys.modules"),
        patch("psycopg2.extras.Json", lambda x: x),
    ):
        import importlib
        import types

        def types_fake_db():
            mod = ModuleType("utils.db")
            mod.async_fetchval = AsyncMock(return_value=None)
            mod.async_execute = AsyncMock()
            mod.async_fetchone = AsyncMock(return_value=None)
            mod.async_fetch = AsyncMock(return_value=[])
            mod.async_transaction = MagicMock()
            mod.DB_RETRYABLE_ERRORS = (Exception,)
            return mod

        original_utils = sys.modules.get("utils")

        db_mod = types_fake_db()
        sys.modules["utils.db"] = db_mod

        if original_utils is not None:
            real_utils_pkg = importlib.import_module("utils")
            real_utils_pkg.db = db_mod
        else:
            import importlib.util as _iu
            utils_init = Path(_workspace_path) / "utils" / "__init__.py"
            spec = _iu.spec_from_file_location("utils", utils_init)
            real_utils_pkg = _iu.module_from_spec(spec)
            sys.modules["utils"] = real_utils_pkg
            spec.loader.exec_module(real_utils_pkg)
            real_utils_pkg.db = db_mod

        from utils.session_file_store import SessionFileStore  # noqa: F401

        sys.modules.pop("lib.channels.postgres_channel", None)
        sys.modules.pop("lib.channels.message_exchange", None)
        sys.modules.pop("lib.channels.priority_commands", None)

        from lib.channels.postgres_channel import PostgresChannel
        from lib.channels.message_exchange import MessageExchange
        from lib.channels.priority_commands import get_priority_commands

        class _Holder:
            def __init__(self):
                self.PostgresChannel = PostgresChannel
                self.MessageExchange = MessageExchange
                self.get_priority_commands = get_priority_commands
                self.db = db_mod

            def __iter__(self):
                yield PostgresChannel
                yield MessageExchange
                yield get_priority_commands
                yield db_mod

        yield _Holder()


def _make_channel(mock_db, **overrides):
    PostgresChannel, _, _, _ = mock_db
    config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": "agent_conversation_messages",
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
        "_print_worker_activity": False,
        "_print_db_activity": False,
    }
    config.update(overrides)
    bus = MagicMock()
    return PostgresChannel(config, bus)


class TestClaimOneSinglePriorityFilter:
    """``_claim_one_single(priority_contents=...)`` фильтрует по списку команд."""

    @pytest.mark.asyncio
    async def test_priority_filter_added_to_where(self, priority_polling_mock_db):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        db.async_fetchone.return_value = None

        await ch._claim_one_single(priority_contents=("/stop", "/restart"))

        sql_text = db.async_fetchone.await_args.args[0]
        assert "AND content = ANY(%s)" in sql_text, (
            f"WHERE должен содержать фильтр AND content = ANY(%s); "
            f"получили: {sql_text[:500]}"
        )

        params = db.async_fetchone.await_args.args[1:]
        # params содержит error_retry_delay (int) и list priority commands
        list_params = [p for p in params if isinstance(p, (list, tuple))]
        assert any(
            list(p) == ["/stop", "/restart"] for p in list_params
        ), (
            f"priority commands должны быть в параметрах SQL; получили: {params}"
        )

    @pytest.mark.asyncio
    async def test_priority_filter_absent_when_no_priority(self, priority_polling_mock_db):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        db.async_fetchone.return_value = None

        await ch._claim_one_single()

        sql_text = db.async_fetchone.await_args.args[0]
        assert "AND content = ANY(%s)" not in sql_text, (
            f"обычный claim НЕ должен содержать content-фильтр; "
            f"получили: {sql_text[:500]}"
        )


class TestPollPriorityOnce:
    """``_poll_priority_once``: claim → race-check → dispatch (без slot/inflight)."""

    @pytest.mark.asyncio
    async def test_priority_dispatch_does_not_acquire_slot(self, priority_polling_mock_db):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()

        exchange = MagicMock()
        exchange.acquire_slot = AsyncMock()

        result = await ch._poll_priority_once(exchange)
        assert result is True

        exchange.acquire_slot.assert_not_awaited()
        exchange.add_inflight.assert_not_called()

    @pytest.mark.asyncio
    async def test_priority_dispatch_skips_chat_inflight(self, priority_polling_mock_db):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._chat_inflight.add("chat-A")

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()

        exchange = MagicMock()
        exchange.acquire_slot = AsyncMock()

        result = await ch._poll_priority_once(exchange)
        assert result is True

        ch._handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_priority_dispatch_does_not_create_assistant_placeholder(
        self, priority_polling_mock_db
    ):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._insert_assistant_message = AsyncMock(return_value="asst-1")
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()

        exchange = MagicMock()
        await ch._poll_priority_once(exchange)

        ch._insert_assistant_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_priority_dispatch_sets_priority_metadata(
        self, priority_polling_mock_db
    ):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()

        exchange = MagicMock()
        await ch._poll_priority_once(exchange)

        meta_arg = ch._handle_message.await_args.kwargs["metadata"]
        assert meta_arg.get("priority") is True
        assert meta_arg.get("answer_id") is None

    @pytest.mark.asyncio
    async def test_priority_dispatch_releases_claim_lease_ctx_chat(
        self, priority_polling_mock_db
    ):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()

        ch._leases.add("m-stop")
        ch._msg_ctx["m-stop"] = {"x": 1}
        ch._msg_chat["m-stop"] = "chat-A"

        exchange = MagicMock()
        await ch._poll_priority_once(exchange)

        ch._delete_claim.assert_awaited_once()
        assert "m-stop" not in ch._leases
        assert "m-stop" not in ch._msg_ctx
        assert "m-stop" not in ch._msg_chat

    @pytest.mark.asyncio
    async def test_priority_dispatch_does_not_release_slot(
        self, priority_polling_mock_db
    ):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()
        ch._release_slot = MagicMock()

        exchange = MagicMock()
        await ch._poll_priority_once(exchange)

        ch._release_slot.assert_not_called()

    @pytest.mark.asyncio
    async def test_priority_skips_cancelled_race(self, priority_polling_mock_db):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "cancelled"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()

        exchange = MagicMock()
        result = await ch._poll_priority_once(exchange)
        assert result is False

        ch._handle_message.assert_not_called()
        ch._delete_claim.assert_awaited()


class TestPollPriorityInbound:
    """``poll_priority_inbound``: thin wrapper для ``_poll_priority_once``."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_priority_candidate(
        self, priority_polling_mock_db
    ):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)
        ch._claim_one = AsyncMock(return_value=None)

        exchange = MagicMock()
        result = await ch.poll_priority_inbound(exchange)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_when_priority_handled(
        self, priority_polling_mock_db
    ):
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)
        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()

        exchange = MagicMock()
        result = await ch.poll_priority_inbound(exchange)
        assert result is True

    @pytest.mark.asyncio
    async def test_passes_priority_contents_to_claim(self, priority_polling_mock_db):
        from lib.channels.priority_commands import get_priority_commands
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)
        ch._claim_one = AsyncMock(return_value=None)

        exchange = MagicMock()
        await ch.poll_priority_inbound(exchange)

        expected = get_priority_commands()
        ch._claim_one.assert_awaited_once()
        call_kwargs = ch._claim_one.await_args.kwargs
        assert call_kwargs.get("priority_contents") == expected


class TestPriorityRaceConditions:
    """Race-сценарии для priority polling path."""

    @pytest.mark.asyncio
    async def test_priority_skips_msg_became_cancelled_after_claim(
        self, priority_polling_mock_db
    ):
        """Если между claim и re-check AW пометил msg как cancelled —
        priority polling пропускает его (race-fix)."""
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        # re-check fetchval возвращает 'cancelled' (race window).
        db.async_fetchval.return_value = "cancelled"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()

        exchange = MagicMock()
        result = await ch._poll_priority_once(exchange)
        assert result is False

        ch._handle_message.assert_not_called()
        ch._delete_claim.assert_awaited()

    @pytest.mark.asyncio
    async def test_priority_does_not_acquire_slot_under_any_condition(
        self, priority_polling_mock_db
    ):
        """Priority path НЕ должен вызывать acquire_slot даже если
        инфраструктура слотов пуста (проверяет изоляцию от semaphore)."""
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()

        exchange = MagicMock()
        exchange.acquire_slot = AsyncMock()

        await ch._poll_priority_once(exchange)

        exchange.acquire_slot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_priority_dispatch_failure_triggers_mark_failed(
        self, priority_polling_mock_db
    ):
        """Если ``_handle_message`` падает с исключением — priority path
        вызывает ``_mark_failed`` (cleanup через стандартный путь)."""
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        ch._mark_failed = AsyncMock()
        ch._delete_claim = AsyncMock()

        exchange = MagicMock()
        result = await ch._poll_priority_once(exchange)
        assert result is True

        ch._mark_failed.assert_awaited_once()
        # cleanup НЕ делается нашим кодом, если _mark_failed был вызван
        # (он сам управляет cleanup).
        ch._delete_claim.assert_not_called()

    @pytest.mark.asyncio
    async def test_priority_does_not_release_slot_on_success(
        self, priority_polling_mock_db
    ):
        """Success path не вызывает _release_slot — slot не занимался."""
        PostgresChannel, _, _, db = priority_polling_mock_db
        ch = _make_channel(priority_polling_mock_db)

        ch._claim_one = AsyncMock(return_value={
            "id": "m-stop",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "/stop",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        })
        db.async_fetchval.return_value = "processing"
        ch._handle_message = AsyncMock()
        ch._delete_claim = AsyncMock()
        ch._release_slot = MagicMock()

        exchange = MagicMock()
        await ch._poll_priority_once(exchange)

        ch._release_slot.assert_not_called()


class TestPollLoopPriorityFirst:
    """``MessageExchange._poll_loop`` вызывает ``poll_priority_inbound`` всегда.

    Тесты проверяют структуру вызова через прямую инспекцию кода
    ``_poll_loop`` (исходный текст метода), а не через запуск цикла —
    потому что ``_poll_loop`` с бесконечным циклом требует аккуратного
    прерывания, которое непредсказуемо в pytest-asyncio.
    """

    def test_poll_loop_calls_poll_priority_first(self, priority_polling_mock_db):
        """``_poll_loop`` source-code проверка: priority вызывается первым."""
        import inspect
        from lib.channels import message_exchange as me_module

        src = inspect.getsource(me_module.MessageExchange._poll_loop)
        # Выделяем тело функции (после последнего """).
        body_start = src.rfind('"""') + 3
        body = src[body_start:]

        # priority вызывается ДО проверки is_slot_free в теле
        priority_pos = body.find("poll_priority_inbound")
        slot_free_pos = body.find("is_slot_free")
        assert priority_pos != -1, "poll_priority_inbound не найден в _poll_loop"
        assert slot_free_pos != -1, "is_slot_free не найден в _poll_loop"
        assert priority_pos < slot_free_pos, (
            f"poll_priority_inbound должен вызываться ДО is_slot_free; "
            f"priority_pos={priority_pos}, slot_free_pos={slot_free_pos}, "
            f"body={body[:500]}"
        )

    def test_poll_loop_uses_getattr_for_optional_method(self, priority_polling_mock_db):
        """``_poll_loop`` использует getattr — каналы без priority не ломаются."""
        import inspect
        from lib.channels import message_exchange as me_module

        src = inspect.getsource(me_module.MessageExchange._poll_loop)
        assert "getattr(self.channel, \"poll_priority_inbound\", None)" in src, (
            f"должен быть getattr с default=None для опционального метода; "
            f"src={src}"
        )

    def test_poll_loop_yields_after_priority(self, priority_polling_mock_db):
        """После priority-handled — ``asyncio.sleep(0)``, не busy-loop."""
        import inspect
        from lib.channels import message_exchange as me_module

        src = inspect.getsource(me_module.MessageExchange._poll_loop)
        idx = src.find("priority_handled")
        assert idx != -1
        # Берём большой фрагмент, чтобы захватить ``await asyncio.sleep(0)`` ниже.
        snippet = src[idx:idx + 500]
        assert "asyncio.sleep" in snippet, (
            f"после priority_handled нужен asyncio.sleep (yield), иначе busy-loop; "
            f"snippet={snippet}"
        )
