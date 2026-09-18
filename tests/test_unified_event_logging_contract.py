"""Contract-тесты единого logging pipeline ``DbLoggingService``.

Покрывают поведенческие контракты, описанные в tasks §7
``unify-agent-event-logging-pipeline``:

* ``TestContextCompactionNotifyBehavior`` — разделение concerns в
  ``ContextCompactionService._notify``:
    - ``notify_in_history=True`` → оба эффекта (``_write_history_notice``
      + ``_record_event_log`` через ``try_log_event``);
    - ``notify_in_history=False`` → только ``_record_event_log`` (нет
      UI-уведомления);
    - ``record_external_compaction`` при ``notify_in_history=False`` тоже
      пишет в event log (закрывает design D8 — ``patch_compaction_tracking``
      остаётся активным при выключенном UI-уведомлении).

* ``TestDbLoggingServiceUnavailableBehavior`` — единый WARNING-уровень
  при недоступности ``DbLoggingService``:
    - ``compact()`` при ``db_logging_service=None`` — no-op for business +
      WARNING;
    - ``_log_sync_event`` при ``db_logging_service=None`` — то же;
    - ``_emit_health_event`` (preload_service) — то же.

* ``TestTryLogEvent`` — контракт helper'а ``DbLoggingService.try_log_event``:
    - ``svc is None`` → WARNING + ``False``;
    - ``not svc.is_running()`` → WARNING + ``False``;
    - ``svc.is_running()`` → ``svc.log_event(log_event)`` + возвращает
      его bool-результат;
    - ``svc.log_event`` бросил → WARNING + ``False``;
    - уровень WARNING (НЕ DEBUG, НЕ INFO).

Тесты с ``caplog`` проверяют уровень WARNING через ``caplog.records``.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


def _settings(**overrides):
    """Compact-section settings factory."""
    section = {"enabled": True, "notify_in_history": True, "print_to_terminal": False}
    section.update(overrides)
    return {"gateway": {"compact": section}}


class _FakeLogEvent:
    """Stub ``LogEvent`` для проверки аргументов ``try_log_event``."""
    event_type = "test_event"
    level = "INFO"
    session_id = None
    channel = None
    actor = None
    summary = None
    payload = None

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# ============================================================================
# try_log_event contract (tasks 3.1)
# ============================================================================


class TestTryLogEvent:
    """Контракт helper'а ``DbLoggingService.try_log_event``."""

    def test_noop_when_service_none(self, caplog):
        from lib.services.db_logging_service import LogEvent, try_log_event

        log_event = LogEvent(event_type="x")
        with caplog.at_level(logging.WARNING, logger="lib.services.db_logging_service"):
            ok = try_log_event(None, log_event, producer="TestProducer", event_type="x")

        assert ok is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("TestProducer" in r.getMessage() for r in warnings)
        assert any("DbLoggingService is None" in r.getMessage() for r in warnings)

    def test_noop_when_service_not_running(self, caplog):
        from lib.services.db_logging_service import LogEvent, try_log_event

        svc = MagicMock()
        svc.is_running.return_value = False
        log_event = LogEvent(event_type="x")
        with caplog.at_level(logging.WARNING, logger="lib.services.db_logging_service"):
            ok = try_log_event(svc, log_event, producer="TestProducer", event_type="x")

        assert ok is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("not running" in r.getMessage() for r in warnings)

    def test_success_when_running(self, caplog):
        from lib.services.db_logging_service import LogEvent, try_log_event

        svc = MagicMock()
        svc.is_running.return_value = True
        svc.log_event.return_value = True
        log_event = LogEvent(event_type="x")

        with caplog.at_level(logging.DEBUG, logger="lib.services.db_logging_service"):
            ok = try_log_event(svc, log_event, producer="TestProducer", event_type="x")

        assert ok is True
        svc.log_event.assert_called_once_with(log_event)
        # Уровень НЕ WARNING на happy-path.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings

    def test_success_returns_log_event_result(self, caplog):
        """Если ``svc.log_event`` вернул ``False`` (очередь переполнена) —
        helper тоже возвращает ``False`` (без WARNING — это бизнес-возврат)."""
        from lib.services.db_logging_service import LogEvent, try_log_event

        svc = MagicMock()
        svc.is_running.return_value = True
        svc.log_event.return_value = False
        log_event = LogEvent(event_type="x")

        with caplog.at_level(logging.WARNING, logger="lib.services.db_logging_service"):
            ok = try_log_event(svc, log_event, producer="TestProducer", event_type="x")

        assert ok is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings

    def test_log_event_exception_returns_false_with_warning(self, caplog):
        """Если ``svc.log_event`` бросил — ``try_log_event`` глотает и
        возвращает ``False`` с WARNING (operational visibility)."""
        from lib.services.db_logging_service import LogEvent, try_log_event

        svc = MagicMock()
        svc.is_running.return_value = True
        svc.log_event.side_effect = RuntimeError("queue is full")
        log_event = LogEvent(event_type="x")

        with caplog.at_level(logging.WARNING, logger="lib.services.db_logging_service"):
            ok = try_log_event(svc, log_event, producer="TestProducer", event_type="x")

        assert ok is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("log_event failed" in r.getMessage() for r in warnings)

    def test_is_running_exception_treated_as_not_running(self, caplog):
        """Если ``svc.is_running()`` бросил — трактуем как not running
        (защита от сломанных mock'ов в тестах)."""
        from lib.services.db_logging_service import LogEvent, try_log_event

        svc = MagicMock()
        svc.is_running.side_effect = RuntimeError("oops")
        log_event = LogEvent(event_type="x")

        with caplog.at_level(logging.WARNING, logger="lib.services.db_logging_service"):
            ok = try_log_event(svc, log_event, producer="TestProducer", event_type="x")

        assert ok is False


