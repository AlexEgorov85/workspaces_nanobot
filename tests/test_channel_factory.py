from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from lib.services.channel_factory import ChannelFactory


@pytest.fixture
def fake_modules():
    """Подменяем модули каналов (импортируются лениво внутри фабрики)."""
    with patch.dict("sys.modules"):
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

        fake = {
            "ChannelManager": nano_manager.ChannelManager,
            "cm": cm,
            "RedisChannel": redis_mod.RedisChannel,
            "PostgresChannel": pg_mod.PostgresChannel,
        }
        yield fake


def _settings(channels):
    return type("Settings", (), {"channels": channels})()


def _config():
    cfg = MagicMock()
    cfg.channels.send_progress = True
    cfg.channels.send_tool_hints = False
    cfg.channels.show_reasoning = True
    return cfg


class TestCreateAll:
    def test_returns_manager_and_messages(self, fake_modules):
        factory = ChannelFactory()
        channels, messages = factory.create_all(
            _config(),
            _settings({"redis": {"enabled": False}, "postgres": {"enabled": False}}),
            MagicMock(),
            MagicMock(),
        )
        assert channels is fake_modules["cm"]
        assert any("Channels enabled" in m for m in messages)
        assert fake_modules["ChannelManager"].called


class TestRedis:
    def test_disabled(self, fake_modules):
        factory = ChannelFactory()
        messages = factory._add_redis(
            fake_modules["cm"], _config(),
            _settings({"redis": {"enabled": False}}), MagicMock(),
        )
        assert any("disabled" in m for m in messages)
        fake_modules["RedisChannel"].assert_not_called()

    def test_enabled_registers(self, fake_modules):
        factory = ChannelFactory()
        channels = fake_modules["cm"]
        factory._add_redis(
            channels, _config(),
            _settings({"redis": {"enabled": True, "host": "1.2.3.4"}}), MagicMock(),
        )
        fake_modules["RedisChannel"].assert_called_once()
        assert "redis" in channels.channels


class TestPostgres:
    def test_disabled(self, fake_modules):
        factory = ChannelFactory()
        messages = factory._add_postgres(
            fake_modules["cm"], _config(),
            _settings({"postgres": {"enabled": False}}), MagicMock(),
        )
        assert any("disabled" in m for m in messages)
        fake_modules["PostgresChannel"].assert_not_called()

    def test_enabled_without_dsn_errors(self, fake_modules):
        factory = ChannelFactory()
        messages = factory._add_postgres(
            fake_modules["cm"], _config(),
            _settings({"postgres": {"enabled": True, "dsn": ""}}), MagicMock(),
        )
        assert any("no DSN" in m for m in messages)
        fake_modules["PostgresChannel"].assert_not_called()

    def test_enabled_with_dsn_configures_transcription(self, fake_modules):
        transcription = MagicMock()
        transcription.provider = "groq"
        transcription.get_api_key.return_value = "gsk-key"
        transcription.get_base_url.return_value = "https://api.groq.com/v1"
        transcription.get_language.return_value = "ru"

        factory = ChannelFactory(transcription=transcription)
        channels = fake_modules["cm"]
        factory._add_postgres(
            channels, _config(),
            _settings({"postgres": {"enabled": True, "dsn": "postgresql://u@h/db"}}),
            MagicMock(),
        )
        fake_modules["PostgresChannel"].assert_called_once()
        assert "postgres" in channels.channels
        pg_channel = fake_modules["PostgresChannel"].return_value
        assert pg_channel.transcription_provider == "groq"
        assert pg_channel.transcription_api_key == "gsk-key"
        assert pg_channel.transcription_api_base == "https://api.groq.com/v1"
        assert pg_channel.transcription_language == "ru"

    def test_settings_as_dict(self, fake_modules):
        factory = ChannelFactory()
        settings = {"channels": {"redis": {"enabled": True}}}
        channels = fake_modules["cm"]
        factory._add_redis(channels, _config(), settings, MagicMock())
        assert "redis" in channels.channels
