from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lib.channels.redis_channel import RedisChannel


def _make_channel(**overrides):
    config = {
        "host": "127.0.0.1",
        "port": 6379,
        "db": 0,
        "incoming_key": "nanobot:inbox",
        "outgoing_prefix": "nanobot:outbox",
        "poll_timeout": 1.0,
        "max_concurrent": 1,
    }
    config.update(overrides)
    bus = MagicMock()
    return RedisChannel(config, bus)


class TestRedisChannelInit:
    def test_defaults(self):
        ch = _make_channel()
        assert ch._host == "127.0.0.1"
        assert ch._port == 6379
        assert ch._db == 0
        assert ch._incoming_key == "nanobot:inbox"
        assert ch._outgoing_prefix == "nanobot:outbox"
        assert ch._max_concurrent == 1
        assert ch._poll_timeout == 1.0
        assert ch._semaphore._value == 1

    def test_custom_config(self):
        ch = _make_channel(
            host="10.0.0.1",
            port=6380,
            db=2,
            incoming_key="my:inbox",
            outgoing_prefix="my:outbox",
            max_concurrent=5,
            poll_timeout=10.0,
        )
        assert ch._host == "10.0.0.1"
        assert ch._port == 6380
        assert ch._db == 2
        assert ch._max_concurrent == 5
        assert ch._poll_timeout == 10.0

    def test_default_config(self):
        cfg = RedisChannel.default_config()
        assert cfg["enabled"] is True
        assert cfg["host"] == "127.0.0.1"
        assert cfg["port"] == 6379
        assert cfg["incoming_key"] == "nanobot:inbox"

    def test_password_none(self):
        ch = _make_channel(password=None)
        assert ch._password is None

    def test_password_set(self):
        ch = _make_channel(password="secret123")
        assert ch._password == "secret123"


class TestRedisChannelSend:
    @pytest.mark.asyncio
    async def test_noop_when_not_connected(self):
        ch = _make_channel()
        ch._redis = None
        msg = MagicMock()
        msg.metadata = {}
        msg.channel = "redis"
        msg.chat_id = "chat-1"
        msg.content = "Hi"
        msg.media = []
        msg.buttons = []
        msg.reply_to = None

        await ch.send(msg)  # should not raise

    @pytest.mark.asyncio
    async def test_skips_reasoning_delta(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        msg = MagicMock()
        msg.metadata = {"_reasoning_delta": True}

        await ch.send(msg)
        ch._redis.lpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_reasoning_end(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        msg = MagicMock()
        msg.metadata = {"_reasoning_end": True}

        await ch.send(msg)
        ch._redis.lpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_progress(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        msg = MagicMock()
        msg.metadata = {"_progress": True}

        await ch.send(msg)
        ch._redis.lpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_turn_end(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        msg = MagicMock()
        msg.metadata = {"_turn_end": True}

        await ch.send(msg)
        ch._redis.lpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_to_redis(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        msg = MagicMock()
        msg.metadata = {}
        msg.channel = "redis"
        msg.chat_id = "chat-1"
        msg.content = "Hello"
        msg.media = []
        msg.buttons = []
        msg.reply_to = "ext-msg-1"

        await ch.send(msg)
        ch._redis.lpush.assert_called_once()
        args, _ = ch._redis.lpush.call_args
        assert args[0] == "nanobot:outbox:chat-1"

    @pytest.mark.asyncio
    async def test_reply_to_from_map(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._reply_to_map["chat-1"] = "saved-reply"

        msg = MagicMock()
        msg.metadata = {}
        msg.channel = "redis"
        msg.chat_id = "chat-1"
        msg.content = "Hi"
        msg.media = []
        msg.buttons = []
        msg.reply_to = None

        await ch.send(msg)
        assert "chat-1" not in ch._reply_to_map  # popped

    @pytest.mark.asyncio
    async def test_redis_error_logged(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._redis.lpush.side_effect = Exception("Redis down")
        msg = MagicMock()
        msg.metadata = {}
        msg.channel = "redis"
        msg.chat_id = "chat-1"
        msg.content = "Hi"
        msg.media = []
        msg.buttons = []

        await ch.send(msg)  # should not raise


class TestRedisChannelSendDelta:
    @pytest.mark.asyncio
    async def test_noop(self):
        ch = _make_channel()
        await ch.send_delta("chat-1", "delta")  # should not raise


class TestRedisChannelPollOnce:
    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._redis.brpop.return_value = None
        await ch._poll_once()  # should not raise

    @pytest.mark.asyncio
    async def test_invalid_json_ignored(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._redis.brpop.return_value = ("nanobot:inbox", "not-json")
        await ch._poll_once()  # should not raise

    @pytest.mark.asyncio
    async def test_dispatches_valid_message(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._redis.brpop.return_value = (
            "nanobot:inbox",
            '{"sender_id": "u1", "chat_id": "c1", "content": "hello", "message_id": "m1"}',
        )
        ch._handle_message = AsyncMock()

        await ch._poll_once()
        ch._handle_message.assert_called_once()
        # reply_to is set before dispatch and preserved afterwards
        assert ch._reply_to_map.get("c1") == "m1"

    @pytest.mark.asyncio
    async def test_session_key_override(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._redis.brpop.return_value = (
            "nanobot:inbox",
            '{"sender_id": "u1", "chat_id": "c1", "content": "hi", "session_key_override": "custom-session"}',
        )
        ch._handle_message = AsyncMock()

        await ch._poll_once()
        _, kwargs = ch._handle_message.call_args
        assert kwargs["session_key"] == "custom-session"

    @pytest.mark.asyncio
    async def test_error_in_dispatch_doesnt_crash(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._redis.brpop.return_value = (
            "nanobot:inbox",
            '{"sender_id": "u1", "chat_id": "c1", "content": "hi"}',
        )
        ch._handle_message = AsyncMock(side_effect=Exception("Dispatch error"))

        await ch._poll_once()  # should not raise

    @pytest.mark.asyncio
    async def test_media_list_validation(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._redis.brpop.return_value = (
            "nanobot:inbox",
            '{"sender_id": "u1", "chat_id": "c1", "content": "hi", "media": "not-a-list"}',
        )
        ch._handle_message = AsyncMock()

        await ch._poll_once()  # should not raise, media coerced to []

    @pytest.mark.asyncio
    async def test_metadata_is_dict(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._redis.brpop.return_value = (
            "nanobot:inbox",
            '{"sender_id": "u1", "chat_id": "c1", "content": "hi", "metadata": "not-a-dict"}',
        )
        ch._handle_message = AsyncMock()

        await ch._poll_once()  # should not raise

    @pytest.mark.asyncio
    async def test_reply_to_map_cleanup(self):
        ch = _make_channel()
        ch._redis = AsyncMock()
        ch._redis.brpop.return_value = (
            "nanobot:inbox",
            '{"sender_id": "u1", "chat_id": "c1", "content": "hi", "message_id": "m1"}',
        )
        ch._handle_message = AsyncMock()

        # Fill reply_to_map beyond limit
        for i in range(10005):
            ch._reply_to_map[f"chat-{i}"] = f"msg-{i}"

        await ch._poll_once()
        # Should have cleaned up to 5000 entries
        assert len(ch._reply_to_map) <= 5001  # 5000 + the new one
