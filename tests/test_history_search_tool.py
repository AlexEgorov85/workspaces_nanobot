"""Тесты на инструмент ``history_search`` (поиск по agent_gateway_logs)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from workspace.tools.history_search_tool import (
    HistorySearchTool,
    HistorySearchToolConfig,
    _current_session_key,
    _current_user_id,
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
@pytest.mark.xfail(
    reason=(
        "Order-dependent flake: мокает utils.db.fetch глобально; "
        "test_streamlit_app.py и другие тесты переустанавливают "
        "sys.modules['utils.db'] через свой mock, что ломает патч "
        "здесь. Pre-existing, не связано с change config-profile-cli-flag. "
        "TODO: выделить в отдельный changelog или вынести utils.db "
        "в conftest-fixture (см. ROADMAP)."
    ),
    strict=False,
)
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
async def test_search_all_scope_filters_by_user_id() -> None:
    """``scope='all'`` фильтрует по ``user_id`` из identity-store."""
    with patch(
        "workspace.tools.history_search_tool._current_user_id",
        return_value="alice",
    ), patch("utils.db.fetch", return_value=[]) as fetch:
        tool = _make_tool()
        await tool.execute(query=None, session_scope="all")
        sql, *params = fetch.call_args.args
        assert "user_id = %s" in sql
        assert "alice" in params
        # Никакой ``session_id = %s`` в SQL для scope='all'.
        assert "session_id = %s" not in sql


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
        parsed = json.loads(result)
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
        parsed = json.loads(result)
        assert parsed["count"] == 1
        ev = parsed["events"][0]
        assert ev["name"] == "compact_context"
        assert ev["event_type"] == "tool_result"


@pytest.mark.asyncio
async def test_search_returns_event_id_in_each_event() -> None:
    """Закрывает gap №3 (ANALYSIS.md): SELECT возвращает ``id``, и в JSON
    каждое событие содержит ``event_id``. Колонка ``id`` в таблице уже была,
    правка только в tool'е (расширение SELECT)."""
    
    rows = [
        {
            "id": "11111111-2222-3333-4444-555555555555",
            "timestamp": "2026-01-01T10:00:00+00:00",
            "event_type": "tool_result",
            "name": "compact_context",
            "level": "INFO",
            "summary": "compact ok",
            "payload": {"status": "ok"},
        },
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "timestamp": "2026-01-01T09:00:00+00:00",
            "event_type": "run_finished",
            "name": "run",
            "level": "INFO",
            "summary": "ответ агента",
            "payload": {"final_content": "..."},
        },
    ]
    with patch(
        "workspace.tools.history_search_tool._current_session_key",
        return_value="s",
    ), patch("utils.db.fetch", return_value=rows):
        tool = _make_tool()
        result = await tool.execute(query=None)
        parsed = json.loads(result)
        assert parsed["count"] == 2
        ids = [ev.get("event_id") for ev in parsed["events"]]
        assert "11111111-2222-3333-4444-555555555555" in ids
        assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in ids


