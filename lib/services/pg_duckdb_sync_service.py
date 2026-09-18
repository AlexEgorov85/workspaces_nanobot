"""
PgDuckDbSyncService вЂ” С„РѕРЅРѕРІР°СЏ СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ audit-РґР°РЅРЅС‹С… РёР· PostgreSQL РІ РєСЌС€.

РћС‚РІРµС‡Р°РµС‚ Р·Р°:
  * РёРЅРєСЂРµРјРµРЅС‚Р°Р»СЊРЅСѓСЋ СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЋ РґР°РЅРЅС‹С… РёР· PG РІ in-memory РєСЌС€ (DuckDbCacheStore);
  * РєРѕСЂСЂРµРєС‚РЅРѕРµ Р·Р°РІРµСЂС€РµРЅРёРµ (graceful shutdown) СЃ РіР°СЂР°РЅС‚РёРµР№ СЃРѕС…СЂР°РЅРµРЅРёСЏ РѕС‡РµСЂРµРґРё.

Р’РµСЃСЊ SQL-РґРѕСЃС‚СѓРї РёРґС‘С‚ С‡РµСЂРµР· РѕР±С‰РёР№ РїСѓР» ``utils.db`` (worker-РїРѕС‚РѕРє РЅРµ РґРµСЂР¶РёС‚
СЃРѕР±СЃС‚РІРµРЅРЅРѕРіРѕ psycopg2-СЃРѕРµРґРёРЅРµРЅРёСЏ вЂ” СЃРѕРµРґРёРЅРµРЅРёРµ РІС‹РґР°С‘С‚ РїСѓР» РЅР° РІСЂРµРјСЏ Р·Р°РїСЂРѕСЃР°,
РѕР±СЂС‹РІ Рё РїРµСЂРµРїРѕРґРєР»СЋС‡РµРЅРёРµ РѕР±СЃР»СѓР¶РёРІР°РµС‚ СЃР°Рј РїСѓР»). РРЅРєСЂРµРјРµРЅС‚Р°Р»СЊРЅС‹Рµ РјРµС‚РєРё
(``_last_sync``) Р¶РёРІСѓС‚ РІ СЃРµСЂРІРёСЃРµ Рё СЃР±СЂР°СЃС‹РІР°СЋС‚СЃСЏ РїСЂРё РѕР±СЂС‹РІРµ, С‡С‚РѕР±С‹
РїРµСЂРµР·Р°РіСЂСѓР·РёС‚СЊ С‚Р°Р±Р»РёС†С‹ С†РµР»РёРєРѕРј. РџСѓР±Р»РёС‡РЅС‹Р№ API Р±РµР·РѕРїР°СЃРµРЅ РґР»СЏ РІС‹Р·РѕРІР°
РёР· asyncio/Р»СЋР±РѕРіРѕ РїРѕС‚РѕРєР°:

    sync_service = PgDuckDbSyncService(dsn=dsn, tables=[...])
    sync_service.set_on_new_records_callback(memory_store.upsert_records)
    sync_service.start(initial_load=True)
    ...
    sync_service.stop(timeout_sec=10.0)

РљРѕРјР°РЅРґС‹ РІ РѕС‡РµСЂРµРґРё: ``POLL_CHANGES`` (РЅРµРјРµРґР»РµРЅРЅС‹Р№ РїРѕР»Р»РёРЅРі), ``SHUTDOWN``
(sentinel Р·Р°РІРµСЂС€РµРЅРёСЏ).
"""

from __future__ import annotations

import datetime
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

COMMAND_POLL = "POLL_CHANGES"
COMMAND_SHUTDOWN = "SHUTDOWN"


