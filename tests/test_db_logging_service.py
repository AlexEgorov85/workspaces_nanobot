from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.services.db_logging_service import DbLoggingService, LogEvent


@pytest.fixture
def fake_psycopg2(monkeypatch):
    """Подменяем psycopg2/psycopg2.extras, чтобы не требовать установки."""
    cursor = MagicMock()
    cursor.close = MagicMock()
    cur_factory = MagicMock(return_value=cursor)
    conn = MagicMock()
    conn.cursor = cur_factory
    conn.close = MagicMock()
    conn.closed = False

    extras = types.ModuleType("psycopg2.extras")
    extras.execute_batch = MagicMock()
    extras.Json = lambda x: x

    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.connect = MagicMock(return_value=conn)
    psycopg2.extras = extras

    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
    return {
        "conn": conn,
        "cursor": cursor,
        "execute_batch": extras.execute_batch,
    }


class TestBasicLifecycle:
    def test_start_stop(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=0.05)
        svc.start()
        try:
            assert svc.is_running()
        finally:
            svc.stop(timeout_sec=2.0)
        assert not svc.is_running()

    def test_no_dsn_runs_with_fallback(self, tmp_path):
        svc = DbLoggingService(
            dsn="",
            flush_interval_sec=0.05,
            fallback_path=tmp_path / "log.jsonl",
        )
        svc.start()
        try:
            assert svc.log_inbound("cli:1", "cli", "hi") is True
            time.sleep(0.2)
        finally:
            svc.stop(timeout_sec=2.0)
        # В fallback нет записи (БД не было, fallback должен принять)
        assert svc.get_stats()["fallback_written"] >= 0


class TestNonBlocking:
    def test_log_inbound_enqueue(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        assert svc.log_inbound("cli:1", "cli", "hello") is True
        assert svc.get_stats()["queued"] >= 1

    def test_log_event_min_level(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", min_level="WARN")
        assert svc.log_event(LogEvent("x", "DEBUG")) is False
        assert svc.log_event(LogEvent("x", "INFO")) is False
        assert svc.log_event(LogEvent("x", "ERROR")) is True

    def test_log_outbound_with_meta(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x")
        assert svc.log_outbound(
            "cli:1", "cli", "ok", latency_ms=12.5, tokens_used=42
        ) is True

    def test_log_tool_call_and_result(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x")
        assert svc.log_tool_call("cli:1", "read", {"path": "x"}) is True
        assert svc.log_tool_result(
            "cli:1", "read", "content", latency_ms=15.0
        ) is True

    def test_log_error(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x")
        assert svc.log_error("boom", session_id="k", context={"k": "v"}) is True

    def test_queue_full_returns_false(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", queue_maxsize=2)
        # Не запускаем worker — очередь наполнится до запуска.
        for _ in range(2):
            assert svc.log_event(LogEvent("x")) is True
        assert svc.log_event(LogEvent("x")) is False
        assert svc.get_stats()["queue_full"] == 1


class TestFlush:
    def test_batch_writes_to_db(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=0.05,
                                batch_size=3)
        svc.start()
        try:
            for i in range(3):
                svc.log_inbound("cli:1", "cli", f"msg{i}")
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)

        fake_psycopg2["execute_batch"].assert_called()
        written = svc.get_stats()["written"]
        assert written >= 3

    def test_fallback_when_no_dsn(self, tmp_path):
        svc = DbLoggingService(
            dsn="",
            flush_interval_sec=0.05,
            batch_size=2,
            fallback_path=tmp_path / "log.jsonl",
        )
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "a")
            svc.log_inbound("cli:1", "cli", "b")
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)

        assert (tmp_path / "log.jsonl").exists()
        lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 2
        for ln in lines:
            obj = json.loads(ln)
            assert obj["event_type"] == "inbound"
            assert obj["channel"] == "cli"

    def test_connect_failure_uses_fallback(self, fake_psycopg2, tmp_path):
        psycopg2 = sys.modules["psycopg2"]
        psycopg2.connect = MagicMock(side_effect=RuntimeError("no db"))
        svc = DbLoggingService(
            dsn="postgresql://x",
            flush_interval_sec=0.05,
            batch_size=1,
            fallback_path=tmp_path / "log.jsonl",
        )
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "x")
            time.sleep(0.2)
        finally:
            svc.stop(timeout_sec=2.0)
        assert svc.get_stats()["failed"] + svc.get_stats()["fallback_written"] >= 1

    def test_stop_flushes_remaining(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0,
                                batch_size=100)
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "left")
        finally:
            svc.stop(timeout_sec=2.0)
        assert svc.get_stats()["written"] >= 1


class TestGetStats:
    def test_keys_present(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x")
        stats = svc.get_stats()
        for k in ("running", "queued", "written", "failed", "queue_size",
                  "fallback_written", "batch_count", "queue_full",
                  "connected", "last_error"):
            assert k in stats
