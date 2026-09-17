"""Тесты user_stop_signal — polling skip cancelled + finalize drop.

Фикс: когда пользователь нажал СТОП, AW помечает user-сообщение в
public.agent_conversation_messages как status='cancelled'. Nanobot должен:
  1. В polling: пропускать cancelled user-сообщения (не диспатчить в LLM).
  2. После claim (но до dispatch): re-check статуса — race-окно между
     SELECT и UPDATE может привести к захвату уже-cancelled записи.
  3. В _finalize_turn: если user-сообщение стало cancelled ПОКА LLM
     работал — не записывать ответ, освободить ресурсы.

Это юнит-тесты на SQL-логику (через мок utils.db), без реального PG.
Интеграционный тест с реальной PG — в test_user_stop_signal_integration.py
(требует живой БД, запускается отдельно).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


@pytest.fixture(autouse=True)
def user_stop_signal_mock_db(tmp_path):
    """Fake utils.db — моки async_fetch/fetchval/execute/transaction."""
    with (
        patch.dict("sys.modules"),
        patch("psycopg2.extras.Json", lambda x: x),
    ):
        import importlib
        import types

        def types_fake_db():
            mod = types.ModuleType("utils.db")
            mod.async_fetchval = AsyncMock(return_value=None)
            mod.async_execute = AsyncMock()
            mod.async_fetchone = AsyncMock(return_value=None)
            mod.async_fetch = AsyncMock(return_value=[])
            mod.async_transaction = MagicMock()
            mod.DB_RETRYABLE_ERRORS = (Exception,)
            return mod

        # Сохраним оригинальный ``utils`` (настоящий пакет из workspace),
        # чтобы канал мог импортировать из utils.session_file_store.
        original_utils = sys.modules.get("utils")

        # Создаём фейковый ``utils.db`` (чтобы канал взял наши моки).
        db_mod = types_fake_db()
        sys.modules["utils.db"] = db_mod

        # Восстанавливаем настоящий utils как пакет, но подменяем db внутри.
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

        # Форсируем свежий импорт: если предыдущие тестовые файлы уже
        # импортировали канал с НАСТОЯЩИМ utils.db, класс остался связан
        # с реальным пулом — тесты ушли бы в живую БД. Ре-импорт под
        # фейковым utils.db это исключает.
        sys.modules.pop("lib.channels.postgres_channel", None)

        from lib.channels.postgres_channel import (
            PostgresChannel,
            _decode_jsonb,
        )

        class _Holder:
            def __init__(self):
                self.PostgresChannel = PostgresChannel
                self._decode_jsonb = _decode_jsonb
                self.db = db_mod

            def __iter__(self):
                yield PostgresChannel
                yield _decode_jsonb
                yield db_mod

        yield _Holder()


def _make_channel(mock_db, **overrides):
    PostgresChannel, _, _ = mock_db
    config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": "agent_conversation_messages",
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
    }
    config.update(overrides)
    bus = MagicMock()
    return PostgresChannel(config, bus)


class TestClaimOneSingleSkipsCancelled:
    """Тест SQL-логики _claim_one_single: WHERE status != 'cancelled'."""

    @pytest.mark.asyncio
    async def test_claim_returns_none_when_user_status_is_cancelled(self, user_stop_signal_mock_db):
        """Если единственная задача имеет status='cancelled', polling не берёт её."""
        PostgresChannel, _, db = user_stop_signal_mock_db
        ch = _make_channel(user_stop_signal_mock_db)

        # fetchone возвращает None — SELECT подзапрос не нашёл ничего,
        # потому что user со status='cancelled' отфильтрован.
        # (UPDATE ... RETURNING возвращает 0 rows.)
        # НЕ пересоздаём мок (иначе теряется await_args) — устанавливаем
        # только return_value на существующем mock'е из фикстуры.
        db.async_fetchone.return_value = None

        result = await ch._claim_one_single()
        assert result is None

        # fetchone должен быть вызван с WHERE status != 'cancelled'
        call_args = db.async_fetchone.await_args
        assert call_args is not None
        sql_text = call_args.args[0]
        assert "status != 'cancelled'" in sql_text, (
            f"WHERE clause должен содержать status != 'cancelled', "
            f"получили: {sql_text[:500]}"
        )


class TestPollOnceRaceCheck:
    """Тест re-check статуса после claim (race между SELECT и UPDATE)."""

    @pytest.mark.asyncio
    async def test_poll_skips_msg_if_status_changed_to_cancelled(self, user_stop_signal_mock_db):
        """Если между SELECT в claim и re-check в poll_once AW поставил cancelled —
        polling пропускает сообщение и освобождает claim."""
        PostgresChannel, _, db = user_stop_signal_mock_db
        ch = _make_channel(user_stop_signal_mock_db)

        # _claim_one_single возвращает row (ещё status='processing' в момент claim).
        claim_row = {
            "id": "m-1",
            "chat_id": "chat-A",
            "user_id": "u",
            "content": "hello",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        }
        db.async_fetchone.return_value = claim_row
        # re-check fetchval возвращает 'cancelled'.
        db.async_fetchval.return_value = "cancelled"

        # Подменяем exchange, чтобы не упасть в реальную логику.
        exchange = MagicMock()
        exchange.acquire_slot = AsyncMock()

        result = await ch._poll_once(exchange)
        assert result is False, "cancelled msg должна быть пропущена"

        # В single-режиме _delete_claim — no-op (только worker_pool удаляет
        # claim). Главное — polling НЕ диспатчил в LLM (мы это проверяем
        # через result=False) и fetchval был вызван для re-check статуса.
        assert db.async_fetchval.await_count >= 1, (
            "re-check fetchval должен быть вызван"
        )
        recheck_sql = db.async_fetchval.await_args.args[0]
        assert "status" in recheck_sql and "WHERE id" in recheck_sql, (
            f"re-check должен быть SELECT status ... WHERE id; "
            f"получили: {recheck_sql[:200]}"
        )

    @pytest.mark.asyncio
    async def test_poll_processes_msg_if_still_pending(self, user_stop_signal_mock_db):
        """Если re-check возвращает 'processing' — polling продолжает нормально.

        Этот тест проверяет, что НЕ-сancelled путь тоже работает
        (важно — рефакторинг не должен ломать happy path).
        """
        PostgresChannel, _, db = user_stop_signal_mock_db
        ch = _make_channel(user_stop_signal_mock_db)

        claim_row = {
            "id": "m-2",
            "chat_id": "chat-B",
            "user_id": "u",
            "content": "ok",
            "media": "[]",
            "metadata": "{}",
            "created_at": None,
        }
        db.async_fetchone.return_value = claim_row
        db.async_fetchval.return_value = "processing"
        # _insert_assistant_message возвращает UUID assistant.
        ch._insert_assistant_message = AsyncMock(return_value="asst-1")

        exchange = MagicMock()
        exchange.acquire_slot = AsyncMock()
        # dispatch (_handle_message) не должен вызвать LLM в тесте — мокнем.
        ch._handle_message = AsyncMock()

        result = await ch._poll_once(exchange)
        assert result is True, "non-cancelled msg должна быть dispatch'ена"

        # _handle_message должен быть вызван ровно один раз.
        ch._handle_message.assert_awaited_once()


class TestFinalizeTurnDropsCancelled:
    """Тест _finalize_turn: drop response если user стал cancelled."""

    @pytest.mark.asyncio
    async def test_finalize_skips_response_for_cancelled_user(self, user_stop_signal_mock_db):
        """Если user-сообщение стало 'cancelled' во время LLM-обработки —
        _finalize_turn НЕ пишет ответ и НЕ обновляет user-статус."""
        PostgresChannel, _, db = user_stop_signal_mock_db
        ch = _make_channel(user_stop_signal_mock_db)

        # _resolve_turn_context возвращает user/assistant ids.
        ch._resolve_turn_context = AsyncMock(return_value={
            "user_msg_id": "u-1",
            "assistant_msg_id": "asst-1",
            "chat_id": "chat-A",
            "source": "metadata",
        })
        # fetchval для user-status возвращает 'cancelled'.
        db.async_fetchval.return_value = "cancelled"

        # _delete_claim и _release_slot — моки для проверки.
        ch._delete_claim = AsyncMock()
        ch._release_slot = MagicMock()
        ch._msg_ctx = {"u-1": {}}
        ch._leases = {"u-1"}
        ch._drop_context_bridge = MagicMock()

        # OutboundMessage с content+final_turn.
        from nanobot.bus.events import OutboundMessage
        msg = OutboundMessage(
            channel="postgres",
            chat_id="chat-A",
            content="some answer",
            reply_to="u-1",
            metadata={"_final_turn": True},
        )

        await ch.send(msg)

        # Удаляем assistant-заглушку.
        # async_execute должен быть вызван с DELETE FROM ... WHERE id = %s AND
        # role = 'assistant'. Проверяем args (asst-1 в параметре %s).
        delete_calls = [
            (str(call.args[0]), call.args[1] if len(call.args) > 1 else None)
            for call in db.async_execute.await_args_list
            if "DELETE FROM" in str(call.args[0])
            and "role = 'assistant'" in str(call.args[0])
        ]
        assert len(delete_calls) >= 1, (
            f"должен быть DELETE assistant placeholder; "
            f"calls={db.async_execute.await_args_list}"
        )
        # Второй аргумент DELETE — UUID assistant'а.
        args_with_asst = [
            args for sql, args in delete_calls if "asst-1" in str(args)
        ]
        assert len(args_with_asst) >= 1, (
            f"asst-1 должен быть в args DELETE; calls={delete_calls}"
        )

        # user-статус НЕ должен быть обновлён на 'completed'.
        # _finalize_turn должен early-return без транзакции.
        update_user_calls = [
            str(call.args[0])
            for call in db.async_execute.await_args_list
            if "UPDATE" in str(call.args[0])
            and "u-1" in str(call.args)
        ]
        # Может быть _claim DELETE или подобное, но НЕ должно быть UPDATE ... SET status='completed' для user.
        assert not any("status = 'completed'" in c for c in update_user_calls), (
            f"user-статус НЕ должен переписываться на completed; calls={update_user_calls}"
        )

        # _release_slot должен быть вызван (освобождение слота).
        ch._release_slot.assert_called_once_with("u-1")

    @pytest.mark.asyncio
    async def test_finalize_writes_response_for_non_cancelled(self, user_stop_signal_mock_db):
        """Happy path: user НЕ cancelled — _finalize_turn пишет ответ как обычно."""
        PostgresChannel, _, db = user_stop_signal_mock_db
        ch = _make_channel(user_stop_signal_mock_db)

        ch._resolve_turn_context = AsyncMock(return_value={
            "user_msg_id": "u-2",
            "assistant_msg_id": "asst-2",
            "chat_id": "chat-B",
            "source": "metadata",
        })
        db.async_fetchval.return_value = "processing"

        # Моки для транзакционных операций внутри _finalize_turn.
        # _finalize_turn использует ``async with transaction() as conn``
        # — мок этого контекст-менеджера: возвращает объект с методами
        # fetchrow/execute (как у psycopg2.AsyncConnection).
        existing_row = {
            "metadata": "{}",
            "media": "[]",
            "content": "",
        }
        db.async_fetchone.return_value = existing_row
        # Контекст-менеджер для ``async with transaction()``.
        conn_mock = MagicMock()
        conn_mock.fetchrow = AsyncMock(return_value=existing_row)
        conn_mock.execute = AsyncMock()
        tx_mock = MagicMock()
        tx_mock.__aenter__ = AsyncMock(return_value=conn_mock)
        tx_mock.__aexit__ = AsyncMock(return_value=None)
        db.async_transaction.return_value = tx_mock

        # _embed_media_for_db — мок (используется в _finalize_turn).
        ch._embed_media_for_db = AsyncMock(return_value=[])
        # _reasoning_io_lock — мок (async context manager).
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_lock():
            yield

        ch._reasoning_io_lock = _fake_lock()
        # _delete_claim и _release_slot.
        ch._delete_claim = AsyncMock()
        ch._release_slot = MagicMock()
        ch._msg_ctx = {"u-2": {}}
        ch._leases = {"u-2"}
        ch._drop_context_bridge = MagicMock()

        from nanobot.bus.events import OutboundMessage
        msg = OutboundMessage(
            channel="postgres",
            chat_id="chat-B",
            content="done",
            reply_to="u-2",
            metadata={"_final_turn": True},
        )

        await ch.send(msg)

        # UPDATE ... SET status = 'completed' должен быть в вызовах.
        # Внутри транзакции UPDATE делается через conn.execute (не
        # db.async_execute), поэтому проверяем conn_mock.
        update_calls = [
            str(call.args[0])
            for call in conn_mock.execute.await_args_list
        ]
        assert any(
            "status = 'completed'" in c
            and "WHERE id = %s" in c
            for c in update_calls
        ), (
            f"должен быть UPDATE user SET status='completed'; "
            f"calls={update_calls}"
        )
