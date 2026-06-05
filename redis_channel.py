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
    media               — массив URL медиафайлов
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
    media       — массив URL медиафайлов
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

import asyncio
import json
from contextlib import suppress
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


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

        # ---- Ключи Redis ----
        # inbox — список, откуда канал читает входящие (BRPOP)
        self._incoming_key: str = _get("incoming_key", "nanobot:inbox")
        # префикс для outbox-списков: к нему добавляется ":chat_id"
        self._outgoing_prefix: str = _get("outgoing_prefix", "nanobot:outbox")

        # ---- Тюнинг ----
        self._poll_timeout: float = float(_get("poll_timeout", 5.0))
        self._max_concurrent: int = int(_get("max_concurrent", 1))
        self._semaphore = asyncio.Semaphore(self._max_concurrent)

        # ---- Состояние ----
        self._inflight: set[str] = set()
        # хранит message_id последнего входящего сообщения на chat_id
        # используется для заполнения reply_to в ответе
        self._reply_to_map: dict[str, str | None] = {}
        self._redis: Any = None
        self._poll_task: asyncio.Task | None = None

    # ══════════════════════════════════════════════════════════════════
    # Жизненный цикл
    # ══════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """Подключиться к Redis и запустить цикл опроса."""
        self._running = True
        self._redis = await self._connect()
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info(
            "Redis channel started: {}:{}/{} inbox={}",
            self._host, self._port, self._db, self._incoming_key,
        )

    async def stop(self) -> None:
        """Остановить опрос и закрыть соединение с Redis."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
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
    # Цикл опроса входящих сообщений
    # ══════════════════════════════════════════════════════════════════

    async def _poll_loop(self) -> None:
        """
        Бесконечный цикл: ждёт новые сообщения из Redis и диспатчит их
        агенту. Уважает max_concurrent — не берёт новое сообщение, пока
        не освободится слот.
        """
        while self._running:
            try:
                if len(self._inflight) < self._max_concurrent:
                    await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Redis poll error: {}", e)
                await asyncio.sleep(1.0)

    async def _poll_once(self) -> None:
        """
        Прочитать одно сообщение из Redis (BRPOP), распарсить JSON
        и отправить агенту через _handle_message.
        """
        result = await self._redis.brpop(
            self._incoming_key, timeout=int(self._poll_timeout)
        )
        if result is None:
            return  # таймаут — ничего не пришло

        _key, raw = result

        # ---- парсинг JSON ----
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self.logger.warning("Invalid JSON in Redis inbox: {}", e)
            return

        # ---- извлечение полей (все поля InboundMessage) ----
        sender_id = str(data.get("sender_id", "unknown"))
        chat_id = str(data.get("chat_id", sender_id))
        content = data.get("content", "")
        media: list[str] = data.get("media", [])
        raw_meta = data.get("metadata", {})
        session_key_override: str | None = data.get("session_key_override")
        # message_id сохраняется для подстановки в reply_to ответа
        message_id: str | None = data.get("message_id")

        # запасной вариант — поле "id" как message_id
        self._reply_to_map[chat_id] = message_id if message_id else data.get("id")

        # ---- типизация ----
        if not isinstance(media, list):
            media = []
        if not isinstance(raw_meta, dict):
            raw_meta = {}

        # ---- метаданные для агента ----
        meta: dict[str, Any] = {
            "redis_message": True,
            **raw_meta,
        }

        # ---- захват слота (concurrency) ----
        await self._semaphore.acquire()
        self._inflight.add(chat_id)

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
        finally:
            self._inflight.discard(chat_id)
            self._semaphore.release()

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
        if meta.get("_reasoning_delta") or meta.get("_reasoning_end"):
            return
        if meta.get("_progress"):
            return
        if meta.get("_turn_end"):
            return

        # ---- reply_to: сначала из OutboundMessage, иначе из сохранённого ----
        reply_to = msg.reply_to or self._reply_to_map.pop(msg.chat_id, None)

        # ---- сборка payload (поля OutboundMessage) ----
        payload = {
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "content": msg.content,
            "reply_to": reply_to,
            "media": msg.media or [],
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
            "incoming_key": "nanobot:inbox",
            "outgoing_prefix": "nanobot:outbox",
            "poll_timeout": 5.0,
            "max_concurrent": 1,
            "allow_from": ["*"],
        }