class PgDuckDbSyncService:
    """Р¤РѕРЅРѕРІР°СЏ СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ РїСЂРѕРёР·РІРѕР»СЊРЅС‹С… С‚Р°Р±Р»РёС† РёР· PostgreSQL РІ in-memory РєСЌС€.

    Worker-РїРѕС‚РѕРє РІР»Р°РґРµРµС‚ РµРґРёРЅСЃС‚РІРµРЅРЅС‹Рј РїРѕРґРєР»СЋС‡РµРЅРёРµРј Рє PG. РџРѕР»Р»РёРЅРі С‚Р°Р±Р»РёС†
    РёРЅРєСЂРµРјРµРЅС‚Р°Р»РµРЅ (РїРѕ track-РєРѕР»РѕРЅРєРµ), РЅРѕРІС‹Рµ/РёР·РјРµРЅС‘РЅРЅС‹Рµ СЃС‚СЂРѕРєРё РїРµСЂРµРґР°СЋС‚СЃСЏ
    РІ callback ``on_new_records(table, records)`` вЂ” РѕР±С‹С‡РЅРѕ СЌС‚Рѕ
    :class:`DuckDbCacheStore.upsert_records`.

    РРјСЏ РєР»Р°СЃСЃР° СЃРѕС…СЂР°РЅРµРЅРѕ РґР»СЏ back-compat (СЃРј. TARGET_ARCHITECTURE.md В§15).
    """

    def __init__(
        self,
        dsn: str,
        schema: str = "main",
        tables: list[str] | None = None,
        vector_table: str = "",
        poll_interval_sec: float = 0.0,
        max_queue_size: int = 0,
        reconnect_backoff: float = 0.0,
        reconnect_backoff_max: float = 0.0,
        full_resync_every: int = 0,
        db_logging_service: Any | None = None,
    ) -> None:
        self._dsn = dsn
        self._schema = schema
        self._tables = [t for t in (tables or []) if t]
        self._vector_table = vector_table
        # Все параметры — обязательны, передаются явно из settings (project.json).
        # Никаких defaults в коде (TARGET: конфигурация только в settings).
        self._poll_interval = float(poll_interval_sec)
        self._max_queue_size = max_queue_size
        self._reconnect_backoff = reconnect_backoff
        self._reconnect_backoff_max = reconnect_backoff_max
        self._full_resync_every = max(0, int(full_resync_every))
        self._resync_counter = 0
        # Опциональный sink в ``agent_gateway_logs`` (через ``DbLoggingService``,
        # async/пул). Если None — события идут через ``event_log.record_sync_event``
        # (sync, всегда работает при logging.db.enabled+DSN). См. ``_log_sync_event``.
        self._db_logging_service = db_logging_service

        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=max_queue_size)
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()

        self._conn: psycopg2.extensions.connection | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._initial_load = True

        # РРЅРєСЂРµРјРµРЅС‚Р°Р»СЊРЅС‹Р№ РїРѕР»Р»РёРЅРі: {table: РїРѕСЃР»РµРґРЅРµРµ Р·РЅР°С‡РµРЅРёРµ track-РєРѕР»РѕРЅРєРё}
        self._last_sync: dict[str, Any] = {}
        # Batch-prefetch: {table: track-РєРѕР»РѕРЅРєР°}. Р—Р°РїРѕР»РЅСЏРµС‚СЃСЏ РїСЂРё РїРµСЂРІРѕРј РѕРїСЂРѕСЃРµ,
        # РґР°Р»РµРµ С‡РёС‚Р°РµС‚СЃСЏ Р·Р° O(1). РЈСЃС‚СЂР°РЅСЏРµС‚ РїРѕРІС‚РѕСЂРЅС‹Р№ lookup С‡РµСЂРµР· table_registry
        # РЅР° РєР°Р¶РґРѕРј poll-С†РёРєР»Рµ.
        self._column_cache: dict[str, str] = {}
        self._on_new_records: Callable[[str, list[dict]], None] | None = None
        self._on_replace_records: Callable[[str, list[dict]], None] | None = None
        self._on_schema: Callable[[str, list[dict]], None] | None = None
        self._on_sync_callback: Callable[[], None] | None = None

        self._stats: dict[str, Any] = {
            "started_at": None,
            "polls": 0,
            "full_resyncs": 0,
            "queue_full": 0,
            "reconnects": 0,
            "errors": 0,
            "tables": list(self._tables),
        }

    # ------------------------------------------------------------------
    # РџСѓР±Р»РёС‡РЅС‹Р№ API
    # ------------------------------------------------------------------

    def set_on_new_records_callback(
        self, callback: Callable[[str, list[dict]], None]
    ) -> None:
        """Р—Р°РґР°С‚СЊ callback РґР»СЏ РЅРѕРІС‹С…/РёР·РјРµРЅС‘РЅРЅС‹С… СЃС‚СЂРѕРє: ``callback(table, records)``."""
        self._on_new_records = callback

    def set_on_replace_records_callback(
        self, callback: Callable[[str, list[dict]], None]
    ) -> None:
        """Р—Р°РґР°С‚СЊ callback РґР»СЏ РїРѕР»РЅРѕР№ РїРµСЂРµСЃРёРЅС…СЂРѕРЅРёР·Р°С†РёРё: ``callback(table, records)``.

        Р’С‹Р·С‹РІР°РµС‚СЃСЏ РїСЂРё РїРµСЂРёРѕРґРёС‡РµСЃРєРѕР№ РїРѕР»РЅРѕР№ РїРµСЂРµР·Р°РіСЂСѓР·РєРµ С‚Р°Р±Р»РёС†С‹ (СЃРІРµСЂРєР°
        СѓРґР°Р»С‘РЅРЅС‹С… СЃС‚СЂРѕРє). РћР±С‹С‡РЅРѕ СЌС‚Рѕ ``DuckDbCacheStore.replace_records``.
        """
        self._on_replace_records = callback

    def set_on_schema_callback(
        self, callback: Callable[[str, list[dict]], None]
    ) -> None:
        """Р—Р°РґР°С‚СЊ callback РґР»СЏ РѕРїРёСЃР°РЅРёСЏ РєРѕР»РѕРЅРѕРє С‚Р°Р±Р»РёС†С‹ РёР· PG information_schema.

        Р’С‹Р·С‹РІР°РµС‚СЃСЏ РїРµСЂРµРґ Р·Р°РіСЂСѓР·РєРѕР№/РїРѕР»РЅРѕР№ РїРµСЂРµСЃРёРЅС…СЂРѕРЅРёР·Р°С†РёРµР№ С‚Р°Р±Р»РёС†С‹:
        ``callback(table, columns)``, РіРґРµ ``columns`` вЂ” СЃРїРёСЃРѕРє РѕРїРёСЃР°РЅРёР№
        ``[{"name", "type", "not_null", "comment"}, ...]``. РћР±С‹С‡РЅРѕ СЌС‚Рѕ
        ``DuckDbCacheStore.ensure_schema``.
        """
        self._on_schema = callback

    def set_on_sync_callback(self, callback: Callable[[], None]) -> None:
        """Р—Р°РґР°С‚СЊ callback РїРѕ Р·Р°РІРµСЂС€РµРЅРёРё С†РёРєР»Р° СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёРё.

        Р’С‹Р·С‹РІР°РµС‚СЃСЏ РёР· worker-РїРѕС‚РѕРєР° РїРѕСЃР»Рµ initial load Рё РїРѕСЃР»Рµ РєР°Р¶РґРѕРіРѕ
        РїРѕР»Р»РёРЅРіР° вЂ” СѓРґРѕР±РЅРѕ РґР»СЏ РїСѓР±Р»РёРєР°С†РёРё СЃРЅРёРјРєР° РєРµС€Р° (store.publish).
        """
        self._on_sync_callback = callback

    def _log_sync_event(
        self,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        *,
        level: str = "INFO",
        name: str | None = None,
    ) -> None:
        """Записать sync-событие в ``agent_gateway_logs`` (единый конвейер).

        Единственный writer — ``DbLoggingService`` через ``try_log_event``.
        """
        from lib.services.db_logging_service import LogEvent, try_log_event

        log_event = LogEvent(
            event_type=event_type,
            level=level,
            session_id="gateway:sync",
            channel=None,
            actor="sync",
            name=name or event_type,
            summary=summary,
            payload=payload,
        )
        try_log_event(
            self._db_logging_service,
            log_event,
            producer="PgDuckDbSyncService",
            event_type=event_type,
        )

    def start(self, initial_load: bool = True) -> None:
        """Р—Р°РїСѓСЃС‚РёС‚СЊ worker-РїРѕС‚РѕРє.

        Args:
            initial_load: РµСЃР»Рё True вЂ” СЃРЅР°С‡Р°Р»Р° РїРѕР»РЅР°СЏ Р·Р°РіСЂСѓР·РєР° РІСЃРµС… С‚Р°Р±Р»РёС†.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning(
                "PgDuckDbSyncService.start: worker СѓР¶Рµ Р·Р°РїСѓС‰РµРЅ, РїРѕРІС‚РѕСЂРЅС‹Р№ start РёРіРЅРѕСЂРёСЂСѓРµС‚СЃСЏ."
            )
            return
        self._initial_load = bool(initial_load)
        self._running = True
        self._stop_event.clear()
        self._stats["started_at"] = time.time()
        logger.info(
            "PgDuckDbSyncService.start: initial_load=%s tables=%d dsn_set=%s vector_table=%s",
            self._initial_load,
            len(self._tables),
            bool(self._dsn),
            self._vector_table or "(none)",
        )
        self._log_sync_event(
            event_type="sync_service_started",
            summary=f"initial_load={self._initial_load} tables={len(self._tables)}",
            payload={
                "initial_load": self._initial_load,
                "tables": list(self._tables),
                "vector_table": self._vector_table or None,
                "dsn_set": bool(self._dsn),
            },
            level="INFO",
        )
        self._thread = threading.Thread(
            target=self._worker, name="audit-sync", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_sec: float = 10.0) -> None:
        """РћСЃС‚Р°РЅРѕРІРёС‚СЊ worker-РїРѕС‚РѕРє, СЃРѕС…СЂР°РЅРёРІ РѕСЃС‚Р°РІС€РёРµСЃСЏ Р·Р°РїРёСЃРё РёР· РѕС‡РµСЂРµРґРё."""
        self._running = False
        self._stop_event.set()
        try:
            self._queue.put_nowait((COMMAND_SHUTDOWN, None))
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout_sec)
            self._thread = None
        self._close_connection()

    def get_stats(self) -> dict[str, Any]:
        """РњРѕРЅРёС‚РѕСЂРёРЅРі: СЂР°Р·РјРµСЂ РѕС‡РµСЂРµРґРё, СЃС‡С‘С‚С‡РёРєРё, СЃРѕСЃС‚РѕСЏРЅРёРµ РїРѕРґРєР»СЋС‡РµРЅРёСЏ."""
        with self._state_lock:
            stats = dict(self._stats)
        connected = False
        try:
            from utils.db import get_stats as _pool_stats

            connected = _pool_stats().get("connected", 0) > 0
        except Exception:
            pass
        stats.update(
            {
                "running": self._running,
                "connected": connected,
                "queue_size": self._queue.qsize(),
                "last_sync": dict(self._last_sync),
                "full_resync_every": self._full_resync_every,
            }
        )
        return stats

    def get_sync_stats(self) -> dict[str, Any]:
        """РџСЃРµРІРґРѕРЅРёРј ``get_stats`` (РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РІ РјРѕРЅРёС‚РѕСЂРёРЅРіРµ/Р»РѕРіР°С…)."""
        return self.get_stats()

    # ------------------------------------------------------------------
    # Worker-С†РёРєР»
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        try:
            self._ensure_connected()
            if self._initial_load:
                self._do_initial_load()
            self._fire_sync_callback()
            while self._running:
                self._drain_queue()
                if not self._running:
                    break
                self._poll_changes()
                self._fire_sync_callback()
                # Р–РґС‘Рј РёРЅС‚РµСЂРІР°Р» РїРѕР»Р»РёРЅРіР° РёР»Рё СЃРёРіРЅР°Р» РѕСЃС‚Р°РЅРѕРІРєРё
                self._stop_event.wait(self._poll_interval)
        finally:
            self._running = False
            # Р¤РёРЅР°Р»СЊРЅР°СЏ РїРѕРїС‹С‚РєР° РґРѕРїРёСЃР°С‚СЊ РѕСЃС‚Р°РІС€РёРµСЃСЏ Р·Р°РїРёСЃРё
            try:
                self._drain_queue()
            except Exception:
                pass
            self._close_connection()

    def _fire_sync_callback(self) -> None:
        """Уведомить о завершении цикла синхронизации (после load/поллинга)."""
        cb = self._on_sync_callback
        if cb is None:
            logger.warning(
                "PgDuckDbSyncService: _fire_sync_callback вызван, но _on_sync_callback=None "
                "(publish в cache.duckdb не произойдёт)."
            )
            self._log_sync_event(
                event_type="sync_publish_skipped",
                summary="_on_sync_callback=None — publish в cache.duckdb не произойдёт",
                level="WARN",
            )
            return
        try:
            cb()
        except Exception as exc:
            with self._state_lock:
                self._stats["errors"] += 1
            logger.warning(
                "PgDuckDbSyncService: _on_sync_callback бросил исключение: %s",
                exc,
                exc_info=True,
            )
            self._log_sync_event(
                event_type="sync_publish_failed",
                summary=f"_on_sync_callback exception: {exc}",
                payload={"error_type": type(exc).__name__, "error": str(exc)},
                level="WARN",
            )

    def _drain_queue(self) -> None:
        """РћР±СЂР°Р±РѕС‚Р°С‚СЊ РІСЃРµ РєРѕРјР°РЅРґС‹ РёР· РѕС‡РµСЂРµРґРё (РЅРµР±Р»РѕРєРёСЂСѓСЋС‰Рµ)."""
        while True:
            try:
                cmd, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd == COMMAND_POLL:
                    self._poll_changes()
                elif cmd == COMMAND_SHUTDOWN:
                    self._running = False
            except Exception:
                with self._state_lock:
                    self._stats["errors"] += 1
                self._reconnect()
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # РџРѕР»Р»РёРЅРі С‚Р°Р±Р»РёС†
    # ------------------------------------------------------------------

    def _track_column_for(self, table: str) -> str:
        """Р’РµСЂРЅСѓС‚СЊ РєРѕР»РѕРЅРєСѓ РґР»СЏ РёРЅРєСЂРµРјРµРЅС‚Р°Р»СЊРЅРѕРіРѕ РѕС‚СЃР»РµР¶РёРІР°РЅРёСЏ РёР·РјРµРЅРµРЅРёР№.

        РСЃС‚РѕС‡РЅРёРє РёСЃС‚РёРЅС‹ вЂ” ``lib.services.table_registry`` (С‡РµСЂРµР·
        ``SkillRegistration.tracking_column_for(table)``). Р­С‚Рѕ РїРѕР·РІРѕР»СЏРµС‚
        skill'Р°Рј Р·Р°РґР°РІР°С‚СЊ per-table track-РєРѕР»РѕРЅРєСѓ С‡РµСЂРµР·
        ``TableResource.tracking_column`` Р±РµР· РїСЂР°РІРѕРє core.
        Fallback вЂ” ``updated_at`` РґР»СЏ РѕР±С‹С‡РЅС‹С… С‚Р°Р±Р»РёС†, ``id`` РґР»СЏ vector.

        РћРїС‚РёРјРёР·Р°С†РёСЏ: СЂРµР·СѓР»СЊС‚Р°С‚ РєРµС€РёСЂСѓРµС‚СЃСЏ РІ ``self._column_cache`` РїРѕСЃР»Рµ
        РїРµСЂРІРѕРіРѕ lookup'Р° вЂ” РїРѕСЃР»РµРґСѓСЋС‰РёРµ РІС‹Р·РѕРІС‹ Р·Р° O(1).
        """
        cached = self._column_cache.get(table)
        if cached is not None:
            return cached

        try:
            from lib.services.table_registry import table_registry
            reg = table_registry.skill_for_table(table)
            if reg is not None:
                col = reg.tracking_column_for(table)
                self._column_cache[table] = col
                return col
        except Exception:
            pass
        col = "id" if table == self._vector_table else "updated_at"
        self._column_cache[table] = col
        return col

    def _do_initial_load(self) -> None:
        """РџР°СЂР°Р»Р»РµР»СЊРЅР°СЏ РЅР°С‡Р°Р»СЊРЅР°СЏ Р·Р°РіСЂСѓР·РєР° РІСЃРµС… С‚Р°Р±Р»РёС† С‡РµСЂРµР· thread-pool.

        РљР°Р¶РґР°СЏ С‚Р°Р±Р»РёС†Р° РїРѕР»Р»СЊРёС‚СЃСЏ РІ РѕС‚РґРµР»СЊРЅРѕРј РїРѕС‚РѕРєРµ (psycopg2 connections
        Р±РµСЂСѓС‚СЃСЏ РёР· РѕР±С‰РµРіРѕ РїСѓР»Р° utils.db, РєРѕС‚РѕСЂС‹Р№ thread-safe).
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not self._tables:
            logger.warning(
                "PgDuckDbSyncService: initial_load Р·Р°РїСѓС‰РµРЅ, РЅРѕ self._tables РїСѓСЃС‚ "
                "(РЅРё РѕРґРЅРѕР№ С‚Р°Р±Р»РёС†С‹ РЅРµ Р·Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°РЅРѕ вЂ” _make_sync_services РїРµСЂРµРґР°Р» РїСѓСЃС‚РѕР№ СЃРїРёСЃРѕРє)."
            )
            return

        logger.info(
            "PgDuckDbSyncService: initial_load START tables=%d dsn_set=%s",
            len(self._tables),
            bool(self._dsn),
        )
        self._log_sync_event(
            event_type="sync_initial_load_started",
            summary=f"initial_load START tables={len(self._tables)} dsn_set={bool(self._dsn)}",
            payload={"tables": list(self._tables), "dsn_set": bool(self._dsn)},
            level="INFO",
        )

        # max_workers = число таблиц (но не более 8, чтобы не утилизировать пул)
        max_workers = min(len(self._tables), 8)
        loaded_count = 0
        error_count = 0
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="audit-sync-init") as ex:
            futures = {
                ex.submit(self._poll_table_initial, table): table
                for table in self._tables
            }
            for future in as_completed(futures):
                if not self._running:
                    return
                table = futures[future]
                try:
                    rows = future.result()
                    if rows is None:
                        rows = 0
                    loaded_count += 1
                    logger.info(
                        "PgDuckDbSyncService: initial_load loaded %d rows for %s",
                        rows,
                        table,
                    )
                    self._log_sync_event(
                        event_type="sync_table_loaded",
                        summary=f"{table}: {rows} rows",
                        payload={"table": table, "rows": rows},
                        level="INFO",
                    )
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                    logger.warning(
                        "PgDuckDbSyncService: initial_load OperationalError на %s — пробую reconnect",
                        table,
                        exc_info=True,
                    )
                    self._log_sync_event(
                        event_type="sync_initial_load_error",
                        summary=f"OperationalError на {table}",
                        payload={"table": table, "error_type": "OperationalError", "error": str(exc)},
                        level="WARN",
                    )
                    self._reconnect()
                    return
                except psycopg2.errors.UndefinedTable as exc:
                    error_count += 1
                    logger.error(
                        "PgDuckDbSyncService: таблица-источник не найдена: %s "
                        "— пропускаю. Проверьте настройки db.tables/db.additional_tables "
                        "в project.json::skills.<name> для соответствующего skill'а.",
                        table,
                    )
                    self._log_sync_event(
                        event_type="sync_table_missing",
                        summary=f"таблица-источник не найдена: {table}",
                        payload={"table": table, "error_type": "UndefinedTable"},
                        level="ERROR",
                    )
                except Exception as exc:
                    error_count += 1
                    logger.warning(
                        "PgDuckDbSyncService: initial_load FAILED %s: %s",
                        table,
                        exc,
                        exc_info=True,
                    )
                    self._log_sync_event(
                        event_type="sync_initial_load_error",
                        summary=f"initial_load FAILED {table}: {exc}",
                        payload={
                            "table": table,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        level="WARN",
                    )
                    with self._state_lock:
                        self._stats["errors"] += 1

        logger.info(
            "PgDuckDbSyncService: initial_load DONE loaded_ok=%d errors=%d total_tables=%d",
            loaded_count,
            error_count,
            len(self._tables),
        )
        self._log_sync_event(
            event_type="sync_initial_load_done",
            summary=(
                f"initial_load DONE loaded_ok={loaded_count} errors={error_count} "
                f"total_tables={len(self._tables)}"
            ),
            payload={
                "loaded_ok": loaded_count,
                "errors": error_count,
                "total_tables": len(self._tables),
            },
            level="WARN" if error_count else "INFO",
        )

    def _poll_table_initial(self, table: str) -> int:
        """Начальная загрузка одной таблицы (для ThreadPoolExecutor).

        Возвращает количество загруженных строк (для логирования).
        """
        self._ensure_table_schema(table)
        rows, last = self._fetch_all(table)
        self._dispatch(table, rows)
        with self._state_lock:
            if last is not None:
                self._last_sync[table] = last
            else:
                # РџСѓСЃС‚Р°СЏ С‚Р°Р±Р»РёС†Р°: Р·Р°РїРѕРјРёРЅР°РµРј "СЃРµР№С‡Р°СЃ", С‡С‚РѕР±С‹ РґР°Р»СЊС€Рµ
                # РїРѕР»Р»РёС‚СЊ РёРЅРєСЂРµРјРµРЅС‚Р°Р»СЊРЅРѕ, Р° РЅРµ РїРµСЂРµС‡РёС‚С‹РІР°С‚СЊ РІСЃС‘.
                self._last_sync[table] = datetime.datetime.now(
                    datetime.UTC
                )
        return len(rows)

    def _poll_changes(self) -> None:
        """РџР°СЂР°Р»Р»РµР»СЊРЅС‹Р№ РёРЅРєСЂРµРјРµРЅС‚Р°Р»СЊРЅС‹Р№ РїРѕР»Р»РёРЅРі РІСЃРµС… С‚Р°Р±Р»РёС†."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not self._running:
            return
        if not self._tables:
            return

        max_workers = min(len(self._tables), 8)
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="audit-sync-poll") as ex:
            futures = {
                ex.submit(self._poll_table, table): table
                for table in self._tables
            }
            for future in as_completed(futures):
                if not self._running:
                    return
                table = futures[future]
                try:
                    future.result()
                    with self._state_lock:
                        self._stats["polls"] += 1
                except (psycopg2.OperationalError, psycopg2.InterfaceError):
                    self._reconnect()
                    return
                except psycopg2.errors.UndefinedTable:
                    logger.error(
                        "PgDuckDbSyncService: С‚Р°Р±Р»РёС†Р°-РёСЃС‚РѕС‡РЅРёРє РЅРµ РЅР°Р№РґРµРЅР° РїСЂРё РїРѕР»Р»РёРЅРіРµ: %s "
                        "вЂ” РїСЂРѕРїСѓСЃРєР°СЋ. РџСЂРѕРІРµСЂСЊС‚Рµ РЅР°СЃС‚СЂРѕР№РєРё db.tables/db.additional_tables "
                        "РІ project.json::skills.<name> РґР»СЏ СЃРѕРѕС‚РІРµС‚СЃС‚РІСѓСЋС‰РµРіРѕ skill'Р°.",
                        table,
                    )
                except Exception:
                    with self._state_lock:
                        self._stats["errors"] += 1

    def _poll_table(self, table: str) -> None:
        # РџРµСЂРёРѕРґРёС‡РµСЃРєР°СЏ РїРѕР»РЅР°СЏ РїРµСЂРµСЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ вЂ” СЃРІРµСЂРєР° СѓРґР°Р»С‘РЅРЅС‹С… СЃС‚СЂРѕРє.
        if self._full_resync_every > 0:
            self._resync_counter += 1
            if self._resync_counter >= self._full_resync_every:
                self._resync_counter = 0
                with self._state_lock:
                    self._stats["full_resyncs"] += 1
                self._ensure_table_schema(table)
                rows, last = self._fetch_all(table)
                self._dispatch_replace(table, rows)
                # РєСѓСЂСЃРѕСЂ РЅРµ РѕС‚РєР°С‚С‹РІР°РµРј: РЅРѕРІРѕРµ Р·РЅР°С‡РµРЅРёРµ С‚РѕР»СЊРєРѕ РµСЃР»Рё РѕРЅРѕ Р±РѕР»СЊС€Рµ
                prev = self._last_sync.get(table)
                if last is not None and (prev is None or last > prev):
                    self._last_sync[table] = last
                return
        track_col = self._track_column_for(table)
        last = self._last_sync.get(table)
        if last is None:
            rows, last = self._fetch_all(table)
        else:
            rows, new_last = self._fetch_incremental(table, track_col, last)
            if new_last is not None:
                last = new_last
        self._dispatch(table, rows)
        if last is not None:
            self._last_sync[table] = last

    def _dispatch(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        callback = self._on_new_records
        if callback is None:
            logger.warning(
                "PgDuckDbSyncService: _dispatch(%s, %d rows), но _on_new_records=None "
                "— данные не попадут в DuckDbCacheStore.upsert_records.",
                table,
                len(rows),
            )
            self._log_sync_event(
                event_type="sync_dispatch_skipped",
                summary=f"callback отсутствует для {table} ({len(rows)} rows)",
                payload={"table": table, "rows": len(rows)},
                level="WARN",
            )
            return
        try:
            callback(table, rows)
        except Exception as exc:
            with self._state_lock:
                self._stats["errors"] += 1
            logger.warning(
                "PgDuckDbSyncService: _on_new_records(%s, %d rows) упал: %s",
                table,
                len(rows),
                exc,
                exc_info=True,
            )
            self._log_sync_event(
                event_type="sync_dispatch_failed",
                summary=f"_on_new_records({table}, {len(rows)} rows) упал: {exc}",
                payload={
                    "table": table,
                    "rows": len(rows),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                level="WARN",
            )

    def _dispatch_replace(self, table: str, rows: list[dict]) -> None:
        """РџРѕР»РЅР°СЏ РїРµСЂРµСЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ: Р·Р°РјРµРЅРёС‚СЊ СЃРѕРґРµСЂР¶РёРјРѕРµ С‚Р°Р±Р»РёС†С‹ С†РµР»РёРєРѕРј."""
        callback = self._on_replace_records
        if callback is None:
            return
        try:
            callback(table, rows)
        except Exception:
            with self._state_lock:
                self._stats["errors"] += 1

    def _ensure_table_schema(self, table: str) -> None:
        """РџРµСЂРµРґР°С‚СЊ РѕРїРёСЃР°РЅРёРµ РєРѕР»РѕРЅРѕРє С‚Р°Р±Р»РёС†С‹ (PG information_schema) РІ store."""
        callback = self._on_schema
        if callback is None:
            return
        columns = self._fetch_schema(table)
        if not columns:
            return
        try:
            callback(table, columns)
        except Exception:
            with self._state_lock:
                self._stats["errors"] += 1

    def _fetch_schema(self, table: str) -> list[dict]:
        """РћРїРёСЃР°РЅРёРµ РєРѕР»РѕРЅРѕРє С‚Р°Р±Р»РёС†С‹ РёР· PG: С‚РёРїС‹, NOT NULL, РєРѕРјРјРµРЅС‚Р°СЂРёРё."""
        schema, name = self._split_table(table)
        if not name:
            return []

        def _work(conn: Any) -> list[dict]:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(
                    "SELECT c.column_name, c.data_type, c.is_nullable, "
                    "c.character_maximum_length, c.numeric_precision, c.numeric_scale, "
                    "pgd.description AS column_comment "
                    "FROM information_schema.columns c "
                    "JOIN pg_class pc ON pc.relname = c.table_name "
                    "AND pc.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s) "
                    "LEFT JOIN pg_catalog.pg_description pgd "
                    "ON pgd.objsubid = c.ordinal_position AND pgd.objoid = pc.oid "
                    "WHERE c.table_schema = %s AND c.table_name = %s "
                    "ORDER BY c.ordinal_position",
                    [schema, schema, name],
                )
                col_rows = [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()

            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT obj_description(pc.oid) FROM pg_class pc "
                    "JOIN pg_namespace n ON n.oid = pc.relnamespace "
                    "WHERE n.nspname = %s AND pc.relname = %s",
                    [schema, name],
                )
                row = cur.fetchone()
            finally:
                cur.close()
            return col_rows, (row[0] if row else None)

        col_rows, table_comment = self._db_run(_work)

        columns: list[dict] = []
        if table_comment:
            columns.append({
                "name": "__table__", "type": "", "not_null": False,
                "comment": table_comment,
            })
        for r in col_rows:
            dt = r["data_type"]
            if dt == "character varying" and r["character_maximum_length"]:
                dt = f"character varying({r['character_maximum_length']})"
            elif dt == "character" and r["character_maximum_length"]:
                dt = f"character({r['character_maximum_length']})"
            elif dt == "numeric" and r["numeric_precision"]:
                dt = f"numeric({r['numeric_precision']},{r.get('numeric_scale') or 0})"
            columns.append({
                "name": r["column_name"],
                "type": dt,
                "not_null": r["is_nullable"] == "NO",
                "comment": r["column_comment"],
            })
        return columns

    def _split_table(self, table: str) -> tuple[str, str]:
        """Р Р°Р·Р±РёС‚СЊ 'oarb.audits' РЅР° (schema, table)."""
        if "." in table:
            schema, name = table.split(".", 1)
            return schema, name
        return self._schema, table

    # ------------------------------------------------------------------
    # SQL-РґРѕСЃС‚СѓРї (С‡РµСЂРµР· РѕР±С‰РёР№ РїСѓР» utils.db)
    # ------------------------------------------------------------------

    def _fq_table(self, table: str) -> str:
        """РџРѕР»РЅРѕРµ РёРјСЏ С‚Р°Р±Р»РёС†С‹ ``schema.table`` (Р±РµР· С‚РѕС‡РєРё вЂ” СЃС…РµРјР° РёР· РєРѕРЅС„РёРіР°)."""
        if "." in table:
            return f'"{table.split(".", 1)[0]}"."{table.split(".", 1)[1]}"'
        return f'"{self._schema}"."{table}"'

    def _db_run(self, fn):
        """Р’С‹РїРѕР»РЅРёС‚СЊ ``fn(conn)`` РЅР° СЃРІРѕР±РѕРґРЅРѕРј СЃРѕРµРґРёРЅРµРЅРёРё РѕР±С‰РµРіРѕ РїСѓР»Р° ``utils.db``."""
        from utils.db import configure, run

        if self._dsn:
            configure(self._dsn)
        return run(fn)

    def _fetch_all(self, table: str) -> tuple[list[dict], Any]:
        def _work(conn: Any) -> tuple[list[dict], Any]:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(f'SELECT * FROM {self._fq_table(table)}')
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()
            return rows

        rows = self._db_run(_work)
        track_col = self._track_column_for(table)
        last = self._max_track(rows, track_col)
        return rows, last

    def _fetch_incremental(
        self, table: str, track_col: str, last: Any
    ) -> tuple[list[dict], Any]:
        def _work(conn: Any) -> list[dict]:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(
                    f'SELECT * FROM {self._fq_table(table)} '
                    f'WHERE "{track_col}" > %s ORDER BY "{track_col}"',
                    [last],
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()

        rows = self._db_run(_work)
        return rows, self._max_track(rows, track_col)

    @staticmethod
    def _max_track(rows: list[dict], track_col: str) -> Any:
        values = [r.get(track_col) for r in rows if r.get(track_col) is not None]
        return max(values) if values else None

    # ------------------------------------------------------------------
    # РџРѕРґРєР»СЋС‡РµРЅРёРµ (С‡РµСЂРµР· РѕР±С‰РёР№ РїСѓР»)
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        """РЈР±РµРґРёС‚СЊСЃСЏ, С‡С‚Рѕ DNS РїСѓР»Р° РЅР°СЃС‚СЂРѕРµРЅ; РІРѕСЂРєРµСЂС‹ РїРѕРґРєР»СЋС‡Р°СЋС‚СЃСЏ Р»РµРЅРёРІРѕ."""
        from utils.db import configure

        if self._dsn:
            configure(self._dsn)

    def _reconnect(self) -> None:
        """РЎР±СЂРѕСЃРёС‚СЊ РёРЅРєСЂРµРјРµРЅС‚Р°Р»СЊРЅС‹Рµ РјРµС‚РєРё РїРѕСЃР»Рµ РѕР±СЂС‹РІР° СЃРѕРµРґРёРЅРµРЅРёСЏ.

        РџРѕСЃР»Рµ РѕР±СЂС‹РІР° РїРµСЂРµР·Р°РіСЂСѓР¶Р°РµРј С‚Р°Р±Р»РёС†С‹ С†РµР»РёРєРѕРј, С‡С‚РѕР±С‹ РЅРµ РїСЂРѕРїСѓСЃС‚РёС‚СЊ
        РёР·РјРµРЅРµРЅРёСЏ, РїСЂРѕРёР·РѕС€РµРґС€РёРµ РІРѕ РІСЂРµРјСЏ РЅРµРґРѕСЃС‚СѓРїРЅРѕСЃС‚Рё Р‘Р”. РЎР°РјРѕ СЃРѕРµРґРёРЅРµРЅРёРµ
        (Рё РµРіРѕ РїРµСЂРµРїРѕРґРєР»СЋС‡РµРЅРёРµ) Р¶РёРІС‘С‚ РІ РѕР±С‰РµРј РїСѓР»Рµ ``utils.db``.
        """
        self._last_sync.clear()
        self._ensure_connected()

    def _close_connection(self) -> None:
        """Р‘РѕР»СЊС€Рµ РЅРµ РІР»Р°РґРµРµРј СЃРѕРµРґРёРЅРµРЅРёРµРј вЂ” РїСѓР» Р·Р°РєСЂС‹РІР°РµС‚СЃСЏ СЃР°Рј."""
