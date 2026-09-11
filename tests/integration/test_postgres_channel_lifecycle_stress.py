"""Интеграционный stress-тест lifecycle PostgresChannel (гейт «точно устранит»).

Проверяется на РЕАЛЬНОЙ БД в отдельной тестовой схеме
``test_pg_lifecycle_<rand>`` (создаётся и удаляется автоматически).
Главный критерий: после серии задач с разными финалами (обычный / стрим /
stream_end с пустым delta / восстановление по answer_id) polling
продолжает работать и берёт новые сообщения — без перезапуска процесса
и без зависания ``exchange.inflight``.

Запуск (opt-in)::

    $env:NANOBOT_INTEGRATION = "1"
    python -m pytest tests/integration/test_postgres_channel_lifecycle_stress.py -v

Сценарии (Phase 7 плана):
  S1 — обычный ``_final_turn`` final;
  S2 — ``stream_end=True`` с непустым буфером;
  S3 — ``stream_end=True`` с пустым delta;
  S4 — final с потерянным ``origin_message_id`` (только ``answer_id``);
  S5 — финал на сообщении, которое воркер ещё не начал обрабатывать
       (имитация orphan через прямой INSERT в ``processing``).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.integration

_project_root = Path(__file__).resolve().parent.parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


# ---------------------------------------------------------------------------
# DSN / схема
# ---------------------------------------------------------------------------


def _resolve_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or ""
    if dsn:
        return dsn
    try:
        from config import SETTINGS

        pg = (SETTINGS.get("channels") or {}).get("postgres") or {}
        return pg.get("dsn") or ""
    except Exception:
        return ""


def _connect(dsn: str):
    import psycopg2

    return psycopg2.connect(dsn, gssencmode="disable")


def _exec(dsn: str, sql: str, params: list | None = None, fetch: bool = False):
    import psycopg2.extras

    conn = _connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or [])
            rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


_MSG_DDL = """
CREATE TABLE IF NOT EXISTS "{schema}".agent_conversation_messages (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    chat_id TEXT,
    user_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    media JSONB DEFAULT '[]'::jsonb,
    metadata JSONB DEFAULT '{{}}'::jsonb,
    reply_to UUID,
    buttons JSONB DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
)
"""


@pytest.fixture(scope="module")
def test_schema():
    dsn = _resolve_dsn()
    if not dsn:
        pytest.skip("DATABASE_URL не задан; integration-тест пропущен")
    schema = f"test_pg_lifecycle_{uuid.uuid4().hex[:8]}"
    _exec(dsn, f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    _exec(dsn, _MSG_DDL.format(schema=schema))
    yield dsn, schema
    _exec(dsn, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _insert_user(dsn, schema, chat_id, content, status="pending"):
    """Вставить user-сообщение и вернуть ``id``."""
    rows = _exec(
        dsn,
        f"""
        INSERT INTO "{schema}".agent_conversation_messages
        (chat_id, user_id, role, content, status)
        VALUES (%s, %s, 'user', %s, %s)
        RETURNING id
        """,
        [chat_id, chat_id, content, status],
        fetch=True,
    )
    return str(rows[0]["id"])


def _insert_assistant(dsn, schema, chat_id, reply_to, content="", status="processing"):
    rows = _exec(
        dsn,
        f"""
        INSERT INTO "{schema}".agent_conversation_messages
        (chat_id, user_id, role, content, reply_to, status)
        VALUES (%s, %s, 'assistant', %s, %s, %s)
        RETURNING id
        """,
        [chat_id, chat_id, content, str(reply_to), status],
        fetch=True,
    )
    return str(rows[0]["id"])


def _row(dsn, schema, table, msg_id):
    rows = _exec(
        dsn,
        f'SELECT status, content FROM "{schema}".{table} WHERE id = %s',
        [msg_id],
        fetch=True,
    )
    return rows[0] if rows else None


def _make_channel(test_schema, max_concurrent=1):
    dsn, schema = test_schema
    from lib.channels.postgres_channel import PostgresChannel
    from nanobot.bus.queue import MessageBus

    ch = PostgresChannel(
        {
            "dsn": dsn,
            "schema": schema,
            "table_name": "agent_conversation_messages",
            "claims_table": "agent_worker_claims_disabled",
            "poll_interval": 0.1,
            "flush_interval": 60.0,
            "max_concurrent": max_concurrent,
            "processing_timeout": 60,
            "claim_strategy": "single",
            "unstick_interval": 999.0,
        },
        MessageBus(),
    )
    ch._claims_table = "agent_worker_claims_disabled"
    ch._fq_claims = f"{schema}.agent_worker_claims_disabled"
    return ch


def _simulate_claim(ch, user_msg_id, chat_id, assistant_msg_id):
    """Поднять локальное состояние воркера как будто он только что взял задачу."""
    ch.exchange.add_inflight(user_msg_id)
    ch._chat_inflight.add(chat_id)
    ch._msg_chat[user_msg_id] = chat_id
    ch._msg_ctx[user_msg_id] = {"assistant_msg_id": assistant_msg_id}


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


def _msg(content="Final answer", chat_id="chat-x", **meta):
    from nanobot.bus.events import OutboundMessage

    return OutboundMessage(
        channel="postgres",
        chat_id=chat_id,
        content=content,
        media=[],
        metadata=meta,
        buttons=[],
    )


async def _run_turn(ch, ds, schema, user_msg_id, chat_id, assistant_msg_id, finalizer):
    """Симулировать оборот: claim → finalizer → проверка состояния."""
    _simulate_claim(ch, user_msg_id, chat_id, assistant_msg_id)
    await finalizer(ch, user_msg_id, chat_id, assistant_msg_id)

    # локальное состояние очищено
    assert user_msg_id not in ch._msg_ctx
    assert user_msg_id not in ch.exchange.inflight
    assert user_msg_id not in ch._msg_chat
    assert chat_id not in ch._chat_inflight

    # в БД user → completed, assistant → completed с контентом
    u = _row(ds, schema, "agent_conversation_messages", user_msg_id)
    a = _row(ds, schema, "agent_conversation_messages", assistant_msg_id)
    assert u["status"] == "completed", u
    assert a["status"] == "completed", a


async def test_s1_regular_final(test_schema):
    """S1: обычный ``_final_turn`` финал."""
    ds, schema = test_schema
    ch = _make_channel(test_schema)

    user_id = _insert_user(ds, schema, "chat-s1", "Q1")
    assistant_id = _insert_assistant(ds, schema, "chat-s1", user_id)

    async def fin(ch, user_msg_id, chat_id, assistant_msg_id):
        await ch.send(_msg(
            content="Answer S1",
            chat_id=chat_id,
            origin_message_id=user_msg_id,
            answer_id=assistant_msg_id,
            _final_turn=True,
        ))

    await _run_turn(ch, ds, schema, user_id, "chat-s1", assistant_id, fin)
    assert _row(ds, schema, "agent_conversation_messages", assistant_id)["content"] == "Answer S1"


async def test_s2_streaming_final_with_buffer(test_schema):
    """S2: стрим + ``stream_end=True`` с накопленным буфером."""
    ds, schema = test_schema
    ch = _make_channel(test_schema)

    user_id = _insert_user(ds, schema, "chat-s2", "Q2")
    assistant_id = _insert_assistant(ds, schema, "chat-s2", user_id)

    async def fin(ch, user_msg_id, chat_id, assistant_msg_id):
        await ch.send_delta(chat_id, "Hello ", {
            "_stream_id": "s-2", "origin_message_id": user_msg_id,
            "answer_id": assistant_msg_id,
        })
        await ch.send_delta(chat_id, "world", {
            "_stream_id": "s-2", "origin_message_id": user_msg_id,
            "answer_id": assistant_msg_id,
        })
        await ch.send_delta(chat_id, "", {
            "_stream_end": True, "_stream_id": "s-2",
            "origin_message_id": user_msg_id, "answer_id": assistant_msg_id,
        })

    await _run_turn(ch, ds, schema, user_id, "chat-s2", assistant_id, fin)
    a = _row(ds, schema, "agent_conversation_messages", assistant_id)
    assert a["content"] == "Hello world"


async def test_s3_stream_end_empty_delta(test_schema):
    """S3: ``stream_end=True`` с пустым delta. Раньше ломалось."""
    ds, schema = test_schema
    ch = _make_channel(test_schema)

    user_id = _insert_user(ds, schema, "chat-s3", "Q3")
    assistant_id = _insert_assistant(ds, schema, "chat-s3", user_id)

    async def fin(ch, user_msg_id, chat_id, assistant_msg_id):
        # Никаких delta-чанков: сразу stream_end с пустым delta.
        await ch.send_delta(chat_id, "", {
            "_stream_end": True, "_stream_id": "s-3",
            "origin_message_id": user_msg_id, "answer_id": assistant_msg_id,
        })

    await _run_turn(ch, ds, schema, user_id, "chat-s3", assistant_id, fin)


async def test_s4_final_with_only_answer_id(test_schema):
    """S4: финал без ``origin_message_id``, только ``answer_id``.

    Канал должен восстановить user_id через SELECT assistant.reply_to.
    """
    ds, schema = test_schema
    ch = _make_channel(test_schema)

    user_id = _insert_user(ds, schema, "chat-s4", "Q4")
    assistant_id = _insert_assistant(ds, schema, "chat-s4", user_id)

    async def fin(ch, user_msg_id, chat_id, assistant_msg_id):
        # Без origin_message_id/message_id!
        await ch.send(_msg(
            content="Recovered answer",
            chat_id=chat_id,
            answer_id=assistant_msg_id,
            _final_turn=True,
        ))

    await _run_turn(ch, ds, schema, user_id, "chat-s4", assistant_id, fin)


async def test_s5_polling_continues_after_finishes(test_schema):
    """S5: после серии финалов polling не зависает, новые задачи берутся.

    Это главный acceptance criterion: ``exchange.inflight`` пуст после
    любого финала, поэтому следующий ``_poll_once`` возьмёт новую задачу.
    """
    ds, schema = test_schema
    ch = _make_channel(test_schema, max_concurrent=1)

    scenarios = ["Q1", "Q2", "Q3", "Q4"]

    user_ids = []
    assistant_ids = []
    for content in scenarios:
        u = _insert_user(ds, schema, f"chat-{content}", content)
        a = _insert_assistant(ds, schema, f"chat-{content}", u)
        user_ids.append(u)
        assistant_ids.append(a)

    async def fin_regular(ch, u, c, a):
        await ch.send(_msg(
            content="A", chat_id=c,
            origin_message_id=u, answer_id=a, _final_turn=True,
        ))

    async def fin_stream(ch, u, c, a):
        await ch.send_delta(c, "streamed", {
            "_stream_id": f"s-{u}", "origin_message_id": u, "answer_id": a,
        })
        await ch.send_delta(c, "", {
            "_stream_end": True, "_stream_id": f"s-{u}",
            "origin_message_id": u, "answer_id": a,
        })

    async def fin_empty_stream(ch, u, c, a):
        await ch.send_delta(c, "", {
            "_stream_end": True, "_stream_id": f"s-{u}",
            "origin_message_id": u, "answer_id": a,
        })

    finalizers = [fin_regular, fin_stream, fin_empty_stream, fin_regular]
    for content, u, a, fin in zip(scenarios, user_ids, assistant_ids, finalizers):
        chat = f"chat-{content}"
        await _run_turn(ch, ds, schema, u, chat, a, fin)

    # inflight пуст — следующий poll возьмёт новую задачу
    assert ch.exchange.inflight == set()
    assert ch._msg_ctx == {}
    assert ch._msg_chat == {}
    assert ch._chat_inflight == set()

    # Новая задача берётся в работу
    new_user = _insert_user(ds, schema, "chat-next", "Q-next")
    row = await ch._claim_one_single()
    assert row is not None
    assert str(row["id"]) == new_user


# ---------------------------------------------------------------------------
# S6 — полный poll-цикл через MessageExchange (главный acceptance criterion)
# ---------------------------------------------------------------------------


async def test_s6_full_poll_loop_with_max_concurrent_2(test_schema):
    """Главный критерий приёмки фикса lifecycle deadlock.

    Сценарий:
      * ``max_concurrent=2`` — воркер обрабатывает 2 задачи параллельно;
      * вставлены 5 ``pending`` задач (chat-1 .. chat-5, разные чаты);
      * поднимаем ``MessageExchange`` с реальным ``_poll_once``;
      * инжектим мок-обработчик ``_handle_message``, который для каждой
        user-задачи немедленно отправляет финальный outbound через
        ``ch.send(_final_turn=True)`` (имитация быстрого агента);
      * ждём, пока все 5 дойдут до ``completed``.

    Acceptance criterion (из плана):
      > После любого количества завершённых/ошибочных задач агент
      > продолжает принимать новые вопросы без перезапуска процесса.
    """
    from nanobot.bus.events import OutboundMessage

    ds, schema = test_schema
    ch = _make_channel(test_schema, max_concurrent=2)

    # 1. Вставить 5 user-задач в разных чатах.
    n_questions = 5
    user_ids: list[str] = []
    for i in range(n_questions):
        u = _insert_user(ds, schema, f"chat-loop-{i}", f"Q{i}")
        user_ids.append(u)

    # 2. Подменить _handle_message: вместо реальной отправки в шину —
    #    дождаться assistant-placeholder и сделать финал через ch.send.
    seen: list[str] = []

    async def fake_handle_message(
        sender_id: str,
        chat_id: str,
        content: str,
        media: list,
        metadata: dict,
    ) -> None:
        # Здесь мы — «агент». Дождёмся assistant-placeholder (он создаётся
        # в _poll_once до вызова _handle_message) и сразу финализируем.
        user_msg_id = metadata.get("message_id") or metadata.get("answer_id")
        # Подождём, пока _poll_once запишет ctx.
        for _ in range(50):
            if user_msg_id in ch._msg_ctx:
                break
            await asyncio.sleep(0.02)
        assistant_id = (ch._msg_ctx.get(user_msg_id) or {}).get(
            "assistant_msg_id"
        )
        assert assistant_id, (
            f"assistant placeholder not found for {user_msg_id}"
        )
        seen.append(user_msg_id)
        final = OutboundMessage(
            channel="postgres",
            chat_id=chat_id,
            content=f"Answer to {content}",
            media=[],
            metadata={
                "origin_message_id": user_msg_id,
                "answer_id": assistant_id,
                "_final_turn": True,
            },
            buttons=[],
        )
        await ch.send(final)

    ch._handle_message = fake_handle_message

    # 3. Запустить MessageExchange (полный цикл: poll + dispatch + finalize).
    await ch.start()

    # 4. Ждём, пока все 5 задач дойдут до completed.
    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        done = sum(
            1
            for uid in user_ids
            if _row(ds, schema, "agent_conversation_messages", uid)["status"]
            == "completed"
        )
        if done == n_questions:
            break
        await asyncio.sleep(0.1)
    else:
        statuses = [
            _row(ds, schema, "agent_conversation_messages", uid)["status"]
            for uid in user_ids
        ]
        raise AssertionError(
            f"timeout: only {done}/{n_questions} completed, statuses={statuses}"
        )

    await ch.stop()

    # 5. Acceptance criterion: всё локальное состояние пусто.
    assert ch.exchange.inflight == set(), (
        f"inflight не пуст: {ch.exchange.inflight}"
    )
    assert ch._msg_ctx == {}
    assert ch._msg_chat == {}
    assert ch._chat_inflight == set()
    assert ch._leases == set()

    # 6. Видим, что обработчик реально дёргался для всех 5.
    assert sorted(seen) == sorted(user_ids)

    # 7. Вставка новой задачи в pending: poll её поднимет немедленно
    #    (доказывает, что воркер продолжает работать, а не висит).
    new_user = _insert_user(ds, schema, "chat-after-loop", "Q-after-loop")
    # Небольшая пауза, чтобы _poll_loop успел сделать тик
    deadline = asyncio.get_event_loop().time() + 3.0
    await ch.start()  # второй start для следующего цикла
    while asyncio.get_event_loop().time() < deadline:
        row = _row(
            ds, schema, "agent_conversation_messages", new_user,
        )
        if row["status"] != "pending":
            break
        await asyncio.sleep(0.1)
    await ch.stop()
    final = _row(ds, schema, "agent_conversation_messages", new_user)
    assert final["status"] == "completed", (
        f"новая задача не была поднята поллом, status={final['status']}"
    )
