"""Тесты на generic-эмиттер событий ``workspace.utils.event_log.record_event``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _fake_settings(enabled: bool = True, dsn: str = "postgresql://u:p@localhost/db") -> dict:
    return {
        "logging": {"db": {"enabled": enabled, "table_name": "agent_gateway_logs", "schema": "public"}},
        "channels": {"postgres": {"dsn": dsn}},
    }


def test_record_event_inserts_with_params() -> None:
    settings = _fake_settings()
    with patch.dict("sys.modules", {}), patch(
        "config.SETTINGS", settings, create=True
    ), patch("utils.db.configure") as configure, patch(
        "utils.db.execute"
    ) as execute:
        from workspace.utils.event_log import record_event

        record_event(
            "context_compacted",
            "system",
            "context compacted",
            {"archived_msgs": 5},
            session_id="postgres:123",
            channel=None,
            actor="system",
        )

        configure.assert_called_once_with("postgresql://u:p@localhost/db")
        execute.assert_called_once()
        sql, *params = execute.call_args.args
        assert "INSERT INTO" in sql and "agent_gateway_logs" in sql
        # params: id, level, event_type, session_id, channel, actor, name, summary, payload
        assert "context_compacted" in params
        assert "postgres:123" in params
        # payload обёрнут в psycopg2.extras.Json (класс доступен)
        from psycopg2.extras import Json

        assert any(isinstance(p, Json) for p in params)


def test_record_event_skipped_when_disabled() -> None:
    settings = _fake_settings(enabled=False)
    with patch("config.SETTINGS", settings, create=True), patch(
        "utils.db.execute"
    ) as execute:
        from workspace.utils.event_log import record_event

        record_event("x", "y", "z", {}, session_id="s")
        execute.assert_not_called()


def test_record_event_skipped_when_no_dsn() -> None:
    settings = _fake_settings(dsn="")
    with patch("config.SETTINGS", settings, create=True), patch(
        "utils.db.execute"
    ) as execute:
        from workspace.utils.event_log import record_event

        record_event("x", "y", "z", {}, session_id="s")
        execute.assert_not_called()


def test_record_event_truncates_summary() -> None:
    settings = _fake_settings()
    with patch("config.SETTINGS", settings, create=True), patch(
        "utils.db.configure"
    ), patch("utils.db.execute") as execute:
        from workspace.utils.event_log import record_event

        record_event("x", "y", "z" * 500, {}, session_id="s")
        params = execute.call_args.args[1:]  # пропускаем sql
        # порядок: id, level, event_type, session_id, channel, actor, name, summary, payload
        summary = params[7]
        assert isinstance(summary, str) and len(summary) == 200
        assert summary == "z" * 200