# ============================================================================
# ContextCompactionService._notify contract (tasks 3.2, 3.4, 7.1)
# ============================================================================


class TestContextCompactionNotifyBehavior:
    """Поведение ``_notify`` и ``record_external_compaction`` при разных
    значениях ``notify_in_history``."""

    @pytest.mark.asyncio
    async def test_notify_with_history_calls_both(self, monkeypatch):
        """``notify_in_history=True`` → ``_write_history_notice`` +
        ``_record_event_log`` (через ``try_log_event``)."""
        from lib.services.context_compaction import ContextCompactionService

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(agent, settings=_settings(notify_in_history=True))

        svc._write_history_notice = AsyncMock()
        svc._record_event_log = AsyncMock()

        report = {
            "session_key": "postgres:1",
            "mode": "idle",
            "ok": True,
            "archived_msgs": 10,
            "kept_msgs": 20,
            "tokens_before": 2000,
            "tokens_after": 800,
            "summary": "сводка",
            "raw_dump": False,
        }
        await svc._notify("postgres:1", report)

        svc._write_history_notice.assert_awaited_once_with("postgres:1", report)
        svc._record_event_log.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notify_without_history_skips_notice_only(self, monkeypatch):
        """``notify_in_history=False`` → ``_record_event_log`` ВСЕ ЕЩЁ
        вызывается (observability-trail не зависит от UI-уведомления)."""
        from lib.services.context_compaction import ContextCompactionService

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(
            agent, settings=_settings(notify_in_history=False),
        )

        svc._write_history_notice = AsyncMock()
        svc._record_event_log = AsyncMock()

        report = {
            "session_key": "postgres:1",
            "mode": "idle",
            "ok": True,
            "archived_msgs": 5,
            "kept_msgs": 5,
            "tokens_before": 100,
            "tokens_after": 50,
            "summary": None,
            "raw_dump": False,
        }
        await svc._notify("postgres:1", report)

        svc._write_history_notice.assert_not_called()
        svc._record_event_log.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_record_external_compaction_no_history_still_writes_event_log(self):
        """``record_external_compaction`` при ``notify_in_history=False``
        всё равно пишет ``_record_event_log`` — это закрывает design D8
        (auto-compact patch остаётся активным при выключенном UI)."""
        from lib.services.context_compaction import ContextCompactionService

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(
            agent, settings=_settings(notify_in_history=False),
        )
        svc._write_history_notice = AsyncMock()
        svc._record_event_log = AsyncMock()

        await svc.record_external_compaction(
            session_key="postgres:1",
            mode="token",
            summary="x",
            archived_msgs=10,
            kept_msgs=20,
            tokens_before=2000,
            tokens_after=800,
        )

        svc._write_history_notice.assert_not_called()
        svc._record_event_log.assert_awaited_once()