class TestPagination:
    """``offset`` и ``has_more``/``next_offset`` (D1, D2 change)."""

    @pytest.mark.asyncio
    async def test_schema_includes_offset_with_default_zero(self):
        """JSON-schema ``history_search`` содержит ``offset`` с дефолтом 0."""
        from workspace.tools.history_search_tool import (
            HistorySearchTool,
            HistorySearchToolConfig,
        )

        tool = HistorySearchTool(config=HistorySearchToolConfig())
        params = tool.to_schema()["function"]["parameters"]
        assert "offset" in params["properties"]
        assert params["properties"]["offset"]["type"] == "integer"
        assert params["properties"]["offset"].get("default") == 0
        assert params["properties"]["offset"]["minimum"] == 0

    @pytest.mark.asyncio
    async def test_sql_uses_limit_offset_and_deterministic_order(self):
        """SQL: ``ORDER BY timestamp DESC, id DESC LIMIT %s OFFSET %s``."""
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            await tool.execute(query=None, limit=7, offset=3)
            sql, *params = fetch.call_args.args
            assert '"timestamp" DESC, "id" DESC' in sql
            assert "LIMIT %s OFFSET %s" in sql
            assert "ORDER BY" in sql
            # Параметры: effective_limit+1 и offset
            assert params[-2] == 8  # 7 + 1
            assert params[-1] == 3

    @pytest.mark.asyncio
    async def test_offset_zero_equivalent_to_default_behavior(self):
        """``offset=0`` (по умолчанию) ведёт себя как раньше."""
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "x",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"i": i}}
            for i in range(3)
        ]
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows) as fetch:
            tool = _make_tool()
            r1 = await tool.execute(query=None)
            r2 = await tool.execute(query=None, offset=0)
            assert r1 == r2
            # offset=0 в params
            assert fetch.call_args.args[-1] == 0

    @pytest.mark.asyncio
    async def test_has_more_true_when_more_rows_in_db(self):
        """25 событий в БД, limit=10 → db_has_more=true, count=10."""
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "x",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {}}
            for i in range(11)  # LIMIT N+1 = 11 строк
        ]
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            tool = _make_tool()
            result = await tool.execute(query=None, limit=10)
            data = json.loads(result)
            assert data["has_more"] is True
            assert data["count"] == 10
            assert data["next_offset"] == 10
            assert data["results_truncated"] is False

    @pytest.mark.asyncio
    async def test_has_more_false_on_last_page(self):
        """Последняя страница: limit=10, offset=20, в БД ровно 25 строк → count=5."""
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "x",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {}}
            for i in range(5)  # только 5 последних
        ]
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            tool = _make_tool()
            result = await tool.execute(query=None, limit=10, offset=20)
            data = json.loads(result)
            assert data["has_more"] is False
            assert data["count"] == 5
            assert data["next_offset"] == 25
            assert data["results_truncated"] is False

    @pytest.mark.asyncio
    async def test_has_more_true_when_results_truncated_even_without_db_more(self):
        """Регрессия: ровно ``effective_limit`` событий в БД, но
        truncation выбросил часть → ``has_more=true`` (агенту
        обязательно звать следующую страницу)."""
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "tool_call",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"k": "v"}}
            for i in range(10)
        ]
        # max_result_chars настолько мал, что 4 события уже не влезают.
        tool = HistorySearchTool(
            config=HistorySearchToolConfig(max_result_chars=300),
        )
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            result = await tool.execute(query=None, limit=10)
            data = json.loads(result)
            # db_has_more = false (10 строк в БД == effective_limit),
            # но truncation выбросил 6 событий → results_truncated=true.
            assert data["results_truncated"] is True
            assert data["has_more"] is True
            assert data["count"] < 10
            # next_offset = offset + count после truncation'а
            assert data["next_offset"] == data["count"]

    @pytest.mark.asyncio
    async def test_next_offset_no_truncation(self):
        """Без truncation: ``next_offset = offset + count``."""
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "x",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {}}
            for i in range(10)
        ]
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            tool = _make_tool()
            result = await tool.execute(query=None, limit=10)
            data = json.loads(result)
            assert data["count"] == 10
            assert data["next_offset"] == 10

    @pytest.mark.asyncio
    async def test_next_offset_after_truncation(self):
        """С truncation: ``next_offset = 0 + count`` (НЕ ``0 + limit``)."""
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "tool_call",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"k": "v"}}
            for i in range(10)
        ]
        tool = HistorySearchTool(
            config=HistorySearchToolConfig(max_result_chars=400),
        )
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            result = await tool.execute(query=None, limit=10, offset=0)
            data = json.loads(result)
            assert data["results_truncated"] is True
            assert data["count"] < 10
            assert data["next_offset"] == data["count"]

    @pytest.mark.asyncio
    async def test_next_offset_with_offset_arg(self):
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "x",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {}}
            for i in range(4)
        ]
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            tool = _make_tool()
            result = await tool.execute(query=None, limit=10, offset=10)
            data = json.loads(result)
            assert data["count"] == 4
            assert data["next_offset"] == 14

    @pytest.mark.asyncio
    async def test_empty_result_no_truncation(self):
        """Пустой результат: ``count=0, has_more=false, results_truncated=false``."""
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=[]):
            tool = _make_tool()
            result = await tool.execute(query=None)
            data = json.loads(result)
            assert data["status"] == "success"
            assert data["count"] == 0
            assert data["has_more"] is False
            assert data["results_truncated"] is False
            assert data["next_offset"] == 0
            assert data["events"] == []


