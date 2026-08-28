"""Тесты на инструмент ``history_search`` (поиск по agent_gateway_logs)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from workspace.tools.history_search_tool import (
    HistorySearchTool,
    HistorySearchToolConfig,
)


def _make_tool() -> HistorySearchTool:
    return HistorySearchTool(config=HistorySearchToolConfig())


def _fake_rows() -> list[dict]:
    return [
        {
            "timestamp": "2026-01-01T10:00:00+00:00",
            "event_type": "context_compacted",
            "level": "INFO",
            "summary": "context compacted",
            "payload": {"archived_msgs": 5, "tokens_before": 1000, "tokens_after": 200},
        },
        {
            "timestamp": "2026-01-01T09:00:00+00:00",
            "event_type": "run_finished",
            "level": "INFO",
            "summary": "итоговый ответ",
            "payload": {"final_content": "длинный ответ агента"},
        },
    ]


@pytest.mark.asyncio
async def test_search_current_session_filters_by_session() -> None:
    with patch(
        "workspace.tools.history_search_tool._current_session_key",
        return_value="postgres:123",
    ), patch("utils.db.fetch", return_value=_fake_rows()) as fetch:
        tool = _make_tool()
        result = await tool.execute(query="ответ", session_scope="current")
        data = __import__("json").loads(result)
        assert data["status"] == "success"
        # fetch вызван именно с фильтром по session_id
        sql, *params = fetch.call_args.args
        assert "session_id = %s" in sql
        assert "postgres:123" in params
        assert data["count"] == 2


@pytest.mark.asyncio
async def test_search_all_scope_sets_allow_all_flag() -> None:
    with patch(
        "workspace.tools.history_search_tool._current_session_key",
        return_value="postgres:123",
    ), patch("utils.db.fetch", return_value=[]) as fetch:
        tool = _make_tool()
        await tool.execute(query=None, session_scope="all")
        sql, *params = fetch.call_args.args
        # первый булев параметр — allow_all
        assert params[0] is True
        assert "session_id = %s" in sql


@pytest.mark.asyncio
async def test_search_event_type_filter() -> None:
    with patch(
        "workspace.tools.history_search_tool._current_session_key",
        return_value="s",
    ), patch("utils.db.fetch", return_value=_fake_rows()) as fetch:
        tool = _make_tool()
        await tool.execute(query="", event_type="context_compacted")
        sql, *params = fetch.call_args.args
        assert "event_type = %s" in sql
        assert "context_compacted" in params


@pytest.mark.asyncio
async def test_search_truncates_long_output() -> None:
    import json as _json

    big = [{"timestamp": "t", "event_type": "e", "level": "INFO",
            "summary": "s", "payload": {"x": "y" * 10000}}]
    tool = HistorySearchTool(config=HistorySearchToolConfig(max_result_chars=200))
    with patch(
        "workspace.tools.history_search_tool._current_session_key",
        return_value="s",
    ), patch("utils.db.fetch", return_value=big):
        result = await tool.execute(query=None)
        # исходный JSON был бы ~10030 символов; усечение заметно режет,
        # при этом JSON остаётся валидным (агент должен его распарсить)
        assert len(result) < 1000
        parsed = _json.loads(result)
        assert parsed["status"] == "success"


@pytest.mark.asyncio
async def test_search_tool_name_filter_applies_to_clause() -> None:
    """Фильтр tool_name добавляет ``name = %s`` в WHERE и параметр."""
    with patch(
        "workspace.tools.history_search_tool._current_session_key",
        return_value="s",
    ), patch("utils.db.fetch", return_value=[]) as fetch:
        tool = _make_tool()
        await tool.execute(
            query=None,
            event_type="tool_result",
            tool_name="compact_context",
        )
        sql, *params = fetch.call_args.args
        assert "name = %s" in sql
        assert "compact_context" in params


@pytest.mark.asyncio
async def test_search_tool_name_omitted_skips_filter() -> None:
    """Без tool_name фильтр ``name = %s`` НЕ появляется в SQL."""
    with patch(
        "workspace.tools.history_search_tool._current_session_key",
        return_value="s",
    ), patch("utils.db.fetch", return_value=[]) as fetch:
        tool = _make_tool()
        await tool.execute(query=None, event_type="tool_result")
        sql, *params = fetch.call_args.args
        assert "name = %s" not in sql
        # и среди параметров нет подозрительной tool_name-вставки
        assert "compact_context" not in params


@pytest.mark.asyncio
async def test_search_tool_name_combined_with_query() -> None:
    """tool_name + query работают вместе: WHERE содержит оба фильтра."""
    with patch(
        "workspace.tools.history_search_tool._current_session_key",
        return_value="s",
    ), patch("utils.db.fetch", return_value=[]) as fetch:
        tool = _make_tool()
        await tool.execute(
            query="договор",
            event_type="tool_call",
            tool_name="duckdb_query",
        )
        sql, *params = fetch.call_args.args
        assert "name = %s" in sql
        assert "ILIKE %s" in sql
        assert "duckdb_query" in params
        assert "%договор%" in params


@pytest.mark.asyncio
async def test_search_includes_name_field_in_events() -> None:
    """SELECT возвращает ``name``, и событие содержит поле ``name``."""
    import json as _json

    rows = [
        {
            "timestamp": "2026-01-01T10:00:00+00:00",
            "event_type": "tool_result",
            "name": "compact_context",
            "level": "INFO",
            "summary": "compact ok",
            "payload": {"status": "ok"},
        }
    ]
    with patch(
        "workspace.tools.history_search_tool._current_session_key",
        return_value="s",
    ), patch("utils.db.fetch", return_value=rows):
        tool = _make_tool()
        result = await tool.execute(query=None)
        parsed = _json.loads(result)
        assert parsed["count"] == 1
        ev = parsed["events"][0]
        assert ev["name"] == "compact_context"
        assert ev["event_type"] == "tool_result"