# ============================================================================
# DbLoggingService unavailable behavior (tasks 4.1, 4.2, 4.3, 7.2)
# ============================================================================


class TestDbLoggingServiceUnavailableBehavior:
    """При недоступности ``DbLoggingService`` — no-op for business +
    WARNING (единый уровень)."""

    @pytest.mark.asyncio
    async def test_record_event_log_noop_with_warning_when_service_none(self, caplog):
        """``_record_event_log`` при ``db_logging_service=None`` —
        no-op for business + WARNING."""
        import logging as _logging
        from lib.services.context_compaction import ContextCompactionService

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(
            agent, settings=_settings(), db_logging_service=None,
        )

        with caplog.at_level(_logging.WARNING, logger="lib.services.db_logging_service"):
            await svc._record_event_log(
                "postgres:1",
                {
                    "mode": "idle", "archived_msgs": 5, "kept_msgs": 5,
                    "tokens_before": 100, "tokens_after": 50,
                    "summary": "x", "raw_dump": False,
                },
                "Итог: заархивировано 5 сообщений, 100 → 50 токенов.",
            )

        # WARNING от ``try_log_event`` с producer="ContextCompactionService".
        warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
        assert any("ContextCompactionService" in r.getMessage() for r in warnings)

    def test_log_sync_event_noop_with_warning(self, caplog):
        """``PgDuckDbSyncService._log_sync_event`` при ``None`` — WARNING."""
        from lib.services.pg_duckdb_sync_service import PgDuckDbSyncService

        sync = PgDuckDbSyncService(dsn="x", schema="main", tables=["t"])

        with caplog.at_level(logging.WARNING, logger="lib.services.db_logging_service"):
            sync._log_sync_event("sync_test", "x", payload={"a": 1})

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("PgDuckDbSyncService" in r.getMessage() for r in warnings)

    def test_emit_health_event_noop_with_warning(self, caplog):
        """``PreloadService._emit_health_event`` при ``None`` — WARNING."""
        from lib.services.preload_service import _emit_health_event

        with caplog.at_level(logging.WARNING, logger="lib.services.db_logging_service"):
            _emit_health_event(summary="x", payload={}, level="INFO", service=None)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("PreloadService" in r.getMessage() for r in warnings)


# ============================================================================
# Producer reads no config (tasks 7.3)
# ============================================================================


PRODUCER_MODULES = (
    "lib/services/context_compaction.py",
    "lib/services/pg_duckdb_sync_service.py",
    "lib/services/duckdb_cache_store.py",
    "lib/services/preload_service.py",
)


class TestProducerReadsNoConfig:
    """Producer'ы НЕ должны читать ``logging.db.*`` напрямую — только
    через переданный ``db_logging_service``."""

    @pytest.mark.parametrize("rel_path", PRODUCER_MODULES)
    def test_no_logging_db_lookup(self, rel_path):
        from pathlib import Path
        text = (Path(__file__).resolve().parent.parent / rel_path).read_text(
            encoding="utf-8",
        )
        # Запрет доступа к ``logging.db.table_name`` / ``logging.db.schema``.
        assert "logging.db.table_name" not in text
        assert "logging.db.schema" not in text
        assert 'logging["db"]["table_name"]' not in text
        assert "logging['db']['table_name']" not in text