class TestTruncationFlags:
    """``results_truncated`` vs ``payload_truncated`` — независимые флаги."""

    @pytest.mark.asyncio
    async def test_results_truncated_and_deprecated_alias(self):
        """При truncation: ``results_truncated=true`` и ``truncated=true``.
        Без truncation: оба ``false``."""
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "tool_call",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"k": "v"}}
            for i in range(10)
        ]
        tool_trunc = HistorySearchTool(
            config=HistorySearchToolConfig(max_result_chars=300),
        )
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            r_trunc = await tool_trunc.execute(query=None, limit=10)
            data = json.loads(r_trunc)
            assert data["results_truncated"] is True
            assert data["truncated"] is True

        small_rows = [
            {"id": "id-1", "timestamp": "t1", "event_type": "tool_call",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"k": "v"}}
        ]
        tool = _make_tool()
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=small_rows):
            r_ok = await tool.execute(query=None)
            data = json.loads(r_ok)
            assert data["results_truncated"] is False
            assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_payload_truncated_per_event(self):
        """payload > per_event_cap → ``payload_truncated=true`` только на этом событии."""
        big = "x" * 5000
        rows = [
            {"id": "big", "timestamp": "t1", "event_type": "llm_call",
             "name": "llm", "level": "INFO", "summary": "s",
             "payload": {"prompt": big}},
            {"id": "small", "timestamp": "t2", "event_type": "tool_call",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"k": "ok"}},
        ]
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            tool = _make_tool()
            result = await tool.execute(query=None, limit=10)
            data = json.loads(result)
            assert data["count"] == 2
            assert data["results_truncated"] is False
            by_id = {ev["event_id"]: ev for ev in data["events"]}
            assert by_id["big"]["payload_truncated"] is True
            assert by_id["small"]["payload_truncated"] is False

    @pytest.mark.asyncio
    async def test_results_and_payload_independent(self):
        """payload_truncated=true, results_truncated=false (payload ужат, события не выброшены)."""
        big = "x" * 5000
        rows = [
            {"id": "big", "timestamp": "t1", "event_type": "llm_call",
             "name": "llm", "level": "INFO", "summary": "s",
             "payload": {"prompt": big}},
        ]
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            tool = _make_tool()
            result = await tool.execute(query=None, limit=10)
            data = json.loads(result)
            assert data["count"] == 1
            assert data["results_truncated"] is False
            assert data["events"][0]["payload_truncated"] is True

    @pytest.mark.asyncio
    async def test_payload_truncated_via_max_result_chars_second_pass(self):
        """Регрессия: один event с большим payload, max_result_chars настолько
        мал, что срабатывает второй проход (cap //= 2). Событие остаётся,
        payload_truncated=true, results_truncated=false."""
        big = "x" * 5000  # > per_event_cap (4000)
        rows = [
            {"id": "only", "timestamp": "t1", "event_type": "llm_call",
             "name": "llm", "level": "INFO", "summary": "s",
             "payload": {"prompt": big}},
        ]
        # max_result_chars достаточно мал, чтобы один пережатый payload
        # всё ещё не влез — должно сработать уменьшение cap и пережим.
        tool = HistorySearchTool(
            config=HistorySearchToolConfig(max_result_chars=400),
        )
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            result = await tool.execute(query=None, limit=10)
            data = json.loads(result)
            assert data["count"] == 1
            assert data["results_truncated"] is False
            assert data["events"][0]["payload_truncated"] is True

    @pytest.mark.asyncio
    async def test_truncated_alias_equals_results_truncated(self):
        """Deprecated ``truncated`` всегда равен ``results_truncated``."""
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "tool_call",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"k": "v"}}
            for i in range(10)
        ]
        tool = HistorySearchTool(
            config=HistorySearchToolConfig(max_result_chars=300),
        )
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows):
            result = await tool.execute(query=None, limit=10)
            data = json.loads(result)
            assert data["truncated"] == data["results_truncated"]

    @pytest.mark.asyncio
    async def test_final_json_respects_max_result_chars(self):
        """Регрессия на пункт 5 review: ``_render()`` должен включать
        ``payload_truncated`` в проверяемый JSON, иначе финальный ответ
        превысит ``max_result_chars`` на ~16-20 байт за счёт
        дополнительного поля ``\"payload_truncated\": false`` на каждое
        событие.

        Сценарий: 10 событий с маленькими payload (~120 байт каждое).
        Без ``payload_truncated`` в ``_render`` суммарный JSON был бы
        ~1200 байт. С ``payload_truncated`` — ~1240 байт (10 ×
        ``"payload_truncated": false,`` ≈ 28 байт на событие).
        Тест проверяет, что финальный JSON строго ≤ ``max_result_chars``
        на разумных порогах (400+; меньше — невалидный сценарий
        из-за структурного overhead'а ~310 байт на 1 событие).
        """
        rows = [
            {"id": f"id-{i}", "timestamp": f"t{i}", "event_type": "tool_call",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"k": "v"}}
            for i in range(10)
        ]
        for max_chars in (400, 800, 1200, 2000):
            tool = HistorySearchTool(
                config=HistorySearchToolConfig(max_result_chars=max_chars),
            )
            with patch(
                "workspace.tools.history_search_tool._current_session_key",
                return_value="s",
            ), patch("utils.db.fetch", return_value=rows):
                result = await tool.execute(query=None, limit=10)
                assert len(result) <= max_chars, (
                    f"len(result)={len(result)} > max_result_chars={max_chars} "
                    "(payload_truncated учтён в _render)"
                )


