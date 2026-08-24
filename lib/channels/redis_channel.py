"""
Redis-канал для nanobot.

Канал работает через Redis-списки (блокирующие очереди):
  • Входящие сообщения читаются BRPOP из inbox-списка
  • Ответы пишутся LPUSH в per-chat outbox-список

═ ПРИНЦИП РАБОТЫ ════════════════════════════════════════════════════

  1. Внешняя система кладёт JSON в ``incoming_key`` (по умолчанию
     ``nanobot:inbox``). Канал забирает его через BRPOP.
  2. Канал создаёт InboundMessage и отправляет в шину → агенту.
  3. Агент формирует ответ → OutboundMessage → канал.
  4. Канал кладёт JSON-ответ в ``outgoing_prefix:{chat_id}``
     (например ``nanobot:outbox:chat_123``).

═ ФОРМАТ ВХОДЯЩЕГО СООБЩЕНИЯ ═════════════════════════════════════════

  Ключ в Redis:    ``incoming_key`` (по умолчанию ``nanobot:inbox``)
  Тип операции:    BRPOP (блокирующее чтение с конца списка)
  Формат:          JSON, поля совпадают с InboundMessage

  Поля:

    channel              — строка, обычно "redis"
    sender_id     (обяз) — ID отправителя
    chat_id       (обяз) — ID чата/диалога
    content             — текст сообщения (строка)
    media               — вложения: массив (строки-пути/URL на входе;
                           принимает и dict-схему AW на чтение)
    metadata            — произвольный JSON-объект
    session_key_override — если указан, сессия привязывается к этому
                           ключу вместо "{channel}:{chat_id}"
    message_id          — ID сообщения во внешней системе (попадает
                           в reply_to ответа)

  Пример:

    {"sender_id": "user_42", "chat_id": "support_1", "content": "Привет!",
     "media": ["https://example.com/img.png"], "metadata": {"priority": 1},
     "message_id": "ext_msg_001"}

  Альтернативные поля для совместимости:
    "id" — если нет message_id, будет использован как reply_to

═ ФОРМАТ ИСХОДЯЩЕГО СООБЩЕНИЯ ════════════════════════════════════════

  Ключ в Redis:    ``outgoing_prefix:{chat_id}``
                    (например ``nanobot:outbox:support_1``)
  Тип операции:    LPUSH (добавление в начало списка)
  Формат:          JSON, поля совпадают с OutboundMessage

  Поля:

    channel     — всегда "redis"
    chat_id     — ID чата (копия из запроса)
    content     — текст ответа
    reply_to    — message_id из запроса (если был)
    media       — вложения в AW-формате (list[dict]: filename/file_id/mime_type/file_size)
    metadata    — служебные данные (reasoning, tool_events и т.д.)
    buttons     — массив массивов кнопок (см. OutboundMessage)

  Пример:

    {"channel": "redis", "chat_id": "support_1",
     "content": "Чем могу помочь?", "reply_to": "ext_msg_001",
     "media": [], "metadata": {}, "buttons": []}

═ ПРИМЕР РАБОТЫ С REDIS CLI ═══════════════════════════════════════

  -- Отправить вопрос:
  LPUSH nanobot:inbox '{"sender_id":"user1","chat_id":"chat1","content":"Как дела?","message_id":"m1"}'

  -- Получить ответ (слушать в цикле):
  BRPOP nanobot:outbox:chat1 0

═ ПРИМЕЧАНИЯ ═══════════════════════════════════════════════════════

  • Рассуждения агента (reasoning_delta) и промежуточные прогрессы
    в Redis не пишутся — только финальный ответ.
  • reply_to берётся из message_id последнего входящего сообщения
    от этого chat_id.
  • Для параллельной обработки увеличьте max_concurrent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from utils.media import serialize as media_serialize
from utils.session_file_store import SessionFileStore

from lib.channels.message_exchange import MessageExchange
from lib.utils.outbound_meta import is_dropped

_WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent / "workspace"


def _resolve_sfs_base(media_cache_dir: str | Path) -> Path:
    """Преобразовать ``channels.redis.media_cache_dir`` в ``base_dir`` для
    ``SessionFileStore`` (колонка media → cache/sessions/{key}/attachments)."""
    p = Path(media_cache_dir)
    if not p.is_absolute():
        p = _WORKSPACE_DIR / media_cache_dir
    return p.parent if p.name == "sessions" else p


class RedisChannel(BaseChannel):
    """
    Канал поверх Redis-очередей.

    Читает входящие сообщения из Redis-списка (BRPOP) и отправляет
    ответы в per-chat outbox (LPUSH). Формат JSON повторяет поля
    InboundMessage / OutboundMessage.
    """

    name = "redis"
    display_name = "Redis"
    send_progress = True
    send_tool_hints = True
    show_reasoning = True

    def __init__(self, config: Any, bus: MessageBus) -> None:
        super().__init__(config, bus)

        # ---- чтение конфига (поддержка dict и object) ----
        def _get(key: str, default: Any = None) -> Any:
            return (
                config.get(key, default)
                if isinstance(config, dict)
                else getattr(config, key, default)
            )

        # ---- Redis-соединение ----
        self._host: str = _get("host", "127.0.0.1")
        self._port: int = int(_get("port", 6379))
        self._db: int = int(_get("db", 0))
        self._password: str | None = _get("password", None)

        # ---- медиа-файлы (вложения декодируются в cache/sessions/{key}) ----
        media_cache_dir = _get("media_cache_dir", "data_store/cache/sessions")
        self._file_store = SessionFileStore(
            _resolve_sfs_base(media_cache_dir), attachments_subdir="attachments"
        )

        # ---- Ключи Redis ----
        # inbox — список, откуда канал читает входящие (BRPOP)
        self._incoming_key: str = _get("incoming_key", "nanobot:inbox")
        # префикс для outbox-списков: к нему добавляется ":chat_id"
        self._outgoing_prefix: str = _get("outgoing_prefix", "nanobot:outbox")

        # ---- Тюнинг ----
        self._poll_timeout: float = float(_get("poll_timeout", 5.0))
        self._max_concurrent: int = int(_get("max_concurrent", 1))
        self._error_backoff_sec: float = float(_get("error_backoff_sec", 1.0))
        self._reply_to_max_size: int = int(_get("reply_to_max_size", 10000))
        self._reply_to_trim_to: int = int(_get("reply_to_trim_to", 5000))

        # ---- Состояние ----
        # Общий движок обмена: поллинг, конкуренция, кодек media.
        self.exchange = MessageExchange(
            self,
            max_concurrent=self._max_concurrent,
            poll_interval=self._poll_timeout,
            error_backoff=self._error_backoff_sec,
        )
        # хранит message_id последнего входящего сообщения на chat_id
        # используется для заполнения reply_to в ответе
        self._reply_to_map: dict[str, str | None] = {}
        self._redis: Any = None

    # ══════════════════════════════════════════════════════════════════
    # Жизненный цикл
    # ══════════════════════════════════════════════════════════════════

    @property
    def file_store(self):
        """Хранилище вложений для ``MessageExchange`` (кодек media)."""
        return self._file_store

    async def start(self) -> None:
        """Подключиться к Redis и запустить общий движок обмена."""
        self._running = True
        self._redis = await self._connect()
        await self.exchange.start()
        self.logger.info(
            "Redis channel started: {}:{}/{} inbox={}",
            self._host, self._port, self._db, self._incoming_key,
        )

    async def stop(self) -> None:
        """Остановить общий движок обмена и закрыть соединение с Redis."""
        self._running = False
        await self.exchange.stop()
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    # ══════════════════════════════════════════════════════════════════
    # Подключение к Redis
    # ══════════════════════════════════════════════════════════════════

    async def _connect(self):
        """Создать асинхронное Redis-соединение."""
        import redis.asyncio as aioredis

        return await aioredis.from_url(
            f"redis://{self._host}:{self._port}/{self._db}",
            password=self._password or None,
            decode_responses=True,
        )

    # ══════════════════════════════════════════════════════════════════
    # Хук транспорта для MessageExchange (поллинг входящих)
    # ══════════════════════════════════════════════════════════════════

    async def poll_inbound(self, exchange) -> bool:
        """
        Забрать одно сообщение из Redis (BRPOP), распарсить JSON,
        декодировать вложения и отправить агенту через _handle_message.
        Возвращает True, если сообщение обработано.
        """
        result = await self._redis.brpop(
            self._incoming_key, timeout=int(self._poll_timeout)
        )
        if result is None:
            return False  # таймаут — ничего не пришло

        _key, raw = result

        # ---- парсинг JSON ----
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self.logger.warning("Invalid JSON in Redis inbox: {}", e)
            return False

        # ---- извлечение полей (все поля InboundMessage) ----
        sender_id = str(data.get("sender_id", "unknown"))
        chat_id = str(data.get("chat_id", sender_id))
        content = data.get("content", "")
        raw_media: Any = data.get("media", [])
        raw_meta = data.get("metadata", {})
        session_key_override: str | None = data.get("session_key_override")
        # message_id сохраняется для подстановки в reply_to ответа
        message_id: str | None = data.get("message_id")

        # запасной вариант — поле "id" как message_id
        self._reply_to_map[chat_id] = message_id if message_id else data.get("id")

        # ---- типизация ----
        if not isinstance(raw_media, list):
            raw_media = []
        if not isinstance(raw_meta, dict):
            raw_meta = {}

        # ---- декодирование вложений (единый кодек) ----
        session_key = session_key_override or f"redis:{chat_id}"
        media = exchange.decode(raw_media, session_key)
        media_paths, hints = exchange.resolve(media)
        if hints:
            suffix = "\n".join(hints)
            content = f"{content}\n\n{suffix}" if content else suffix
        media = media_paths

        # ---- метаданные для агента ----
        meta: dict[str, Any] = {
            "redis_message": True,
            **raw_meta,
        }

        # ---- захват слота (concurrency) ----
        await exchange.acquire_slot()
        exchange.add_inflight(chat_id)

        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media,
                metadata=meta,
                session_key=session_key_override,
            )
        except Exception:
            self.logger.exception("Failed to dispatch Redis message from {}", sender_id)
            self._reply_to_map.pop(chat_id, None)
        finally:
            exchange.discard_inflight(chat_id)
            exchange.release_slot()
            if len(self._reply_to_map) > self._reply_to_max_size:
                while len(self._reply_to_map) > self._reply_to_trim_to:
                    self._reply_to_map.pop(next(iter(self._reply_to_map)), None)
        return True

    # ══════════════════════════════════════════════════════════════════
    # Отправка ответа (OutboundMessage → Redis)
    # ══════════════════════════════════════════════════════════════════

    async def send(self, msg: OutboundMessage) -> None:
        """
        Записать ответ агента в ``outgoing_prefix:{chat_id}`` (LPUSH).

        Пропускает служебные сообщения:
          • _reasoning_delta / _reasoning_end — поток рассуждений
          • _progress — промежуточный прогресс (тул-коллы)
          • _turn_end — маркер конца оборота
        """
        if self._redis is None:
            return

        meta = dict(msg.metadata or {})

        # ---- фильтрация служебных сообщений ----
        if is_dropped(meta):
            return

        # ---- reply_to: сначала из OutboundMessage, иначе из сохранённого ----
        reply_to = msg.reply_to or self._reply_to_map.pop(msg.chat_id, None)

        # ---- сборка payload (поля OutboundMessage) ----
        # Вложения сериализуются единым кодеком в AW-формат (как у
        # postgres_channel: {"filename","file_id","mime_type","file_size"}),
        # чтобы потребитель видел превью/скачивание, а не «голые» пути.
        payload = {
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "content": msg.content,
            "reply_to": reply_to,
            "media": media_serialize(msg.media or []),
            "metadata": meta,
            "buttons": msg.buttons or [],
        }

        # ---- запись в Redis ----
        outbox_key = f"{self._outgoing_prefix}:{msg.chat_id}"
        try:
            await self._redis.lpush(
                outbox_key, json.dumps(payload, ensure_ascii=False)
            )
        except Exception:
            self.logger.exception(
                "Failed to push response to Redis key {}", outbox_key
            )

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """
        Стриминг не поддерживается — все сообщения приходят целиком
        в send(). Заглушка нужна для совместимости с BaseChannel.
        """
        pass

    # ══════════════════════════════════════════════════════════════════
    # Конфиг по умолчанию (для nanobot onboard)
    # ══════════════════════════════════════════════════════════════════

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 6379,
            "db": 0,
            "password": None,
            "media_cache_dir": "data_store/cache/sessions",
            "incoming_key": "nanobot:inbox",
            "outgoing_prefix": "nanobot:outbox",
            "poll_timeout": 5.0,
            "max_concurrent": 1,
            "allow_from": ["*"],
        }
