"""ChannelFactory — создание и настройка каналов связи.

Перенесено из gateway.py:

  * ``ChannelManager`` (стандартные каналы nanobot: Telegram, Slack и т.д.);
  * Postgres-канал по секции ``settings.channels.postgres``.

Redis-канал снят: целевая архитектура — один канал, PostgreSQL. Второй
транспорт означал бы второй цикл поллинга, вторую оркестрацию оборота и
второе место, где правила очереди могут разойтись с боевыми.

Голос разбирает базовый класс библиотеки (``BaseChannel.transcribe_audio()``):
собственного сервиса транскрипции у канала нет и не нужно — тот пробрасывал
четыре атрибута, которых в ``PostgresChannel`` не существовало.

``create_all`` возвращает ``(ChannelManager, messages)`` — список статусных
сообщений для вывода вызывающей стороной (Rich-консоль).
"""

from __future__ import annotations

from typing import Any

from lib.utils.node_access import get_settings_section as _section


class ChannelFactory:
    """Фабрика каналов: стандартные (Telegram/Slack/...) + Postgres.

    Стандартные каналы nanobot регистрируются самим ``ChannelManager``
    на основе ``config.channels.<name>.enabled``. Эта фабрика добавляет
    Postgres, который стандартным каналом nanobot не является
    (наш проектный код).

    Attributes:
        _print_worker_activity: печать активности воркеров канала.
    """

    def __init__(
        self,
        print_worker_activity: bool = False,
        db_logging_service: Any | None = None,
    ) -> None:
        self._print_worker_activity = print_worker_activity
        self._db_logging_service = db_logging_service

    def create_all(
        self,
        config: Any,
        settings: Any,
        bus: Any,
        session_manager: Any,
    ) -> tuple[Any, list[str]]:
        """Создать и настроить все каналы.

        Args:
            config: runtime-конфиг nanobot (для ``config.channels.send_progress``
                и других настроек вывода, общих для всех каналов).
            settings: ``SETTINGS`` (для ``channels.postgres.*``).
            bus: ``MessageBus`` (все каналы публикуют сюда).
            session_manager: менеджер сессий — всегда класс библиотеки
                ``nanobot.session.manager.SessionManager`` (у нас поверх
                ``SanitizingSessionStore``); пробрасывается в ``ChannelManager``
                для сохранения истории сообщений.

        Returns:
            ``(channels, messages)`` — менеджер каналов и список
            статусных сообщений для вывода в консоль. Каждое сообщение
            уже содержит Rich-разметку (``[green]✓[/green]`` и т.п.).
        """
        from nanobot.channels.manager import ChannelManager

        channels = ChannelManager(config, bus, session_manager=session_manager)
        messages: list[str] = []

        messages.extend(self._add_postgres(channels, config, settings, bus))
        messages.append(
            f"[green]✓[/green] Channels enabled: "
            f"{', '.join(channels.enabled_channels)}"
        )
        return channels, messages

    # ------------------------------------------------------------------
    # Postgres
    # ------------------------------------------------------------------

    def _add_postgres(
        self, channels: Any, config: Any, settings: Any, bus: Any,
    ) -> list[str]:
        """Зарегистрировать Postgres-канал (если включён в ``settings.channels.postgres``).

        Канал поверх таблицы ``agent_conversation_messages``: агент отвечает,
        записывая строку в таблицу. Это основной способ интеграции с
        внешними бизнес-процессами, а также с web-UI
        (клиент читает эту таблицу и рендерит ответы в UI).

        Поведение:
          * ``enabled=False`` (по умолчанию в config.json) — no-op;
          * ``enabled=True`` + есть ``dsn`` — создаёт ``PostgresChannel``
            и пробрасывает ему настройки вывода;
          * ``enabled=True`` + нет ``dsn`` — сообщение об ошибке в
            консоль, канал НЕ создаётся (это явная ошибка конфига).
        """
        pg = _section(settings, "channels").get("postgres", {})
        if not pg.get("enabled", False):
            return ["[dim]PostgreSQL channel disabled[/dim]"]

        from lib.channels.postgres_channel import PostgresChannel

        dsn = pg.get("dsn", "")
        if not dsn:
            return [
                "[red]✗[/red] PostgresChannel enabled but no DSN "
                "(channels.postgres.dsn)"
            ]

        ch_cfg = {
            "enabled": True,
            "dsn": dsn,
            "schema": pg.get("schema", "public"),
            "table_name": pg.get("table_name", ""),
            "poll_interval": pg.get("poll_interval", 2.0),
            "flush_interval": pg.get("flush_interval", 2.0),
            "max_concurrent": pg.get("max_concurrent", 1),
            "processing_timeout": pg.get("processing_timeout", 120),
            "allow_from": pg.get("allow_from", ["*"]),
            "print_worker_activity": self._print_worker_activity,
        }
        pg_channel = PostgresChannel(
            ch_cfg, bus, db_logging_service=self._db_logging_service,
        )
        pg_channel.send_progress = config.channels.send_progress
        pg_channel.send_tool_hints = config.channels.send_tool_hints
        pg_channel.show_reasoning = config.channels.show_reasoning
        channels.channels["postgres"] = pg_channel
        return ["[green]✓[/green] PostgreSQL channel enabled"]
