"""PostgreSQL / Greenplum канал — связывает БД веб-сервера с nanobot-агентом.

Канал опрашивает таблицу ``conversation_messages``, забирает входящие
сообщения от пользователей (status='pending'), отправляет их агенту,
и записывает ответы обратно в ту же таблицу.

Пример конфига (config.json → channels.postgres)::

    {
        "enabled": true,
        "dsn": "postgresql://user:pass@localhost:5432/nanobot",
        "schema": "public",
        "table_name": "conversation_messages",
        "poll_interval": 2.0,
        "max_concurrent": 1,
        "processing_timeout": 300
    }
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import timedelta
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from utils.db import async_fetchval as fetchval, async_execute as execute, async_fetchone as fetchone, async_transaction as transaction, async_fetch as fetch
from psycopg2.extras import Json


def _decode_jsonb(val: Any) -> dict:
    """Безопасно декодировать JSONB значение.

    psycopg2 с register_json возвращает dict.
    Старые записи (сохранённые до перехода) могут быть строкой.
    """
    if val is None:
        return {}
    if isinstance(val, str):
        return json.loads(val)
    if isinstance(val, dict):
        return val
    return dict(val) if val else {}


class PostgresChannel(BaseChannel):
    name = "postgres"
    """Опрашивает ``conversation_messages`` и отправляет ответы агенту.

    Жизненный цикл сообщения:

        1. Пользователь пишет сообщение → INSERT с status='pending'
        2. ``_poll_once`` забирает его (UPDATE status='processing')
        3. ``_handle_message`` отправляет в шину → агенту
        4. Агент формирует ответ → ``send()`` пишет status='completed'
        5. Web-сервер (Streamlit) видит completed и показывает ответ

    Рассуждения агента (reasoning) пишутся в real-time через
    ``send_reasoning_delta`` → буферизируются → ``_flush_reasoning``
    периодически сбрасывает в ``metadata.reasoning``.

    Параллельность ограничена ``max_concurrent`` через asyncio.Semaphore.
    """

    def __init__(self, config: dict, bus: MessageBus) -> None:
        super().__init__(config, bus)
        _get = config.get

        # ---- настройки подключения к БД ----
        self._dsn: str = _get("dsn", "")
        self._schema: str = _get("schema", "public")
        self._table_name: str = _get("table_name", "conversation_messages")
        self._fq_table: str = f"{self._schema}.{self._table_name}"

        # ---- тайминги ----
        # как часто опрашивать БД на новые сообщения (сек)
        self._poll_interval: float = float(_get("poll_interval", 2.0))
        # через сколько секунд сообщение в processing считается зависшим
        self._processing_timeout: int = int(_get("processing_timeout", 600))
        # как часто сбрасывать буферы reasoning в БД (сек)
        self._flush_interval: float = float(_get("flush_interval", 2.0))

        # ---- параллельность ----
        # максимум одновременных сообщений в обработке у агента
        self._max_concurrent: int = int(_get("max_concurrent", 1))
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        # user_msg_id сообщений, которые сейчас в обработке
        self._inflight: set[str] = set()
        # chat_id, которые сейчас заняты (чтобы не диспатчить второе
        # сообщение в тот же чат, пока первое не完成)
        self._chat_inflight: set[str] = set()
        # user_msg_id → chat_id (для освобождения chat_inflight)
        self._msg_chat: dict[str, str] = {}

        # ---- стриминг (потоковая передача ответа) ----
        self._poll_task: asyncio.Task | None = None
        # stream_id → накопленный текст (для send_delta)
        self._stream_buffers: dict[str, str] = {}

        # ---- рассуждения (reasoning) ----
        # assistant_msg_id → накопленный текст рассуждений
        self._reasoning_buffers: dict[str, str] = {}
        # блокировка для атомарности read-modify-write reasoning в БД
        self._reasoning_io_lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

        # ---- контекст сообщения ----
        # user_msg_id → {assistant_msg_id, tool_events, reasoning_buf}
        self._msg_ctx: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Жизненный цикл (start / stop)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Запустить циклы опроса БД и сброса рассуждений."""
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_reasoning_loop())
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info(
            "Polling {} every {}s (processing timeout {}s)",
            self._fq_table,
            self._poll_interval,
            self._processing_timeout,
        )

    async def stop(self) -> None:
        """Остановить все циклы и сбросить оставшиеся рассуждения."""
        self._running = False
        await self._flush_reasoning()
        if self._flush_task:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
        if self._poll_task:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        # db — глобальный singleton из utils.db, закрывается при выходе
        # из процесса. Явно не закрываем, чтобы не сломать другие каналы.

    # ------------------------------------------------------------------
    # Сброс рассуждений (reasoning flush)
    # ------------------------------------------------------------------

    async def _flush_reasoning_loop(self) -> None:
        """Фоновая задача: каждые ``_flush_interval`` секунд сбрасывает
        накопленные буферы рассуждений в ``metadata.reasoning`` в БД."""
        while self._running:
            await asyncio.sleep(self._flush_interval)
            try:
                await self._flush_reasoning()
            except Exception as e:
                self.logger.error("Flush reasoning error: {}", e)

    async def _flush_reasoning(self) -> None:
        """Сбросить все грязные буферы рассуждений в БД одной пачкой.

        Атомарность гарантируется ``_reasoning_io_lock``: пока одна
        корутина читает-модифицирует-пишет, другая ждёт. Это исключает
        race condition между ``_flush_reasoning`` и финальным ``send()``.

        Алгоритм:
          1. Захватить ``_reasoning_buffers`` и обнулить (swap)
          2. Для каждого assistant_msg_id с непустым delta:
             a. ``async with _reasoning_io_lock``
             b. SELECT metadata → дописать reasoning → UPDATE
        """
        if not self._reasoning_buffers:
            return
        buffers = self._reasoning_buffers
        self._reasoning_buffers = {}
        for assistant_msg_id, delta in buffers.items():
            if delta:
                async with self._reasoning_io_lock:
                    row = await fetchone(
                        f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                        assistant_msg_id,
                    )
                    if not row:
                        continue
                    meta = _decode_jsonb(row["metadata"])
                    meta["reasoning"] = (meta.get("reasoning") or "") + delta
                    await execute(
                        f"UPDATE {self._fq_table} SET metadata = %s, updated_at = NOW() WHERE id = %s",
                        meta, assistant_msg_id,
                    )

    # ------------------------------------------------------------------
    # Цикл опроса БД
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Фоновая задача: бесконечно опрашивает БД.

        На каждой итерации:
          1. ``_unstick_processing`` — разблокировать зависшие сообщения
          2. Если есть свободный слот → ``_poll_once`` — взять новое сообщение

        Ошибки логируются, цикл продолжается.
        """
        while self._running:
            try:
                await self._unstick_processing()
                if len(self._inflight) < self._max_concurrent:
                    await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Poll error: {}", e)
            await asyncio.sleep(self._poll_interval)

    async def _unstick_processing(self) -> None:
        """Освободить сообщения, зависшие в ``processing`` дольше таймаута.

        Механизм:
          — Если сообщение в processing > ``_processing_timeout`` секунд,
            оно считается зависшим.
          — Счётчик retry_count в metadata увеличивается.
          — Если retry_count >= 3 → status = 'failed' (окончательно).
          — Иначе → status = 'pending' (повторная попытка).
          — Сообщение assistant с reply_to на это сообщение тоже failed.

        Это защита от ситуаций, когда агент упал, а сообщение осталось
        висеть в processing навсегда.
        """
        max_retries = 3
        timeout_s = self._processing_timeout

        async with transaction() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, metadata FROM {self._fq_table}
                WHERE role = 'user' AND status = 'processing'
                AND updated_at + interval '1 second' * %s < NOW()
                """,
                timeout_s,
            )

            for row in rows:
                msg_id = str(row["id"])
                meta = _decode_jsonb(row["metadata"])
                retry_count = meta.get("retry_count", 0) + 1
                meta["retry_count"] = retry_count

                if retry_count >= max_retries:
                    # исчерпали лимит попыток — окончательно failed
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'failed', "
                        f"metadata = %s, updated_at = NOW() WHERE id = %s",
                        meta, msg_id,
                    )
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'failed', "
                        f"updated_at = NOW() WHERE reply_to = %s AND role = 'assistant' AND status = 'processing'",
                        msg_id,
                    )
                    self.logger.warning(
                        "User msg {} exceeded max retries ({}/{})",
                        msg_id, retry_count, max_retries,
                    )
                else:
                    # возвращаем в pending для повторной попытки
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'pending', "
                        f"metadata = %s, updated_at = NOW() WHERE id = %s",
                        meta, msg_id,
                    )
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'failed', "
                        f"updated_at = NOW() WHERE reply_to = %s AND role = 'assistant' AND status = 'processing'",
                        msg_id,
                    )
                    self.logger.warning(
                        "Released stuck user msg {} (retry {}/{})",
                        msg_id, retry_count, max_retries,
                    )

            # также помечаем failed orphaned assistant-сообщения (без user)
            await conn.execute(
                f"UPDATE {self._fq_table} SET status = 'failed', "
                f"updated_at = NOW() WHERE role = 'assistant' AND status = 'processing' "
                f"AND updated_at + interval '1 second' * %s < NOW()",
                timeout_s,
            )

    async def _poll_once(self) -> None:
        """Забрать самое старое pending-сообщение и отправить агенту.

        Алгоритм:
          1. UPDATE ... RETURNING — атомарно захватываем сообщение
          2. Проверяем, не занят ли chat_id (chat_inflight)
          3. Создаём assistant-placeholder (чтобы web-клиент мог опрашивать)
          4. Захватываем слот семафора → _handle_message

        Если из этого chat_id уже есть активное сообщение, возвращаем
        статус в 'pending' — не диспатчим второе до завершения первого.
        """
        row = await fetchone(
            f"""
            UPDATE {self._fq_table}
            SET status = 'processing', updated_at = NOW()
            WHERE id = (
                SELECT id FROM {self._fq_table}
                WHERE role = 'user' AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
            )
            AND status = 'pending'
            RETURNING id, chat_id, user_id, content, media, metadata, created_at
            """
        )
        if row is None:
            return  # нет новых сообщений

        user_msg_id = str(row["id"])
        chat_id = str(row["chat_id"]) if row["chat_id"] else str(row["user_id"])
        user_id = str(row["user_id"]) if row["user_id"] else chat_id

        # Не диспатчим, если из этого chat_id уже есть активное сообщение
        if chat_id in self._chat_inflight:
            await execute(
                f"UPDATE {self._fq_table} SET status = 'pending', updated_at = NOW() WHERE id = %s",
                user_msg_id,
            )
            self.logger.debug(
                "Deferred msg {} from busy chat {}", user_msg_id, chat_id,
            )
            return

        content = row["content"] or ""

        raw_meta = _decode_jsonb(row["metadata"])

        raw_media = row["media"] or []
        if isinstance(raw_media, str):
            raw_media = json.loads(raw_media) if raw_media else []
        media: list[str] = raw_media if isinstance(raw_media, list) else []

        # Создаём assistant-placeholder, чтобы Streamlit мог начать опрос
        assistant_msg_id = await self._insert_assistant_message(user_msg_id, chat_id)

        await self._semaphore.acquire()
        self._inflight.add(user_msg_id)
        self._chat_inflight.add(chat_id)
        self._msg_chat[user_msg_id] = chat_id

        meta: dict[str, Any] = {
            "message_id": user_msg_id,
            "answer_id": assistant_msg_id,
            **raw_meta,
        }

        try:
            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
                media=media,
                metadata=meta,
            )
        except Exception:
            self.logger.exception("Failed to dispatch user message {}", user_msg_id)
            await self._mark_failed(user_msg_id, assistant_msg_id, "dispatch_error")

    async def _insert_assistant_message(self, user_msg_id: str, chat_id: str) -> str:
        """Создать assistant-заглушку (status='processing') и сохранить её id.

        Зачем: чтобы web-сервер (Streamlit) мог начать опрашивать ответ
        ДО того, как агент закончит генерацию. Как только агент завершит,
        ``send()`` обновит эту запись: content + status='completed'.

        Возвращает ``assistant_msg_id`` — ID созданной записи.
        """
        row = await fetchone(
            f"""
            INSERT INTO {self._fq_table}
                (chat_id, role, content, reply_to, status, created_at, updated_at)
            VALUES (%s, 'assistant', '', %s, 'processing', NOW(), NOW())
            RETURNING id
            """,
            chat_id, user_msg_id,
        )
        assistant_msg_id = str(row["id"])
        self._msg_ctx[user_msg_id] = {
            "assistant_msg_id": assistant_msg_id,
            "tool_events": [],
            "reasoning_buf": [],
        }
        self.logger.debug(
            "Inserted assistant placeholder {} for user msg {}",
            assistant_msg_id, user_msg_id,
        )
        return assistant_msg_id

    async def _mark_failed(self, user_msg_id: str, assistant_msg_id: str | None, reason: str) -> None:
        """Пометить пользовательское сообщение и ответ ассистента как failed.

        Вызывается при:
          — ошибке диспетчеризации (\"dispatch_error\")
          — ошибке записи ответа (\"write_error\")

        Дополнительно:
          — удаляет контекст из ``_msg_ctx``
          — освобождает слот (``_release_slot``)
          — чистит буфер рассуждений для этого assistant_msg_id
        """
        async with transaction() as conn:
            if assistant_msg_id:
                await conn.execute(
                    f"UPDATE {self._fq_table} SET content = %s, metadata = %s, "
                    f"status = 'failed', updated_at = NOW() WHERE id = %s",
                    f"Internal error: {reason}", {"error": reason}, assistant_msg_id,
                )
            await conn.execute(
                f"UPDATE {self._fq_table} SET status = 'failed', updated_at = NOW() WHERE id = %s",
                user_msg_id,
            )
        self._msg_ctx.pop(user_msg_id, None)
        self._release_slot(user_msg_id)
        if assistant_msg_id:
            self._reasoning_buffers.pop(assistant_msg_id, None)

    # ------------------------------------------------------------------
    # Рассуждения (reasoning) — потоковая запись в metadata.reasoning
    # ------------------------------------------------------------------
    #
    # Агент может отправлять промежуточные рассуждения (chain-of-thought)
    # чанками через ``send_reasoning_delta``. Эти чанки:
    #   1. Буферизируются в ``_reasoning_buffers[assistant_msg_id]``
    #   2. Периодически сбрасываются в БД через ``_flush_reasoning``
    #   3. При финальном ответе ``send()`` остатки дописываются atomic
    #
    # Если assistant_msg_id ещё не известен (не создан placeholder),
    # данные временно складируются в ``_msg_ctx[msg_id][\"reasoning_buf\"]``.
    # ------------------------------------------------------------------

    async def send_reasoning_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Получить чанк рассуждений от агента и добавить в буфер.

        Параметры:
            chat_id — ID чата (не используется, т.к. берём из metadata)
            delta — текст очередного чанка рассуждений
            metadata — может содержать answer_id (assistant_msg_id)

        Поведение:
            — Если известен assistant_msg_id → пишем в ``_reasoning_buffers``
            — Иначе → буферизируем в ``_msg_ctx`` (будет поднят позже)
        """
        assistant_msg_id = self._resolve_assistant_msg_id(metadata)
        if assistant_msg_id:
            buf = self._reasoning_buffers.get(assistant_msg_id, "")
            self._reasoning_buffers[assistant_msg_id] = buf + delta
            return
        # Fallback: assistant_msg_id ещё не известен — буфер в _msg_ctx
        msg_id = (metadata or {}).get("origin_message_id") or (metadata or {}).get("message_id")
        if msg_id:
            ctx = self._msg_ctx.setdefault(msg_id, {})
            ctx.setdefault("reasoning_buf", []).append(delta)

    async def send_reasoning_end(
        self, chat_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Сигнал конца рассуждений. Не используется — финализация в send()."""
        pass

    # ------------------------------------------------------------------
    # Отправка ответа (outbound) — принимает сообщения от агента
    # ------------------------------------------------------------------
    #
    # Через этот метод проходят все исходящие сообщения от агента.
    # В зависимости от флагов в metadata сообщение может быть:
    #
    #   ``_reasoning_delta`` — чанк рассуждений → буфер → БД
    #   ``_reasoning_end``   — конец рассуждений (игнорируется)
    #   ``_progress``        — промежуточный прогресс (тул-коллы)
    #   ``_turn_end``        — маркер конца оборота (игнорируется)
    #   (без флагов)         — финальный ответ → пишем в БД
    #
    # Финальный ответ: читаем metadata assistant-заглушки, мержим
    # с текущими metadata, пишем content + status='completed'.
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        meta = dict(msg.metadata or {})
        msg_id = meta.get("origin_message_id") or meta.get("message_id")

        # --- Чанк рассуждений — буферизируем, в БД попадёт через flush ---
        if meta.get("_reasoning_delta"):
            if msg.content:
                assistant_msg_id = self._resolve_assistant_msg_id(meta)
                if assistant_msg_id:
                    buf = self._reasoning_buffers.get(assistant_msg_id, "")
                    self._reasoning_buffers[assistant_msg_id] = buf + msg.content
                elif msg_id:
                    ctx = self._msg_ctx.setdefault(msg_id, {})
                    ctx.setdefault("reasoning_buf", []).append(msg.content)
            return

        if meta.get("_reasoning_end"):
            return

        # --- Промежуточный прогресс (тул-коллы) — копим, не пишем ---
        if meta.get("_progress"):
            if msg_id:
                # защита от утечки _msg_ctx: удаляем только те записи,
                # которые уже не в полёте (не в _inflight)
                if len(self._msg_ctx) > 100:
                    stale = [k for k in self._msg_ctx if k not in self._inflight]
                    for k in stale:
                        self._msg_ctx.pop(k, None)
                ctx = self._msg_ctx.setdefault(msg_id, {"reasoning_buf": []})
            return

        # --- Сигнал конца оборота ---
        if meta.get("_turn_end"):
            return

        # --- Финальный ответ — пишем в БД ---
        ctx = self._msg_ctx.pop(msg_id, {}) if msg_id else {}
        self._release_slot(msg_id)
        assistant_msg_id = ctx.get("assistant_msg_id") or meta.get("answer_id")
        if not assistant_msg_id:
            self.logger.warning("send: no assistant_msg_id for msg_id={}", msg_id)
            return

        # Дописываем остатки рассуждений перед финальным ответом
        reasoning_delta = ""
        if assistant_msg_id and assistant_msg_id in self._reasoning_buffers:
            delta = self._reasoning_buffers.pop(assistant_msg_id, "")
            if delta:
                reasoning_delta = delta
        if ctx.get("reasoning_buf"):
            buf = " ".join(ctx["reasoning_buf"])
            reasoning_delta = buf + (" " if reasoning_delta else "") + reasoning_delta
        if reasoning_delta:
            # atomic append через _reasoning_io_lock — исключает race
            # с параллельным _flush_reasoning
            async with self._reasoning_io_lock:
                row = await fetchone(
                    f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                    assistant_msg_id,
                )
                if row:
                    meta_row = _decode_jsonb(row["metadata"])
                    meta_row["reasoning"] = (meta_row.get("reasoning") or "") + reasoning_delta
                    await execute(
                        f"UPDATE {self._fq_table} SET metadata = %s, updated_at = NOW() WHERE id = %s",
                        meta_row, assistant_msg_id,
                    )

        chat_id = msg.chat_id

        try:
            async with transaction() as conn:
                row = await conn.fetchrow(
                    f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                    assistant_msg_id,
                )
                existing_meta = _decode_jsonb(row["metadata"]) if row else {}
                existing_meta.update(meta)
                await conn.execute(
                    f"UPDATE {self._fq_table} "
                    f"SET content = %s, metadata = %s, buttons = %s, "
                    f"status = 'completed', updated_at = NOW() WHERE id = %s",
                    msg.content, existing_meta,
                    Json(msg.buttons or []), assistant_msg_id,
                )
                if msg_id:
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'completed', "
                        f"updated_at = NOW() WHERE id = %s",
                        msg_id,
                    )
        except Exception:
            self.logger.exception("Failed to write response for {}", chat_id)
            if msg_id:
                await self._mark_failed(msg_id, assistant_msg_id, "write_error")

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Получить очередной чанк стримингового ответа от агента.

        Когда агент использует стриминг (потоковую генерацию),
        каждый фрагмент текста приходит через ``send_delta``.

        Поведение:
          — Если ``_stream_end`` отсутствует → накапливаем текст
            в ``_stream_buffers[stream_id]``.
          — Если ``_stream_end`` присутствует → финализируем:
            достаём накопленный текст, пишем в БД как status='completed',
            освобождаем слот.
        """
        meta = dict(metadata or {})
        stream_id = meta.get("_stream_id", chat_id)

        if meta.get("_stream_end"):
            msg_id = meta.get("origin_message_id") or meta.get("message_id")
            ctx = self._msg_ctx.pop(msg_id, {}) if msg_id else {}
            self._release_slot(msg_id)
            assistant_msg_id = ctx.get("assistant_msg_id") or meta.get("answer_id")

            if ctx.get("reasoning_buf"):
                meta["reasoning"] = " ".join(ctx["reasoning_buf"])

            content = self._stream_buffers.pop(stream_id, "")
            if content and assistant_msg_id:
                async with transaction() as conn:
                    row = await conn.fetchrow(
                        f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                        assistant_msg_id,
                    )
                    existing_meta = _decode_jsonb(row["metadata"]) if row else {}
                    existing_meta.update(meta | {"streamed": True})
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET content = %s, "
                        f"metadata = %s, status = 'completed', updated_at = NOW() WHERE id = %s",
                        content, existing_meta, assistant_msg_id,
                    )
                    if msg_id:
                        await conn.execute(
                            f"UPDATE {self._fq_table} SET status = 'completed', "
                            f"updated_at = NOW() WHERE id = %s",
                            msg_id,
                        )
        else:
            buf = self._stream_buffers.get(stream_id, "")
            self._stream_buffers[stream_id] = buf + delta

    # ------------------------------------------------------------------
    # Управление слотами параллельности
    # ------------------------------------------------------------------
    #
    # Семафор ``_semaphore`` ограничивает количество сообщений,
    # которые одновременно обрабатываются агентом (max_concurrent).
    #
    # Каждое сообщение при входе захватывает слот (semaphore.acquire),
    # при выходе отпускает (semaphore.release).
    #
    # ``_release_slot`` идемпотентен: если слот уже отпущен,
    # повторный вызов — no-op. Это защищает от двойного отпуска
    # при ошибках (send → _mark_failed → _release_slot).
    # ------------------------------------------------------------------

    def _release_slot(self, user_msg_id: str) -> None:
        """Освободить слот параллельности для указанного сообщения.

        Идемпотентен: можно вызывать多次 для одного id —
        второй вызов будет no-op (проверка ``user_msg_id not in _inflight``).

        Дополнительно:
          — удаляет chat_id из ``_chat_inflight`` (если был)
          — удаляет запись из ``_msg_chat``
        """
        if not user_msg_id:
            return
        if user_msg_id not in self._inflight:
            return
        self._inflight.discard(user_msg_id)
        self._semaphore.release()
        chat_id = self._msg_chat.pop(user_msg_id, None)
        if chat_id:
            self._chat_inflight.discard(chat_id)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _resolve_assistant_msg_id(self, metadata: dict[str, Any] | None) -> str | None:
        """Извлечь ``assistant_msg_id`` из metadata или ``_msg_ctx``.

        Приоритет:
          1. ``metadata["answer_id"]`` — напрямую
          2. ``_msg_ctx[msg_id]["assistant_msg_id"]`` — по message_id

        Возвращает None, если не удалось найти.
        """
        meta = metadata or {}
        answer_id = meta.get("answer_id")
        if answer_id:
            return str(answer_id)
        msg_id = meta.get("origin_message_id") or meta.get("message_id")
        if msg_id:
            ctx = self._msg_ctx.get(msg_id)
            if ctx:
                return ctx.get("assistant_msg_id")
        return None

    # ------------------------------------------------------------------
    # Конфиг по умолчанию (для ``nanobot onboard``)
    # ------------------------------------------------------------------

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """Пример конфигурации канала (вставляется в config.json)."""
        return {
            "enabled": True,
            "dsn": "postgresql://user:pass@localhost:5432/nanobot",
            "schema": "public",
            "table_name": "conversation_messages",
            "poll_interval": 2.0,
            "flush_interval": 2.0,
            "max_concurrent": 1,
            "processing_timeout": 600,
            "allow_from": ["*"],
        }
