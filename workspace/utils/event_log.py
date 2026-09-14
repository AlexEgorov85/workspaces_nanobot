"""Эмиттер ``context_compacted`` в долговечный журнал ``agent_gateway_logs``.

Единственный реальный caller — ``ContextCompactionService._record_event_log``
(см. ``lib/services/context_compaction.py``). Пишет одноимённое событие,
которое агент находит через ``history_search(event_type="context_compacted",
session_scope="current")`` — закрывает gap №1 из
``docs/architecture/HISTORY_SEARCH_ANALYSIS.md``.

Таблица ``public.agent_gateway_logs`` уже существует (``sql/logs/...``),
пишется ``DbLoggingService`` и НЕ чистится Consolidator'ом. Мы делаем прямой
INSERT через ``utils.db`` (тот же DSN, что у всего остального), без привязки
к ``ApplicationContext`` — чтобы эмиттер работал и из subprocess (exec),
и из гейтвей-процесса.

Gating: при отсутствии DSN (``channels.postgres.dsn``) или при выключенном
``logging.db.enabled`` событие тихо не пишется (``history_search`` его
тогда не найдёт — но и compaction не упадёт).
"""

from __future__ import annotations

import uuid
from typing import Any

from loguru import logger

_SUMMARY_MAX_CHARS = 200


def record_event(
    event_type: str,
    name: str,
    summary: str,
    payload: dict[str, Any] | None,
    *,
    session_id: str | None = None,
    channel: str | None = None,
    actor: str = "system",
    level: str = "INFO",
) -> None:
    """Записать событие в agent_gateway_logs (если включено и есть DSN).

    Синхронная функция: вызывать напрямую из sync-кода (cli.py subprocess)
    либо через ``asyncio.to_thread(record_event, ...)`` из async-хуков/методов,
    чтобы не блокировать event loop на сетевом INSERT (utils.db.execute сам
    ставит задачу в пул воркеров, но ожидает результат в вызывающем потоке).
    """
    try:
        from config import SETTINGS
    except Exception:
        return

    try:
        log_cfg = SETTINGS.get("logging", {}) or {}
        db_cfg = log_cfg.get("db", {}) or {}
        if not db_cfg.get("enabled", False):
            return
    except Exception:
        return

    try:
        pg = SETTINGS.get("channels", {}).get("postgres", {})
        dsn = (pg or {}).get("dsn") or "" if isinstance(pg, dict) else ""
    except Exception:
        dsn = ""
    if not dsn:
        return

    table = db_cfg.get("table_name") or "agent_gateway_logs"
    schema = db_cfg.get("schema") or "public"

    try:
        from psycopg2.extras import Json
        from utils.db import configure, execute
    except Exception as exc:
        logger.warning("event_log: import failed: %s", exc)
        return

    safe_summary = (summary or "")[:_SUMMARY_MAX_CHARS]
    try:
        configure(dsn)
        execute(
            f'INSERT INTO "{schema}"."{table}" '
            '(id, "timestamp", level, event_type, session_id, channel, '
            "actor, name, summary, payload) "
            "VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s)",
            str(uuid.uuid4()),
            level,
            event_type,
            session_id,
            channel,
            actor,
            name,
            safe_summary,
            Json(payload if isinstance(payload, dict) else {}),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("event_log: record_event(%s) failed: %s", event_type, exc)


_SYNC_SESSION_ID = "gateway:sync"
_SYNC_ACTOR = "sync"


def record_sync_event(
    event_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    level: str = "INFO",
) -> None:
    """Sync-канал записи в ``agent_gateway_logs`` из PG→DuckDB sync-пути.

    Тонкая обёртка над :func:`record_event` со стандартными для sync-событий
    дефолтами:

      * ``actor='sync'``;
      * ``session_id='gateway:sync'`` — единый «безсессионный» идентификатор,
        не привязанный к чату пользователя (sync-сервис работает независимо
        от диалогов);
      * ``channel=None`` (нет канала).

    Идемпотентно. Любые ошибки внутри глотаются — sync-код не должен
    падать из-за логирования.

    **Низкоуровневый способ записи.** Новый код должен использовать
    единый helper :func:`emit_sync_event` (dual-sink: через
    ``DbLoggingService`` при наличии иначе синхронный fallback сюда).
    Этот же helper используйте из standalone-утилит и очень ранних стадий
    старта, когда ``DbLoggingService`` ещё не создан/не запущен.
    """
    record_event(
        event_type=event_type,
        name=name or event_type,
        summary=summary,
        payload=payload,
        session_id=_SYNC_SESSION_ID,
        channel=None,
        actor=_SYNC_ACTOR,
        level=level,
    )


def emit_sync_event(
    event_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    level: str = "INFO",
    service: Any | None = None,
) -> None:
    """Единая точка записи sync-событий в ``agent_gateway_logs``.

    Единый dual-sink для всех писателей PG→DuckDB sync-пути
    (``PgDuckDbSyncService`` и ``DuckDbCacheStore``):

      1. Если передан работающий ``service`` (``DbLoggingService``) —
         пишем через него (async, через пул, батчи). Это штатный конвейер
         после ``ApplicationContext.start()``.
      2. Иначе — синхронный fallback ``record_sync_event`` (прямой INSERT
         через ``utils.db``) для ранних стадий старта / standalone-утилит,
         где ``DbLoggingService`` ещё не создан/не запущен.

    Обе ветки глотают ошибки — sync-код не должен падать из-за логирования.

    Единая точка правды устраняет «дублирование»: раньше publish-события
    писались в обход ``DbLoggingService`` (с прямой семантикой
    ``NOW()`` при вызове), а остальные sync-события — через него (с
    timestamp на момент flush). Теперь у всех sync-событий один конвейер
    и одна семантика времени.
    """
    if service is not None:
        try:
            if getattr(service, "is_running", lambda: False)():
                ok = service.log_sync_event(
                    event_type=event_type,
                    summary=summary,
                    payload=payload,
                    level=level,
                    name=name,
                )
                if ok:
                    return
        except Exception:
            pass
    try:
        record_sync_event(
            event_type=event_type,
            summary=summary,
            payload=payload,
            level=level,
            name=name,
        )
    except Exception:
        pass
