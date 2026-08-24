"""Общий движок обмена сообщениями для каналов.

``postgres_channel`` и ``redis_channel`` кардинально различаются транспортом
(SQL-таблица vs Redis-очереди), но разделяют одинаковую оркестрацию:

  * цикл поллинга входящих сообщений и backoff при ошибках;
  * ограничение параллельности (семафор + множество in-flight);
  * работу с вложениями ``media`` (единый кодек ``utils.media``);
  * фильтрацию служебных сообщений (reasoning/progress/turn_end).

Этот движок выносит общую часть в одно место, чтобы править её точечно,
а транспорт остаётся pluggable. Канал (транспорт) реализует единственный
async-хук ``poll_inbound()`` (забрать одно входящее и отправить агенту),
а служебные/медиа-операции берёт из движка.

Поток слотов: ``acquire_slot`` занимается при диспатче входящего,
``release_slot`` освобождается после финализации ответа (в ``send``). Такой
жизненный цикл транспорто-зависим, поэтому не прячется внутрь движка, а
предоставляется им как примитив.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from utils.media import (
    deserialize as media_deserialize,
)
from utils.media import (
    resolve_paths_and_hints as media_resolve_paths_and_hints,
)
from utils.media import (
    serialize as media_serialize,
)


class MessageExchange:
    """Общая оркестрация обмена сообщениями поверх транспорта-канала."""

    def __init__(
        self,
        channel: Any,
        *,
        max_concurrent: int = 1,
        poll_interval: float = 5.0,
        error_backoff: float = 1.0,
    ) -> None:
        self.channel = channel
        self.logger = channel.logger
        self._max_concurrent = int(max_concurrent)
        self._poll_interval = float(poll_interval)
        self._error_backoff = float(error_backoff)
        self._semaphore = asyncio.Semaphore(self._max_concurrent or 1)
        self._inflight: set[str] = set()
        self._running = False
        self._poll_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Медиа (единый кодек)
    # ------------------------------------------------------------------

    def embed(self, media: list[str] | None) -> list[Any]:
        """Сериализовать вложения в storage AW-формат для БД/очереди."""
        return media_serialize(media or [])

    def decode(self, media: list[Any], session_key: str) -> list[Any]:
        """Декодировать вложения к runtime-формату (пути для агента)."""
        return media_deserialize(media, self.channel.file_store, session_key)

    def resolve(self, media: list[Any]) -> tuple[list[str], list[str]]:
        """Распаковать media в пути для агента и подсказки."""
        return media_resolve_paths_and_hints(media)

    # ------------------------------------------------------------------
    # Конкуренция
    # ------------------------------------------------------------------

    @property
    def inflight(self) -> set[str]:
        return self._inflight

    def is_slot_free(self) -> bool:
        return len(self._inflight) < self._max_concurrent

    async def acquire_slot(self) -> None:
        await self._semaphore.acquire()

    def add_inflight(self, key: str) -> None:
        self._inflight.add(key)

    def discard_inflight(self, key: str) -> None:
        self._inflight.discard(key)

    def release_slot(self, key: str | None = None) -> None:
        """Идемпотентно отпустить слот (защита от двойного release)."""
        if key is not None:
            if key not in self._inflight:
                return
            self._inflight.discard(key)
        try:
            self._semaphore.release()
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Жизненный цикл поллинга
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info(
            "MessageExchange started (max_concurrent={}, poll_interval={}s)",
            self._max_concurrent, self._poll_interval,
        )

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

    async def _poll_loop(self) -> None:
        """Бесконечный цикл: берёт входящие, пока есть свободный слот."""
        while self._running:
            try:
                if self.is_slot_free():
                    handled = await self.channel.poll_inbound(self)
                    if not handled:
                        await asyncio.sleep(self._poll_interval)
                else:
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Poll error: {}", e)
                await asyncio.sleep(self._error_backoff)
