"""Generic-эмиттер событий в долговечный журнал agent_gateway_logs.

Единая точка записи для ВСЕХ доменных событий, которые должны пережить
context compaction и быть найдены агентом через инструмент ``history_search``:

  * ``document_summarized`` (legal_summarizer и др. skill-CLI через exec);
  * ``context_compacted`` (ContextCompactionService);
  * ``file_attached`` / ``file_created`` / ``file_delivered`` (хуки файлов);
  * любые другие — навык/хук просто передаёт свой event_type.

Таблица public.agent_gateway_logs уже существует (sql/logs/...), пишется
DbLoggingService и НЕ чистится Consolidator'ом. Мы делаем прямой INSERT
через utils.db (тот же DSN, что у всего остального), без привязки к
ApplicationContext — чтобы эмиттер работал и из subprocess (exec), и из
гейтвей-процесса.

Gating: при отсутствии DSN (channels.postgres.dsn) или при выключенном
logging.db.enabled событие тихо не пишется (skill/хук не падает).
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
