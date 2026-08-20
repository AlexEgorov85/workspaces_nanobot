"""PostgreSQL / Greenplum канал — связывает БД веб-сервера с nanobot-агентом.

Канал опрашивает таблицу ``agent_conversation_messages``, забирает входящие
сообщения от пользователей (status='pending'), отправляет их агенту,
и записывает ответы обратно в ту же таблицу.

Пример конфига (config.json → channels.postgres)::

    {
        "enabled": true,
        "dsn": "postgresql://user:pass@localhost:5432/nanobot",
        "schema": "public",
        "table_name": "agent_conversation_messages",
        "claims_table": "agent_worker_claims",
        "poll_interval": 2.0,
        "max_concurrent": 1,
        "processing_timeout": 300,
        "lease_interval": 15.0,
        "error_retry_delay": 60.0,
        "max_stuck_retries": 3,
        "worker_id": ""
    }
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import socket
import uuid
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg2
from loguru import logger
from rich.console import Console

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from utils.db import async_fetchval as fetchval, async_execute as execute, async_fetchone as fetchone, async_transaction as transaction, async_fetch as fetch
from utils.session_file_store import SessionFileStore
from utils.jsonb import decode_jsonb as _decode_jsonb
from utils.media import (
    serialize as media_serialize,
    deserialize as media_deserialize,
    resolve_paths_and_hints as media_resolve_paths_and_hints,
)
from lib.channels.message_exchange import MessageExchange
from lib.utils.outbound_meta import is_dropped, FINAL_TURN_KEY
from psycopg2.extras import Json

_WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent / "workspace"

console = Console()


def _resolve_sfs_base(media_cache_dir: str | Path) -> Path:
    """Преобразовать ``channels.postgres.media_cache_dir`` в ``base_dir``
    для ``SessionFileStore``.

    ``SessionFileStore(base_dir)`` размещает сессии в
    ``base_dir/cache/sessions/``. Обычно ``media_cache_dir`` уже указывает
    ровно на этот каталог (``data_store/cache/sessions``), значит ``base_dir``
    = ``workspace``. Если задан абсолютный путь или другой относительный —
    берём его родителя.
    """
    p = Path(media_cache_dir)
    if not p.is_absolute():
        p = _WORKSPACE_DIR / media_cache_dir
    return p.parent if p.name == "sessions" else p


class PostgresChannel(BaseChannel):
    name = "postgres"
    """Опрашивает ``agent_conversation_messages`` и отправляет ответы агенту.

    Жизненный цикл сообщения:

        1. Пользователь пишет сообщение → INSERT с status='pending'
        2. ``_claim_one`` атомарно захватывает задачу: INSERT claim в
           ``agent_worker_claims`` (UNIQUE PK — арбитр эксклюзивности) +
           UPDATE status='processing'
        3. ``_handle_message`` отправляет в шину → агенту
        4. Агент формирует ответ → ``send()`` пишет status='completed'
           и удаляет claim
        5. Web-сервер (Streamlit) видит completed и показывает ответ

    Рассуждения агента (reasoning) пишутся в real-time через
    ``send_reasoning_delta`` → буферизируются → ``_flush_reasoning``
    периодически сбрасывает в ``metadata.reasoning``.

    Параллельность ограничена ``max_concurrent`` через asyncio.Semaphore.
    Мульти-машинная аренда задач: claim+lease/heartbeat, reclaim+heal,
    статусы ``error`` (повторяется) и ``failed`` (терминал). См. Документация
    DEVELOPMENT.md » «Мульти-машинный пул воркеров».
    """

    def __init__(self, config: dict, bus: MessageBus) -> None:
        super().__init__(config, bus)
        _get = config.get

        # ---- настройки подключения к БД ----
        self._dsn: str = _get("dsn", "")
        self._schema: str = _get("schema", "public")
        self._table_name: str = _get("table_name", "")
        if not self._table_name:
            raise ValueError(
                "PostgresChannel: channels.postgres.table_name обязателен "
                "(нет авто-дефолтов в коде)"
            )
        self._fq_table: str = f"{self._schema}.{self._table_name}"

        # ---- тайминги ----
        # как часто опрашивать БД на новые сообщения (сек)
        self._poll_interval: float = float(_get("poll_interval", 2.0))
        # через сколько секунд сообщение в processing считается зависшим
        self._processing_timeout: int = int(_get("processing_timeout", 120))
        # сколько раз retry'ить зависшее сообщение до отказа
        self._max_stuck_retries: int = int(_get("max_stuck_retries", 3))
        # защитный лимит размера _msg_ctx
        self._msg_ctx_max_size: int = int(_get("msg_ctx_max_size", 100))
        # как часто сбрасывать буферы reasoning в БД (сек)
        self._flush_interval: float = float(_get("flush_interval", 2.0))

        # ---- мульти-машинный пул воркеров (аренда задач через claims) ----
        # Уникальный идентификатор этого воркера: либо явный из конфига,
        # либо авто-генерируемый {hostname}:{pid}:{rand8}.
        self._worker_id: str = (_get("worker_id") or "").strip()
        if not self._worker_id:
            self._worker_id = (
                f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
            )
        self._claims_table: str = _get("claims_table", "agent_worker_claims")
        self._fq_claims: str = f"{self._schema}.{self._claims_table}"
        # период heartbeat-продления аренды (сек)
        self._lease_interval: float = float(_get("lease_interval", 15.0))
        # пауза перед повторным захватом задачи со статусом error (сек)
        self._error_retry_delay: int = int(_get("error_retry_delay", 60))
        # task_id задач, аренда которых сейчас принадлежит этому воркеру
        self._leases: set[str] = set()
        self._lease_task: asyncio.Task | None = None

        # ---- вывод активности пула воркеров в терминал ----
        # Включается в gateway отключаемой опцией `gateway.print_worker_activity`
        # (project.json). Печатает: взял задачу / закончил / размер очереди.
        self._print_worker_activity: bool = bool(_get("print_worker_activity", False))
        # последний напечатанный (pending, error) — чтобы не спамить строку очереди
        self._last_queue_summary: tuple[int, int] | None = None

        # ---- параллельность ----
        self._max_concurrent: int = int(_get("max_concurrent", 1))
        self._error_backoff_sec: float = float(_get("error_backoff_sec", 1.0))
        # Общий движок обмена: поллинг, конкуренция, кодек media.
        self.exchange = MessageExchange(
            self,
            max_concurrent=self._max_concurrent,
            poll_interval=self._poll_interval,
            error_backoff=self._error_backoff_sec,
        )
        # chat_id, которые сейчас заняты (чтобы не диспатчить второе
        # сообщение в тот же чат, пока первое не завершено)
        self._chat_inflight: set[str] = set()

        # ---- единое хранилище файлов сессии ----
        # Канал делит SessionFileStore со всем приложением. Это та же
        # инстанция, через которую tools/streamlit/другие каналы кладут
        # файлы в ``cache/sessions/{session_key}/attachments/`` и
        # ``cache/sessions/{session_key}/results/``.
        injected_store = _get("_file_store")
        if isinstance(injected_store, SessionFileStore):
            self._file_store: SessionFileStore = injected_store
        else:
            media_cache_dir = _get("media_cache_dir", "data_store/cache/sessions")
            base = _resolve_sfs_base(media_cache_dir)
            self._file_store = SessionFileStore(base, attachments_subdir="attachments")

        self._msg_chat: dict[str, str] = {}

        # ---- стриминг (потоковая передача ответа) ----
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
    # Кодирование/декодирование медиа-файлов для передачи через БД
    # ------------------------------------------------------------------

    async def _embed_media_for_db(self, media: list[str]) -> list[Any]:
        """Прочитать локальные файлы и закодировать для БД (AW-формат).

        Делегирует общему ``utils.media.serialize`` — единая схема
        ``{"filename", "file_id", "mime_type", "file_size"}`` для всех каналов.
        """
        return media_serialize(media)

    async def _decode_media_from_db(
        self, media: list[Any], session_key: str = "default"
    ) -> list[Any]:
        """Декодировать storage-медиа обратно в локальные файлы сессии.

        Делегирует общему ``utils.media.deserialize`` — терпит legacy
        ``{filename, data}``, новый AW ``{filename, file_id, ...}`` и
        ``{filename, path}``. Файлы пишутся через ``SessionFileStore`` →
        ``cache/sessions/{session_key}/attachments/{uuid}_{имя}``.
        """
        return media_deserialize(media, self._file_store, session_key)

    @staticmethod
    def _resolve_media_paths_and_hints(
        media: list[Any],
    ) -> tuple[list[str], list[str]]:
        """Из декодированных media (строки-пути или dict filename/path)
        извлечь пути для агента и подсказки «файл лежит там-то»."""
        return media_resolve_paths_and_hints(media)

    # ------------------------------------------------------------------
    # Жизненный цикл (start / stop)
    # ------------------------------------------------------------------

    @property
    def file_store(self) -> SessionFileStore:
        """Хранилище вложений для ``MessageExchange`` (кодек media)."""
        return self._file_store

    async def start(self) -> None:
        """Запустить циклы опроса БД, продления аренды и сброса рассуждений."""
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_reasoning_loop())
        self._lease_task = asyncio.create_task(self._lease_loop())
        await self.exchange.start()
        self.logger.info(
            "Polling {} every {}s (processing timeout {}s, worker_id={})",
            self._fq_table,
            self._poll_interval,
            self._processing_timeout,
            self._worker_id,
        )

    async def stop(self) -> None:
        """Остановить все циклы, освободить аренды и сбросить рассуждения."""
        self._running = False
        await self.exchange.stop()
        await self._flush_reasoning()
        if self._flush_task:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
        if self._lease_task:
            self._lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._lease_task
            self._lease_task = None
        # Вернуть незавершённые задачи в пул (другие воркеры их подберут).
        await self._release_all_leases()
        # db — глобальный singleton из utils.db, закрывается при выходе
        # из процесса. Явно не закрываем, чтобы не сломать другие каналы.

    # ------------------------------------------------------------------
    # Аренда задач (lease) — heartbeat и reclaim
    # ------------------------------------------------------------------

    async def _lease_loop(self) -> None:
        """Фоновая задача: периодически продлевает аренды и возвращает
        задачи с истёкшими арендами обратно в пул.

        Каждые ``_lease_interval`` секунд:
          1. heartbeat — продлить ``lease_until`` для своих аренд;
          2. ``_reclaim_and_heal`` — вернуть в пул задачи, чьи lease истекли.

        Порядок важен: heartbeat идёт **первым**, иначе задержка тика
        (заблокированный event loop, медленная БД) приведёт к тому, что
        воркер отзовёт собственную живую задачу.
        """
        while self._running:
            await asyncio.sleep(self._lease_interval)
            try:
                if self._leases:
                    await execute(
                        f"UPDATE {self._fq_claims} SET lease_until = "
                        f"NOW() + interval '1 second' * %s WHERE worker_id = %s",
                        self._processing_timeout, self._worker_id,
                    )
                await self._reclaim_and_heal()
            except Exception as e:
                self.logger.error("Lease loop error: {}", e)

    async def _reclaim_and_heal(self) -> None:
        """Вернуть задачи с истёкшими арендами и вылечить рассинхроны claims.

        Инвариант: ``processing ⇔ claim``. Нарушения чинятся в одной
        транзакции:

          1. Reclaim: истёкшие lease удаляются, их задачи возвращаются в
             ``pending`` (или ``failed`` при исчерпании лимита retry),
             assistant-placeholder удаляется.
          2. Heal: ``processing`` без claim → ``error`` (аномалия, будет
             повторена после ``error_retry_delay``).
          3. Orphaned assistant (``processing`` без живой user-пары) →
             ``failed``.
          4. Висячая аренда: claim есть, а задача не в ``processing``
             (уже completed/pending/error) — удаляется как мусор.

        Аренды, которые этот воркер держит в памяти (``_leases``), из
        reclaim исключаются: задача физически обрабатывается здесь, и
        отзыв истёкшего lease привёл бы к дублю обработки и удалению
        живого assistant-placeholder.
        """
        async with transaction() as conn:
            # 1. Reclaim по истечению lease (кроме своих живых аренд)
            own = list(self._leases)
            if own:
                rows = await conn.fetch(
                    f"DELETE FROM {self._fq_claims} WHERE lease_until < NOW() "
                    f"AND NOT (task_id = ANY(%s::uuid[]) AND worker_id = %s) "
                    f"RETURNING task_id, worker_id",
                    own, self._worker_id,
                )
            else:
                rows = await conn.fetch(
                    f"DELETE FROM {self._fq_claims} WHERE lease_until < NOW() "
                    f"RETURNING task_id, worker_id"
                )
            for r in rows:
                msg_id = str(r["task_id"])
                meta_row = await conn.fetchrow(
                    f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                    msg_id,
                )
                meta = _decode_jsonb(meta_row["metadata"]) if meta_row else {}
                retry_count = meta.get("retry_count", 0) + 1
                meta["retry_count"] = retry_count
                if retry_count >= self._max_stuck_retries:
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'failed', "
                        f"metadata = %s, updated_at = NOW() "
                        f"WHERE id = %s AND status = 'processing'",
                        meta, msg_id,
                    )
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'failed', "
                        f"updated_at = NOW() WHERE reply_to = %s "
                        f"AND role = 'assistant' AND status = 'processing'",
                        msg_id,
                    )
                    self.logger.warning(
                        "Reclaimed user msg {} exceeded max retries ({}/{})",
                        msg_id, retry_count, self._max_stuck_retries,
                    )
                else:
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'pending', "
                        f"metadata = %s, updated_at = NOW() "
                        f"WHERE id = %s AND status = 'processing'",
                        meta, msg_id,
                    )
                    await conn.execute(
                        f"DELETE FROM {self._fq_table} WHERE reply_to = %s "
                        f"AND role = 'assistant' AND status IN ('processing', 'failed')",
                        msg_id,
                    )
                    self.logger.warning(
                        "Reclaimed stuck user msg {} (retry {}/{})",
                        msg_id, retry_count, self._max_stuck_retries,
                    )

            # 2. Heal: processing без claim — повторяемая ошибка
            await conn.execute(
                f"UPDATE {self._fq_table} SET status = 'error', "
                f"updated_at = NOW() "
                f"WHERE role = 'user' AND status = 'processing' "
                f"AND NOT EXISTS (SELECT 1 FROM {self._fq_claims} c "
                f"WHERE c.task_id = {self._fq_table}.id)"
            )

            # 3. Orphaned assistant-сообщения (без живой user-пары)
            await conn.execute(
                f"UPDATE {self._fq_table} SET status = 'failed', "
                f"updated_at = NOW() WHERE role = 'assistant' "
                f"AND status = 'processing' AND NOT EXISTS "
                f"(SELECT 1 FROM {self._fq_table} u "
                f"WHERE u.id = {self._fq_table}.reply_to AND u.role = 'user')"
            )

            # 4. Висячие аренды: claim есть, а задача не в обработке — мусор
            await conn.execute(
                f"DELETE FROM {self._fq_claims} c "
                f"WHERE NOT EXISTS (SELECT 1 FROM {self._fq_table} m "
                f"WHERE m.id = c.task_id AND m.status = 'processing')"
            )

    async def _release_all_leases(self) -> None:
        """Освободить все аренды этого воркера при остановке.

        Задачи возвращаются в ``pending`` (если всё ещё ``processing``),
        claims удаляются, assistant-placeholder — тоже.
        """
        if not self._leases:
            return
        async with transaction() as conn:
            for task_id in list(self._leases):
                await conn.execute(
                    f"UPDATE {self._fq_table} SET status = 'pending', "
                    f"updated_at = NOW() "
                    f"WHERE id = %s AND status = 'processing'",
                    task_id,
                )
                await conn.execute(
                    f"DELETE FROM {self._fq_claims} "
                    f"WHERE task_id = %s AND worker_id = %s",
                    task_id, self._worker_id,
                )
                await conn.execute(
                    f"DELETE FROM {self._fq_table} WHERE reply_to = %s "
                    f"AND role = 'assistant' AND status = 'processing'",
                    task_id,
                )
        self._leases.clear()

    # ------------------------------------------------------------------
    # Активность пула воркеров (опциональный вывод в терминал gateway)
    # ------------------------------------------------------------------
    #
    # Включается отключаемой опцией ``gateway.print_worker_activity``
    # (project.json). Печатает через Rich-консоль, когда воркер взял
    # задачу, закончил её (completed/error/failed) и текущий размер
    # очереди (pending/error). Форматом повторяет вывод токенов LLM.

    def _activity_print(self, line: str) -> None:
        """Напечатать строку активности воркера, если флаг включён."""
        if self._print_worker_activity:
            # cp1251-консоль Windows не переваривает юникодные стрелки —
            # заменяем на ASCII-эквивалент до вывода. markup=False держит
            # квадратные метки ([task-worker], [очередь-задач]) как текст.
            safe = line.replace("←", "<-").replace("→", "->")
            console.print(safe, style="dim", markup=False)

    @staticmethod
    def _preview(content: Any, limit: int = 60) -> str:
        """Короткий однострочный превью контента задачи для лога."""
        if not content:
            return ""
        text = " ".join(str(content).split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    async def _report_queue(self) -> None:
        """Одноразовая (по изменению) печать размера очереди задач.

        Считает по ``agent_conversation_messages`` число ожидающих
        (``pending``) и повторяемых (``error``) user-задач. Печатает строку
        только когда суммарное значение изменилось с прошлого опроса.
        """
        try:
            if not self._print_worker_activity:
                return
            row = await fetchone(
                f"SELECT count(*) FILTER (WHERE status = 'pending') AS pending, "
                f"count(*) FILTER (WHERE status = 'error') AS error "
                f"FROM {self._fq_table} WHERE role = 'user'"
            )
            pending = int((row or {}).get("pending") or 0)
            error = int((row or {}).get("error") or 0)
            summary = (pending, error)
            if summary != self._last_queue_summary:
                self._last_queue_summary = summary
                total = pending + error
                self._activity_print(
                    f"[очередь-задач] pending={pending}, error={error} (итого {total})"
                )
        except Exception as e:
            self.logger.debug("Worker queue stats error: {}", e)

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
            try:
                await self._flush_live_context()
            except Exception as e:
                self.logger.debug("Flush live context error: {}", e)

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

    async def _flush_live_context(self) -> None:
        """Живое обновление занятости контекста в processing-строки.

        Каждые ``_flush_interval`` секунд читает блок ``context_window`` из
        моста per-iteration usage (``lib.hooks.database_logging_hook``) и
        пишет его в metadata processing assistant-строки. UI (Streamlit)
        через свой поллинг видит его ДО финализации ответа — прогресс-бар
        заполняется «вживую» по мере роста промпта.

        Блок собирается патчем ``agent._assemble_outbound`` на финале
        оборота, а на лету — из usage последней итерации и лимита,
        засеянного на старте оборота (патч ``agent._state_build``). После
        финализации/ошибки мост очищается (``_drop_context_bridge``), и
        финальный ответ перезаписывает блок тем же значением.
        """
        from lib.hooks.database_logging_hook import get_context_window
        for msg_id, chat_id in list(self._msg_chat.items()):
            ctx = self._msg_ctx.get(msg_id) or {}
            assistant_msg_id = ctx.get("assistant_msg_id")
            if not assistant_msg_id:
                continue
            block = get_context_window(f"postgres:{chat_id}")
            if not block:
                continue
            async with self._reasoning_io_lock:
                row = await fetchone(
                    f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                    assistant_msg_id,
                )
                if not row:
                    continue
                meta = _decode_jsonb(row["metadata"])
                if meta.get("context_window") == block:
                    continue
                meta["context_window"] = block
                await execute(
                    f"UPDATE {self._fq_table} SET metadata = %s, updated_at = NOW() "
                    f"WHERE id = %s",
                    meta, assistant_msg_id,
                )

    # ------------------------------------------------------------------
    # Цикл опроса БД
    # ------------------------------------------------------------------

    async def poll_inbound(self, exchange: MessageExchange) -> bool:
        """Хук транспорта для ``MessageExchange``: берет новое сообщение из БД.

        Каждая итерация:
          1. ``_reclaim_and_heal`` — вернуть в пул задачи с истёкшими арендами
          2. Если есть свободный слот → ``_poll_once`` — взять новое сообщение

        Возвращает True, если сообщение обработано (тогда движок опрашивает
        следующее без ожидания).
        """
        await self._reclaim_and_heal()
        if self._print_worker_activity:
            await self._report_queue()
        if not exchange.is_slot_free():
            return False
        had = await self._poll_once(exchange)
        return bool(had)

    async def _claim_one(self) -> dict | None:
        """Атомарно захватить одну задачу: клейм + status='processing'.

        В одной транзакции:
          1. SELECT самого старого кандидата (``pending`` или ``error`` с
             истёкшим backoff) без активного claim и из чата без активной
             user-задачи;
          2. INSERT claim — арбитр эксклюзивности: PK (task_id) не даёт
             второму воркеру захватить ту же задачу (UniqueViolation);
          3. UPDATE сообщения → status='processing'.

        При UniqueViolation (задачу только что захватил другой воркер)
        транзакция откатывается и пробуем следующего кандидата.
        Возвращает строку-кандидата или None, если задач нет.
        """
        while True:
            try:
                async with transaction() as conn:
                    row = await conn.fetchrow(
                        f"""
                        SELECT id, chat_id, user_id, content, media,
                               metadata, created_at
                        FROM {self._fq_table}
                        WHERE role = 'user'
                          AND (
                              status = 'pending'
                              OR (status = 'error'
                                  AND updated_at + interval '1 second' * %s < NOW())
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM {self._fq_claims} c
                              WHERE c.task_id = {self._fq_table}.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM {self._fq_table} m2
                              WHERE m2.chat_id = {self._fq_table}.chat_id
                                AND m2.role = 'user'
                                AND m2.status = 'processing'
                          )
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        self._error_retry_delay,
                    )
                    if row is None:
                        return None
                    task_id = str(row["id"])
                    await conn.execute(
                        f"INSERT INTO {self._fq_claims} "
                        f"(task_id, worker_id, claimed_at, lease_until, created_at) "
                        f"VALUES (%s, %s, NOW(), "
                        f"NOW() + interval '1 second' * %s, NOW())",
                        task_id, self._worker_id, self._processing_timeout,
                    )
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'processing', "
                        f"updated_at = NOW() WHERE id = %s",
                        task_id,
                    )
                    return row
            except psycopg2.IntegrityError:
                # другой воркер только что захватил этого кандидата —
                # транзакция откачена, пробуем следующего
                self.logger.debug("Claim lost (unique violation), retrying")

    async def _poll_once(self, exchange: MessageExchange) -> bool:
        """Забрать самое старое сообщение (через клейм) и отправить агенту.

        Алгоритм:
          1. ``_claim_one`` — атомарный клейм задачи (INSERT claim + processing)
          2. Проверяем, не занят ли chat_id в этом процессе (chat_inflight)
          3. Создаём assistant-placeholder (чтобы web-клиент мог опрашивать)
          4. Захватываем слот (exchange) → _handle_message

        Если из этого chat_id уже есть активное сообщение в этом процессе,
        возвращаем claim и статус в 'pending' — не диспатчим второе.
        Эксклюзивность клейма (PK claims) гарантирует, что одна задача не
        обрабатывается двумя воркерами одновременно.

        Возвращает True, если сообщение взято в обработку.
        """
        row = await self._claim_one()
        if row is None:
            return False  # нет новых сообщений

        user_msg_id = str(row["id"])
        self._leases.add(user_msg_id)
        chat_id = str(row["chat_id"]) if row["chat_id"] else str(row["user_id"])
        user_id = str(row["user_id"]) if row["user_id"] else chat_id

        # Не диспатчим, если из этого chat_id уже есть активное сообщение
        # в этом же процессе (в БД chat уже считается занятым, но защищаемся
        # от гонки между клеймом и фактическим диспатчем).
        if chat_id in self._chat_inflight:
            await execute(
                f"UPDATE {self._fq_table} SET status = 'pending', "
                f"updated_at = NOW() WHERE id = %s",
                user_msg_id,
            )
            await execute(
                f"DELETE FROM {self._fq_claims} "
                f"WHERE task_id = %s AND worker_id = %s",
                user_msg_id, self._worker_id,
            )
            self._leases.discard(user_msg_id)
            self.logger.debug(
                "Deferred msg {} from busy chat {}", user_msg_id, chat_id,
            )
            return False

        content = row["content"] or ""

        raw_meta = _decode_jsonb(row["metadata"])

        raw_media = row["media"] or []
        if isinstance(raw_media, str):
            raw_media = json.loads(raw_media) if raw_media else []
        media: list[str] = raw_media if isinstance(raw_media, list) else []
        # Декодируем data URL из БД обратно в локальные файлы сессии
        session_key = raw_meta.get("session_key") or f"postgres:{chat_id}"
        media = await self._decode_media_from_db(media, session_key)
        # Агенту передаём только пути; в текст добавляем подсказку, что
        # пользователь приложил файл и где он лежит в кэше сессии.
        media_paths, hints = self._resolve_media_paths_and_hints(media)
        if hints:
            suffix = "\n".join(hints)
            content = f"{content}\n\n{suffix}" if content else suffix
        media = media_paths

        # Создаём assistant-placeholder, чтобы Streamlit мог начать опрос
        try:
            assistant_msg_id = await self._insert_assistant_message(user_msg_id, chat_id)
        except Exception:
            self.logger.exception(
                "Failed to insert assistant placeholder for {}", user_msg_id,
            )
            await execute(
                f"UPDATE {self._fq_table} SET status = 'pending', "
                f"updated_at = NOW() WHERE id = %s",
                user_msg_id,
            )
            await execute(
                f"DELETE FROM {self._fq_claims} "
                f"WHERE task_id = %s AND worker_id = %s",
                user_msg_id, self._worker_id,
            )
            self._leases.discard(user_msg_id)
            return False

        await exchange.acquire_slot()
        exchange.add_inflight(user_msg_id)
        self._chat_inflight.add(chat_id)
        self._msg_chat[user_msg_id] = chat_id
        self._activity_print(
            f"→ [task-worker] {self._worker_id} взял задачу {user_msg_id} "
            f"(chat {chat_id}): {self._preview(content)}"
        )

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
        return True

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
        """Зафиксировать ошибку обработки пользовательского сообщения.

        Вызывается при:
          — ошибке диспетчеризации (\"dispatch_error\")
          — ошибке записи ответа (\"write_error\")

        Семантика статусов:
          — пока ``retry_count < max_stuck_retries`` → user='error'
            (повторяемая ошибка; задача вернётся в пул после
            ``error_retry_delay``), assistant-placeholder удаляется, чтобы
            пользователь не видел ошибочный статус до повторной обработки;
          — иначе → user='failed' (терминальный, не повторяется),
            assistant помечается failed с текстом ошибки.

        Дополнительно:
          — удаляет claim задачи (аренда завершена)
          — удаляет контекст из ``_msg_ctx``
          — освобождает слот (``_release_slot``)
          — чистит буфер рассуждений для этого assistant_msg_id
        """
        chat_id = self._msg_chat.get(user_msg_id)
        async with transaction() as conn:
            meta_row = await conn.fetchrow(
                f"SELECT metadata FROM {self._fq_table} WHERE id = %s",
                user_msg_id,
            )
            meta = _decode_jsonb(meta_row["metadata"]) if meta_row else {}
            retry_count = meta.get("retry_count", 0) + 1
            meta["retry_count"] = retry_count
            meta["error"] = reason

            if retry_count < self._max_stuck_retries:
                # повторяемая ошибка — error + backoff
                if assistant_msg_id:
                    await conn.execute(
                        f"DELETE FROM {self._fq_table} WHERE id = %s "
                        f"AND role = 'assistant'",
                        assistant_msg_id,
                    )
                await conn.execute(
                    f"UPDATE {self._fq_table} SET status = 'error', "
                    f"metadata = %s, updated_at = NOW() "
                    f"WHERE id = %s",
                    meta, user_msg_id,
                )
                self.logger.warning(
                    "User msg {} error ({}/{}) [{}]",
                    user_msg_id, retry_count, self._max_stuck_retries, reason,
                )
            else:
                # терминальный failed
                if assistant_msg_id:
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET content = %s, "
                        f"metadata = %s, status = 'failed', updated_at = NOW() "
                        f"WHERE id = %s",
                        f"Internal error: {reason}", {"error": reason},
                        assistant_msg_id,
                    )
                await conn.execute(
                    f"UPDATE {self._fq_table} SET status = 'failed', "
                    f"metadata = %s, updated_at = NOW() "
                    f"WHERE id = %s",
                    meta, user_msg_id,
                )
                self.logger.error(
                    "User msg {} failed ({}/{}) [{}]",
                    user_msg_id, retry_count, self._max_stuck_retries, reason,
                )
            await conn.execute(
                f"DELETE FROM {self._fq_claims} "
                f"WHERE task_id = %s AND worker_id = %s",
                user_msg_id, self._worker_id,
            )
        self._leases.discard(user_msg_id)
        self._msg_ctx.pop(user_msg_id, None)
        self._release_slot(user_msg_id)
        if assistant_msg_id:
            self._reasoning_buffers.pop(assistant_msg_id, None)
        self._drop_context_bridge(chat_id)
        status = "error" if retry_count < self._max_stuck_retries else "failed"
        self._activity_print(
            f"← [task-worker] {self._worker_id} закончил задачу {user_msg_id} "
            f"(chat {chat_id or '?'}) [{status}]: {reason}"
        )

    def _drop_context_bridge(self, chat_id: str | None) -> None:
        """Снять per-iteration мост контекста для чата (анти-stale).

        Вызывается в финалах оборота (``_finalize_turn``, ``_mark_failed``):
        мост живёт ровно столько, сколько идёт активный оборот. Без
        ``pop`` следующий оборот того же чата унаследовал бы data от
        предыдущего (старый лимит/usage) при гонке рестарта воркера.
        """
        if not chat_id:
            return
        try:
            from lib.hooks.database_logging_hook import pop_context_bridge
            pop_context_bridge(f"postgres:{chat_id}")
        except Exception:
            pass

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
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
    ) -> None:
        """Получить чанк рассуждений от агента и добавить в буфер.

        Параметры:
            chat_id — ID чата (не используется, т.к. берём из metadata)
            delta — текст очередного чанка рассуждений
            metadata — может содержать answer_id (assistant_msg_id)
            stream_id — наноtracing: идентификатор потока (на будущее,
                сейчас ключом буфера остаётся assistant_msg_id)

        Поведение:
            — Если известен assistant_msg_id → пишем в ``_reasoning_buffers``
            — Иначе → буферизируем в ``_msg_ctx`` (будет поднят позже)
        """
        del stream_id  # nanobot 0.3.0 передаёт; канал ключует по assistant_msg_id
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
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
    ) -> None:
        """Сигнал конца рассуждений. Не используется — финализация в send().

        Принимает ``stream_id`` для совместимости с ``nanobot 0.3.0``
        (ChannelManager._send_reasoning_end передаёт его kwarg).
        """
        del stream_id

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
    #   ``FINAL_TURN_KEY`` (``_final_turn``) — МАРКЕР КОНЦА ОБОРОТА → финал
    #   ``_record_channel_delivery`` — промежуточная публикация тула
    #                          ``message(...)`` → только merge, не финал
    #   ``latency_ms``       — признак legacy-финала без ``_final_turn``
    #   (без флагов)         — merge (промежуточная публикация)
    #
    # Ключевой контракт: финализировать (статус completed + удалить claim +
    # освободить слот + убрать ``_msg_ctx``) можно ТОЛЬКО один раз за оборот
    # и только на финальном outbound. Публикации тула ``message(...)``
    # приходят ПОСЛЕДОВАТЕЛЬНО в течение оборота (до его завершения) и НЕ
    # должны трогать слот/клейм/аренду/_msg_ctx — иначе оборот рвётся и
    # задача уходит в ``failed`` через reclaim. Поэтому промежуточные
    # публикации merge'ятся в существующую assistant-строку, а завершение
    # делается на маркере ``_final_turn`` (ставится патчем
    # ``_assemble_outbound``, см. ``runtime_patcher``).
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        meta = dict(msg.metadata or {})
        msg_id = meta.get("origin_message_id") or meta.get("message_id")

        # v0.3.0: runtime-события (прогресс тулов, рассуждения, стриминг)
        # переносятся типизированным полем OutboundMessage.event, а не
        # legacy-флагами в metadata. Такие события обрабатываются отдельными
        # методами (send_delta / send_reasoning_delta), а в send() они
        # попадают только как побочный прогресс (например, ProgressEvent
        # c пустым content после выполнения тула message). Если их
        # обработать как финальный ответ — они перезапишут уже записанные
        # content/media пустыми значениями, и вложение тула message
        # пропадёт из БД. Поэтому типизированные события здесь игнорируем.
        if msg.event is not None:
            return

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
                    stale = [k for k in self._msg_ctx if k not in self.exchange.inflight]
                    for k in stale:
                        self._msg_ctx.pop(k, None)
                ctx = self._msg_ctx.setdefault(msg_id, {"reasoning_buf": []})
            return

        # --- Конец оборота — финализируем слот/клейм/статус ---
        # Маркер ``_final_turn`` ставит патч ``_assemble_outbound`` (или
        # приходит как синтетический outbound при подавленном финале после
        # message(...)). ``_turn_end`` — legacy-сигнал runner'а, тоже финал.
        if meta.get(FINAL_TURN_KEY) or meta.get("_turn_end"):
            await self._finalize_turn(msg, meta, msg_id)
            return

        # --- Промежуточная публикация тула message(...) — merge, НЕ финал ---
        # Сообщение пришло до завершения оборота: дописываем content/media
        # в assistant-строку, но не трогаем слот/клейм/аренду/_msg_ctx.
        if meta.get("_record_channel_delivery"):
            await self._merge_tool_delivery(msg, meta, msg_id)
            return

        # --- Прочие служебные сигналы runner'а (_tool_hint, _stream_end...) ---
        if is_dropped(meta):
            return

        # --- Legacy-финал без ``_turn_end`` (патч не применился) ---
        # Реальный ``_assemble_outbound`` ВСЕГДА кладёт ``latency_ms``;
        # промежуточные публикации ``message(...)`` его не несут.
        if meta.get("latency_ms") is not None:
            await self._finalize_turn(msg, meta, msg_id)
            return

        # --- Обычная промежуточная публикация без явных флагов ---
        # (напр. ``message("text")`` без media, где ``_record_channel_delivery``
        # не выставлен). До завершения оборота — только merge.
        await self._merge_tool_delivery(msg, meta, msg_id)

    async def _merge_tool_delivery(
        self, msg: OutboundMessage, meta: dict[str, Any], msg_id: str | None,
    ) -> None:
        """Дописать промежуточную публикацию тула ``message(...)`` в сроку.

        Вызывается на outbound, пришедших ДО завершения оборота. В отличие
        от ``_finalize_turn`` НЕ трогает слот, клейм, аренду и ``_msg_ctx``:
        оборот ещё продолжается, финализация произойдёт на ``_turn_end``.

        content накапливается (через newline), media — мержится без дублей,
        metadata обновляется. status остаётся ``processing``.
        """
        assistant_msg_id = self._resolve_assistant_msg_id(meta)
        if not assistant_msg_id and msg_id:
            assistant_msg_id = (self._msg_ctx.get(msg_id) or {}).get("assistant_msg_id")
        if not assistant_msg_id:
            self.logger.warning(
                "send: merge skipped, no assistant_msg_id for msg_id={}", msg_id,
            )
            return

        db_media = await self._embed_media_for_db(msg.media or [])
        try:
            async with transaction() as conn:
                row = await conn.fetchrow(
                    f"SELECT metadata, media, content FROM {self._fq_table} "
                    f"WHERE id = %s",
                    assistant_msg_id,
                )
                existing_meta = _decode_jsonb(row["metadata"]) if row else {}
                existing_meta.update(meta)

                existing_media = row["media"] if row else []
                if isinstance(existing_media, str):
                    existing_media = json.loads(existing_media) if existing_media else []
                if not isinstance(existing_media, list):
                    existing_media = []
                merged_media = list(existing_media)
                for m in db_media:
                    if m not in merged_media:
                        merged_media.append(m)

                existing_content = row["content"] if row else ""
                if not isinstance(existing_content, str):
                    existing_content = ""
                if msg.content and msg.content != existing_content:
                    existing_content = (
                        f"{existing_content}\n\n{msg.content}" if existing_content
                        else msg.content
                    )

                await conn.execute(
                    f"UPDATE {self._fq_table} "
                    f"SET content = %s, metadata = %s, buttons = %s, media = %s, "
                    f"updated_at = NOW() WHERE id = %s",
                    existing_content, existing_meta,
                    Json(msg.buttons or []), Json(merged_media),
                    assistant_msg_id,
                )
        except Exception:
            self.logger.exception(
                "Failed to merge tool delivery for msg_id={}", msg_id,
            )

    async def _finalize_turn(
        self, msg: OutboundMessage, meta: dict[str, Any], msg_id: str | None,
    ) -> None:
        """Зафинализировать оборот: записать ответ, закрыть claim и слот.

        Вызывается один раз за оборот — на маркере ``_turn_end`` (или на
        legacy-финале с ``latency_ms``). Единственное место, где:
          — снимается ``_msg_ctx`` (``pop``);
          — ``status='completed'`` и удаляется claim;
          — освобождается слот параллельности.

        ``_release_slot`` вызывается ПОСЛЕ успешной записи (и на ошибке через
        ``_mark_failed``). Раньше его вызывали до транзакции — задача снималась
        с heartbeat (``_leases``), claim ещё жил, и ``_reclaim_and_heal`` на
        другом воркере мог забрать задачу и довести до ``failed``.
        """
        ctx = self._msg_ctx.pop(msg_id, {}) if msg_id else {}
        assistant_msg_id = ctx.get("assistant_msg_id") or meta.get("answer_id")
        if not assistant_msg_id:
            self._release_slot(msg_id)
            self.logger.warning("send: no assistant_msg_id for msg_id={}", msg_id)
            if msg.chat_id:
                self._drop_context_bridge(msg.chat_id)
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

        # Кодируем локальные файлы в data URL для хранения в БД
        db_media = await self._embed_media_for_db(msg.media or [])

        try:
            async with transaction() as conn:
                row = await conn.fetchrow(
                    f"SELECT metadata, media, content FROM {self._fq_table} WHERE id = %s",
                    assistant_msg_id,
                )
                existing_meta = _decode_jsonb(row["metadata"]) if row else {}
                existing_meta.update(meta)
                # Не затираем вложения, прикреплённые тулом message в этом же
                # обороте: если финальный ответ приходит без собственных media,
                # сохраняем ранее записанные data URL.
                existing_media = row["media"] if row else []
                if isinstance(existing_media, str):
                    existing_media = json.loads(existing_media) if existing_media else []
                if not isinstance(existing_media, list):
                    existing_media = []
                final_media = db_media if db_media else existing_media
                # Если финальный outbound пуст (синтетический ``_turn_end``
                # после message(...)), сохраняем контент, накопленный merge'ем.
                existing_content = row["content"] if row else ""
                if not isinstance(existing_content, str):
                    existing_content = ""
                final_content = msg.content if msg.content else existing_content
                await conn.execute(
                    f"UPDATE {self._fq_table} "
                    f"SET content = %s, metadata = %s, buttons = %s, "
                    f"media = %s, "
                    f"status = 'completed', updated_at = NOW() WHERE id = %s",
                    final_content, existing_meta,
                    Json(msg.buttons or []), Json(final_media),
                    assistant_msg_id,
                )
                if msg_id:
                    await conn.execute(
                        f"UPDATE {self._fq_table} SET status = 'completed', "
                        f"updated_at = NOW() WHERE id = %s",
                        msg_id,
                    )
                    await conn.execute(
                        f"DELETE FROM {self._fq_claims} "
                        f"WHERE task_id = %s AND worker_id = %s",
                        msg_id, self._worker_id,
                    )
            # Слот освобождаем ПОСЛЕ успешной записи клейма/статуса.
            self._release_slot(msg_id)
            self._activity_print(
                f"← [task-worker] {self._worker_id} закончил задачу {msg_id} "
                f"(chat {chat_id}) [completed]"
            )
        except Exception:
            self.logger.exception("Failed to write response for {}", chat_id)
            if msg_id:
                await self._mark_failed(msg_id, assistant_msg_id, "write_error")
        finally:
            if chat_id:
                self._drop_context_bridge(chat_id)

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
    ) -> None:
        """Получить очередной чанк стримингового ответа от агента.

        Когда агент использует стриминг (потоковую генерацию),
        каждый фрагмент текста приходит через ``send_delta``.

        Совместимость с ``nanobot 0.3.0``: ``stream_id`` приходит как kwarg;
        ``stream_end`` маркирует последний чанк; ``resuming`` — возобновление
        потока (буфер не сбрасывается).

        Поведение:
          — ``stream_end=True`` → финализируем: достаём накопленный текст,
            пишем в БД как status='completed', освобождаем слот.
          — Иначе → накапливаем текст в ``_stream_buffers[stream_id]``.
        """
        del resuming  # на текущей стороне буфер ключуется по stream_id
        meta = dict(metadata or {})
        buf_key = stream_id or meta.get("_stream_id") or chat_id

        if stream_end or meta.get("_stream_end"):
            msg_id = meta.get("origin_message_id") or meta.get("message_id")
            ctx = self._msg_ctx.pop(msg_id, {}) if msg_id else {}
            stream_chat_id = self._msg_chat.get(msg_id) if msg_id else None
            self._release_slot(msg_id)
            assistant_msg_id = ctx.get("assistant_msg_id") or meta.get("answer_id")

            if ctx.get("reasoning_buf"):
                meta["reasoning"] = " ".join(ctx["reasoning_buf"])

            content = self._stream_buffers.pop(buf_key, "")
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
                        await conn.execute(
                            f"DELETE FROM {self._fq_claims} "
                            f"WHERE task_id = %s AND worker_id = %s",
                            msg_id, self._worker_id,
                        )
                        self._activity_print(
                            f"← [task-worker] {self._worker_id} закончил задачу {msg_id} "
                            f"(chat {stream_chat_id or '?'}) [streamed/completed]"
                        )
        else:
            buf = self._stream_buffers.get(buf_key, "")
            self._stream_buffers[buf_key] = buf + delta

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
        второй вызов будет no-op (проверка в ``exchange.release_slot``).

        Дополнительно:
          — удаляет chat_id из ``_chat_inflight`` (если был)
          — удаляет запись из ``_msg_chat``
          — снимает задачу с аренды этого воркера (``_leases``)
        """
        if not user_msg_id:
            return
        self.exchange.release_slot(user_msg_id)
        self._leases.discard(user_msg_id)
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
            "table_name": "agent_conversation_messages",
            "claims_table": "agent_worker_claims",
            "poll_interval": 2.0,
            "flush_interval": 2.0,
            "max_concurrent": 1,
            "processing_timeout": 600,
            "lease_interval": 15.0,
            "error_retry_delay": 60.0,
            "max_stuck_retries": 3,
            "worker_id": "",
            "allow_from": ["*"],
        }
