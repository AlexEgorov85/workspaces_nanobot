"""ChannelFactory — создание и настройка всех каналов связи.

Перенесено из gateway.py:

  * ``ChannelManager`` (стандартные каналы nanobot: Telegram, Slack и т.д.);
  * Redis-канал по секции ``settings.channels.redis``;
  * Postgres-канал по секции ``settings.channels.postgres`` + настройка
    транскрипции через ``TranscriptionService``.

``create_all`` возвращает ``(ChannelManager, messages)`` — список статусных
сообщений для вывода вызывающей стороной (Rich-консоль).
"""

from __future__ import annotations

from typing import Any

from lib.utils.node_access import get_settings_section as _section


class ChannelFactory:
    """Фабрика каналов: стандартные (Telegram/Slack/...) + Redis + Postgres.

    Стандартные каналы nanobot регистрируются самим ``ChannelManager``
    на основе ``config.channels.<name>.enabled``. Эта фабрика добавляет
    Redis/Postgres, которые НЕ являются стандартными каналами nanobot
    (наш проектный код).

    Attributes:
        _transcription: ``TranscriptionService`` или ``None``. Передаётся
            в ``PostgresChannel`` для транскрипции голосовых сообщений.
    """

    def __init__(
        self,
        transcription: Any | None = None,
        print_worker_activity: bool = False,
    ) -> None:
        self._transcription = transcription
        self._print_worker_activity = print_worker_activity

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
            settings: ``SETTINGS`` (для ``channels.redis.*``/``channels.postgres.*``).
            bus: ``MessageBus`` (все каналы публикуют сюда).
            session_manager: ``PGSessionManager``/``SessionManager`` —
                пробрасывается в ``ChannelManager`` для сохранения
                истории сообщений.

        Returns:
            ``(channels, messages)`` — менеджер каналов и список
            статусных сообщений для вывода в консоль. Каждое сообщение
            уже содержит Rich-разметку (``[green]✓[/green]`` и т.п.).
        """
        from nanobot.channels.manager import ChannelManager

        channels = ChannelManager(config, bus, session_manager=session_manager)
        messages: list[str] = []

        messages.extend(self._add_redis(channels, config, settings, bus))
        messages.extend(self._add_postgres(channels, config, settings, bus))
        messages.append(
            f"[green]✓[/green] Channels enabled: "
            f"{', '.join(channels.enabled_channels)}"
        )
        return channels, messages

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------

    def _add_redis(
        self, channels: Any, config: Any, settings: Any, bus: Any,
    ) -> list[str]:
        """Зарегистрировать Redis-канал (если включён в ``settings.channels.redis``).

        Канал поверх Redis pub/sub: читает сообщения из ``incoming_key``
        (BRPOP) и публикует ответы в ``outgoing_prefix:{chat_id}``
        (LPUSH). Это позволяет интегрировать nanobot с внешними
        системами через Redis (например, для веб-чатов).

        Если ``enabled=False`` (по умолчанию) — no-op, возвращает
        ``"[dim]Redis channel disabled[/dim]"`` для консоли. Если
        ``enabled=True`` — создаёт ``RedisChannel``, пробрасывает
        настройки вывода (progress/tool_hints/reasoning) и
        регистрирует в ``channels.channels["redis"]``.
        """
        rs = _section(settings, "channels").get("redis", {})
        if not rs.get("enabled", False):
            return ["[dim]Redis channel disabled[/dim]"]

        from lib.channels.redis_channel import RedisChannel

        redis_cfg = {
            "enabled": True,
            "host": rs.get("host", "127.0.0.1"),
            "port": rs.get("port", 6379),
            "db": rs.get("db", 0),
            "password": rs.get("password"),
            "incoming_key": rs.get("incoming_key", "nanobot:inbox"),
            "outgoing_prefix": rs.get("outgoing_prefix", "nanobot:outbox"),
            "poll_timeout": rs.get("poll_timeout", 5.0),
            "max_concurrent": rs.get("max_concurrent", 1),
            "allow_from": rs.get("allow_from", ["*"]),
        }
        redis_channel = RedisChannel(redis_cfg, bus)
        redis_channel.send_progress = config.channels.send_progress
        redis_channel.send_tool_hints = config.channels.send_tool_hints
        redis_channel.show_reasoning = config.channels.show_reasoning
        channels.channels["redis"] = redis_channel
        return ["[green]✓[/green] Redis channel enabled"]

    # ------------------------------------------------------------------
    # Postgres
    # ------------------------------------------------------------------

    def _add_postgres(
        self, channels: Any, config: Any, settings: Any, bus: Any,
    ) -> list[str]:
        """Зарегистрировать Postgres-канал (если включён в ``settings.channels.postgres``).

        Канал поверх таблицы ``agent_conversation_messages``: агент отвечает,
        записывая строку в таблицу. Это основной способ интеграции с
        внешними бизнес-процессами, а также с Streamlit UI
        (см. ``streamlit_app.py`` — он полит эту таблицу и рендерит
        ответы в UI).

        Поведение:
          * ``enabled=False`` (по умолчанию в config.json) — no-op;
          * ``enabled=True`` + есть ``dsn`` — создаёт ``PostgresChannel``,
            пробрасывает настройки вывода + ``TranscriptionService``
            (для распознавания голосовых вложений);
          * ``enabled=True`` + нет ``dsn`` — сообщение об ошибке в
            консоль, канал НЕ создаётся (это явная ошибка конфига).

        ``claim_strategy`` (``channels.postgres.claim_strategy``) управляет
        режимом аренды задач:
          * ``"single"`` (дефолт) — один инстанс, без ``agent_worker_claims``;
          * ``"worker_pool"`` — мульти-машинный пул с lease/heartbeat.
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
            "claim_strategy": pg.get("claim_strategy", "single"),
        }
        pg_channel = PostgresChannel(ch_cfg, bus)
        if self._transcription is not None:
            pg_channel.transcription_provider = self._transcription.provider
            pg_channel.transcription_api_key = self._transcription.get_api_key()
            pg_channel.transcription_api_base = self._transcription.get_base_url()
            pg_channel.transcription_language = self._transcription.get_language()
        pg_channel.send_progress = config.channels.send_progress
        pg_channel.send_tool_hints = config.channels.send_tool_hints
        pg_channel.show_reasoning = config.channels.show_reasoning
        channels.channels["postgres"] = pg_channel
        return ["[green]✓[/green] PostgreSQL channel enabled"]