class TestUserIsolation:
    """Cross-user isolation для ``history_search(session_scope="all")``.

    Закрывает security gap: раньше ``scope='all'`` через конструкцию
    ``(%s OR session_id = %s)`` возвращал глобальный набор событий —
    alice видела события bob, c, …. Теперь ``scope='all'`` фильтрует
    по ``user_id`` (security boundary) и возвращает только события
    того же пользователя.
    """

    @pytest.mark.asyncio
    async def test_all_scope_filters_by_user_id_alice(self):
        """alice запрашивает scope='all' → SQL содержит ``user_id = 'alice'``."""
        with patch(
            "workspace.tools.history_search_tool._current_user_id",
            return_value="alice",
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            await tool.execute(query=None, session_scope="all")
            sql, *params = fetch.call_args.args
            assert "user_id = %s" in sql
            assert "alice" in params

    @pytest.mark.asyncio
    async def test_all_scope_excludes_other_users(self):
        """bob запрашивает scope='all' → SQL содержит ``user_id = 'bob'``,
        никакого ``alice`` в параметрах."""
        with patch(
            "workspace.tools.history_search_tool._current_user_id",
            return_value="bob",
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            await tool.execute(query=None, session_scope="all")
            sql, *params = fetch.call_args.args
            assert "user_id = %s" in sql
            assert "bob" in params
            assert "alice" not in params

    @pytest.mark.asyncio
    async def test_all_scope_missing_user_returns_error(self):
        """Без identity-store → ``missing_user_identity``, fetch НЕ вызван."""
        with patch(
            "workspace.tools.history_search_tool._current_user_id",
            return_value=None,
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            result = await tool.execute(query=None, session_scope="all")
            data = json.loads(result)
            assert data["status"] == "error"
            assert data["error_type"] == "missing_user_identity"
            assert fetch.call_count == 0

    @pytest.mark.asyncio
    async def test_current_scope_filters_by_session_id(self):
        """Регрессия: ``scope='current'`` сохраняет поведение по session_id."""
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="telegram:42",
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            await tool.execute(query=None, session_scope="current")
            sql, *params = fetch.call_args.args
            assert "session_id = %s" in sql
            assert "telegram:42" in params

    @pytest.mark.asyncio
    async def test_current_scope_missing_session_returns_error(self):
        """Без identity-store для session_key → ``missing_session_identity``."""
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value=None,
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            result = await tool.execute(query=None, session_scope="current")
            data = json.loads(result)
            assert data["status"] == "error"
            assert data["error_type"] == "missing_session_identity"
            assert fetch.call_count == 0

    @pytest.mark.asyncio
    async def test_response_does_not_leak_user_id(self):
        """Ни на одном уровне JSON-ответа нет поля ``user_id``."""
        rows = [
            {
                "id": "id-1",
                "timestamp": "t1",
                "event_type": "tool_call",
                "name": "x",
                "level": "INFO",
                "summary": "s",
                "payload": {"k": "v"},
            }
        ]
        with patch(
            "workspace.tools.history_search_tool._current_user_id",
            return_value="alice",
        ), patch("utils.db.fetch", return_value=rows):
            tool = _make_tool()
            result = await tool.execute(query=None, session_scope="all")
            data = json.loads(result)
            # Корень
            assert "user_id" not in data
            # События
            for ev in data["events"]:
                assert "user_id" not in ev
                # payload внутри события — JSON-string, проверяем строкой
                assert "user_id" not in ev["payload"]

    @pytest.mark.asyncio
    async def test_current_scope_does_not_compare_user_id(self):
        """scope='current' фильтрует по session_id; ``user_id`` НЕ участвует
        в предикате (даже если для строки той же сессии записан
        ошибочный ``user_id``). Это контракт из спеки §3."""
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="telegram:42",
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            await tool.execute(query=None, session_scope="current")
            sql, *params = fetch.call_args.args
            # Никакого ``user_id = %s`` для scope='current'.
            assert "user_id = %s" not in sql

    @pytest.mark.asyncio
    async def test_invalid_session_scope_returns_error(self):
        """Невалидный ``session_scope`` → ``invalid_session_scope``."""
        with patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            result = await tool.execute(query=None, session_scope="bogus")
            data = json.loads(result)
            assert data["status"] == "error"
            assert data["error_type"] == "invalid_session_scope"
            assert fetch.call_count == 0


class TestGeneratedSqlGuard:
    """Primary guard на сгенерированный SQL и параметры (security boundary).

    Защита от регрессии после рефакторинга: даже если grep по исходнику
    пропустит запрещённый паттерн, эти тесты фиксируют фактическую
    форму SQL и порядок параметров.
    """

    @pytest.mark.asyncio
    async def test_scope_current_uses_session_id_predicate(self):
        """scope='current' → SQL содержит ``session_id = %s``, параметр = session_key."""
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="telegram:42",
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            await tool.execute(query=None, session_scope="current")
            sql, *params = fetch.call_args.args
            assert "session_id = %s" in sql
            assert "telegram:42" in params

    @pytest.mark.asyncio
    async def test_scope_all_uses_user_id_predicate(self):
        """scope='all' → SQL содержит ``user_id = %s``, параметр = sender_id."""
        with patch(
            "workspace.tools.history_search_tool._current_user_id",
            return_value="alice",
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            await tool.execute(query=None, session_scope="all")
            sql, *params = fetch.call_args.args
            assert "user_id = %s" in sql
            assert "alice" in params
            # Никакого session_id = %s для scope='all' (никакого unscoped fallback).
            assert "session_id = %s" not in sql

    @pytest.mark.asyncio
    async def test_scope_all_missing_user_does_not_call_fetch(self):
        """scope='all' без identity → ``missing_user_identity``, fetch НЕ вызван."""
        with patch(
            "workspace.tools.history_search_tool._current_user_id",
            return_value=None,
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            result = await tool.execute(query=None, session_scope="all")
            data = json.loads(result)
            assert data["error_type"] == "missing_user_identity"
            assert fetch.call_count == 0

    @pytest.mark.asyncio
    async def test_scope_current_missing_session_does_not_call_fetch(self):
        """scope='current' без session_key → ``missing_session_identity``,
        fetch НЕ вызван."""
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value=None,
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            result = await tool.execute(query=None, session_scope="current")
            data = json.loads(result)
            assert data["error_type"] == "missing_session_identity"
            assert fetch.call_count == 0

    @pytest.mark.asyncio
    async def test_no_unscoped_or_session_id_pattern(self):
        """Primary guard: SQL НЕ содержит запрещённых паттернов unscoped
        fallback'а (``(%s OR session_id = %s)``, ``OR TRUE``, ``WHERE TRUE``,
        ``session_id LIKE``)."""
        with patch(
            "workspace.tools.history_search_tool._current_user_id",
            return_value="alice",
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            await tool.execute(query=None, session_scope="all")
            sql = fetch.call_args.args[0]
            assert "(%s OR session_id = %s)" not in sql
            assert "OR TRUE" not in sql.upper()
            assert "WHERE TRUE" not in sql.upper()
            assert "session_id LIKE" not in sql

        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="telegram:42",
        ), patch("utils.db.fetch", return_value=[]) as fetch:
            tool = _make_tool()
            await tool.execute(query=None, session_scope="current")
            sql = fetch.call_args.args[0]
            assert "(%s OR session_id = %s)" not in sql
            assert "user_id = %s" not in sql

    @pytest.mark.asyncio
    async def test_tool_schema_has_no_user_id_parameter(self):
        """JSON-schema tool'а НЕ содержит ``user_id`` (security attribute
        не должен быть параметром)."""
        tool = _make_tool()
        params = tool.to_schema()["function"]["parameters"]
        assert "user_id" not in params["properties"]
        assert "user_id" not in params.get("required", [])


class TestSnapshotConsistency:
    """Snapshot-неконсистентность при активных INSERT'ах — документируем
    ограничение, не фиксируем конкретные значения."""

    @pytest.mark.asyncio
    async def test_pagination_not_snapshot_consistent(self):
        """Между двумя вызовами ``offset=0`` и ``offset=N`` могут появиться
        новые события в начале выборки — это известное ограничение
        ``offset``-пагинации без cursor'а.

        Тест фиксирует, что новые строки попадают в начало (после
        sort DESC): сценарий документирует ограничение и проверяет,
        что код не делает вид, что пагинация стабильна.
        """
        rows_offset0 = [
            {"id": f"id-{i}", "timestamp": f"2026-01-01T10:0{i}:00+00:00",
             "event_type": "x", "name": "x", "level": "INFO", "summary": "s",
             "payload": {}}
            for i in range(5)
        ]
        # Через offset=0 ответ включает события 0..4.
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows_offset0):
            tool = _make_tool()
            r1 = await tool.execute(query=None, limit=5, offset=0)
            data1 = json.loads(r1)
            ids1 = [ev["event_id"] for ev in data1["events"]]
            assert "id-0" in ids1

        # Имитируем INSERT нового события ДО offset=4 — оно попадает в начало.
        rows_offset10_flat: list[dict] = [
            {"id": "newer", "timestamp": "2026-01-02T10:00:00+00:00",
             "event_type": "x", "name": "x", "level": "INFO", "summary": "s",
             "payload": {}},
        ]
        rows_offset10_flat.extend(
            {"id": f"id-{i}", "timestamp": f"2026-01-01T10:0{i}:00+00:00",
             "event_type": "x", "name": "x", "level": "INFO", "summary": "s",
             "payload": {}}
            for i in range(3)
        )
        with patch(
            "workspace.tools.history_search_tool._current_session_key",
            return_value="s",
        ), patch("utils.db.fetch", return_value=rows_offset10_flat):
            tool = _make_tool()
            r2 = await tool.execute(query=None, limit=5, offset=0)
            data2 = json.loads(r2)
            ids2 = [ev["event_id"] for ev in data2["events"]]
            # Новое событие попало в начало выборки — offset-пагинация
            # может сдвинуться.
            assert "newer" in ids2
