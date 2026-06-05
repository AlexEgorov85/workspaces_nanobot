"""
Настройки для gateway-режима nanobot.

=== КАК ПОЛЬЗОВАТЬСЯ =========================================================

 1. Заполни PostgreSQL DSN в pg.dsn, если хочешь использовать БД.
 2. Выбери режим хранения сессий (storage):
      "auto"      — PG если есть dsn, иначе JSONL-файлы
      "postgres"  — принудительно PG (ошибка если dsn пуст)
      "file"      — принудительно JSONL-файлы
 3. Включи нужные каналы в channels (список имён).
 4. Если используешь PostgresChannel — проверь pg.channel.*.
 5. Настрой port для сохранения больших результатов (persist_threshold).
 6. Стандартные настройки агента (модель, провайдер и т.д.) — в config.json.

=== КАК ВКЛЮЧИТЬ / ВЫКЛЮЧИТЬ КАНАЛЫ =========================================

 Способ 1 — через channels (список имён):
   channels = ["websocket", "telegram"]   # запустить только эти каналы
   channels = []                           # запустить все из config.json

 Способ 2 — через config.json:
   В секции channels найти нужный канал и поставить "enabled": true/false.

=== НАСТРОЙКИ POSTGRESQL =====================================================

 Единая строка подключения (pg.dsn) используется всеми PG-компонентами:
   • PGSessionManager — хранение истории сессий (таблицы session_messages,
     session_meta)
   • PostgresChannel — канал для отправки/получения сообщений через БД
     (таблица conversation_messages)

 Если нужно переопределить DSN или схему для конкретного канала — укажи
 их в его секции (pg.channel.dsn, pg.channel.schema). Пустая строка =
 наследовать из корневых pg.dsn / pg.schema.

=== ПРИМЕР: ТОЛЬКО WEBSOCKET, БЕЗ БД ========================================

   channels = ["websocket"]
   storage = "file"
   pg.dsn = ""

=== ПРИМЕР: ПОЛНЫЙ НАБОР С БД ===============================================

   channels = ["websocket", "telegram"]
   storage = "auto"
   pg.dsn = "postgresql://user:pass@localhost:5432/nanobot"
   pg.channel.enabled = True

=== ПРИМЕР: PostgresChannel НА ДРУГОЙ БАЗЕ ===================================

   pg.dsn = "postgresql://user:pass@host1:5432/sessions"
   pg.channel.dsn = "postgresql://user:pass@host2:5432/messages"
   pg.channel.schema = "custom_schema"
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PostgresChannelConfig:
    """PostgresChannel — приём/отправка сообщений через БД.

    ВНИМАНИЕ: этот канал не использует config.json.
    Все настройки — только здесь.

    Как включить:
      1. Поставить enabled = True
      2. Убедиться что pg.dsn (или pg.channel.dsn) заполнен
      3. Указать в channels список имён (или оставить пустым для всех)

    Канал создаёт свою таблицу (table_name) для очереди сообщений.
    Параметры опроса:
      poll_interval     — как часто проверять новые сообщения (сек)
      flush_interval    — как часто сбрасывать исходящие (сек)
      processing_timeout — таймаут обработки одного сообщения (сек)
      max_concurrent    — сколько сообщений обрабатывать одновременно
    """
    enabled: bool = True
    table_name: str = "conversation_messages"
    poll_interval: float = 2.0
    flush_interval: float = 2.0
    max_concurrent: int = 1
    processing_timeout: int = 600

    # Схема и DSN. Пусто = наследовать из корневых pg.dsn / pg.schema.
    dsn: str = ""
    schema: str = ""


@dataclass
class PGSettings:
    """PostgreSQL — корневые параметры подключения.

    dsn — единая строка подключения для всех PG-компонентов.
    schema — схема БД по умолчанию (обычно "public").

    Если указать dsn/schema в секции конкретного канала
    (например pg.channel.dsn), они переопределят корневые.

    ── Сессии (PGSessionManager) ─────────────────────────────────────────
    Хранит историю диалогов в таблицах:
      messages_table — сообщения сессий
      meta_table     — метаданные сессий (модель, токены, статус)

    Пул соединений:
      pool_min_conn / pool_max_conn — мин/макс число соединений
      pool_timeout — таймаут ожидания свободного соединения (сек)
    """
    dsn: str = ""                          # postgresql://user:pass@host:5432/dbname
    schema: str = "public"

    # ── Сессии (PGSessionManager) ──────────────────────────────────────────
    messages_table: str = "session_messages"
    meta_table: str = "session_meta"
    pool_min_conn: int = 1
    pool_max_conn: int = 4
    pool_timeout: float = 5.0

    # ── PostgresChannel ────────────────────────────────────────────────────
    channel: PostgresChannelConfig = field(default_factory=PostgresChannelConfig)


@dataclass
class RedisSettings:
    """Настройки Redis-канала.

    Все параметры — здесь, ничего в config.json не дублируется.

    Как работает канал:
      • Внешняя система кладёт JSON в incoming_key (список).
        Канал забирает его через BRPOP.
      • Ответ пишется в outgoing_prefix:{chat_id} через LPUSH.

    Как включить:
      enabled = True
      channels = ["redis"]   # в GatewaySettings

    Параметры:
      host / port / db / password — подключение к Redis
      incoming_key    — ключ списка для входящих сообщений
      outgoing_prefix — префикс для per-chat outbox-списков
      poll_timeout    — таймаут BRPOP (сек)
      max_concurrent  — сколько сообщений обрабатывать одновременно
      allow_from      — список разрешённых отправителей (* = все)
    """
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: str | None = None
    incoming_key: str = "nanobot:inbox"
    outgoing_prefix: str = "nanobot:outbox"
    poll_timeout: float = 5.0
    max_concurrent: int = 1
    allow_from: list[str] = field(default_factory=lambda: ["*"])


@dataclass
class GatewaySettings:
    """Главный объект настроек. Импортируется в gateway.py как SETTINGS."""

    # ── Каналы ───────────────────────────────────────────────────────────────
    # Какие каналы запустить. Пустой список = все из config.json.
    # Примеры:
    #   ["websocket"]                  — только WebSocket
    #   ["telegram"]                   — только Telegram
    #   ["websocket", "telegram"]      — WebSocket + Telegram
    #   []                             — все, что включены в config.json
    channels: list[str] = field(default_factory=lambda: [
        "websocket",
        "telegram",
    ])

    # ── Хранилище сессий ────────────────────────────────────────────────────
    # "auto"    — PostgreSQL если заполнен pg.dsn, иначе JSONL-файлы
    # "postgres" — принудительно PostgreSQL (ошибка если pg.dsn пуст)
    # "file"     — принудительно JSONL-файлы (даже если есть pg.dsn)
    #
    # JSONL-файлы хранятся в workspace/sessions/.
    storage: str = "auto"

    # ── PostgreSQL ───────────────────────────────────────────────────────────
    pg: PGSettings = field(default_factory=PGSettings)

    # ── Сохранение больших результатов инструментов ─────────────────────────
    # Когда результат вызова инструмента (exec, grep, web_fetch и т.д.)
    # превышает этот размер в байтах, он сохраняется в файл
    # (workspace/data_store/cache/sessions/.../results/),
    # а в историю LLM вместо обрезанного текста идёт короткая ссылка:
    #   [Result saved to data_store/cache/sessions/.../results/...json (12.5 KB)]
    #
    # 0 = не сохранять, оставить стандартное поведение (обрезать в истории).
    persist_threshold: int = 500

    # ── Таймауты (секунды; 0 = без лимита) ──────────────────────────────────
    llm_timeout: float = 300      # Лимит на один LLM-запрос
    exec_timeout: int = 60        # Лимит на выполнение exec-скрипта

    # ── Логирование ─────────────────────────────────────────────────────────
    # "DEBUG"   — подробно, все вызовы инструментов и ответы LLM
    # "INFO"    — основные события (рекомендуется для ежедневного использования)
    # "WARNING" — только ошибки и предупреждения
    # "ERROR"   — только ошибки
    log_level: str = "WARNING"

    # ── Redis-канал ──────────────────────────────────────────────────────────
    redis: 'RedisSettings' = field(default_factory=lambda: RedisSettings())
