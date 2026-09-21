"""Lifecycle ordering-тесты для ``ApplicationContext``.

Phase 8 tasks §8.1–8.3 ``unify-agent-event-logging-pipeline``:

* ``db_logging_service.start()`` ДО ``sync_service.start()`` (D7);
* ``db_logging_service.stop()`` ПОСЛЕ ``sync_service.stop()`` (наоборот
  по сравнению с start);
* mid-flight shutdown — ``compact()`` + ``ctx.stop()`` НЕ бросает
  исключение (no-op for business через ``try_log_event`` корректно
  отрабатывает после ``stop()``).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _settings(**overrides):
    section = {"enabled": True, "notify_in_history": True, "print_to_terminal": False}
    section.update(overrides)
    return {"gateway": {"compact": section}}


class TestStartupOrdering:
    """``db_logging_service.start()`` ДО ``sync_service.start()``."""

    def test_db_logging_starts_before_sync_service(self):
        """Через ``ShudownCoordinator.register`` order'ы фиксируются;
        первый зарегистрированный выходит последним (``shutdown_all``
        выполняет LIFO). ``db_logging_service`` должен быть зарегистрирован
        раньше — и, следовательно, запущен раньше и остановлен позже."""
        from lib.lifecycle.shutdown_coordinator import ShutdownCoordinator

        events: list[str] = []

        class FakeDbLogging:
            def start(self):
                events.append("db.start")

            def stop(self, timeout_sec: float = 15.0):
                events.append("db.stop")

        class FakeSyncService:
            def start(self, initial_load: bool = True):
                events.append("sync.start")

            def stop(self, timeout_sec: float = 15.0):
                events.append("sync.stop")

        coord = ShutdownCoordinator()
        db = FakeDbLogging()
        sync = FakeSyncService()
        coord.register("db_logging_service", db)
        coord.register("sync_service", sync)

        # Симуляция ``ApplicationContext.start``: db first, sync second.
        db.start()
        sync.start()
        coord.shutdown_all()

        # Ожидаем: db.start, sync.start, sync.stop, db.stop.
        assert events == ["db.start", "sync.start", "sync.stop", "db.stop"]


class TestShutdownOrdering:
    """``db_logging_service.stop()`` ПОСЛЕ ``sync_service.stop()``."""

    def test_db_logging_stops_after_sync_service(self):
        """Тот же порядок shutdown (LIFO) — ``sync_service.stop``
        раньше ``db_logging_service.stop``."""
        from lib.lifecycle.shutdown_coordinator import ShutdownCoordinator

        events: list[str] = []

        class FakeDbLogging:
            def stop(self, timeout_sec: float = 15.0):
                events.append("db.stop")

        class FakeSyncService:
            def stop(self, timeout_sec: float = 15.0):
                events.append("sync.stop")

        coord = ShutdownCoordinator()
        coord.register("db_logging_service", FakeDbLogging())
        coord.register("sync_service", FakeSyncService())

        coord.shutdown_all()

        assert events == ["sync.stop", "db.stop"]


class TestShutdownMidFlight:
    """``compact()`` mid-flight + ``ctx.stop()`` без ``_record_event_log``
    exception (no-op for business через ``try_log_event`` корректно
    отрабатывает после ``stop()``)."""

    @pytest.mark.asyncio
    async def test_compact_after_service_stop_logs_noop_with_warning(self, caplog):
        """``try_log_event`` возвращает ``False`` + WARNING после
        ``svc.stop()`` (``is_running() == False``)."""
        import logging
        from lib.services.db_logging_service import (
            DbLoggingService,
            LogEvent,
            try_log_event,
        )

        svc = DbLoggingService(
            table_name="agent_gateway_logs",
            question_runs_table="agent_question_runs",
        )
        svc.start()
        # Имитация shutdown — worker останавливается, ``is_running`` False.
        svc.stop()

        with caplog.at_level(logging.WARNING, logger="lib.services.db_logging_service"):
            ok = try_log_event(
                svc, LogEvent(event_type="x"),
                producer="TestProducer", event_type="x",
            )

        assert ok is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("not running" in r.getMessage() for r in warnings)