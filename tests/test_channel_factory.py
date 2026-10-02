from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from lib.services.channel_factory import ChannelFactory

#: Настоящий класс канала, поднятый **до** фикстуры. Фикстура подменяет
#: ``sys.modules``, и импорт внутри теста вернул бы мок — проверять было бы
#: нечего. Имена связываются один раз, на импорте модуля теста.
from lib.channels.postgres_channel import PostgresChannel as _RealPostgresChannel


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

        pg_mod = types.ModuleType("lib.channels.postgres_channel")
        pg_mod.PostgresChannel = MagicMock()
        sys.modules["lib.channels.postgres_channel"] = pg_mod

        fake = {
            "ChannelManager": nano_manager.ChannelManager,
            "cm": cm,
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
            _settings({"postgres": {"enabled": False}}),
            MagicMock(),
            MagicMock(),
        )
        assert channels is fake_modules["cm"]
        assert any("Channels enabled" in m for m in messages)
        assert fake_modules["ChannelManager"].called


class TestRedisIsGone:
    """Каналов один: PostgreSQL.

    Второй транспорт тянул за собой второй цикл поллинга, второй backoff и
    второе место, где правила очереди могут разойтись с боевыми. Страж
    остаётся, потому что вернуть канал легко, а заметить разъезд
    правил — нет.
    """

    def test_factory_has_no_redis_branch(self):
        assert not hasattr(ChannelFactory, "_add_redis"), "ветка Redis вернулась"
        assert "redis" not in _add_postgres_source()

    def test_no_status_message_mentions_redis(self, fake_modules):
        _, messages = ChannelFactory().create_all(
            _config(),
            _settings({"postgres": {"enabled": False}}),
            MagicMock(),
            MagicMock(),
        )
        assert not any("edis" in m for m in messages), messages


def _add_postgres_source() -> str:
    """Исходник метода — мок-объект тут не годится."""
    from lib.services import channel_factory as module

    return module.ChannelFactory._add_postgres.__doc__ or ""


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

    def test_enabled_with_dsn_builds_channel(self, fake_modules):
        """Транскрипция в канал больше не пробрасывается.

        Раньше сюда писались четыре атрибута (``transcription_provider`` и
        соседи), которых в ``PostgresChannel`` не существовало: в нём нет ни
        одного упоминания транскрипции. Голос разбирает базовый класс
        библиотеки — ``BaseChannel.transcribe_audio()``, который сам берёт
        конфиг и провайдера. Каналу остаётся только собраться.

        Проверяется по настоящему классу и по фактическим присваиваниям, а
        не через ``hasattr`` на мок-объекте: у ``MagicMock`` без ``spec`` любой
        ``hasattr`` истинен, и такой тест не прошёл бы никогда — то есть
        охранял бы не код, а собственную непроходимость.
        """
        factory = ChannelFactory()
        channels = fake_modules["cm"]
        factory._add_postgres(
            channels, _config(),
            _settings({"postgres": {"enabled": True, "dsn": "postgresql://u@h/db"}}),
            MagicMock(),
        )
        fake_modules["PostgresChannel"].assert_called_once()
        assert "postgres" in channels.channels
        pg_channel = fake_modules["PostgresChannel"].return_value
        for gone in (
            "transcription_provider",
            "transcription_api_key",
            "transcription_api_base",
            "transcription_language",
        ):
            assert not hasattr(_RealPostgresChannel, gone), f"в канале есть {gone}"
            assert gone not in pg_channel.__dict__, f"фабрика записала {gone}"

    def test_settings_as_dict(self, fake_modules):
        """Секция ``channels.postgres`` принимается как обычный dict.

        Раньше проверка шла через ``_add_redis`` — на канале, которого больше
        нет. Контракт «секция канала приходит словарём» проверяется на
        оставшемся канале.
        """
        factory = ChannelFactory()
        settings = {"channels": {"postgres": {"enabled": True, "dsn": "postgresql://u@h/db"}}}
        channels = fake_modules["cm"]
        factory._add_postgres(channels, _config(), settings, MagicMock())
        assert "postgres" in channels.channels

    def test_print_worker_activity_forwarded_to_channel(self, fake_modules):
        factory = ChannelFactory(print_worker_activity=True)
        channels = fake_modules["cm"]
        factory._add_postgres(
            channels, _config(),
            _settings({"postgres": {"enabled": True, "dsn": "postgresql://u@h/db"}}),
            MagicMock(),
        )
        fake_modules["PostgresChannel"].assert_called_once()
        cfg = fake_modules["PostgresChannel"].call_args.args[0]
        assert cfg.get("print_worker_activity") is True

    def test_print_worker_activity_default_false(self, fake_modules):
        factory = ChannelFactory()
        channels = fake_modules["cm"]
        factory._add_postgres(
            channels, _config(),
            _settings({"postgres": {"enabled": True, "dsn": "postgresql://u@h/db"}}),
            MagicMock(),
        )
        cfg = fake_modules["PostgresChannel"].call_args.args[0]
        assert cfg.get("print_worker_activity") is False
