"""Тесты user_stop_signal — polling skip cancelled + finalize drop.

Фикс: когда пользователь нажал СТОП, AW помечает user-сообщение в
public.agent_conversation_messages как status='cancelled'. Nanobot должен:
  1. В polling: пропускать cancelled user-сообщения (не диспатчить в LLM).
  2. После claim (но до dispatch): re-check статуса — race-окно между
     SELECT и UPDATE может привести к захвату уже-cancelled записи.
  3. В _finalize_turn: если user-сообщение стало cancelled ПОКА LLM
     работал — не записывать ответ, освободить ресурсы.

Это юнит-тесты на поведение канала (через подставной клиент
``enterprise-mcp``), без реального PG и без разбора SQL: проверки структуры
запросов живут на платформе, в ``mcp-platform/tests/test_data_task_queue.py``.
Интеграционный тест с реальной PG — в test_user_stop_signal_integration.py
(требует живой БД, запускается отдельно).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config import runtime_table  # noqa: F401

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


@pytest.fixture(autouse=True)
def user_stop_signal_mock_db(tmp_path):
    """Подставной клиент ``enterprise-mcp`` вместо мока ``utils.db``.

    Канал больше не ходит в PostgreSQL: отмена задачи проверяется операцией
    ``get_message``, а финализация сама различает «записать» и «отменённую».
    Проверки SQL-текста уехали на платформу.
    """
    with patch.dict("sys.modules"):
        import importlib

        original_utils = sys.modules.get("utils")
        if original_utils is not None:
            real_utils_pkg = importlib.import_module("utils")
        else:
            import importlib.util as _iu
            utils_init = Path(_workspace_path) / "utils" / "__init__.py"
            spec = _iu.spec_from_file_location("utils", utils_init)
            real_utils_pkg = _iu.module_from_spec(spec)
            sys.modules["utils"] = real_utils_pkg
            spec.loader.exec_module(real_utils_pkg)
        assert real_utils_pkg is not None

        from utils.session_file_store import SessionFileStore  # noqa: F401

        # Форсируем свежий импорт: если предыдущие тестовые файлы уже
        # импортировали канал с настоящим клиентом, класс остался связан
        # с реальным транспортом. Ре-импорт это исключает.
        sys.modules.pop("lib.channels.postgres_channel", None)

        from lib.channels.postgres_channel import (
            PostgresChannel,
            _decode_jsonb,
        )

        from tests.conftest import FakeEnterpriseMcp

        client = FakeEnterpriseMcp()

        class _Holder:
            def __init__(self):
                self.PostgresChannel = PostgresChannel
                self._decode_jsonb = _decode_jsonb
                self.db = client
                self.mcp = client

            def __iter__(self):
                yield PostgresChannel
                yield _decode_jsonb
                yield client

        yield _Holder()


def _make_channel(mock_db, **overrides):
    PostgresChannel, _, client = mock_db
    config = {
        "dsn": "postgresql://localhost:5432/test",
        "table_name": runtime_table("conversation_messages"),
        "poll_interval": 0.1,
        "flush_interval": 0.1,
        "max_concurrent": 1,
        "processing_timeout": 10,
    }
    config.update(overrides)
    bus = MagicMock()
    return PostgresChannel(config, bus, enterprise_mcp=client)


class TestClaimOneSkipsCancelled:
    """Тест SQL-логики _claim_one: WHERE status != 'cancelled'."""

    @pytest.mark.asyncio
    async def test_claim_returns_none_when_user_status_is_cancelled(self, user_stop_signal_mock_db):
        """Если единственная задача имеет status='cancelled', polling не берёт её."""
        PostgresChannel, _, db = user_stop_signal_mock_db
        ch = _make_channel(user_stop_signal_mock_db)

        # Платформа не отдала задачу: user со status='cancelled' отфильтрован
        # на стороне SQL захвата. Канал получает ``None`` и не диспатчит.
        result = await ch._claim_one()
        assert result is None

        # Фильтр ``status != 'cancelled'`` уехал на платформу вместе с
        # запросом; его структура закреплена в
        # ``mcp-platform/tests/test_data_task_queue.py::TestClaimTaskSqlStructure``.
        # Здесь — что канал вообще звал захват.
        assert db.was_called("claim_task")


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
        db.responses["claim_task"] = {"claimed": [claim_row]}
        # re-check fetchval возвращает 'cancelled'.
        db.responses["get_message"] = {"message": {"status": "cancelled"}}

        # Подменяем exchange, чтобы не упасть в реальную логику.
        exchange = MagicMock()
        exchange.acquire_slot = AsyncMock()

        result = await ch._poll_once(exchange)
        assert result is False, "cancelled msg должна быть пропущена"

        # Re-check статуса уехал в операцию get_message: она и читает
        # статус, и не даёт подменной SQL разойтись с реальным захватом.
        assert db.was_called("get_message"), (
            "после захвата статус перепроверяется - иначе отмена пришедшая "
            "между отбором кандидата и захватом будет проигнорирована"
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
        db.responses["claim_task"] = {"claimed": [claim_row]}
        db.responses["get_message"] = {"message": {"status": "processing"}}
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
    async def test_finalize_drops_response_for_cancelled_user(self, user_stop_signal_mock_db):
        """Отменённая задача: оборот закрывается как отмена, а не как запись.

        Проверку отмены делает платформа внутри ``finalize_turn`` — одной
        транзакцией с записью ответа. Канал этого не переигрывает: он читает
        ``outcome`` и, получив ``cancelled_drop``, освобождает слот, не
        выдавая отменённый ответ за успешный.
        """
        PostgresChannel, _, db = user_stop_signal_mock_db
        ch = _make_channel(user_stop_signal_mock_db)

        # _resolve_turn_context возвращает user/assistant ids.
        ch._resolve_turn_context = AsyncMock(return_value={
            "user_msg_id": "u-1",
            "assistant_msg_id": "asst-1",
            "chat_id": "chat-A",
            "source": "metadata",
        })
        db.responses["finalize_turn"] = {
            "outcome": "cancelled_drop",
            "placeholder_deleted": 1,
        }

        ch._release_slot = MagicMock()
        ch._msg_ctx = {"u-1": {}}
        ch._drop_context_bridge = MagicMock()
        ch._embed_media_for_db = AsyncMock(return_value=[])

        from nanobot.bus.events import OutboundMessage
        msg = OutboundMessage(
            channel="postgres",
            chat_id="chat-A",
            content="some answer",
            reply_to="u-1",
            metadata={"_final_turn": True},
        )

        await ch.send(msg)

        # Закрытие оборота — одна операция, а не «прочитал статус, потом
        # записал»: разделение оставляло окно, в котором отмена успевала
        # прийти, а ответ всё равно ложился.
        assert db.was_called("finalize_turn"), (
            "оборот закрывается операцией finalize_turn; "
            f"вызваны: {db.operations()}"
        )
        args = db.last_call("finalize_turn")["arguments"]
        assert args["user_msg_id"] == "u-1"
        assert args["assistant_msg_id"] == "asst-1"

        # Никакого отдельного чтения статуса перед записью: отмена решается
        # внутри той же транзакции.
        assert not db.was_called("get_message"), (
            "отмена проверяется внутри finalize_turn; отдельное чтение статуса "
            "оставляет окно, в котором отмена приходит уже после него"
        )

        # Лот всё равно освобождается — иначе слот воркера залипнет.
        ch._release_slot.assert_called_once_with("u-1")
        # Контекст оборота снят: отменённый ответ не должен остаться в памяти.
        assert "u-1" not in ch._msg_ctx

    @pytest.mark.asyncio
    async def test_finalize_writes_response_for_non_cancelled(self, user_stop_signal_mock_db):
        """Happy path: задача не отменена — ответ уходит через finalize_turn."""
        PostgresChannel, _, db = user_stop_signal_mock_db
        ch = _make_channel(user_stop_signal_mock_db)

        ch._resolve_turn_context = AsyncMock(return_value={
            "user_msg_id": "u-2",
            "assistant_msg_id": "asst-2",
            "chat_id": "chat-B",
            "source": "metadata",
        })
        db.responses["finalize_turn"] = {"outcome": "completed"}

        ch._embed_media_for_db = AsyncMock(return_value=[])
        ch._release_slot = MagicMock()
        ch._msg_ctx = {"u-2": {}}
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

        args = db.last_call("finalize_turn")["arguments"]
        assert args["user_msg_id"] == "u-2"
        assert args["assistant_msg_id"] == "asst-2"
        assert args["content"] == "done", "ответ должен уйти на платформу целиком"

        ch._release_slot.assert_called_once_with("u-2")
        assert "u-2" not in ch._msg_ctx, "контекст оборота снимается после записи"
